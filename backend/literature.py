from __future__ import annotations

from contextlib import contextmanager
import io
import json
import math
import mimetypes
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator

from .db import open_database
from .service import DEFAULT_DB_PATH, PROJECT_ROOT, load_env_file, new_id, normalize_text, short_hash, utc_now
from .storage import blob_enabled, materialize_storage_ref, upload_bytes_to_blob


EXTERNAL_ZOTERO_ENV = Path("F:/Codex-MCP-Skills/mcp-servers/zotero-mcp-server/.env")
DEFAULT_SOURCES = ["openalex", "crossref", "semantic-scholar"]
QUERY_STOP_TERMS = {
    "基于",
    "关于",
    "视角",
    "背景",
    "研究",
    "分析",
    "探究",
    "路径",
    "实践",
    "策略",
    "影响",
    "机制",
    "评价",
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "using",
    "based",
    "study",
    "analysis",
}
SUGGESTION_STOP_TERMS = QUERY_STOP_TERMS | {
    "本文",
    "本研究",
    "论文",
    "章节",
    "问题",
    "方法",
    "结果",
    "讨论",
    "结论",
    "提出",
    "认为",
    "进行",
    "通过",
    "相关",
    "当前",
    "现有",
    "进一步",
    "需要",
    "能够",
    "具有",
    "理念",
    "教学",
    "教育",
    "学生",
    "课程",
    "school",
    "education",
    "teaching",
    "learning",
    "course",
    "students",
    "research",
    "paper",
    "method",
    "result",
    "discussion",
    "double",
    "number",
    "edit",
    "page",
    "figure",
    "table",
    "section",
    "draft",
    "source",
    "label",
    "text",
}


def load_literature_env() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    load_env_file(EXTERNAL_ZOTERO_ENV)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _first(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0] or "").strip()
    if value is None:
        return ""
    return str(value).strip()


def compact_title(title: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(title)).strip(" .")


def normalized_title_key(title: str) -> str:
    lowered = compact_title(title).lower()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", lowered)


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    cleaned = doi.strip().lower()
    cleaned = re.sub(r"^https?://(dx\.)?doi\.org/", "", cleaned)
    return cleaned


def infer_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text or "") else "en"


def split_literature_chunks(text: str, *, max_chars: int = 760, max_chunks: int = 18) -> list[str]:
    normalized = normalize_text(text).replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    if not paragraphs:
        paragraphs = [normalized]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = paragraph
            if len(chunks) >= max_chunks:
                break
            continue
        current = candidate
    if current and len(chunks) < max_chunks:
        chunks.append(current)
    return chunks[:max_chunks]


LOCAL_RAG_VECTOR_DIMENSIONS = 128
LOCAL_RAG_EMBEDDING_MODEL = "draftrefine-local-hash-v1"


def rag_tokens(text: str) -> list[str]:
    normalized = normalize_text(text).lower()
    tokens: list[str] = []
    for word in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{1,}", normalized):
        if word not in QUERY_STOP_TERMS:
            tokens.append(word)
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        if chunk not in QUERY_STOP_TERMS and len(chunk) <= 12:
            tokens.append(chunk)
        for size in (2, 3, 4):
            for index in range(0, max(0, len(chunk) - size + 1)):
                gram = chunk[index : index + size]
                if gram not in QUERY_STOP_TERMS:
                    tokens.append(gram)
    return list(dict.fromkeys(tokens))[:80]


def rag_token_profile(text: str) -> dict[str, float]:
    profile: dict[str, float] = {}
    for token in rag_tokens(text):
        profile[token] = profile.get(token, 0.0) + 1.0 + min(len(token), 8) / 8
    return profile


def rag_hash_vector(text: str, dimensions: int = LOCAL_RAG_VECTOR_DIMENSIONS) -> dict[str, float]:
    profile = rag_token_profile(text)
    vector: dict[str, float] = {}
    for token, weight in profile.items():
        slot = int(short_hash(token), 16) % dimensions
        sign = -1.0 if int(short_hash(f"sign:{token}"), 16) % 2 else 1.0
        key = str(slot)
        vector[key] = vector.get(key, 0.0) + sign * weight
    norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
    return {key: round(value / norm, 6) for key, value in vector.items() if abs(value) > 1e-9}


def sparse_cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def rag_lexical_score(query_profile: dict[str, float], chunk_profile: dict[str, float]) -> tuple[float, list[str]]:
    if not query_profile or not chunk_profile:
        return 0.0, []
    matched = [token for token in query_profile if token in chunk_profile]
    if not matched:
        return 0.0, []
    weight = sum(query_profile[token] + chunk_profile[token] for token in matched)
    coverage = len(matched) / max(1, len(query_profile))
    return min(1.0, (weight / max(8.0, len(query_profile) * 3.0)) + coverage * 0.35), matched[:16]


def extract_query_terms(query: str) -> list[str]:
    normalized = normalize_text(query).lower()
    terms: set[str] = set()
    for word in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{1,}", normalized):
        if word not in QUERY_STOP_TERMS:
            terms.add(word)
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        cleaned = re.sub(r"(基于|关于|视角下|背景下|的|与|和|及|对|在|中)", " ", chunk)
        for part in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned):
            if part not in QUERY_STOP_TERMS and len(part) <= 12:
                terms.add(part)
            if len(part) > 4:
                for size in (2, 3, 4):
                    for index in range(0, len(part) - size + 1):
                        gram = part[index : index + size]
                        if gram not in QUERY_STOP_TERMS:
                            terms.add(gram)
    return sorted(terms, key=lambda item: (-len(item), item))[:28]


def _term_is_useful(term: str) -> bool:
    normalized = term.strip().lower()
    if len(normalized) < 2:
        return False
    if normalized in SUGGESTION_STOP_TERMS:
        return False
    if re.fullmatch(r"\d+", normalized):
        return False
    return True


def _display_term(term: str) -> str:
    if re.fullmatch(r"[a-z]{2,8}", term):
        return term.upper()
    return re.sub(r"^[a-z]{2,8}", lambda match: match.group(0).upper(), term)


def suggest_terms_from_text(project_title: str, sections: list[sqlite3.Row]) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    reasons: dict[str, set[str]] = {}

    def add(term: str, amount: float, reason: str) -> None:
        clean = term.strip(" ：:，,。.;；（）()[]【】《》“”\"'").lower()
        if not _term_is_useful(clean):
            return
        scores[clean] = scores.get(clean, 0) + amount
        reasons.setdefault(clean, set()).add(reason)

    title_text = normalize_text(project_title)
    section_title_text = " ".join(row["title"] for row in sections)
    body_text = normalize_text("\n".join(row["current_text"][:2200] for row in sections[:8]))
    all_text = f"{title_text}\n{section_title_text}\n{body_text}"

    for raw_text, base_weight, reason in [
        (title_text, 8.0, "题名"),
        (section_title_text, 3.5, "章节标题"),
        (body_text, 1.0, "正文"),
    ]:
        text = normalize_text(raw_text)
        for acronym in re.findall(r"\b[A-Z]{2,8}\b", text):
            add(acronym, base_weight + 8, reason)
        for phrase in re.findall(r"\b[A-Za-z][A-Za-z0-9-]{2,}(?:\s+[A-Za-z][A-Za-z0-9-]{2,}){0,2}\b", text):
            words = [word for word in phrase.split() if word.lower() not in SUGGESTION_STOP_TERMS]
            if 1 <= len(words) <= 3:
                add(" ".join(words), base_weight + len(words), reason)
        for mixed in re.findall(r"\b[A-Za-z]{2,8}[\u4e00-\u9fff]{1,6}", text):
            add(mixed, base_weight + 7, reason)
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            cleaned = re.sub(r"(基于|关于|视角下|背景下|本文|本研究|一种|以及|或者|并且|中的|的|与|和|及|对|在|中)", " ", chunk)
            for part in re.findall(r"[\u4e00-\u9fff]{2,12}", cleaned):
                if len(part) <= 6:
                    add(part, base_weight + min(len(part), 6), reason)
                for size in (2, 3, 4):
                    for index in range(0, len(part) - size + 1):
                        gram = part[index : index + size]
                        add(gram, base_weight + size * 0.75, reason)

    domain_phrases = [
        "课程思政",
        "小学体育",
        "体育课程",
        "教学实践",
        "实践路径",
        "教学改革",
        "核心素养",
        "学习成果",
        "outcome based education",
        "OBE理念",
    ]
    for phrase in domain_phrases:
        if phrase.lower() in all_text.lower():
            add(phrase, 18, "正文领域短语")

    ranked: list[dict[str, Any]] = []
    for term, score in scores.items():
        if any(term != other and term in other and scores.get(other, 0) >= score for other in scores):
            continue
        ranked.append(
            {
                "term": _display_term(term),
                "score": round(score, 2),
                "reason": "、".join(sorted(reasons.get(term, {"正文"}))),
            }
        )
    return sorted(ranked, key=lambda item: (-item["score"], -len(item["term"]), item["term"]))[:12]


def score_candidate_relevance(query: str, candidate: dict[str, Any]) -> tuple[int, str, int]:
    terms = extract_query_terms(query)
    if not terms:
        return 0, "没有可用于匹配的关键词", 0
    title = (candidate.get("title") or "").lower()
    abstract = (candidate.get("abstract") or "").lower()
    venue = (candidate.get("venue") or "").lower()
    full_query = normalize_text(query).lower()
    score = 0.0
    matched: list[str] = []
    if full_query and full_query in title:
        score += 45
        matched.append("完整题名")
    for term in terms:
        weight = 1.0 + min(len(term), 8) / 8
        if term in title:
            score += 6 * weight
            matched.append(term)
        elif term in abstract:
            score += 2.5 * weight
            matched.append(term)
        elif term in venue:
            score += 1.2 * weight
            matched.append(term)
    unique_matches = list(dict.fromkeys(matched))
    coverage = min(1.0, len(unique_matches) / max(3, min(len(terms), 8)))
    score += coverage * 22
    if candidate.get("doi"):
        score += 2
    if candidate.get("openAccessStatus") == "open":
        score += 2
    bounded = max(0, min(100, round(score)))
    if not unique_matches:
        return bounded, "未命中核心关键词", 0
    return bounded, "命中：" + "、".join(unique_matches[:6]), len(unique_matches)


def infer_quality_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    sources = set(candidate.get("sources", []))
    tags = set(candidate.get("tags", []))
    publication_type = candidate.get("publicationType") or "unknown"
    venue = candidate.get("venue") or ""
    signals: list[str] = []
    if candidate.get("doi"):
        signals.append("有 DOI")
    if venue:
        signals.append("有期刊/来源")
    if candidate.get("citationCount", 0) > 0:
        signals.append(f"引用 {candidate.get('citationCount', 0)}")
    if candidate.get("openAccessStatus") == "open":
        signals.append("开放全文")
    if candidate.get("isInDoaj"):
        signals.append("DOAJ 开放期刊")
    if candidate.get("isOpenAlexCore"):
        signals.append("OpenAlex core 来源")
    if "crossref" in sources:
        signals.append("Crossref 元数据")
    if "openalex" in sources:
        signals.append("OpenAlex 收录")
    if publication_type == "journal-article":
        signals.append("期刊论文")
    if {"preprint", "posted-content", "dissertation", "book-chapter"} & tags:
        signals.append("非期刊类型需谨慎")
    quality_status = "待分区核验"
    if publication_type != "journal-article":
        quality_status = "非期刊论文或类型待确认"
    return {
        "publicationType": publication_type,
        "qualitySignals": signals,
        "qualityStatus": quality_status,
        "jcrQuartile": None,
        "casQuartile": None,
    }


def normalize_issn_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    issns: list[str] = []
    for item in value:
        text = str(item or "").strip().upper()
        if text and text not in issns:
            issns.append(text)
    return issns


def journal_metadata(
    *,
    journal_title: str = "",
    issn: Any = None,
    issn_l: str = "",
    publisher: str = "",
    source_type: str = "",
    source_id: str = "",
    is_oa_journal: bool | None = None,
    is_in_doaj: bool | None = None,
    is_openalex_core: bool | None = None,
) -> dict[str, Any]:
    return {
        "journalTitle": journal_title or "",
        "issn": normalize_issn_list(issn),
        "issnL": (issn_l or "").strip().upper(),
        "publisher": publisher or "",
        "sourceType": source_type or "",
        "sourceId": source_id or "",
        "isOaJournal": bool(is_oa_journal),
        "isInDoaj": bool(is_in_doaj),
        "isOpenAlexCore": bool(is_openalex_core),
    }


def decode_openalex_abstract(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    tokens: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions:
            tokens.append((position, word))
    return " ".join(word for _, word in sorted(tokens))


class LiteratureService:
    def __init__(
        self,
        database_path: Path | None = None,
        http_json: Callable[[str, dict[str, str] | None], dict[str, Any]] | None = None,
        zotero_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.database_path = Path(os.getenv("DRAFTREFINE_DATABASE_PATH", str(database_path or DEFAULT_DB_PATH)))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_dir = self.database_path.parent / f"{self.database_path.stem}_literature"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.http_json = http_json or self._default_http_json
        self.zotero_client_factory = zotero_client_factory
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with open_database(self.database_path) as connection:
            yield connection

    def _init_db(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS literature_search_runs (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          query TEXT NOT NULL,
          sources_json TEXT NOT NULL,
          status TEXT NOT NULL,
          total_found INTEGER NOT NULL,
          deduped_count INTEGER NOT NULL,
          warnings_json TEXT NOT NULL,
          results_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS literature_items (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          title TEXT NOT NULL,
          authors_json TEXT NOT NULL,
          year INTEGER,
          venue TEXT,
          doi TEXT,
          url TEXT,
          abstract TEXT,
          source TEXT NOT NULL,
          sources_json TEXT NOT NULL,
          language TEXT NOT NULL,
          citation_count INTEGER,
          open_access_status TEXT NOT NULL,
          pdf_url TEXT,
          zotero_item_key TEXT,
          tags_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS literature_collections (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          name TEXT NOT NULL,
          zotero_collection_key TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS literature_attachments (
          id TEXT PRIMARY KEY,
          item_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          url TEXT,
          local_path TEXT,
          access_type TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS literature_chunks (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          item_id TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          source_label TEXT NOT NULL,
          chunk_order INTEGER NOT NULL,
          chunk_text TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS literature_chunk_vectors (
          chunk_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          item_id TEXT NOT NULL,
          embedding_model TEXT NOT NULL,
          vector_json TEXT NOT NULL,
          tokens_json TEXT NOT NULL,
          indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS zotero_sync_events (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          item_id TEXT NOT NULL,
          status TEXT NOT NULL,
          zotero_item_key TEXT,
          error TEXT,
          synced_at TEXT NOT NULL
        );
        """
        with self._connect() as connection:
            connection.executescript(schema)
            self._ensure_literature_item_columns(connection)
            self._ensure_literature_vector_table(connection)
            connection.commit()

    def _ensure_literature_item_columns(self, connection: sqlite3.Connection) -> None:
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(literature_items)").fetchall()}
        columns = {
            "journal_title": "TEXT",
            "issn_json": "TEXT",
            "issn_l": "TEXT",
            "publisher": "TEXT",
            "source_type": "TEXT",
            "source_id": "TEXT",
            "is_oa_journal": "INTEGER",
            "is_in_doaj": "INTEGER",
            "is_openalex_core": "INTEGER",
        }
        for name, column_type in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE literature_items ADD COLUMN {name} {column_type}")

    def _ensure_literature_vector_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS literature_chunk_vectors (
              chunk_id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              item_id TEXT NOT NULL,
              embedding_model TEXT NOT NULL,
              vector_json TEXT NOT NULL,
              tokens_json TEXT NOT NULL,
              indexed_at TEXT NOT NULL
            )
            """
        )

    def _default_http_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request_headers = {"User-Agent": "DraftRefine/0.1 literature beta"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def _ensure_project(self, connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return row

    def _serialize_item(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "projectId": row["project_id"],
            "title": row["title"],
            "authors": _json_loads(row["authors_json"], []),
            "year": row["year"],
            "venue": row["venue"] or "",
            "doi": row["doi"] or "",
            "url": row["url"] or "",
            "abstract": row["abstract"] or "",
            "source": row["source"],
            "sources": _json_loads(row["sources_json"], [row["source"]]),
            "language": row["language"],
            "citationCount": row["citation_count"] or 0,
            "openAccessStatus": row["open_access_status"],
            "pdfUrl": row["pdf_url"] or "",
            "tags": _json_loads(row["tags_json"], []),
            "relevanceScore": 100,
            "matchReason": "已导入项目文献库",
            "publicationType": "journal-article",
            "qualitySignals": _json_loads(row["tags_json"], []),
            "qualityStatus": "待分区核验",
            "jcrQuartile": None,
            "casQuartile": None,
            "journalTitle": row["journal_title"] or row["venue"] or "",
            "issn": _json_loads(row["issn_json"], []),
            "issnL": row["issn_l"] or "",
            "publisher": row["publisher"] or "",
            "sourceType": row["source_type"] or "",
            "sourceId": row["source_id"] or "",
            "isOaJournal": bool(row["is_oa_journal"]),
            "isInDoaj": bool(row["is_in_doaj"]),
            "isOpenAlexCore": bool(row["is_openalex_core"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _serialize_attachment(self, row: sqlite3.Row) -> dict[str, Any]:
        download_url = ""
        local_path = (row["local_path"] or "").strip()
        if local_path:
            with self._connect() as connection:
                item_row = connection.execute(
                    "SELECT project_id FROM literature_items WHERE id = ?",
                    (row["item_id"],),
                ).fetchone()
            if item_row is not None:
                download_url = f"/api/projects/{item_row['project_id']}/literature/attachments/{row['id']}/download"
        return {
            "id": row["id"],
            "itemId": row["item_id"],
            "kind": row["kind"],
            "url": row["url"] or "",
            "localPath": row["local_path"] or "",
            "accessType": row["access_type"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "downloadUrl": download_url,
        }

    def _serialize_chunk(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "projectId": row["project_id"],
            "itemId": row["item_id"],
            "sourceKind": row["source_kind"],
            "sourceLabel": row["source_label"],
            "chunkOrder": row["chunk_order"],
            "chunkText": row["chunk_text"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _serialize_sync_event(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "projectId": row["project_id"],
            "itemId": row["item_id"],
            "status": row["status"],
            "zoteroItemKey": row["zotero_item_key"],
            "error": row["error"] or "",
            "syncedAt": row["synced_at"],
        }

    def _serialize_run(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "projectId": row["project_id"],
            "query": row["query"],
            "sources": _json_loads(row["sources_json"], []),
            "status": row["status"],
            "totalFound": row["total_found"],
            "dedupedCount": row["deduped_count"],
            "warnings": _json_loads(row["warnings_json"], []),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_project_literature(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            self._ensure_project(connection, project_id)
            items = connection.execute(
                "SELECT * FROM literature_items WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
            item_ids = [row["id"] for row in items]
            attachments: list[sqlite3.Row] = []
            if item_ids:
                placeholders = ",".join("?" for _ in item_ids)
                attachments = connection.execute(
                    f"SELECT * FROM literature_attachments WHERE item_id IN ({placeholders}) ORDER BY created_at DESC",
                    item_ids,
                ).fetchall()
                chunks = connection.execute(
                    f"SELECT * FROM literature_chunks WHERE item_id IN ({placeholders}) ORDER BY updated_at DESC, chunk_order ASC",
                    item_ids,
                ).fetchall()
            else:
                chunks = []
            return {
                "items": [self._serialize_item(row) for row in items],
                "attachments": [self._serialize_attachment(row) for row in attachments],
                "chunks": [self._serialize_chunk(row) for row in chunks],
            }

    def _backfill_missing_chunk_vectors(self, connection: sqlite3.Connection, project_id: str) -> int:
        self._ensure_literature_vector_table(connection)
        rows = connection.execute(
            """
            SELECT c.id, c.project_id, c.item_id, c.chunk_text
            FROM literature_chunks c
            LEFT JOIN literature_chunk_vectors v ON v.chunk_id = c.id
            WHERE c.project_id = ? AND v.chunk_id IS NULL
            """,
            (project_id,),
        ).fetchall()
        for row in rows:
            self._upsert_chunk_vector(
                connection,
                chunk_id=row["id"],
                project_id=row["project_id"],
                item_id=row["item_id"],
                chunk_text=row["chunk_text"] or "",
            )
        return len(rows)

    def search_project_evidence(self, project_id: str, query: str, limit: int = 8) -> dict[str, Any]:
        clean_query = normalize_text(query)
        if not clean_query:
            raise ValueError("RAG query is required.")
        query_vector = rag_hash_vector(clean_query)
        query_profile = rag_token_profile(clean_query)
        query_terms = list(query_profile.keys())
        with self._connect() as connection:
            self._ensure_project(connection, project_id)
            backfilled = self._backfill_missing_chunk_vectors(connection, project_id)
            rows = connection.execute(
                """
                SELECT c.id AS chunk_id, c.item_id, c.source_kind, c.source_label, c.chunk_order, c.chunk_text,
                       v.vector_json, v.tokens_json,
                       i.title, i.authors_json, i.year, i.venue, i.doi, i.url, i.tags_json,
                       i.zotero_item_key, i.open_access_status, i.journal_title
                FROM literature_chunks c
                JOIN literature_chunk_vectors v ON v.chunk_id = c.id
                JOIN literature_items i ON i.id = c.item_id
                WHERE c.project_id = ?
                ORDER BY c.updated_at DESC
                LIMIT 500
                """,
                (project_id,),
            ).fetchall()
            if backfilled:
                connection.commit()

        scored: list[dict[str, Any]] = []
        for row in rows:
            vector = _json_loads(row["vector_json"], {})
            token_profile = _json_loads(row["tokens_json"], {})
            vector_score = max(0.0, sparse_cosine(query_vector, vector))
            lexical_score, matched_terms = rag_lexical_score(query_profile, token_profile)
            metadata_text = " ".join(
                part
                for part in [
                    row["title"] or "",
                    row["venue"] or "",
                    row["journal_title"] or "",
                    " ".join(_json_loads(row["tags_json"], [])),
                ]
                if part
            )
            metadata_profile = rag_token_profile(metadata_text)
            metadata_score, metadata_terms = rag_lexical_score(query_profile, metadata_profile)
            source_boost = 0.08 if row["source_kind"] in {"manual-fulltext", "oa-pdf-fulltext"} else 0.03
            doi_boost = 0.04 if row["doi"] else 0.0
            score = vector_score * 42 + lexical_score * 42 + metadata_score * 12 + source_boost * 10 + doi_boost * 10
            if score <= 1.5:
                continue
            matched = list(dict.fromkeys([*matched_terms, *metadata_terms]))[:16]
            confidence = "high" if score >= 26 else "medium" if score >= 12 else "low"
            scored.append(
                {
                    "sourceKind": "literature",
                    "sourceId": row["chunk_id"],
                    "label": f"RAG文献片段：{(row['title'] or '')[:48]}",
                    "excerpt": normalize_text(row["chunk_text"] or "")[:360],
                    "score": round(score, 3),
                    "metadata": {
                        "retrievalMode": "local-hybrid-rag",
                        "confidence": confidence,
                        "matchedTerms": matched,
                        "queryTerms": query_terms[:24],
                        "vectorScore": round(vector_score, 4),
                        "lexicalScore": round(lexical_score, 4),
                        "metadataScore": round(metadata_score, 4),
                        "itemId": row["item_id"],
                        "chunkOrder": row["chunk_order"],
                        "chunkSourceKind": row["source_kind"],
                        "chunkSourceLabel": row["source_label"] or "",
                        "title": row["title"] or "",
                        "authors": _json_loads(row["authors_json"], []),
                        "venue": row["venue"] or row["journal_title"] or "",
                        "year": row["year"],
                        "doi": row["doi"] or "",
                        "url": row["url"] or "",
                        "zoteroItemKey": row["zotero_item_key"] or "",
                        "openAccessStatus": row["open_access_status"] or "",
                    },
                }
            )
        ranked = sorted(scored, key=lambda item: (-float(item["score"]), item["label"]))[: max(1, min(limit, 20))]
        return {
            "projectId": project_id,
            "query": clean_query,
            "retrievalMode": "local-hybrid-rag",
            "embeddingModel": LOCAL_RAG_EMBEDDING_MODEL,
            "queryTerms": query_terms[:24],
            "backfilledVectorCount": backfilled,
            "candidateChunkCount": len(rows),
            "evidence": ranked,
        }

    def get_project_rag_status(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            self._ensure_project(connection, project_id)
            self._ensure_literature_vector_table(connection)
            item_row = connection.execute(
                "SELECT COUNT(*) AS total FROM literature_items WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            chunk_row = connection.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN source_kind IN ('manual-fulltext', 'oa-pdf-fulltext') THEN 1 ELSE 0 END) AS fulltext_total
                FROM literature_chunks
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            vector_row = connection.execute(
                "SELECT COUNT(*) AS total FROM literature_chunk_vectors WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            missing_row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM literature_chunks c
                LEFT JOIN literature_chunk_vectors v ON v.chunk_id = c.id
                WHERE c.project_id = ? AND v.chunk_id IS NULL
                """,
                (project_id,),
            ).fetchone()
        item_count = int(item_row["total"] or 0)
        chunk_count = int(chunk_row["total"] or 0)
        vector_count = int(vector_row["total"] or 0)
        missing_vector_count = int(missing_row["total"] or 0)
        return {
            "projectId": project_id,
            "retrievalMode": "local-hybrid-rag",
            "embeddingModel": LOCAL_RAG_EMBEDDING_MODEL,
            "itemCount": item_count,
            "chunkCount": chunk_count,
            "fulltextChunkCount": int(chunk_row["fulltext_total"] or 0),
            "vectorCount": vector_count,
            "missingVectorCount": missing_vector_count,
            "ready": item_count > 0 and chunk_count > 0 and missing_vector_count == 0,
        }

    def get_search_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM literature_search_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return {"run": self._serialize_run(row), "candidates": _json_loads(row["results_json"], [])}

    def suggest_search_query(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            project = self._ensure_project(connection, project_id)
            sections = connection.execute(
                "SELECT title, current_text FROM sections WHERE project_id = ? ORDER BY order_index ASC",
                (project_id,),
            ).fetchall()
        terms = suggest_terms_from_text(project["title"], sections)
        warnings: list[str] = []
        if not sections:
            warnings.append("当前项目还没有可用正文，只能从项目标题生成建议词。")
        if len(terms) < 3:
            warnings.append("可抽取的高质量关键词较少，建议补充英文关键词、DOI 或更具体的研究对象。")
        selected = terms[:6]
        return {
            "query": " ".join(item["term"] for item in selected),
            "terms": terms,
            "sourceSections": [row["title"] for row in sections[:8]],
            "warnings": warnings,
            "generatedAt": utc_now(),
        }

    def search_literature(
        self,
        project_id: str,
        query: str,
        sources: list[str] | None = None,
        limit: int = 12,
    ) -> dict[str, Any]:
        clean_query = normalize_text(query)
        if not clean_query:
            raise ValueError("Search query is required.")
        selected_sources = [source for source in (sources or DEFAULT_SOURCES) if source in DEFAULT_SOURCES]
        if not selected_sources:
            selected_sources = DEFAULT_SOURCES
        per_source_limit = max(3, min(20, limit))
        candidates: list[dict[str, Any]] = []
        warnings: list[str] = []
        with self._connect() as connection:
            self._ensure_project(connection, project_id)

        source_handlers = {
            "openalex": self._search_openalex,
            "crossref": self._search_crossref,
            "semantic-scholar": self._search_semantic_scholar,
        }
        for source in selected_sources:
            try:
                candidates.extend(source_handlers[source](clean_query, per_source_limit))
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
                warnings.append(f"{source} 检索失败，已跳过：{exc}")
            except Exception as exc:  # pragma: no cover - defensive external connector boundary
                warnings.append(f"{source} 暂时不可用，已降级到其他开放源：{exc}")

        if not candidates:
            warnings.append("开放检索源暂时没有返回结果；已保留一条本地占位结果，方便前端继续演示导入流程。")
            candidates = [self._fallback_candidate(clean_query)]
        deduped_all = self._dedupe_candidates(candidates)
        ranked = self._rank_candidates(clean_query, deduped_all)
        filtered_count = max(0, len(deduped_all) - len(ranked))
        if filtered_count:
            warnings.append(f"已过滤 {filtered_count} 条低相关候选；当前只保留命中核心关键词的结果。")
        deduped = ranked[: max(1, min(50, limit))]
        if not deduped:
            warnings.append("没有候选达到相关性阈值。中文主题建议输入 DOI、英文关键词，或使用 CNKI/机构库作人工补充。")
            deduped = [self._fallback_candidate(clean_query)]
        run_id = new_id("lit-run")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO literature_search_runs (
                  id, project_id, query, sources_json, status, total_found, deduped_count,
                  warnings_json, results_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    clean_query,
                    _json_dumps(selected_sources),
                    len(candidates),
                    len(deduped),
                    _json_dumps(warnings),
                    _json_dumps(deduped),
                    now,
                    now,
                ),
            )
            connection.commit()
        return {
            "run": {
                "id": run_id,
                "projectId": project_id,
                "query": clean_query,
                "sources": selected_sources,
                "status": "completed",
                "totalFound": len(candidates),
                "dedupedCount": len(deduped),
                "warnings": warnings,
                "createdAt": now,
                "updatedAt": now,
            },
            "candidates": deduped,
        }

    def _search_openalex(self, query: str, limit: int) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"search": query, "per-page": limit})
        payload = self.http_json(f"https://api.openalex.org/works?{params}", None)
        results: list[dict[str, Any]] = []
        for work in payload.get("results", []):
            title = compact_title(work.get("title") or "")
            if not title:
                continue
            authors = [
                (authorship.get("author") or {}).get("display_name", "")
                for authorship in work.get("authorships", [])
                if (authorship.get("author") or {}).get("display_name")
            ][:8]
            location = work.get("primary_location") or {}
            source = location.get("source") or {}
            open_access = work.get("open_access") or {}
            pdf_url = (location.get("pdf_url") or "").strip()
            is_oa = bool(open_access.get("is_oa") and pdf_url)
            doi = normalize_doi(work.get("doi"))
            publication_type = "journal-article" if work.get("type") == "article" and source.get("type") == "journal" else work.get("type") or "unknown"
            candidate = self._candidate(
                title=title,
                authors=authors,
                year=work.get("publication_year"),
                venue=source.get("display_name") or "",
                doi=doi,
                url=work.get("doi") or open_access.get("oa_url") or work.get("id") or "",
                abstract=decode_openalex_abstract(work.get("abstract_inverted_index")),
                source="openalex",
                citation_count=work.get("cited_by_count") or 0,
                open_access_status="open" if is_oa else "metadata-only",
                pdf_url=pdf_url if is_oa else "",
                tags=["openalex", publication_type, "开放全文可获取" if is_oa else "仅元数据"],
            )
            journal = journal_metadata(
                journal_title=source.get("display_name") or "",
                issn=source.get("issn"),
                issn_l=source.get("issn_l") or "",
                publisher=source.get("host_organization_name") or "",
                source_type=source.get("type") or "",
                source_id=source.get("id") or "",
                is_oa_journal=source.get("is_oa"),
                is_in_doaj=source.get("is_in_doaj"),
                is_openalex_core=source.get("is_core"),
            )
            enriched = {**candidate, "publicationType": publication_type, **journal}
            results.append({**enriched, **infer_quality_metadata(enriched)})
        return results

    def _search_crossref(self, query: str, limit: int) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"query.bibliographic": query, "rows": limit})
        payload = self.http_json(f"https://api.crossref.org/works?{params}", None)
        results: list[dict[str, Any]] = []
        for work in (payload.get("message") or {}).get("items", []):
            title = compact_title(_first(work.get("title")))
            if not title:
                continue
            authors = [
                " ".join(part for part in [author.get("given", ""), author.get("family", "")] if part).strip()
                for author in work.get("author", [])
                if author.get("given") or author.get("family")
            ][:8]
            date_parts = ((work.get("issued") or {}).get("date-parts") or [[None]])[0]
            year = date_parts[0] if date_parts else None
            publication_type = "journal-article" if work.get("type") == "journal-article" else work.get("type") or "unknown"
            candidate = self._candidate(
                title=title,
                authors=authors,
                year=year,
                venue=_first(work.get("container-title")),
                doi=normalize_doi(work.get("DOI")),
                url=work.get("URL") or "",
                abstract=normalize_text(re.sub(r"<[^>]+>", "", work.get("abstract") or "")),
                source="crossref",
                citation_count=work.get("is-referenced-by-count") or 0,
                open_access_status="metadata-only",
                pdf_url="",
                tags=["crossref", "DOI 校验", publication_type],
            )
            journal = journal_metadata(
                journal_title=_first(work.get("container-title")),
                issn=work.get("ISSN"),
                issn_l=_first(work.get("ISSN")),
                publisher=work.get("publisher") or "",
                source_type=work.get("type") or "",
            )
            enriched = {**candidate, "publicationType": publication_type, **journal}
            results.append({**enriched, **infer_quality_metadata(enriched)})
        return results

    def _search_semantic_scholar(self, query: str, limit: int) -> list[dict[str, Any]]:
        fields = "title,authors,year,venue,abstract,url,citationCount,externalIds,openAccessPdf,isOpenAccess,publicationTypes,publicationVenue"
        params = urllib.parse.urlencode({"query": query, "limit": limit, "fields": fields})
        headers: dict[str, str] = {}
        api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
        if api_key:
            headers["x-api-key"] = api_key
        payload = self.http_json(f"https://api.semanticscholar.org/graph/v1/paper/search?{params}", headers)
        results: list[dict[str, Any]] = []
        for paper in payload.get("data", []):
            title = compact_title(paper.get("title") or "")
            if not title:
                continue
            external_ids = paper.get("externalIds") or {}
            pdf = paper.get("openAccessPdf") or {}
            pdf_url = (pdf.get("url") or "").strip()
            is_oa = bool(paper.get("isOpenAccess") and pdf_url)
            publication_types = paper.get("publicationTypes") or []
            publication_type = "journal-article" if "JournalArticle" in publication_types else (publication_types[0] if publication_types else "unknown")
            publication_venue = paper.get("publicationVenue") or {}
            candidate = self._candidate(
                title=title,
                authors=[author.get("name", "") for author in paper.get("authors", []) if author.get("name")][:8],
                year=paper.get("year"),
                venue=paper.get("venue") or "",
                doi=normalize_doi(external_ids.get("DOI")),
                url=paper.get("url") or "",
                abstract=paper.get("abstract") or "",
                source="semantic-scholar",
                citation_count=paper.get("citationCount") or 0,
                open_access_status="open" if is_oa else "metadata-only",
                pdf_url=pdf_url if is_oa else "",
                tags=["semantic-scholar", publication_type, "开放全文可获取" if is_oa else "仅元数据"],
            )
            journal = journal_metadata(
                journal_title=publication_venue.get("name") or paper.get("venue") or "",
                issn=publication_venue.get("issn"),
                source_type=publication_venue.get("type") or "",
                source_id=publication_venue.get("id") or "",
            )
            enriched = {**candidate, "publicationType": publication_type, **journal}
            results.append({**enriched, **infer_quality_metadata(enriched)})
        return results

    def _candidate(
        self,
        *,
        title: str,
        authors: list[str],
        year: int | None,
        venue: str,
        doi: str,
        url: str,
        abstract: str,
        source: str,
        citation_count: int,
        open_access_status: str,
        pdf_url: str,
        tags: list[str],
    ) -> dict[str, Any]:
        clean_title = compact_title(title)
        clean_doi = normalize_doi(doi)
        return {
            "id": f"candidate-{short_hash((clean_doi or clean_title) + source)}",
            "projectId": "",
            "title": clean_title,
            "authors": [author for author in authors if author],
            "year": int(year) if isinstance(year, int) or (isinstance(year, str) and year.isdigit()) else None,
            "venue": venue or "",
            "doi": clean_doi,
            "url": url or "",
            "abstract": normalize_text(abstract or ""),
            "source": source,
            "sources": [source],
            "language": infer_language(f"{clean_title} {abstract}"),
            "citationCount": int(citation_count or 0),
            "openAccessStatus": open_access_status if open_access_status in {"open", "metadata-only", "unknown"} else "unknown",
            "pdfUrl": pdf_url or "",
            "zoteroItemKey": None,
            "tags": tags,
            "relevanceScore": 0,
            "matchReason": "待评分",
            "publicationType": "unknown",
            "qualitySignals": [],
            "qualityStatus": "待分区核验",
            "jcrQuartile": None,
            "casQuartile": None,
            **journal_metadata(),
        }

    def _fallback_candidate(self, query: str) -> dict[str, Any]:
        return self._candidate(
            title=f"待人工确认的文献线索：{query}",
            authors=[],
            year=None,
            venue="本地检索占位",
            doi="",
            url="",
            abstract="当前开放检索源没有返回稳定结果。建议换一个关键词，或从 DOI / 标题粘贴导入。",
            source="local-fallback",
            citation_count=0,
            open_access_status="unknown",
            pdf_url="",
            tags=["需人工确认", "低相关结果已过滤"],
        ) | {"matchReason": "开放源没有返回足够相关的结果", "relevanceScore": 0}

    def _merge_candidate(self, base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = {**base}
        merged["sources"] = sorted(set(base.get("sources", []) + incoming.get("sources", [])))
        merged["source"] = merged["sources"][0]
        if not merged.get("doi") and incoming.get("doi"):
            merged["doi"] = incoming["doi"]
        for field in ["abstract", "venue", "url"]:
            if not merged.get(field) and incoming.get(field):
                merged[field] = incoming[field]
        if not merged.get("year") and incoming.get("year"):
            merged["year"] = incoming["year"]
        if not merged.get("authors") and incoming.get("authors"):
            merged["authors"] = incoming["authors"]
        if incoming.get("citationCount", 0) > merged.get("citationCount", 0):
            merged["citationCount"] = incoming["citationCount"]
        if incoming.get("openAccessStatus") == "open" and incoming.get("pdfUrl"):
            merged["openAccessStatus"] = "open"
            merged["pdfUrl"] = incoming["pdfUrl"]
        if merged.get("publicationType") != "journal-article" and incoming.get("publicationType") == "journal-article":
            merged["publicationType"] = "journal-article"
        for field in ["journalTitle", "issnL", "publisher", "sourceType", "sourceId"]:
            if not merged.get(field) and incoming.get(field):
                merged[field] = incoming[field]
        merged["issn"] = normalize_issn_list(merged.get("issn", []) + incoming.get("issn", []))
        for field in ["isOaJournal", "isInDoaj", "isOpenAlexCore"]:
            merged[field] = bool(merged.get(field) or incoming.get(field))
        merged["tags"] = sorted(set(base.get("tags", []) + incoming.get("tags", [])))
        quality = infer_quality_metadata(merged)
        merged.update(quality)
        merged["id"] = f"candidate-{short_hash((merged.get('doi') or normalized_title_key(merged['title'])) + ','.join(merged['sources']))}"
        return merged

    def _dedupe_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for candidate in candidates:
            title_key = normalized_title_key(candidate.get("title", ""))
            doi = normalize_doi(candidate.get("doi"))
            key = f"doi:{doi}" if doi else f"title:{title_key}:{candidate.get('year') or ''}"
            if not title_key:
                continue
            if key not in merged:
                merged[key] = candidate
                order.append(key)
            else:
                merged[key] = self._merge_candidate(merged[key], candidate)
        return sorted((merged[key] for key in order), key=lambda item: (item.get("openAccessStatus") != "open", -item.get("citationCount", 0)))

    def _rank_candidates(self, query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        query_terms = extract_query_terms(query)
        threshold = 28 if re.search(r"[\u4e00-\u9fff]", query) and len(query_terms) >= 5 else 16
        minimum_matches = 2 if len(query_terms) >= 5 else 1
        ranked: list[dict[str, Any]] = []
        for candidate in candidates:
            score, reason, match_count = score_candidate_relevance(query, candidate)
            if score < threshold or (match_count < minimum_matches and score < 45):
                continue
            ranked.append({**candidate, "relevanceScore": score, "matchReason": reason})
        return sorted(
            ranked,
            key=lambda item: (
                item.get("publicationType") != "journal-article",
                "有 DOI" not in item.get("qualitySignals", []),
                -item.get("relevanceScore", 0),
                item.get("openAccessStatus") != "open",
                -item.get("citationCount", 0),
            ),
        )

    def import_literature(
        self,
        project_id: str,
        *,
        run_id: str | None = None,
        item_ids: list[str] | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        candidates = list(items or [])
        if run_id:
            run = self.get_search_run(run_id)
            selected = set(item_ids or [])
            run_candidates = run["candidates"]
            candidates.extend([item for item in run_candidates if not selected or item["id"] in selected])
        if not candidates:
            raise ValueError("No literature items selected for import.")
        imported: list[dict[str, Any]] = []
        auto_fetch_results: list[dict[str, Any]] = []
        with self._connect() as connection:
            self._ensure_project(connection, project_id)
            for candidate in self._dedupe_candidates(candidates):
                imported.append(self._upsert_literature_item(connection, project_id, candidate))
            connection.commit()
        for item in imported:
            if item.get("openAccessStatus") == "open" and item.get("pdfUrl"):
                try:
                    auto_fetch_results.append(self.fetch_open_fulltext(project_id, item["id"]))
                except Exception as exc:
                    auto_fetch_results.append(
                        {
                            "itemId": item["id"],
                            "title": item.get("title", ""),
                            "status": "failed",
                            "error": str(exc),
                        }
                    )
        return {
            "items": imported,
            "fulltextResults": auto_fetch_results,
            "library": self.list_project_literature(project_id),
        }

    def _find_existing_item(self, connection: sqlite3.Connection, project_id: str, candidate: dict[str, Any]) -> sqlite3.Row | None:
        doi = normalize_doi(candidate.get("doi"))
        if doi:
            row = connection.execute(
                "SELECT * FROM literature_items WHERE project_id = ? AND lower(coalesce(doi, '')) = ?",
                (project_id, doi),
            ).fetchone()
            if row is not None:
                return row
        title_key = normalized_title_key(candidate.get("title", ""))
        year = candidate.get("year")
        rows = connection.execute("SELECT * FROM literature_items WHERE project_id = ?", (project_id,)).fetchall()
        for row in rows:
            if normalized_title_key(row["title"]) == title_key and (row["year"] or None) == year:
                return row
        return None

    def _upsert_literature_item(self, connection: sqlite3.Connection, project_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        existing = self._find_existing_item(connection, project_id, candidate)
        if existing is not None:
            merged = self._merge_candidate(self._serialize_item(existing), {**candidate, "projectId": project_id})
            connection.execute(
                """
                UPDATE literature_items
                SET authors_json = ?, venue = ?, doi = ?, url = ?, abstract = ?, source = ?,
                    sources_json = ?, language = ?, citation_count = ?, open_access_status = ?,
                    pdf_url = ?, tags_json = ?, journal_title = ?, issn_json = ?, issn_l = ?,
                    publisher = ?, source_type = ?, source_id = ?, is_oa_journal = ?,
                    is_in_doaj = ?, is_openalex_core = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _json_dumps(merged.get("authors", [])),
                    merged.get("venue", ""),
                    normalize_doi(merged.get("doi")),
                    merged.get("url", ""),
                    merged.get("abstract", ""),
                    merged.get("source", ""),
                    _json_dumps(merged.get("sources", [])),
                    merged.get("language", "en"),
                    merged.get("citationCount", 0),
                    merged.get("openAccessStatus", "unknown"),
                    merged.get("pdfUrl", ""),
                    _json_dumps(merged.get("tags", [])),
                    merged.get("journalTitle", ""),
                    _json_dumps(merged.get("issn", [])),
                    merged.get("issnL", ""),
                    merged.get("publisher", ""),
                    merged.get("sourceType", ""),
                    merged.get("sourceId", ""),
                    int(bool(merged.get("isOaJournal"))),
                    int(bool(merged.get("isInDoaj"))),
                    int(bool(merged.get("isOpenAlexCore"))),
                    now,
                    existing["id"],
                ),
            )
            row = connection.execute("SELECT * FROM literature_items WHERE id = ?", (existing["id"],)).fetchone()
            self._replace_item_chunks(
                connection,
                project_id=project_id,
                item_id=existing["id"],
                source_kind="metadata-abstract",
                source_label="Imported abstract",
                text=f"{merged.get('title', '')}\n\n{merged.get('abstract', '')}",
            )
            return self._serialize_item(row)
        item_id = new_id("lit")
        connection.execute(
            """
            INSERT INTO literature_items (
              id, project_id, title, authors_json, year, venue, doi, url, abstract, source,
              sources_json, language, citation_count, open_access_status, pdf_url,
              zotero_item_key, tags_json, journal_title, issn_json, issn_l, publisher,
              source_type, source_id, is_oa_journal, is_in_doaj, is_openalex_core,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                project_id,
                compact_title(candidate.get("title", "")),
                _json_dumps(candidate.get("authors", [])),
                candidate.get("year"),
                candidate.get("venue", ""),
                normalize_doi(candidate.get("doi")),
                candidate.get("url", ""),
                normalize_text(candidate.get("abstract", "")),
                candidate.get("source", "unknown"),
                _json_dumps(candidate.get("sources", [candidate.get("source", "unknown")])),
                candidate.get("language") or infer_language(candidate.get("title", "")),
                candidate.get("citationCount", 0),
                candidate.get("openAccessStatus", "unknown"),
                candidate.get("pdfUrl", ""),
                _json_dumps(candidate.get("tags", [])),
                candidate.get("journalTitle", candidate.get("venue", "")),
                _json_dumps(candidate.get("issn", [])),
                candidate.get("issnL", ""),
                candidate.get("publisher", ""),
                candidate.get("sourceType", ""),
                candidate.get("sourceId", ""),
                int(bool(candidate.get("isOaJournal"))),
                int(bool(candidate.get("isInDoaj"))),
                int(bool(candidate.get("isOpenAlexCore"))),
                now,
                now,
            ),
        )
        self._replace_item_chunks(
            connection,
            project_id=project_id,
            item_id=item_id,
            source_kind="metadata-abstract",
            source_label="Imported abstract",
            text=f"{candidate.get('title', '')}\n\n{candidate.get('abstract', '')}",
        )
        row = connection.execute("SELECT * FROM literature_items WHERE id = ?", (item_id,)).fetchone()
        return self._serialize_item(row)

    def _replace_item_chunks(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        item_id: str,
        source_kind: str,
        source_label: str,
        text: str,
    ) -> list[dict[str, Any]]:
        self._ensure_literature_vector_table(connection)
        existing_chunks = connection.execute(
            "SELECT id FROM literature_chunks WHERE project_id = ? AND item_id = ? AND source_kind = ?",
            (project_id, item_id, source_kind),
        ).fetchall()
        if existing_chunks:
            placeholders = ",".join("?" for _ in existing_chunks)
            connection.execute(
                f"DELETE FROM literature_chunk_vectors WHERE chunk_id IN ({placeholders})",
                [row["id"] for row in existing_chunks],
            )
        connection.execute(
            "DELETE FROM literature_chunks WHERE project_id = ? AND item_id = ? AND source_kind = ?",
            (project_id, item_id, source_kind),
        )
        chunks = split_literature_chunks(text)
        now = utc_now()
        stored: list[dict[str, Any]] = []
        for chunk_order, chunk_text in enumerate(chunks):
            chunk_id = new_id("lit-chunk")
            connection.execute(
                """
                INSERT INTO literature_chunks (
                  id, project_id, item_id, source_kind, source_label, chunk_order,
                  chunk_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, project_id, item_id, source_kind, source_label, chunk_order, chunk_text, now, now),
            )
            self._upsert_chunk_vector(
                connection,
                chunk_id=chunk_id,
                project_id=project_id,
                item_id=item_id,
                chunk_text=chunk_text,
                indexed_at=now,
            )
            stored.append(
                {
                    "id": chunk_id,
                    "projectId": project_id,
                    "itemId": item_id,
                    "sourceKind": source_kind,
                    "sourceLabel": source_label,
                    "chunkOrder": chunk_order,
                    "chunkText": chunk_text,
                    "createdAt": now,
                    "updatedAt": now,
                }
            )
        return stored

    def _upsert_chunk_vector(
        self,
        connection: sqlite3.Connection,
        *,
        chunk_id: str,
        project_id: str,
        item_id: str,
        chunk_text: str,
        indexed_at: str | None = None,
    ) -> None:
        now = indexed_at or utc_now()
        connection.execute(
            """
            INSERT INTO literature_chunk_vectors (
              chunk_id, project_id, item_id, embedding_model, vector_json, tokens_json, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chunk_id) DO UPDATE SET
              project_id = EXCLUDED.project_id,
              item_id = EXCLUDED.item_id,
              embedding_model = EXCLUDED.embedding_model,
              vector_json = EXCLUDED.vector_json,
              tokens_json = EXCLUDED.tokens_json,
              indexed_at = EXCLUDED.indexed_at
            """,
            (
                chunk_id,
                project_id,
                item_id,
                LOCAL_RAG_EMBEDDING_MODEL,
                _json_dumps(rag_hash_vector(chunk_text)),
                _json_dumps(rag_token_profile(chunk_text)),
                now,
            ),
        )

    def index_item_text(
        self,
        *,
        project_id: str,
        item_id: str,
        text: str,
        source_label: str = "manual-fulltext",
    ) -> dict[str, Any]:
        normalized = normalize_text(text)
        if not normalized:
            raise ValueError("Text is required for literature indexing.")
        with self._connect() as connection:
            self._ensure_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM literature_items WHERE id = ? AND project_id = ?",
                (item_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(item_id)
            chunks = self._replace_item_chunks(
                connection,
                project_id=project_id,
                item_id=item_id,
                source_kind="manual-fulltext",
                source_label=source_label,
                text=normalized,
            )
            connection.commit()
        return {"itemId": item_id, "chunkCount": len(chunks), "chunks": chunks}

    def _download_binary(self, url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "DraftRefine/1.0 (+https://draftrefine.local)",
                "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.read()

    def _extract_pdf_text(self, raw_bytes: bytes) -> str:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw_bytes))
        pages: list[str] = []
        for page in reader.pages:
            extracted = normalize_text(page.extract_text() or "")
            if extracted:
                pages.append(extracted)
        return "\n\n".join(pages).strip()

    def _store_open_fulltext_pdf(self, item_id: str, raw_bytes: bytes) -> str:
        if blob_enabled():
            uploaded = upload_bytes_to_blob(
                pathname=f"draftrefine/literature/{item_id}.pdf",
                body=raw_bytes,
                content_type="application/pdf",
            )
            return uploaded["url"]
        path = self.storage_dir / f"{item_id}.pdf"
        path.write_bytes(raw_bytes)
        return str(path)

    def _attachment_download_payload(self, project_id: str, attachment: sqlite3.Row | None) -> str:
        if attachment is None:
            return ""
        local_path = (attachment["local_path"] or "").strip()
        attachment_url = (attachment["url"] or "").strip()
        if not local_path and not attachment_url:
            return ""
        return f"/api/projects/{project_id}/literature/attachments/{attachment['id']}/download"

    def _build_fulltext_fetch_result(
        self,
        *,
        project_id: str,
        item: dict[str, Any],
        attachment: sqlite3.Row | None,
        status: str,
        warning: str = "",
        error: str = "",
        chunk_count: int = 0,
        indexing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        serialized_attachment = self._serialize_attachment(attachment) if attachment is not None else None
        download_url = self._attachment_download_payload(project_id, attachment)
        if serialized_attachment is not None and download_url:
            serialized_attachment["downloadUrl"] = download_url
        return {
            "itemId": item["id"],
            "title": item.get("title", ""),
            "status": status,
            "warning": warning,
            "error": error,
            "downloadUrl": download_url,
            "attachment": serialized_attachment,
            "chunkCount": chunk_count,
            "indexing": indexing,
        }

    def fetch_open_fulltext(self, project_id: str, item_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            self._ensure_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM literature_items WHERE id = ? AND project_id = ?",
                (item_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(item_id)
            item = self._serialize_item(row)
            if item["openAccessStatus"] != "open" or not item["pdfUrl"]:
                raise ValueError("Only explicitly open-access PDF links can be attached.")
            existing = connection.execute(
                "SELECT * FROM literature_attachments WHERE item_id = ? AND url = ?",
                (item_id, item["pdfUrl"]),
            ).fetchone()
            indexed_row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM literature_chunks
                WHERE project_id = ? AND item_id = ? AND source_kind = 'oa-pdf-fulltext'
                """,
                (project_id, item_id),
            ).fetchone()
            existing_indexing = None
            if indexed_row is not None and int(indexed_row["total"] or 0) > 0:
                existing_indexing = {
                    "itemId": item_id,
                    "sourceKind": "oa-pdf-fulltext",
                    "chunkCount": int(indexed_row["total"] or 0),
                }
            if existing is not None and (existing["local_path"] or "").strip():
                return self._build_fulltext_fetch_result(
                    project_id=project_id,
                    item=item,
                    attachment=existing,
                    status="downloaded",
                    chunk_count=int(existing_indexing["chunkCount"]) if existing_indexing else 0,
                    indexing=existing_indexing,
                )

            try:
                raw_bytes = self._download_binary(item["pdfUrl"])
                stored_path = self._store_open_fulltext_pdf(item_id, raw_bytes)
                now = utc_now()
                if existing is None:
                    attachment_id = new_id("attach")
                    connection.execute(
                        """
                        INSERT INTO literature_attachments (id, item_id, kind, url, local_path, access_type, status, created_at)
                        VALUES (?, ?, 'pdf', ?, ?, 'open-access', 'downloaded', ?)
                        """,
                        (attachment_id, item_id, item["pdfUrl"], stored_path, now),
                    )
                else:
                    attachment_id = existing["id"]
                    connection.execute(
                        """
                        UPDATE literature_attachments
                        SET local_path = ?, access_type = 'open-access', status = 'downloaded'
                        WHERE id = ?
                        """,
                        (stored_path, attachment_id),
                    )
                chunk_count = 0
                warning = ""
                try:
                    extracted_text = self._extract_pdf_text(raw_bytes)
                    if extracted_text.strip():
                        chunks = self._replace_item_chunks(
                            connection,
                            project_id=project_id,
                            item_id=item_id,
                            source_kind="oa-pdf-fulltext",
                            source_label="Open-access PDF",
                            text=extracted_text,
                        )
                        chunk_count = len(chunks)
                    else:
                        warning = "PDF å·²ä¸‹è½½ï¼Œä½†æš‚æ—¶æ²¡æœ‰æŠ½å–å‡ºå¯ç”¨æ­£æ–‡ã€‚"
                except Exception as exc:
                    warning = str(exc) or "PDF å·²ä¸‹è½½ï¼Œä½†æš‚æ—¶æ²¡æœ‰æŠ½å–å‡ºå¯ç”¨æ­£æ–‡ã€‚"
                connection.commit()
                attachment = connection.execute(
                    "SELECT * FROM literature_attachments WHERE id = ?",
                    (attachment_id,),
                ).fetchone()
                return self._build_fulltext_fetch_result(
                    project_id=project_id,
                    item=item,
                    attachment=attachment,
                    status="downloaded",
                    warning=warning,
                    chunk_count=chunk_count,
                    indexing=(
                        {
                            "itemId": item_id,
                            "sourceKind": "oa-pdf-fulltext",
                            "chunkCount": chunk_count,
                        }
                        if chunk_count
                        else None
                    ),
                )
            except Exception as exc:
                warning = str(exc)

            attachment_id = existing["id"] if existing is not None else new_id("attach")
            now = utc_now()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO literature_attachments (id, item_id, kind, url, local_path, access_type, status, created_at)
                    VALUES (?, ?, 'pdf', ?, NULL, 'open-access', 'linked', ?)
                    """,
                    (attachment_id, item_id, item["pdfUrl"], now),
                )
            else:
                connection.execute(
                    """
                    UPDATE literature_attachments
                    SET local_path = NULL, access_type = 'open-access', status = 'linked'
                    WHERE id = ?
                    """,
                    (attachment_id,),
                )
            connection.commit()
            attachment = connection.execute("SELECT * FROM literature_attachments WHERE id = ?", (attachment_id,)).fetchone()
            return self._build_fulltext_fetch_result(
                project_id=project_id,
                item=item,
                attachment=attachment,
                status="linked",
                warning=warning,
                chunk_count=int(existing_indexing["chunkCount"]) if existing_indexing else 0,
                indexing=existing_indexing,
            )

    def fetch_open_fulltext_batch(self, project_id: str, item_ids: list[str]) -> dict[str, Any]:
        selected_ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id]
        if not selected_ids:
            raise ValueError("Select at least one literature item before fetching full text.")

        results: list[dict[str, Any]] = []
        downloadable_paths: list[Path] = []
        downloadable_names: list[str] = []
        for item_id in selected_ids:
            try:
                result = self.fetch_open_fulltext(project_id, item_id)
            except Exception as exc:
                result = {
                    "itemId": item_id,
                    "title": "",
                    "status": "failed",
                    "error": str(exc),
                    "warning": "",
                    "downloadUrl": "",
                    "attachment": None,
                    "chunkCount": 0,
                }
            results.append(result)
            attachment = result.get("attachment") or {}
            try:
                resolved_path = materialize_storage_ref(
                    storage_ref=attachment.get("localPath") or attachment.get("url") or "",
                    file_name=f"{result.get('itemId', 'literature')}.pdf",
                    temp_dir=self.storage_dir / "_materialized",
                )
                downloadable_paths.append(resolved_path)
                downloadable_names.append(f"{result.get('itemId', 'literature')}.pdf")
            except FileNotFoundError:
                continue

        bundle_url = ""
        file_count = len(downloadable_paths)
        if downloadable_paths:
            bundle_name = self._create_download_bundle(project_id, downloadable_paths, downloadable_names)
            bundle_url = f"/api/projects/{project_id}/literature/download-bundles/{bundle_name}"

        return {
            "results": results,
            "downloadedCount": sum(1 for item in results if item.get("status") == "downloaded"),
            "linkedCount": sum(1 for item in results if item.get("status") == "linked"),
            "failedCount": sum(1 for item in results if item.get("status") == "failed"),
            "skippedCount": sum(1 for item in results if item.get("status") == "skipped"),
            "downloadUrl": bundle_url,
            "fileCount": file_count,
        }

    def _create_download_bundle(self, project_id: str, files: list[Path], names: list[str]) -> str:
        bundle_dir = self.storage_dir / "bundles"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_name = f"{project_id}-{short_hash('|'.join(str(path) for path in files))}.zip"
        bundle_path = bundle_dir / bundle_name
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, file_path in enumerate(files):
                arcname = names[index] if index < len(names) else file_path.name
                archive.write(file_path, arcname=arcname)
        return bundle_name

    def get_attachment_download(self, *, project_id: str, attachment_id: str) -> tuple[Path, str, str]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT a.*, i.project_id
                FROM literature_attachments a
                JOIN literature_items i ON i.id = a.item_id
                WHERE a.id = ? AND i.project_id = ?
                """,
                (attachment_id, project_id),
            ).fetchone()
        if row is None:
            raise KeyError(attachment_id)
        try:
            path = materialize_storage_ref(
                storage_ref=(row["local_path"] or "").strip() or (row["url"] or "").strip(),
                file_name=f"{attachment_id}.pdf",
                temp_dir=self.storage_dir / "_materialized",
            )
        except FileNotFoundError:
            raise KeyError(attachment_id)
        content_type, _ = mimetypes.guess_type(path.name)
        return path, content_type or "application/octet-stream", path.name

    def get_download_bundle(self, *, project_id: str, bundle_name: str) -> tuple[Path, str, str]:
        safe_name = Path(bundle_name).name
        path = self.storage_dir / "bundles" / safe_name
        if not path.exists() or path.suffix.lower() != ".zip" or not safe_name.startswith(f"{project_id}-"):
            raise KeyError(bundle_name)
        return path, "application/zip", safe_name

    def sync_to_zotero(self, project_id: str, item_ids: list[str]) -> dict[str, Any]:
        selected_ids = [item_id for item_id in item_ids if item_id]
        if not selected_ids:
            raise ValueError("Select at least one literature item before syncing to Zotero.")
        with self._connect() as connection:
            self._ensure_project(connection, project_id)
            placeholders = ",".join("?" for _ in selected_ids)
            rows = connection.execute(
                f"SELECT * FROM literature_items WHERE project_id = ? AND id IN ({placeholders})",
                [project_id, *selected_ids],
            ).fetchall()
        if not rows:
            raise ValueError("Selected literature items were not found.")

        zotero = self._zotero_client()
        collection_key = self._ensure_zotero_collection(project_id, zotero)
        events: list[dict[str, Any]] = []
        with self._connect() as connection:
            for row in rows:
                item = self._serialize_item(row)
                now = utc_now()
                if item.get("zoteroItemKey"):
                    event = self._record_sync_event(
                        connection,
                        project_id,
                        item["id"],
                        "skipped",
                        item["zoteroItemKey"],
                        "已同步过 Zotero，本次跳过重复写入。",
                        now,
                    )
                    events.append(event)
                    continue
                try:
                    zotero_key = self._create_zotero_item(zotero, item, collection_key)
                    connection.execute(
                        "UPDATE literature_items SET zotero_item_key = ?, updated_at = ? WHERE id = ?",
                        (zotero_key, now, item["id"]),
                    )
                    events.append(self._record_sync_event(connection, project_id, item["id"], "success", zotero_key, "", now))
                except Exception as exc:  # pragma: no cover - depends on external Zotero API
                    events.append(self._record_sync_event(connection, project_id, item["id"], "failed", None, str(exc), now))
            connection.commit()
        return {"events": events, "library": self.list_project_literature(project_id)}

    def _record_sync_event(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        item_id: str,
        status: str,
        zotero_item_key: str | None,
        error: str,
        synced_at: str,
    ) -> dict[str, Any]:
        event_id = new_id("zotero")
        connection.execute(
            """
            INSERT INTO zotero_sync_events (id, project_id, item_id, status, zotero_item_key, error, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, project_id, item_id, status, zotero_item_key, error, synced_at),
        )
        return {
            "id": event_id,
            "projectId": project_id,
            "itemId": item_id,
            "status": status,
            "zoteroItemKey": zotero_item_key,
            "error": error,
            "syncedAt": synced_at,
        }

    def _zotero_client(self) -> Any:
        if self.zotero_client_factory is not None:
            return self.zotero_client_factory()
        load_literature_env()
        api_key = os.getenv("ZOTERO_API_KEY", "")
        user_id = os.getenv("ZOTERO_USER_ID", "")
        group_id = os.getenv("ZOTERO_GROUP_ID", "")
        if not api_key or not (user_id or group_id):
            raise ValueError("Zotero is not configured. Set ZOTERO_API_KEY and ZOTERO_USER_ID or ZOTERO_GROUP_ID.")
        try:
            from pyzotero import zotero
        except ImportError as exc:  # pragma: no cover - dependency setup guard
            raise RuntimeError("pyzotero is required for Zotero sync. Run: python -m pip install -r backend/requirements.txt") from exc
        if group_id:
            return zotero.Zotero(group_id, "group", api_key)
        return zotero.Zotero(user_id, "user", api_key)

    def _ensure_zotero_collection(self, project_id: str, zotero: Any) -> str | None:
        with self._connect() as connection:
            project = self._ensure_project(connection, project_id)
            existing = connection.execute(
                "SELECT * FROM literature_collections WHERE project_id = ? ORDER BY created_at ASC LIMIT 1",
                (project_id,),
            ).fetchone()
            if existing is not None and existing["zotero_collection_key"]:
                return existing["zotero_collection_key"]
            collection_name = f"DraftRefine - {project['title']}"
            collection_id = existing["id"] if existing is not None else new_id("lit-col")
            collection_key: str | None = None
            try:
                created = zotero.create_collections([{"name": collection_name}])
                success = (created.get("successful") or {}).get("0") if isinstance(created, dict) else None
                if isinstance(success, dict):
                    collection_key = success.get("key") or (success.get("data") or {}).get("key")
            except Exception:
                collection_key = None
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO literature_collections (id, project_id, name, zotero_collection_key, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (collection_id, project_id, collection_name, collection_key, utc_now()),
                )
            else:
                connection.execute(
                    "UPDATE literature_collections SET zotero_collection_key = ? WHERE id = ?",
                    (collection_key, collection_id),
                )
            connection.commit()
            return collection_key

    def _create_zotero_item(self, zotero: Any, item: dict[str, Any], collection_key: str | None) -> str:
        template = zotero.item_template("journalArticle")
        template["title"] = item["title"]
        template["creators"] = [{"creatorType": "author", "lastName": author} for author in item.get("authors", [])[:8]]
        if item.get("year"):
            template["date"] = str(item["year"])
        if item.get("venue"):
            template["publicationTitle"] = item["venue"]
        if item.get("doi"):
            template["DOI"] = item["doi"]
        if item.get("url"):
            template["url"] = item["url"]
        if item.get("abstract"):
            template["abstractNote"] = item["abstract"][:4000]
        template["language"] = item.get("language", "")
        template["tags"] = [{"tag": tag} for tag in item.get("tags", [])[:10]]
        if collection_key:
            template["collections"] = [collection_key]
        created = zotero.create_items([template])
        successful = created.get("successful", {}) if isinstance(created, dict) else {}
        first = successful.get("0") or next(iter(successful.values()), None)
        if not isinstance(first, dict):
            raise RuntimeError("Zotero did not return a created item key.")
        key = first.get("key") or (first.get("data") or {}).get("key")
        if not key:
            raise RuntimeError("Zotero did not return a created item key.")
        return key

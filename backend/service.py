from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hmac
import hashlib
from contextlib import contextmanager
import io
import json
import os
import re
import secrets
import socket
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from time import perf_counter, sleep
from typing import Any, Iterator
from xml.etree import ElementTree as ET

import yaml

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - optional dependency in minimal setups
    PdfReader = None

from .db import open_database
from .demo_seed import DEMO_PROJECTS
from .revision_agent import RevisionGraph, RevisionState
from .storage import blob_enabled, is_remote_storage_ref, materialize_storage_ref, upload_bytes_to_blob

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent


def resolve_runtime_data_dir() -> Path:
    configured = (os.getenv("DRAFTREFINE_DATA_DIR") or "").strip()
    if configured:
        return Path(configured)
    if (os.getenv("VERCEL") or "").strip():
        return Path("/tmp/draftrefine-data")
    return BACKEND_DIR / "data"


DATA_DIR = resolve_runtime_data_dir()
UPLOAD_DIR = DATA_DIR / "uploads"
PREVIEW_DIR = DATA_DIR / "previews"
PROMPTS_DIR = BACKEND_DIR / "prompts"
DEFAULT_DB_PATH = DATA_DIR / "draftrefine.sqlite3"
LOCAL_PYTHON_PACKAGES = PROJECT_ROOT / ".python_packages"

if LOCAL_PYTHON_PACKAGES.exists():
    local_packages = str(LOCAL_PYTHON_PACKAGES)
    if local_packages not in sys.path:
        sys.path.insert(0, local_packages)

try:
    import fitz
except ImportError:  # pragma: no cover - optional dependency in minimal setups
    fitz = None


def load_env_file(env_path: Path = PROJECT_ROOT / ".env") -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_env_file()


class ModelInvocationError(RuntimeError):
    def __init__(self, message: str, *, attempts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def normalize_email(value: str) -> str:
    return compact_text(value).lower()


def normalize_username(value: str) -> str:
    return compact_text(value).lower()


def session_expiry_iso(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_password_hash(password: str, iterations: int = 480000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password_hash(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt, digest = encoded_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        recalculated = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            int(iterations_text),
        ).hex()
        return hmac.compare_digest(recalculated, digest)
    except (ValueError, TypeError):
        return False


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def normalize_text(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def locate_selected_text(base_text: str, selected_text: str, selection_start: int | None = None, selection_end: int | None = None) -> tuple[int, int] | None:
    selected = selected_text.strip()
    if not selected:
        return None
    if selection_start is not None and selection_end is not None and 0 <= selection_start < selection_end <= len(base_text):
        indexed_slice = base_text[selection_start:selection_end]
        if compact_text(indexed_slice) == compact_text(selected):
            return selection_start, selection_end
    exact_index = base_text.find(selected)
    if exact_index >= 0:
        return exact_index, exact_index + len(selected)
    parts = [re.escape(part) for part in re.split(r"\s+", selected) if part]
    if not parts:
        return None
    match = re.search(r"\s+".join(parts), base_text)
    if match:
        return match.start(), match.end()
    return None


def first_diff_span(before: str, after: str) -> tuple[int, int, int, int]:
    prefix = 0
    max_prefix = min(len(before), len(after))
    while prefix < max_prefix and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    max_suffix = min(len(before) - prefix, len(after) - prefix)
    while suffix < max_suffix and before[len(before) - 1 - suffix] == after[len(after) - 1 - suffix]:
        suffix += 1
    before_end = len(before) - suffix
    after_end = len(after) - suffix
    if before_end < prefix:
        before_end = prefix
    if after_end < prefix:
        after_end = prefix
    return prefix, before_end, prefix, after_end


def preview_kind_for_file(file_name: str, content_type: str | None) -> str:
    extension = Path(file_name).suffix.lower()
    if extension == ".pdf" or content_type == "application/pdf":
        return "pdf"
    if extension in {".doc", ".docx"} or content_type in {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        return "docx"
    if extension == ".txt" or (content_type or "").startswith("text/"):
        return "text"
    return "unsupported"


def is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def detect_revision_language(text: str, fallback: str = "en") -> str:
    normalized = normalize_text(text)
    if not normalized:
        return fallback if fallback in {"zh", "en"} else "en"
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    latin_words = len(re.findall(r"[A-Za-z]{2,}", normalized))
    if chinese_chars > 0 and latin_words == 0:
        return "zh"
    if latin_words > 0 and chinese_chars == 0:
        return "en"
    if chinese_chars >= 8 and chinese_chars >= latin_words * 1.2:
        return "zh"
    if latin_words >= 6 and latin_words >= chinese_chars * 1.2:
        return "en"
    return fallback if fallback in {"zh", "en"} else ("zh" if chinese_chars >= latin_words else "en")


def token_estimate(text: str) -> int:
    return max(1, len(text) // 4)


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def chinese_overlap_ratio(source: str, candidate: str) -> float:
    source_chars = set(re.findall(r"[\u4e00-\u9fff]", source))
    candidate_chars = set(re.findall(r"[\u4e00-\u9fff]", candidate))
    if not source_chars:
        return 1.0
    return len(source_chars & candidate_chars) / len(source_chars)


REVISION_QUERY_STOP_TERMS = {
    "论文",
    "研究",
    "本研究",
    "本文",
    "问题",
    "方法",
    "结果",
    "讨论",
    "结论",
    "以及",
    "还有",
    "通过",
    "基于",
    "关于",
    "背景",
    "section",
    "study",
    "paper",
    "method",
    "result",
    "discussion",
    "conclusion",
}


def extract_revision_terms(text: str) -> list[str]:
    normalized = normalize_text(text).lower()
    terms: set[str] = set()
    for word in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", normalized):
        if word not in REVISION_QUERY_STOP_TERMS:
            terms.add(word)
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        cleaned = re.sub(r"(基于|关于|以及|并且|本研究|本文|论文|研究|问题|方法|结果|讨论|结论|背景|中的|与|和|对|在|中)", " ", chunk)
        for part in re.findall(r"[\u4e00-\u9fff]{2,12}", cleaned):
            if part not in REVISION_QUERY_STOP_TERMS:
                terms.add(part)
            if len(part) > 4:
                for size in (2, 3, 4):
                    for index in range(0, len(part) - size + 1):
                        gram = part[index : index + size]
                        if gram not in REVISION_QUERY_STOP_TERMS:
                            terms.add(gram)
    return sorted(terms, key=lambda item: (-len(item), item))[:24]


def score_revision_match(terms: list[str], *fields: str) -> tuple[float, list[str]]:
    if not terms:
        return 0.0, []
    haystacks = [compact_text(field).lower() for field in fields if field]
    score = 0.0
    matched: list[str] = []
    for term in terms:
        for index, haystack in enumerate(haystacks):
            if term and term in haystack:
                weight = 1.0 + min(len(term), 8) / 8
                score += weight * (4.0 if index == 0 else 2.0 if index == 1 else 1.0)
                matched.append(term)
                break
    unique = list(dict.fromkeys(matched))
    if unique:
        score += min(18.0, len(unique) * 2.5)
    return score, unique


def summarize_evidence_excerpt(text: str, limit: int = 220) -> str:
    compact = compact_text(text)
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit].rstrip()}..."


def has_citation_markers(text: str) -> bool:
    normalized = normalize_text(text)
    return bool(
        re.search(r"(\[[0-9]{1,3}(?:\s*[-,]\s*[0-9]{1,3})*\]|\([A-Za-z\u4e00-\u9fff][^)]*\d{4}[^)]*\)|doi\s*[:：]?\s*10\.)", normalized, re.I)
    )


def default_section_titles(language: str) -> list[str]:
    if language == "zh":
        return ["研究背景", "研究问题", "方法设计", "结果与讨论", "结论", "附录"]
    return ["Introduction", "Research Question", "Methods", "Discussion", "Conclusion", "Appendix"]


def clean_heading_title(raw_title: str, language: str, index: int) -> str:
    title = re.sub(r"^[#\s]+", "", raw_title).strip()
    title = re.sub(r"^[0-9\u4e00-\u9fff]+[\.\s\u3001\)\uff09]+", "", title).strip()
    if title:
        return title[:80]
    return f"章节 {index + 1}" if language == "zh" else f"Section {index + 1}"


def split_into_sections(text: str, language: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    heading_sections: list[dict[str, Any]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    def flush_heading_section() -> None:
        nonlocal current_title, current_lines
        if current_title is None:
            return
        chunk = normalize_text("\n".join(current_lines))
        if chunk:
            index = len(heading_sections)
            heading_sections.append(
                {
                    "title": clean_heading_title(current_title, language, index),
                    "text": chunk,
                    "path": str(index + 1),
                    "parse_confidence": 0.95,
                }
            )
        current_title = None
        current_lines = []

    for line in normalized.split("\n"):
        heading_match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if heading_match:
            flush_heading_section()
            current_title = heading_match.group(2)
            current_lines = [line]
        elif current_title is not None:
            current_lines.append(line)

    flush_heading_section()
    if heading_sections:
        return heading_sections

    chunks = [normalize_text(chunk) for chunk in re.split(r"\n{2,}", normalized) if normalize_text(chunk)]
    if not chunks and normalized.strip():
        chunks = [normalize_text(normalized)]
    titles = default_section_titles(language)
    return [
        {
            "title": titles[index] if index < len(titles) else (f"章节 {index + 1}" if language == "zh" else f"Section {index + 1}"),
            "text": chunk,
            "path": str(index + 1),
            "parse_confidence": 0.88,
        }
        for index, chunk in enumerate(chunks)
    ]


def extract_text_from_docx(raw_bytes: bytes) -> str:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
        document_xml = archive.read("word/document.xml")
    root = ET.fromstring(document_xml)
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        joined = "".join(parts).strip()
        if joined:
            paragraphs.append(joined)
    return "\n\n".join(paragraphs)


def extract_text_from_pdf(raw_bytes: bytes) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed")
    reader = PdfReader(io.BytesIO(raw_bytes))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(page for page in pages if page)


KNOWN_SECTION_TITLES = {
    "zh": {
        "摘要": "摘要",
        "关键词": "关键词",
        "关键字": "关键词",
        "引言": "引言",
        "绪论": "绪论",
        "研究背景": "研究背景",
        "文献综述": "文献综述",
        "理论基础": "理论基础",
        "研究设计": "研究设计",
        "研究方法": "研究方法",
        "方法": "研究方法",
        "实验设计": "实验设计",
        "结果": "研究结果",
        "结果与讨论": "结果与讨论",
        "讨论": "讨论",
        "结论": "结论",
        "参考文献": "参考文献",
        "致谢": "致谢",
        "附录": "附录",
    },
    "en": {
        "abstract": "Abstract",
        "keywords": "Keywords",
        "index terms": "Keywords",
        "introduction": "Introduction",
        "background": "Research Background",
        "literature review": "Literature Review",
        "methodology": "Methods",
        "methods": "Methods",
        "materials and methods": "Methods",
        "results": "Results",
        "results and discussion": "Results and Discussion",
        "discussion": "Discussion",
        "conclusion": "Conclusion",
        "conclusions": "Conclusion",
        "references": "References",
        "acknowledgment": "Acknowledgments",
        "acknowledgments": "Acknowledgments",
        "appendix": "Appendix",
    },
}

GENERIC_SECTION_TITLE_RE = re.compile(r"^(?:章节|section)\s+\d+$", re.IGNORECASE)


def normalize_heading_key(value: str) -> str:
    cleaned = compact_text(value).strip(" .:-_·•\t")
    cleaned = cleaned.replace("：", ":")
    return cleaned.lower()


def strip_heading_prefix(value: str) -> tuple[str, int]:
    title = re.sub(r"^[#\s]+", "", value).strip()
    level = 1
    patterns = [
        (r"^第[0-9一二三四五六七八九十百千]+[章节部分篇]\s*", 1),
        (r"^[（(]?[一二三四五六七八九十百千IVXivx0-9]+[)）]\s*", 1),
        (r"^[一二三四五六七八九十百千]+[、.]\s*", 1),
        (r"^(\d+(?:\.\d+){0,3})\s*[-.)、]?\s*", None),
        (r"^([IVXLC]+)\.\s*", 1),
    ]
    for pattern, fixed_level in patterns:
        match = re.match(pattern, title)
        if not match:
            continue
        if fixed_level is None:
            level = match.group(1).count(".") + 1
        else:
            level = fixed_level
        title = title[match.end() :].strip()
        break
    return title, level
DOI_EXTRACT_PATTERN = re.compile(r"(?:doi\s*[:ï¼š]?\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.I)
NUMERIC_CITATION_PATTERN = re.compile(r"\[[0-9]{1,3}(?:\s*[-,]\s*[0-9]{1,3})*\]")
AUTHOR_YEAR_PATTERNS = [
    re.compile(r"([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff\s·\.-]{0,32}?)(?:\s+et\s+al\.?|等)?\s*[（(]\s*(20\d{2}[a-z]?)\s*[)）]", re.I),
    re.compile(r"[（(]\s*([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff\s·\.-]{0,32}?)(?:\s+et\s+al\.?|等)?\s*[,，]\s*(20\d{2}[a-z]?)\s*[)）]", re.I),
]


def normalize_citation_doi(value: str | None) -> str:
    if not value:
        return ""
    cleaned = compact_text(value).lower()
    cleaned = re.sub(r"^https?://(dx\.)?doi\.org/", "", cleaned)
    cleaned = re.sub(r"[)\]）】,.;，。；]+$", "", cleaned)
    return cleaned


def normalize_author_token(value: str | None) -> str:
    if not value:
        return ""
    cleaned = compact_text(value).lower()
    cleaned = re.sub(r"\bet\s+al\.?\b", "", cleaned)
    cleaned = cleaned.replace("等", "")
    cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", cleaned)
    return cleaned


def author_keys_for_name(name: str) -> list[str]:
    compact = compact_text(name)
    keys: set[str] = set()
    normalized = normalize_author_token(compact)
    if normalized:
        keys.add(normalized)
    if re.search(r"[\u4e00-\u9fff]", compact):
        merged = re.sub(r"\s+", "", compact)
        if merged:
            keys.add(normalize_author_token(merged))
            keys.add(normalize_author_token(merged[:1]))
            if len(merged) >= 2:
                keys.add(normalize_author_token(merged[:2]))
    else:
        parts = [part for part in re.split(r"\s+", compact) if part]
        if parts:
            keys.add(normalize_author_token(parts[0]))
            keys.add(normalize_author_token(parts[-1]))
    return [item for item in sorted(keys) if item]


def extract_doi_mentions(text: str) -> list[dict[str, str]]:
    mentions: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in DOI_EXTRACT_PATTERN.finditer(text):
        doi = normalize_citation_doi(match.group(1))
        if not doi or doi in seen:
            continue
        seen.add(doi)
        mentions.append({"text": match.group(0), "doi": doi})
    return mentions


def extract_author_year_mentions(text: str) -> list[dict[str, str]]:
    mentions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern in AUTHOR_YEAR_PATTERNS:
        for match in pattern.finditer(text):
            author = compact_text(match.group(1))
            year = match.group(2)
            author_key = normalize_author_token(author)
            if not author_key:
                continue
            key = (author_key, year)
            if key in seen:
                continue
            seen.add(key)
            mentions.append(
                {
                    "text": match.group(0),
                    "author": author,
                    "authorKey": author_key,
                    "year": year,
                }
            )
    return mentions


REFERENCE_SECTION_KEYS = {"参考文献", "references", "bibliography"}
REFERENCE_ENTRY_START_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)、])\s*(.+)$")


def normalize_title_token(value: str | None) -> str:
    if not value:
        return ""
    lowered = compact_text(value).lower()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", lowered)


def looks_like_reference_section(title: str) -> bool:
    stripped, _ = strip_heading_prefix(title)
    return normalize_heading_key(stripped) in REFERENCE_SECTION_KEYS


def extract_reference_block(text: str) -> str:
    lines = normalize_text(text).split("\n")
    for index, line in enumerate(lines):
        stripped, _ = strip_heading_prefix(line)
        if normalize_heading_key(stripped) in REFERENCE_SECTION_KEYS:
            return "\n".join(lines[index + 1 :]).strip()
    return ""


def parse_numeric_citation_values(text: str) -> list[int]:
    values: list[int] = []
    for part in re.split(r"\s*,\s*", text):
        if "-" in part:
            left, right = part.split("-", 1)
            if left.isdigit() and right.isdigit():
                start = int(left)
                end = int(right)
                if start <= end:
                    values.extend(range(start, end + 1))
                continue
        if part.strip().isdigit():
            values.append(int(part.strip()))
    return list(dict.fromkeys(values))


def parse_reference_entries(text: str) -> list[dict[str, Any]]:
    block = extract_reference_block(text) or normalize_text(text)
    if not block:
        return []
    entries: list[str] = []
    current = ""
    for raw_line in block.split("\n"):
        line = compact_text(raw_line)
        if not line:
            if current:
                entries.append(current.strip())
                current = ""
            continue
        matched = REFERENCE_ENTRY_START_RE.match(line)
        if matched:
            if current:
                entries.append(current.strip())
            current = line
        elif current:
            current = f"{current} {line}".strip()
        else:
            current = line
    if current:
        entries.append(current.strip())

    parsed: list[dict[str, Any]] = []
    for position, entry in enumerate(entries, start=1):
        matched = REFERENCE_ENTRY_START_RE.match(entry)
        if matched:
            index = int(matched.group(1) or matched.group(2))
            body = matched.group(3).strip()
        else:
            index = position
            body = entry
        doi_mentions = extract_doi_mentions(body)
        year_match = re.search(r"(19|20)\d{2}[a-z]?", body)
        lead = re.split(r"(?:[，。,.;；]|\(\d{4}|\（\d{4})", body, maxsplit=1)[0]
        author = compact_text(lead[:48])
        parsed.append(
            {
                "index": index,
                "rawText": body,
                "doi": doi_mentions[0]["doi"] if doi_mentions else "",
                "year": year_match.group(0) if year_match else "",
                "author": author,
                "authorKey": normalize_author_token(author),
                "titleToken": normalize_title_token(body[:120]),
            }
        )
    return parsed


def format_author_names(authors: list[str], style: str) -> str:
    clean_authors = [compact_text(str(author)) for author in authors if compact_text(str(author))]
    if not clean_authors:
        return "Unknown author"
    if style == "apa":
        return ", ".join(clean_authors[:6]) + (" et al." if len(clean_authors) > 6 else "")
    if style == "ieee":
        return ", ".join(clean_authors[:6]) + (" et al." if len(clean_authors) > 6 else "")
    return ", ".join(clean_authors[:3]) + (" 等" if len(clean_authors) > 3 else "")


def format_literature_reference(item: dict[str, Any], style: str, index: int) -> str:
    title = compact_text(item.get("title") or "Untitled")
    authors = format_author_names(item.get("authors") or [], style)
    year = item.get("year") or "n.d."
    venue = compact_text(item.get("venue") or item.get("journalTitle") or "")
    doi = normalize_citation_doi(item.get("doi") or "")
    doi_suffix = f" DOI:{doi}" if doi else ""
    if style == "apa":
        venue_part = f" {venue}." if venue else ""
        doi_part = f" https://doi.org/{doi}" if doi else ""
        return f"{authors}. ({year}). {title}.{venue_part}{doi_part}".strip()
    if style == "ieee":
        venue_part = f", {venue}" if venue else ""
        doi_part = f", doi: {doi}" if doi else ""
        return f"[{index}] {authors}, \"{title}\"{venue_part}, {year}{doi_part}."
    return f"[{index}] {authors}. {title}[J]. {venue}, {year}.{doi_suffix}".strip()


def default_section_titles(language: str) -> list[str]:
    if language == "zh":
        return ["摘要", "研究背景", "研究方法", "结果讨论", "结论", "附录"]
    return ["Introduction", "Research Question", "Methods", "Discussion", "Conclusion", "Appendix"]


def looks_like_noise_line(line: str) -> bool:
    compact = compact_text(line)
    if not compact:
        return True
    if re.fullmatch(r"\d{1,3}", compact):
        return True
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", compact):
        return True
    if re.fullmatch(r"(?:[A-Za-z]\s+){2,}[A-Za-z]", compact):
        return True
    if compact.lower() in {"toa", "boa", "wvc", "lst", "lse"}:
        return True
    if re.fullmatch(r"page\s+\d+\s+of\s+\d+", compact, flags=re.IGNORECASE):
        return True
    return False


def clean_heading_title(raw_title: str, language: str, index: int) -> str:
    stripped, _ = strip_heading_prefix(raw_title)
    title = compact_text(stripped)
    key = normalize_heading_key(title)
    canonical = KNOWN_SECTION_TITLES[language].get(key)
    if canonical:
        return canonical
    if title:
        return title[:80]
    defaults = default_section_titles(language)
    return defaults[index] if index < len(defaults) else (f"未命名片段 {index + 1}" if language == "zh" else f"Untitled section {index + 1}")


def detect_heading(line: str, language: str) -> tuple[str, int, float] | None:
    stripped = compact_text(line)
    if not stripped or looks_like_noise_line(stripped):
        return None
    markdown_match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
    if markdown_match:
        level = len(markdown_match.group(1))
        title = clean_heading_title(markdown_match.group(2), language, 0)
        return title, level, 0.98

    core, level = strip_heading_prefix(stripped)
    key = normalize_heading_key(core)
    canonical = KNOWN_SECTION_TITLES[language].get(key)
    if canonical:
        confidence = 0.97 if key == normalize_heading_key(stripped) else 0.94
        return canonical, level, confidence

    word_count = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", core))
    if not core or len(core) > 80:
        return None
    if re.search(r"[。！？.!?]$", core) and word_count > 6:
        return None
    if language == "en":
        if re.fullmatch(r"[A-Z][A-Z0-9\s,&/-]{2,80}", core) and word_count <= 8:
            return core.title(), level, 0.88
        if level > 1 and word_count <= 12:
            return core, level, 0.84
    else:
        if word_count <= 16 and level > 1:
            return core, level, 0.85
    return None


def derive_fallback_title(chunk: str, language: str, index: int) -> str:
    for line in chunk.splitlines():
        stripped = compact_text(line)
        if not stripped or looks_like_noise_line(stripped):
            continue
        sentence = re.split(r"[。！？.!?；;:\n]", stripped, maxsplit=1)[0].strip(" :：-")
        if sentence:
            if language == "zh":
                return sentence[:18]
            words = sentence.split()
            return " ".join(words[:8])[:72]
    defaults = default_section_titles(language)
    return defaults[index] if index < len(defaults) else (f"未命名片段 {index + 1}" if language == "zh" else f"Untitled section {index + 1}")


def split_into_sections(text: str, language: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parsed_sections: list[dict[str, Any]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    current_level = 1
    current_confidence = 0.9
    saw_heading = False

    def flush_current() -> None:
        nonlocal current_title, current_lines, current_level, current_confidence
        chunk = normalize_text("\n".join(current_lines))
        if chunk:
            index = len(parsed_sections)
            parsed_sections.append(
                {
                    "title": current_title or derive_fallback_title(chunk, language, index),
                    "text": chunk,
                    "path": str(index + 1),
                    "level": current_level,
                    "parse_confidence": current_confidence if current_title else 0.82,
                }
            )
        current_title = None
        current_lines = []
        current_level = 1
        current_confidence = 0.9

    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            if current_lines and current_lines[-1] != "":
                current_lines.append("")
            continue
        if looks_like_noise_line(line):
            continue
        heading = detect_heading(line, language)
        if heading is not None:
            saw_heading = True
            flush_current()
            current_title, current_level, current_confidence = heading
            continue
        current_lines.append(line)

    flush_current()
    if saw_heading and parsed_sections:
        return parsed_sections

    chunks = [normalize_text(chunk) for chunk in re.split(r"\n{2,}", normalized) if normalize_text(chunk)]
    if not chunks and normalized.strip():
        chunks = [normalize_text(normalized)]
    return [
        {
            "title": derive_fallback_title(chunk, language, index),
            "text": chunk,
            "path": str(index + 1),
            "level": 1,
            "parse_confidence": 0.8,
        }
        for index, chunk in enumerate(chunks)
    ]


def extract_text_from_pdf(raw_bytes: bytes) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed")

    replacements = {
        "¦Ě": "μ",
        "Âμ": "μ",
        "Î¼": "μ",
        "âˆ’": "-",
        "–": "-",
        "—": "-",
        "芒聙聰": "-",
        "盲聙聰": "-",
        "＿": "_",
    }

    def repair_fragment(text: str) -> str:
        repaired = text
        for source, target in replacements.items():
            repaired = repaired.replace(source, target)
        repaired = re.sub(r"\s+([,.;:)\]])", r"\1", repaired)
        repaired = re.sub(r"([(\[])\s+", r"\1", repaired)
        repaired = re.sub(r"\s*-\s*", "-", repaired)
        repaired = re.sub(r"\s{2,}", " ", repaired)
        repaired = repaired.replace("Index Te", "Index Terms")
        return repaired.strip()

    def should_join(previous: str, current: str) -> bool:
        if not previous:
            return False
        if re.search(r"[。！？.!?:;]$", previous):
            return False
        if detect_heading(current, "en") or detect_heading(current, "zh"):
            return False
        if re.match(r"^(Fig\.|Table|TABLE|Figure|\[[0-9]+\])", current):
            return False
        if previous.endswith("-"):
            return True
        if re.match(r"^[a-z0-9(\[]", current):
            return True
        if re.match(r"^[A-Z][a-z]+", current) and len(previous) < 48:
            return True
        return False

    def collapse_lines(lines: list[str]) -> str:
        paragraphs: list[str] = []
        current = ""
        for raw_line in lines:
            line = repair_fragment(raw_line.strip())
            if not line:
                if current:
                    paragraphs.append(current.strip())
                    current = ""
                continue
            if not current:
                current = line
                continue
            if should_join(current, line):
                separator = "" if current.endswith("-") else " "
                current = f"{current.rstrip('-')}{separator}{line}"
            else:
                paragraphs.append(current.strip())
                current = line
        if current:
            paragraphs.append(current.strip())
        return "\n\n".join(paragraphs)

    reader = PdfReader(io.BytesIO(raw_bytes))
    pages: list[str] = []
    for page in reader.pages:
        raw_page = (page.extract_text() or "").strip()
        if not raw_page:
            continue
        lines = [line.rstrip() for line in raw_page.splitlines()]
        standalone_numbers = sum(1 for line in lines if re.fullmatch(r"\s*\d{1,3}\s*", line))
        cleaned_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                cleaned_lines.append("")
                continue
            if standalone_numbers >= 8 and re.fullmatch(r"\d{1,3}", stripped):
                continue
            if re.fullmatch(r"page\s+\d+\s+of\s+\d+", stripped, flags=re.IGNORECASE):
                continue
            cleaned_lines.append(stripped)
        cleaned_page = normalize_text("\n".join(cleaned_lines))
        if cleaned_page:
            pages.append(cleaned_page)
    return "\n\n".join(pages)


def lookup_heading_title(raw_title: str, preferred_language: str, index: int = 0) -> str:
    stripped, _ = strip_heading_prefix(raw_title)
    title = compact_text(stripped)
    key = normalize_heading_key(title)
    language_order = [preferred_language, "en" if preferred_language == "zh" else "zh"]
    for language in language_order:
        canonical = KNOWN_SECTION_TITLES[language].get(key)
        if canonical:
            return canonical
    if title:
        return title[:80]
    defaults = default_section_titles(preferred_language)
    return defaults[index] if index < len(defaults) else (f"未命名片段 {index + 1}" if preferred_language == "zh" else f"Untitled section {index + 1}")


def detect_heading(line: str, language: str) -> tuple[str, int, float] | None:
    stripped = compact_text(line)
    if not stripped or looks_like_noise_line(stripped):
        return None
    markdown_match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
    if markdown_match:
        level = len(markdown_match.group(1))
        title = lookup_heading_title(markdown_match.group(2), language, 0)
        return title, level, 0.98

    core, level = strip_heading_prefix(stripped)
    title = lookup_heading_title(core, language, 0)
    key = normalize_heading_key(core)
    if key.startswith(("table ", "fig ", "fig. ", "figure ")):
        return None
    if re.fullmatch(r"(?:toa|boa|lst|lse|wvc)(?:\s+(?:toa|boa|lst|lse|wvc))*", key):
        return None
    if re.fullmatch(r"l[89]\s*b1[01]", key):
        return None
    if any(key in KNOWN_SECTION_TITLES[lang] for lang in ("zh", "en")):
        confidence = 0.97 if key == normalize_heading_key(stripped) else 0.94
        return title, level, confidence

    numeric_prefix = bool(re.match(r"^\d+(?:\.\d+){0,3}\s*[-.)、]?\s*", stripped))
    word_count = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", core))
    if not core or len(core) > 80:
        return None
    if re.search(r"[。！？.!?]$", core) and word_count > 6:
        return None
    if "," in core or ";" in core:
        return None
    if numeric_prefix and (len(core) > 36 or word_count > 8):
        return None
    heading_tokens = [token for token in re.split(r"\s+", core) if token]
    if heading_tokens and all(len(token) <= 2 for token in heading_tokens):
        return None
    alpha_tokens = [token for token in heading_tokens if re.search(r"[A-Za-z0-9\u4e00-\u9fff]", token)]
    if alpha_tokens and len(alpha_tokens) <= 2 and max(len(token) for token in alpha_tokens) <= 3:
        return None
    if len(heading_tokens) == 1 and word_count <= 2:
        return None
    if re.fullmatch(r"[A-Z][A-Z0-9\s,&/-]{2,80}", core) and word_count <= 8:
        return core.title(), level, 0.88
    heading_case_score = sum(1 for token in heading_tokens if token[:1].isupper() or token.isupper())
    if level > 1 and word_count <= 14 and heading_tokens and heading_case_score / len(heading_tokens) >= 0.45:
        return core, level, 0.84
    return None


def split_into_sections(text: str, language: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\b(Abstract|ABSTRACT)\s*[-—–:：芒聙聰]+\s*", "\nAbstract\n", normalized)
    normalized = re.sub(r"\b(Index Terms|INDEX TERMS|Keywords|KEYWORDS)\s*[-—–:：芒聙聰]+\s*", "\nIndex Terms\n", normalized)
    parsed_sections: list[dict[str, Any]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    current_level = 1
    current_confidence = 0.9
    saw_heading = False

    def flush_current() -> None:
        nonlocal current_title, current_lines, current_level, current_confidence
        chunk = normalize_text("\n".join(current_lines))
        if chunk:
            index = len(parsed_sections)
            parsed_sections.append(
                {
                    "title": current_title or derive_fallback_title(chunk, language, index),
                    "text": chunk,
                    "path": str(index + 1),
                    "level": current_level,
                    "parse_confidence": current_confidence if current_title else 0.82,
                }
            )
        current_title = None
        current_lines = []
        current_level = 1
        current_confidence = 0.9

    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            if current_lines and current_lines[-1] != "":
                current_lines.append("")
            continue
        if looks_like_noise_line(line):
            continue
        heading = detect_heading(line, language)
        if heading is not None:
            if saw_heading:
                flush_current()
            else:
                current_lines = []
                saw_heading = True
            current_title, current_level, current_confidence = heading
            continue
        current_lines.append(line)

    flush_current()
    if saw_heading and parsed_sections:
        return parsed_sections

    chunks = [normalize_text(chunk) for chunk in re.split(r"\n{2,}", normalized) if normalize_text(chunk)]
    if not chunks and normalized.strip():
        chunks = [normalize_text(normalized)]
    return [
        {
            "title": derive_fallback_title(chunk, language, index),
            "text": chunk,
            "path": str(index + 1),
            "level": 1,
            "parse_confidence": 0.8,
        }
        for index, chunk in enumerate(chunks)
    ]


def extract_text_from_pdf(raw_bytes: bytes) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed")
    replacements = {
        "Â¦Äš": "μ",
        "Ã‚Î¼": "μ",
        "ÃŽÂ¼": "μ",
        "Ã¢Ë†â€™": "-",
        "â€“": "-",
        "â€”": "-",
        "èŠ’è™è°": "-",
        "ç›²è™è°": "-",
        "ï¼¿": "_",
        "ﬁ": "fi",
        "ﬂ": "fl",
        "□": " ",
        "■": " ",
        "�": " ",
    }

    equation_symbol_re = re.compile(r"[=+\-*/^_<>≤≥≈∑πμβσλΔ×÷]")

    def repair_fragment(text: str) -> str:
        repaired = text.replace("\u00a0", " ")
        for source, target in replacements.items():
            repaired = repaired.replace(source, target)
        repaired = re.sub(r"[\uf000-\uf0ff]", " ", repaired)
        repaired = re.sub(r"\s+([,.;:)\]])", r"\1", repaired)
        repaired = re.sub(r"([(\[])\s+", r"\1", repaired)
        repaired = re.sub(r"\s*([=+\-*/^_<>])\s*", r" \1 ", repaired)
        repaired = re.sub(r"\s{2,}", " ", repaired)
        repaired = repaired.replace("Index Te", "Index Terms")
        repaired = repaired.replace("Landsat_8/9", "Landsat 8/9")
        repaired = repaired.replace("Landsat_8", "Landsat 8")
        repaired = repaired.replace("Landsat_9", "Landsat 9")
        repaired = re.sub(r"([A-Za-z])\s+[□■]\s*", r"\1 ", repaired)
        return repaired.strip()

    def is_equation_like(line: str) -> bool:
        compact = compact_text(line)
        if not compact:
            return False
        if compact.lower() in {"where", "wherein"}:
            return True
        if re.search(r"\(\d+\)", compact):
            return True
        if equation_symbol_re.search(compact) and len(compact) <= 220:
            return True
        if len(re.findall(r"[(){}\[\]]", compact)) >= 4 and len(compact) <= 220:
            return True
        if re.fullmatch(r"[A-Za-z]\s*(?:[,;:]?\s*[A-Za-z0-9]+){1,8}", compact):
            return True
        return False

    def should_skip_line(line: str, standalone_numbers: int) -> bool:
        lowered = line.lower()
        if standalone_numbers >= 8 and re.fullmatch(r"\d{1,3}", line):
            return True
        if re.fullmatch(r"page\s+\d+\s+of\s+\d+", line, flags=re.IGNORECASE):
            return True
        if "replace this line with your manuscript id number" in lowered:
            return True
        if lowered.startswith(("journal:", "manuscript id", "manuscript type:", "date submitted by the", "complete list of authors:")):
            return True
        if line in {
            "Transactions on Geoscience and Remote Sensing",
            "IEEE Transactions on Geoscience and Remote Sensing",
        }:
            return True
        return False

    def should_join(previous: str, current: str) -> bool:
        if not previous or not current:
            return False
        if is_equation_like(previous) or is_equation_like(current):
            return False
        if detect_heading(previous, "en") or detect_heading(previous, "zh"):
            return False
        if detect_heading(current, "en") or detect_heading(current, "zh"):
            return False
        if re.match(r"^(Fig\.|Figure|Table|Appendix|\[[0-9]+\])", current, flags=re.IGNORECASE):
            return False
        if previous.endswith("-"):
            return True
        if re.search(r"[。！？.!?:;]$", previous):
            return False
        if re.match(r"^[a-z0-9(\[]", current):
            return True
        if len(previous) < 84 and not re.search(r"[。！？.!?]$", previous):
            return True
        return False

    def collapse_lines(lines: list[str]) -> str:
        blocks: list[str] = []
        current_lines: list[str] = []
        current_kind = "text"

        def flush() -> None:
            nonlocal current_lines, current_kind
            if not current_lines:
                return
            if current_kind == "equation":
                blocks.append("\n".join(current_lines).strip())
            else:
                blocks.append("".join(current_lines).strip())
            current_lines = []
            current_kind = "text"

        for raw_line in lines:
            line = repair_fragment(raw_line.strip())
            if not line:
                flush()
                continue
            if looks_like_noise_line(line) and not is_equation_like(line):
                continue

            line_kind = "equation" if is_equation_like(line) else "text"
            if not current_lines:
                current_lines = [line]
                current_kind = line_kind
                continue
            if line_kind != current_kind:
                flush()
                current_lines = [line]
                current_kind = line_kind
                continue
            if line_kind == "equation":
                current_lines.append(line)
                continue
            previous = current_lines[-1]
            if should_join(previous, line):
                separator = "" if previous.endswith("-") else " "
                current_lines[-1] = f"{previous.rstrip('-')}{separator}{line}"
            else:
                flush()
                current_lines = [line]
                current_kind = line_kind

        flush()
        return "\n\n".join(block for block in blocks if block.strip())

    def trim_front_matter(text: str) -> str:
        candidates: list[int] = []
        for pattern in (r"\bAbstract\b", r"\bABSTRACT\b", r"\bI\.\s+INTRODUCTION\b", r"\bIntroduction\b"):
            match = re.search(pattern, text)
            if match:
                candidates.append(match.start())
        if candidates:
            first_index = min(candidates)
            if first_index > 120:
                return text[first_index:].lstrip()
        return text

    if fitz is not None:
        try:
            document = fitz.open(stream=raw_bytes, filetype="pdf")
            try:
                pages: list[str] = []
                for page in document:
                    raw_page = (page.get_text("text") or "").strip()
                    if not raw_page:
                        continue
                    lines = [line.rstrip() for line in raw_page.splitlines()]
                    standalone_numbers = sum(1 for line in lines if re.fullmatch(r"\s*\d{1,3}\s*", line))
                    cleaned_lines: list[str] = []
                    for raw_line in lines:
                        stripped = repair_fragment(raw_line.strip())
                        if not stripped:
                            cleaned_lines.append("")
                            continue
                        if should_skip_line(stripped, standalone_numbers):
                            continue
                        cleaned_lines.append(stripped)
                    cleaned_page = normalize_text(collapse_lines(cleaned_lines))
                    if cleaned_page:
                        pages.append(cleaned_page)
                extracted = normalize_text(trim_front_matter("\n\n".join(pages)))
                if extracted:
                    return extracted
            finally:
                document.close()
        except Exception:
            pass

    reader = PdfReader(io.BytesIO(raw_bytes))
    pages: list[str] = []
    for page in reader.pages:
        raw_page = (page.extract_text() or "").strip()
        if not raw_page:
            continue
        lines = [line.rstrip() for line in raw_page.splitlines()]
        standalone_numbers = sum(1 for line in lines if re.fullmatch(r"\s*\d{1,3}\s*", line))
        cleaned_lines: list[str] = []
        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                cleaned_lines.append("")
                continue
            if should_skip_line(stripped, standalone_numbers):
                continue
            cleaned_lines.append(stripped)
        cleaned_page = normalize_text(collapse_lines(cleaned_lines))
        if cleaned_page:
            pages.append(cleaned_page)
    return normalize_text(trim_front_matter("\n\n".join(pages)))


def academic_rewrite(text: str) -> str:
    if is_chinese(text):
        rewritten = (
            text.replace("比较", "相对")
            .replace("很多", "多项")
            .replace("比较重要", "具有关键意义")
            .replace("这个问题", "这一研究问题")
            .replace("我们发现", "研究结果表明")
        )
        if normalize_text(rewritten) == normalize_text(text):
            rewritten = re.sub(r"本文", "本研究", rewritten, count=1)
            rewritten = re.sub(r"说明", "表明", rewritten, count=1)
            rewritten = re.sub(r"需要", "有必要", rewritten, count=1)
        return rewritten
    rewritten = (
        text.replace("very important", "methodologically significant")
        .replace("a lot of", "a substantial number of")
        .replace("we found", "the analysis indicates")
        .replace("kind of", "partially")
        .replace("This draft", "This manuscript")
        .replace("this draft", "this manuscript")
        .replace("This paper", "This study")
        .replace("this paper", "this study")
        .replace("The current", "The present")
        .replace("the current", "the present")
        .replace("kind of broad", "insufficiently focused")
        .replace("too broad", "overly broad")
        .replace("loose", "insufficiently specified")
        .replace("needs to", "should")
    )
    rewritten = re.sub(r"\bbut\b", "however", rewritten, count=1, flags=re.IGNORECASE)
    if normalize_text(rewritten) == normalize_text(text):
        sentences = [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]
        if sentences:
            first = sentences[0]
            if not first.lower().startswith(("this study", "this manuscript", "the present")):
                sentences[0] = f"The present section argues that {first[0].lower()}{first[1:]}"
                rewritten = " ".join(sentences)
    return rewritten


def shorten_text(text: str) -> str:
    chunks = [chunk for chunk in re.split(r"(?<=[。！？.!?])\s*", text) if chunk]
    if len(chunks) <= 2:
        return text
    return "".join(chunk for index, chunk in enumerate(chunks) if index == 0 or index == len(chunks) - 1 or index % 2 == 0)


def expand_text(text: str) -> str:
    if is_chinese(text):
        return f"{text}\n\n进一步来看，这一结论不仅回应了研究问题，也为后续章节中的方法选择与结果解释提供了更稳固的逻辑支点。"
    return (
        f"{text}\n\nThis also clarifies the bridge between the empirical observation and the broader scholarly implication, "
        "making the argument easier to defend during review."
    )


def unify_terms(text: str) -> str:
    if is_chinese(text):
        return text.replace("模型", "参考模型").replace("实验结果", "实证结果")
    return text.replace("model", "reference model").replace("results", "empirical results")


def polish_transitions(text: str) -> str:
    paragraphs = [paragraph for paragraph in re.split(r"\n{2,}", text) if paragraph.strip()]
    if len(paragraphs) < 2:
        return text
    bridge = "基于以上分析，下一部分进一步聚焦于" if is_chinese(text) else "Building on this point, the next section focuses on "
    merged: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        if index == 0:
            merged.append(paragraph)
        else:
            merged.append(f"{bridge}{paragraph[:1].lower()}{paragraph[1:]}")
    return "\n\n".join(merged)


def revise_by_comment(text: str, comment_context: str) -> str:
    rewritten = academic_rewrite(text)
    if is_chinese(text):
        tail = "本次修改优先收紧论证路径，并尽量把模糊描述替换为更容易经受导师追问的表达。"
        if comment_context:
            tail = f"本次修改重点响应以下意见：{comment_context}\n\n{tail}"
        return f"{rewritten}\n\n{tail}"
    tail = "This pass tightens the argument so the section responds more directly to reviewer expectations."
    if comment_context:
        tail = f"This pass explicitly responds to: {comment_context}\n\n{tail}"
    return f"{rewritten}\n\n{tail}"


def rewrite_summary(action_type: str, language: str) -> str:
    zh = {
        "academic-rewrite": "提升学术语气，收紧口语化表达。",
        "shorten": "压缩重复句式，保留段落主干。",
        "expand": "补强论点承接，让章节更像可审阅草稿。",
        "unify-terms": "统一高频术语，减少表达漂移。",
        "comment-revision": "按导师意见重写当前章节关键句。",
        "transition-polish": "补强段间过渡，减少跳跃感。",
        "translate-en-zh": "将英文段落准确转成中文表达。",
        "translate-zh-en": "将中文段落准确转成英文表达。",
        "reduce-aigc": "减少模板化措辞，让语气更自然。",
    }
    en = {
        "academic-rewrite": "Tightened the academic tone and reduced informal phrasing.",
        "shorten": "Condensed repetitive phrasing while preserving the core point.",
        "expand": "Expanded the argumentative bridge so the section reads like a reviewable draft.",
        "unify-terms": "Normalized key terminology across the section.",
        "comment-revision": "Revised the section to address reviewer feedback directly.",
        "transition-polish": "Inserted a transition so the section flows more clearly.",
        "translate-en-zh": "Translated the English passage into natural Chinese.",
        "translate-zh-en": "Translated the Chinese passage into natural English.",
        "reduce-aigc": "Reduced formulaic wording and made the passage read more naturally.",
    }
    table = zh if language == "zh" else en
    return table.get(action_type, table["academic-rewrite"])


def heuristic_rewrite(action_type: str, text: str, comment_context: str, language: str) -> dict[str, Any]:
    strategies = {
        "academic-rewrite": academic_rewrite,
        "shorten": shorten_text,
        "expand": expand_text,
        "unify-terms": unify_terms,
        "comment-revision": lambda current: revise_by_comment(current, comment_context),
        "transition-polish": polish_transitions,
        "translate-en-zh": lambda current: current,
        "translate-zh-en": lambda current: current,
        "reduce-aigc": academic_rewrite,
    }
    rewritten = strategies.get(action_type, academic_rewrite)(text)
    warnings: list[str] = []
    if normalize_text(rewritten) == normalize_text(text) and action_type in {"academic-rewrite", "unify-terms", "transition-polish"}:
        warnings.append("模型调用不可用时，本地规则未识别到明显可替换表达；候选稿可能与原文接近。")
    if len(text.strip()) < 80:
        warnings.append("当前章节较短，建议人工确认 AI 是否改得过度。")
    if action_type == "comment-revision" and not comment_context:
        warnings.append("未提供导师意见上下文，本次只做了定向学术化改写。")
    return {"text": rewritten, "summary": rewrite_summary(action_type, language), "warnings": warnings}


def heuristic_comment_map(raw_comment: str, sections: list[dict[str, Any]]) -> dict[str, Any]:
    lowered = raw_comment.lower()
    match = next((section for section in sections if section["title"].lower() in lowered), None)
    if match is None:
        keyword_map = (
            (("结论", "conclusion"), ("结论", "总结", "conclusion", "answer")),
            (("结果与讨论", "discussion"), ("讨论", "discussion", "why", "interpret", "implication")),
            (("方法设计", "methods"), ("方法", "method", "procedure", "detail", "taxonomy")),
            (("研究问题", "research question"), ("研究问题", "question", "claim")),
        )
        for titles, keywords in keyword_map:
            if any(keyword in lowered or keyword in raw_comment for keyword in keywords):
                match = next((section for section in sections if section["title"].lower() in titles), None)
                if match:
                    break
    if match is None and sections:
        match = sections[0]
    confidence = 0.92 if match and match["title"].lower() in lowered else 0.74 if match else 0.5
    if is_chinese(raw_comment):
        suggested_action = f"围绕“{match['title']}”生成一版可执行修改，并保留前后对比。" if match else "先把这条意见映射到最相关章节，再生成修改建议。"
    else:
        suggested_action = f"Turn this into an actionable edit plan for \"{match['title']}\" and keep the before/after trace." if match else "Map this comment to the most relevant section before generating the edit plan."
    return {"mapped_section_id": match["id"] if match else None, "confidence": confidence, "suggested_action": suggested_action}


def heuristic_diagnostics(project_language: str, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for section in sections:
        text = section["currentText"]
        lowered = text.lower()
        if len(text) < 140:
            issues.append({
                "section_id": section["id"],
                "issue_type": "structure",
                "severity": "high",
                "title": "主张还不够饱满" if project_language == "zh" else "The section is still too thin",
                "detail": "当前段落过短，读者难以看出这一节真正承担什么论证任务。" if project_language == "zh" else "The passage is short enough that its argumentative role is still unclear.",
                "suggested_action": "补出主张句和收束句，再决定哪些背景信息可以后移。" if project_language == "zh" else "Add a clearer claim sentence and closing sentence before expanding supporting detail.",
            })
        if any(marker in text for marker in ("比较", "很多", "这个问题")) or any(marker in lowered for marker in ("very ", "a lot of", "kind of")):
            issues.append({
                "section_id": section["id"],
                "issue_type": "academic-tone",
                "severity": "medium",
                "title": "表达还不够学术化" if project_language == "zh" else "The tone is still too informal",
                "detail": "当前章节含有较口语化的句式，容易削弱答辩或审稿场景下的可信度。" if project_language == "zh" else "The section still contains informal phrasing that weakens credibility during review.",
                "suggested_action": "用更精确的学术句式替换口语表达，并保留原意。" if project_language == "zh" else "Replace informal phrasing with more precise academic wording while preserving meaning.",
            })
        if any(keyword in section["title"].lower() for keyword in ("method", "方法")) and not any(marker in lowered for marker in ("validate", "verification", "robust", "验证", "稳健", "可靠")):
            issues.append({
                "section_id": section["id"],
                "issue_type": "evidence",
                "severity": "medium",
                "title": "验证步骤不够清楚" if project_language == "zh" else "Validation logic is underspecified",
                "detail": "方法描述了做法，但没有足够解释为什么结果值得信任。" if project_language == "zh" else "The section explains what was done, but not why the resulting evidence should be trusted.",
                "suggested_action": "补出稳健性验证或可信度判断的关键步骤。" if project_language == "zh" else "Add the validation or trustworthiness step that makes the evidence convincing.",
            })
        if any(keyword in section["title"].lower() for keyword in ("discussion", "讨论")) and not any(marker in lowered for marker in ("why", "because", "reason", "机制", "原因", "意义")):
            issues.append({
                "section_id": section["id"],
                "issue_type": "logic",
                "severity": "high",
                "title": "讨论层还不够完整" if project_language == "zh" else "The interpretive layer is still weak",
                "detail": "结果已经呈现，但“为什么成立”与“意味着什么”仍不够充分。" if project_language == "zh" else "The findings are present, but the explanation of why they matter remains too thin.",
                "suggested_action": "补出因果解释，再落到研究或实践意义。" if project_language == "zh" else "Add the causal explanation before moving into scholarly or practical implications.",
            })
    if not issues and sections:
        first = sections[0]
        issues.append({
            "section_id": first["id"],
            "issue_type": "structure",
            "severity": "medium",
            "title": "建议先收紧主问题" if project_language == "zh" else "Start by tightening the core claim",
            "detail": "当前草稿已经可读，但最先值得动的是主问题和章节任务的对应关系。" if project_language == "zh" else "The draft is readable, but the best first move is still to tighten the core claim.",
            "suggested_action": "先把这一节压缩得更清楚，再逐步扩展后续段落。" if project_language == "zh" else "Clarify this section first, then expand the following paragraphs.",
        })
    return issues[: max(3, len(sections))]

class BackendService:
    def __init__(self, database_path: Path | None = None) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        self.database_path = Path(os.getenv("DRAFTREFINE_DATABASE_PATH", str(database_path or DEFAULT_DB_PATH)))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        if (os.getenv("DRAFTREFINE_SKIP_DEMO_SEED") or "").strip().lower() not in {"1", "true", "yes"}:
            self._seed_demo_data_if_needed()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with open_database(self.database_path) as connection:
            yield connection

    def _init_db(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          type TEXT NOT NULL,
          language TEXT NOT NULL,
          source_type TEXT NOT NULL,
          status TEXT NOT NULL,
          progress_state TEXT NOT NULL,
          next_action TEXT NOT NULL,
          overview TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          file_id TEXT,
          last_job_id TEXT,
          issue_count INTEGER NOT NULL DEFAULT 0,
          unresolved_comment_count INTEGER NOT NULL DEFAULT 0,
          pending_revision_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS source_files (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          file_name TEXT NOT NULL,
          content_type TEXT,
          storage_path TEXT NOT NULL,
          parse_status TEXT NOT NULL,
          parse_error TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sections (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          title TEXT NOT NULL,
          order_index INTEGER NOT NULL,
          level INTEGER NOT NULL,
          current_text TEXT NOT NULL,
          original_text TEXT NOT NULL,
          path TEXT NOT NULL,
          parse_confidence REAL NOT NULL,
          issue_count INTEGER NOT NULL DEFAULT 0,
          comment_count INTEGER NOT NULL DEFAULT 0,
          revision_state TEXT NOT NULL DEFAULT 'clean',
          completion_state TEXT NOT NULL DEFAULT 'not-started'
        );
        CREATE TABLE IF NOT EXISTS issues (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          section_id TEXT NOT NULL,
          issue_type TEXT NOT NULL,
          severity TEXT NOT NULL,
          title TEXT NOT NULL,
          detail TEXT NOT NULL,
          suggested_action TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviewer_comments (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          raw_comment TEXT NOT NULL,
          mapped_section_id TEXT,
          manual_override_section_id TEXT,
          confidence REAL NOT NULL,
          suggested_action TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revision_candidates (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          section_id TEXT NOT NULL,
          action_type TEXT NOT NULL,
          request_id TEXT,
          base_text TEXT NOT NULL DEFAULT '',
          selected_text TEXT NOT NULL DEFAULT '',
          replacement_text TEXT NOT NULL DEFAULT '',
          selection_start INTEGER,
          selection_end INTEGER,
          comment_id TEXT,
          candidate_text TEXT NOT NULL,
          summary TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          model TEXT NOT NULL,
          warnings_json TEXT NOT NULL,
          evidence_json TEXT NOT NULL DEFAULT '[]',
          plan_json TEXT NOT NULL DEFAULT '{}',
          review_json TEXT NOT NULL DEFAULT '{}',
          agent_trace_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revision_requests (
          id TEXT PRIMARY KEY,
          project_id TEXT,
          section_id TEXT,
          action_type TEXT NOT NULL,
          source_type TEXT NOT NULL,
          input_text TEXT NOT NULL,
          note TEXT NOT NULL,
          selected_text TEXT NOT NULL DEFAULT '',
          comment_id TEXT,
          status TEXT NOT NULL,
          result_summary TEXT NOT NULL DEFAULT '',
          state_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revision_events (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          section_id TEXT NOT NULL,
          source_type TEXT NOT NULL,
          source_label TEXT NOT NULL,
          action_type TEXT NOT NULL,
          summary TEXT NOT NULL,
          old_text TEXT NOT NULL,
          new_text TEXT NOT NULL,
          prompt_version TEXT,
          model TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS llm_runs (
          id TEXT PRIMARY KEY,
          project_id TEXT,
          section_id TEXT,
          action_name TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          status TEXT NOT NULL,
          latency_ms INTEGER NOT NULL,
          token_estimate INTEGER NOT NULL,
          input_hash TEXT NOT NULL,
          output_hash TEXT NOT NULL,
          error TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retrieval_logs (
          id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL,
          project_id TEXT,
          section_id TEXT,
          source_kind TEXT NOT NULL,
          source_id TEXT,
          score REAL NOT NULL,
          label TEXT NOT NULL,
          excerpt TEXT NOT NULL,
          metadata_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          job_type TEXT NOT NULL,
          status TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
        with self._connect() as connection:
            connection.executescript(schema)
            self._ensure_columns(
                connection,
                "revision_candidates",
                {
                    "request_id": "TEXT",
                    "base_text": "TEXT NOT NULL DEFAULT ''",
                    "selected_text": "TEXT NOT NULL DEFAULT ''",
                    "replacement_text": "TEXT NOT NULL DEFAULT ''",
                    "selection_start": "INTEGER",
                    "selection_end": "INTEGER",
                    "comment_id": "TEXT",
                    "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
                    "plan_json": "TEXT NOT NULL DEFAULT '{}'",
                    "review_json": "TEXT NOT NULL DEFAULT '{}'",
                    "agent_trace_json": "TEXT NOT NULL DEFAULT '{}'",
                },
            )
            self._ensure_columns(
                connection,
                "projects",
                {
                    "deleted_at": "TEXT",
                    "owner_user_id": "TEXT",
                },
            )
            connection.commit()

    def _ensure_columns(self, connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _serialize_user(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "email": row["email"],
            "username": row["username"],
            "createdAt": row["created_at"],
        }

    def auth_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            user_count = int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        return {
            "hasUsers": user_count > 0,
            "registrationMode": "invite-only",
            "inviteConfigured": bool((os.getenv("DRAFTREFINE_INVITE_CODE") or os.getenv("INVITE_CODE") or "").strip()),
        }

    def get_or_create_demo_user(self) -> dict[str, Any]:
        demo_email = "demo@draftrefine.local"
        with self._connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE email = ?", (demo_email,)).fetchone()
            if user is None:
                user_id = new_id("user")
                connection.execute(
                    """
                    INSERT INTO users (id, email, username, password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, demo_email, "demo", make_password_hash(secrets.token_urlsafe(32)), utc_now()),
                )
                user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                connection.commit()
        self._seed_demo_data_if_needed(owner_user_id=user["id"])
        return self._serialize_user(user)

    def _configured_invite_code(self) -> str:
        return (os.getenv("DRAFTREFINE_INVITE_CODE") or os.getenv("INVITE_CODE") or "").strip()

    def _configured_account_recovery_code(self) -> str:
        return (os.getenv("DRAFTREFINE_ACCOUNT_RECOVERY_CODE") or "").strip()

    def _session_days(self) -> int:
        return max(1, int((os.getenv("DRAFTREFINE_SESSION_DAYS") or "30").strip() or "30"))

    def _create_session(self, connection: sqlite3.Connection, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        connection.execute(
            """
            INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                new_id("session"),
                user_id,
                hash_session_token(token),
                utc_now(),
                session_expiry_iso(self._session_days()),
            ),
        )
        return token

    def _claim_unowned_projects(self, connection: sqlite3.Connection, user_id: str) -> None:
        connection.execute(
            "UPDATE projects SET owner_user_id = ? WHERE owner_user_id IS NULL",
            (user_id,),
        )

    def register_user(self, *, email: str, username: str, password: str, invite_code: str) -> tuple[dict[str, Any], str]:
        normalized_email = normalize_email(email)
        normalized_username = normalize_username(username)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_email):
            raise ValueError("请输入有效邮箱。")
        if not re.fullmatch(r"[a-z0-9_-]{3,32}", normalized_username):
            raise ValueError("用户名需为 3-32 位字母、数字、下划线或短横线。")
        if len(password) < 8:
            raise ValueError("密码至少需要 8 位。")
        configured_invite_code = self._configured_invite_code()
        if configured_invite_code and invite_code.strip() != configured_invite_code:
            raise ValueError("邀请码不正确。")
        if not configured_invite_code:
            raise ValueError("当前未配置邀请码，暂时无法注册。")

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM users WHERE email = ? OR username = ?",
                (normalized_email, normalized_username),
            ).fetchone()
            if existing is not None:
                raise ValueError("邮箱或用户名已被使用。")
            user_id = new_id("user")
            now = utc_now()
            first_user = int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]) == 0
            connection.execute(
                """
                INSERT INTO users (id, email, username, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, normalized_email, normalized_username, make_password_hash(password), now),
            )
            if first_user:
                self._claim_unowned_projects(connection, user_id)
            token = self._create_session(connection, user_id)
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            connection.commit()
        return self._serialize_user(user), token

    def login_user(self, *, identifier: str, password: str) -> tuple[dict[str, Any], str]:
        normalized_identifier = normalize_email(identifier)
        with self._connect() as connection:
            self._cleanup_expired_sessions(connection)
            user = connection.execute(
                "SELECT * FROM users WHERE email = ? OR username = ?",
                (normalized_identifier, normalize_username(identifier)),
            ).fetchone()
            if user is None or not verify_password_hash(password, user["password_hash"]):
                raise ValueError("账号或密码不正确。")
            token = self._create_session(connection, user["id"])
            connection.commit()
        return self._serialize_user(user), token

    def recover_user_password(
        self,
        *,
        identifier: str,
        new_password: str,
        recovery_code: str,
    ) -> tuple[dict[str, Any], str]:
        if len(new_password) < 8:
            raise ValueError("密码至少需要 8 位。")
        configured_code = self._configured_account_recovery_code()
        if not configured_code:
            raise ValueError("站点尚未配置账号恢复码，请联系站点所有者。")
        if not secrets.compare_digest(recovery_code.strip(), configured_code):
            raise ValueError("恢复信息不正确。")

        with self._connect() as connection:
            normalized_identifier = identifier.strip()
            if normalized_identifier:
                user = connection.execute(
                    "SELECT * FROM users WHERE email = ? OR username = ?",
                    (normalize_email(normalized_identifier), normalize_username(normalized_identifier)),
                ).fetchone()
            else:
                users = connection.execute("SELECT * FROM users ORDER BY created_at ASC LIMIT 2").fetchall()
                if len(users) != 1:
                    raise ValueError("站点中存在多个账号，请填写原邮箱或用户名。")
                user = users[0]
            if user is None:
                raise ValueError("恢复信息不正确。")
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (make_password_hash(new_password), user["id"]),
            )
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            token = self._create_session(connection, user["id"])
            refreshed_user = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            connection.commit()
        return self._serialize_user(refreshed_user), token

    def _cleanup_expired_sessions(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now(),))

    def get_user_by_session(self, session_token: str | None) -> dict[str, Any] | None:
        if not session_token:
            return None
        with self._connect() as connection:
            self._cleanup_expired_sessions(connection)
            row = connection.execute(
                """
                SELECT u.*
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                (hash_session_token(session_token), utc_now()),
            ).fetchone()
            connection.commit()
        if row is None:
            return None
        return self._serialize_user(row)

    def delete_session(self, session_token: str | None) -> None:
        if not session_token:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_session_token(session_token),))
            connection.commit()

    def ensure_project_access(self, project_id: str, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, owner_user_id FROM projects WHERE id = ? AND owner_user_id = ?",
                (project_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return {"projectId": row["id"]}

    def ensure_source_file_access(self, file_id: str, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT sf.id, sf.project_id
                FROM source_files sf
                JOIN projects p ON p.id = sf.project_id
                WHERE sf.id = ? AND p.owner_user_id = ?
                """,
                (file_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(file_id)
        return {"fileId": row["id"], "projectId": row["project_id"]}

    def ensure_section_access(self, section_id: str, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.id, s.project_id
                FROM sections s
                JOIN projects p ON p.id = s.project_id
                WHERE s.id = ? AND p.owner_user_id = ?
                """,
                (section_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(section_id)
        return {"sectionId": row["id"], "projectId": row["project_id"]}

    def ensure_comment_access(self, comment_id: str, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT c.id, c.project_id
                FROM reviewer_comments c
                JOIN projects p ON p.id = c.project_id
                WHERE c.id = ? AND p.owner_user_id = ?
                """,
                (comment_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(comment_id)
        return {"commentId": row["id"], "projectId": row["project_id"]}

    def ensure_issue_access(self, issue_id: str, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT i.id, i.project_id
                FROM issues i
                JOIN projects p ON p.id = i.project_id
                WHERE i.id = ? AND p.owner_user_id = ?
                """,
                (issue_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(issue_id)
        return {"issueId": row["id"], "projectId": row["project_id"]}

    def ensure_job_access(self, job_id: str, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT j.id, j.project_id
                FROM jobs j
                JOIN projects p ON p.id = j.project_id
                WHERE j.id = ? AND p.owner_user_id = ?
                """,
                (job_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return {"jobId": row["id"], "projectId": row["project_id"]}

    def ensure_revision_candidate_access(self, candidate_id: str, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT rc.id, COALESCE(rc.project_id, s.project_id) AS project_id
                FROM revision_candidates rc
                LEFT JOIN sections s ON s.id = rc.section_id
                JOIN projects p ON p.id = COALESCE(rc.project_id, s.project_id)
                WHERE rc.id = ? AND p.owner_user_id = ?
                """,
                (candidate_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return {"candidateId": row["id"], "projectId": row["project_id"]}

    def ensure_revision_access(self, revision_id: str, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT r.id, r.project_id
                FROM revision_events r
                JOIN projects p ON p.id = r.project_id
                WHERE r.id = ? AND p.owner_user_id = ?
                """,
                (revision_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(revision_id)
        return {"revisionId": row["id"], "projectId": row["project_id"]}

    def _prompt_path(self, category: str, language: str, action_name: str | None = None) -> Path:
        candidates: list[Path] = []
        if action_name:
            slug = re.sub(r"[^a-z0-9-]+", "-", action_name.lower()).strip("-")
            if slug:
                candidates.append(PROMPTS_DIR / category / f"{slug}.{language}.yaml")
                if language != "en":
                    candidates.append(PROMPTS_DIR / category / f"{slug}.en.yaml")
        candidates.append(PROMPTS_DIR / category / f"default.{language}.yaml")
        if language != "en":
            candidates.append(PROMPTS_DIR / category / "default.en.yaml")
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return PROMPTS_DIR / category / "default.en.yaml"

    def _load_prompt(self, category: str, language: str, action_name: str | None = None) -> dict[str, Any]:
        prompt_path = self._prompt_path(category, language, action_name)
        prompt = yaml.safe_load(prompt_path.read_text(encoding="utf-8"))
        prompt["version_tag"] = f"{category}/{prompt_path.stem}@{prompt.get('version', '1.0.0')}"
        return prompt

    def _provider_candidates(self, model_profile: str = "normal") -> list[dict[str, str]]:
        normalized_profile = "pro" if model_profile == "pro" else "normal"
        normal_deepseek_model = os.getenv(
            "DRAFTREFINE_DEEPSEEK_MODEL",
            os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        )
        pro_deepseek_model = os.getenv(
            "DRAFTREFINE_DEEPSEEK_PRO_MODEL",
            os.getenv("DEEPSEEK_PRO_MODEL", normal_deepseek_model),
        )
        normal_qwen_model = os.getenv(
            "DRAFTREFINE_QWEN_MODEL",
            os.getenv("QWEN_MODEL", "qwen-plus"),
        )
        pro_qwen_model = os.getenv(
            "DRAFTREFINE_QWEN_PRO_MODEL",
            os.getenv("QWEN_PRO_MODEL", normal_qwen_model),
        )
        return [
            {
                "provider": "deepseek",
                "api_key": os.getenv("DRAFTREFINE_DEEPSEEK_API_KEY", os.getenv("DEEPSEEK_API_KEY", "")),
                "base_url": os.getenv("DRAFTREFINE_DEEPSEEK_BASE_URL", os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")),
                "model": pro_deepseek_model if normalized_profile == "pro" else normal_deepseek_model,
            },
            {
                "provider": "qwen",
                "api_key": os.getenv("DRAFTREFINE_QWEN_API_KEY", os.getenv("QWEN_API_KEY", "")),
                "base_url": os.getenv("DRAFTREFINE_QWEN_BASE_URL", os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
                "model": pro_qwen_model if normalized_profile == "pro" else normal_qwen_model,
            },
        ]

    def _extract_json(self, content: str) -> Any:
        content = content.strip()
        if content.startswith("{") or content.startswith("["):
            return json.loads(content)
        match = re.search(r"(\{.*\}|\[.*\])", content, re.S)
        if not match:
            raise ValueError("No JSON found in model response")
        return json.loads(match.group(1))

    def _call_provider_json(
        self,
        *,
        action_name: str,
        prompt_version: str,
        system_prompt: str,
        input_payload: dict[str, Any],
        schema_hint: str,
        model_profile: str = "normal",
    ) -> tuple[Any | None, dict[str, Any]]:
        payload_text = json.dumps(input_payload, ensure_ascii=False, indent=2)
        normalized_profile = "pro" if model_profile == "pro" else "normal"
        timeout_seconds = max(
            float(
                os.getenv(
                    "DRAFTREFINE_MODEL_TIMEOUT_SECONDS_PRO" if normalized_profile == "pro" else "DRAFTREFINE_MODEL_TIMEOUT_SECONDS_NORMAL",
                    os.getenv("DRAFTREFINE_MODEL_TIMEOUT_SECONDS", "18" if normalized_profile == "pro" else "10"),
                )
            ),
            60.0,
        )
        max_attempts = max(
            1,
            int(
                os.getenv(
                    "DRAFTREFINE_MODEL_MAX_RETRIES_PRO" if normalized_profile == "pro" else "DRAFTREFINE_MODEL_MAX_RETRIES_NORMAL",
                    os.getenv("DRAFTREFINE_MODEL_MAX_RETRIES", "1" if normalized_profile == "pro" else "2"),
                )
            ),
        )
        retry_backoff_seconds = max(0.0, float(os.getenv("DRAFTREFINE_MODEL_RETRY_BACKOFF_SECONDS", "0.75")))
        max_tokens = 1800 if action_name in {"academic-rewrite", "expand", "comment-revision"} else 900
        attempt_logs: list[dict[str, Any]] = []
        configured_provider = False
        for candidate in self._provider_candidates(model_profile):
            if not candidate["api_key"]:
                continue
            configured_provider = True
            for attempt_index in range(max_attempts):
                started = perf_counter()
                body = {
                    "model": candidate["model"],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": (
                                f"Task: {action_name}\n"
                                f"Prompt version: {prompt_version}\n"
                                f"Schema:\n{schema_hint}\n\n"
                                f"Input JSON:\n{payload_text}\n\n"
                                "Return JSON only. Do not wrap it in Markdown."
                            ),
                        },
                    ],
                }
                if candidate["provider"] == "deepseek":
                    thinking_env_key = (
                        "DRAFTREFINE_DEEPSEEK_PRO_THINKING"
                        if normalized_profile == "pro"
                        else "DRAFTREFINE_DEEPSEEK_THINKING"
                    )
                    thinking_value = os.getenv(
                        thinking_env_key,
                        os.getenv(
                            "DEEPSEEK_PRO_THINKING" if normalized_profile == "pro" else "DEEPSEEK_THINKING",
                            "disabled",
                        ),
                    ).strip().lower()
                    body["thinking"] = {"type": "enabled" if thinking_value == "enabled" else "disabled"}
                    body["response_format"] = {"type": "json_object"}
                request = urllib.request.Request(
                    f"{candidate['base_url'].rstrip('/')}/chat/completions",
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {candidate['api_key']}",
                        "Connection": "close",
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                        response_payload = json.loads(response.read().decode("utf-8"))
                    content = response_payload["choices"][0]["message"]["content"]
                    parsed = self._extract_json(content)
                    return parsed, {
                        "provider": candidate["provider"],
                        "model": candidate["model"],
                        "status": "completed",
                        "latency_ms": int((perf_counter() - started) * 1000),
                        "error": None,
                    }
                except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, KeyError, json.JSONDecodeError) as exc:
                    latency_ms = int((perf_counter() - started) * 1000)
                    error_text = f"{type(exc).__name__}: {exc}"
                    attempt_logs.append(
                        {
                            "action_name": action_name,
                            "prompt_version": prompt_version,
                            "provider": candidate["provider"],
                            "model": candidate["model"],
                            "status": "failed",
                            "latency_ms": latency_ms,
                            "error": error_text,
                            "input_text": payload_text,
                            "output_text": "",
                        }
                    )
                    if attempt_index + 1 < max_attempts:
                        sleep(retry_backoff_seconds * (attempt_index + 1))
                    continue
        if not configured_provider:
            raise ModelInvocationError("未配置可用的模型提供方凭证。", attempts=attempt_logs)
        joined_errors = "; ".join(
            f"{item['provider']}/{item['model']}({item['error']})"
            for item in attempt_logs[-min(len(attempt_logs), 6) :]
        )
        raise ModelInvocationError(
            f"模型调用失败，请稍后重试。最近错误：{joined_errors or 'unknown error'}",
            attempts=attempt_logs,
        )

    def _insert_llm_run(self, connection: sqlite3.Connection, *, project_id: str | None, section_id: str | None, action_name: str, prompt_version: str, provider: str, model: str, status: str, latency_ms: int, input_text: str, output_text: str, error: str | None) -> None:
        connection.execute(
            """
            INSERT INTO llm_runs (
              id, project_id, section_id, action_name, prompt_version, provider, model, status,
              latency_ms, token_estimate, input_hash, output_hash, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("run"), project_id, section_id, action_name, prompt_version, provider, model, status, latency_ms, token_estimate(input_text), short_hash(input_text), short_hash(output_text or status), error, utc_now()),
        )

    def _start_revision_request(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str | None,
        section_id: str | None,
        action_type: str,
        source_type: str,
        input_text: str,
        note: str,
        selected_text: str,
        comment_id: str | None,
    ) -> str:
        request_id = new_id("request")
        now = utc_now()
        connection.execute(
            """
            INSERT INTO revision_requests (
              id, project_id, section_id, action_type, source_type, input_text, note,
              selected_text, comment_id, status, result_summary, state_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', '', '{}', ?, ?)
            """,
            (request_id, project_id, section_id, action_type, source_type, input_text, note, selected_text, comment_id, now, now),
        )
        return request_id

    def _finish_revision_request(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        status: str,
        result_summary: str,
        state: dict[str, Any],
    ) -> None:
        connection.execute(
            "UPDATE revision_requests SET status = ?, result_summary = ?, state_json = ?, updated_at = ? WHERE id = ?",
            (status, result_summary, json.dumps(state, ensure_ascii=False), utc_now(), request_id),
        )

    def _run_prompt_json(
        self,
        *,
        category: str,
        language: str,
        action_name: str,
        input_payload: dict[str, Any],
        schema_hint: str,
        model_profile: str = "normal",
    ) -> tuple[Any | None, dict[str, Any]]:
        prompt = self._load_prompt(category, language, action_name)
        model_output, run_meta = self._call_provider_json(
            action_name=action_name,
            prompt_version=prompt["version_tag"],
            system_prompt=prompt["system_prompt"],
            input_payload=input_payload,
            schema_hint=prompt.get("schema_hint") or schema_hint,
            model_profile=model_profile,
        )
        return model_output, {
            "actionName": action_name,
            "promptVersion": prompt["version_tag"],
            "provider": run_meta["provider"],
            "model": run_meta["model"],
            "status": run_meta["status"],
            "latencyMs": run_meta["latency_ms"],
            "error": run_meta["error"],
        }

    def _insert_retrieval_logs(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        project_id: str | None,
        section_id: str | None,
        evidence: list[dict[str, Any]],
    ) -> None:
        now = utc_now()
        for item in evidence:
            connection.execute(
                """
                INSERT INTO retrieval_logs (
                  id, request_id, project_id, section_id, source_kind, source_id,
                  score, label, excerpt, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("retrieval"),
                    request_id,
                    project_id,
                    section_id,
                    item.get("source_kind") or item.get("sourceKind") or "unknown",
                    item.get("source_id") or item.get("sourceId"),
                    float(item.get("score", 0)),
                    item.get("label", "Untitled evidence"),
                    item.get("excerpt", ""),
                    json.dumps(item.get("metadata", {}), ensure_ascii=False),
                    now,
                ),
            )

    def _collect_revision_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str | None,
        section_id: str | None,
        title: str,
        query_text: str,
        action_type: str,
        comment_context: str,
    ) -> list[dict[str, Any]]:
        if not project_id:
            return []

        terms = extract_revision_terms("\n".join(part for part in [title, query_text, comment_context] if part.strip()))
        evidence: list[dict[str, Any]] = []

        if section_id:
            section_rows = connection.execute(
                "SELECT id, title, current_text, order_index FROM sections WHERE project_id = ? ORDER BY order_index ASC",
                (project_id,),
            ).fetchall()
            selected_index = next((index for index, row in enumerate(section_rows) if row["id"] == section_id), None)
            if selected_index is not None:
                for offset in (-1, 1):
                    neighbor_index = selected_index + offset
                    if 0 <= neighbor_index < len(section_rows):
                        row = section_rows[neighbor_index]
                        score, matched = score_revision_match(terms, row["title"], row["current_text"])
                        if score > 0:
                            evidence.append(
                                {
                                    "sourceKind": "project-section",
                                    "sourceId": row["id"],
                                    "label": f"相邻章节：{row['title']}",
                                    "excerpt": summarize_evidence_excerpt(row["current_text"]),
                                    "score": score + 6,
                                    "metadata": {"matchedTerms": matched, "orderIndex": row["order_index"]},
                                }
                            )

        comment_rows = connection.execute(
            "SELECT id, raw_comment, suggested_action, status FROM reviewer_comments WHERE project_id = ? ORDER BY created_at DESC LIMIT 6",
            (project_id,),
        ).fetchall()
        for row in comment_rows:
            score, matched = score_revision_match(terms, row["raw_comment"], row["suggested_action"])
            if score > 0:
                evidence.append(
                    {
                        "sourceKind": "reviewer-comment",
                        "sourceId": row["id"],
                        "label": f"导师意见：{row['status']}",
                        "excerpt": summarize_evidence_excerpt(f"{row['raw_comment']} {row['suggested_action']}"),
                        "score": score + 4,
                        "metadata": {"matchedTerms": matched},
                    }
                )

        revision_rows = connection.execute(
            "SELECT id, summary, new_text, action_type FROM revision_events WHERE project_id = ? ORDER BY created_at DESC LIMIT 6",
            (project_id,),
        ).fetchall()
        for row in revision_rows:
            score, matched = score_revision_match(terms, row["summary"], row["new_text"])
            if score > 0:
                evidence.append(
                    {
                        "sourceKind": "revision-memory",
                        "sourceId": row["id"],
                        "label": f"历史改稿：{row['action_type']}",
                        "excerpt": summarize_evidence_excerpt(row["new_text"]),
                        "score": score + 2,
                        "metadata": {"matchedTerms": matched, "summary": row["summary"]},
                    }
                )

        literature_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'literature_items'"
        ).fetchone()
        if literature_table is not None:
            literature_chunk_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'literature_chunks'"
            ).fetchone()
            matched_literature = 0
            rag_chunk_ids: set[str] = set()
            try:
                from .literature import LiteratureService

                rag_query = "\n".join(part for part in [title, query_text, comment_context] if part.strip())
                rag_result = LiteratureService(database_path=self.database_path).search_project_evidence(
                    project_id=project_id,
                    query=rag_query,
                    limit=10,
                )
                for item in rag_result.get("evidence", []):
                    source_id = item.get("sourceId") or item.get("source_id")
                    if source_id:
                        rag_chunk_ids.add(source_id)
                    evidence.append(item)
                    matched_literature += 1
            except Exception:
                rag_chunk_ids = set()
            if literature_chunk_table is not None:
                chunk_rows = connection.execute(
                    """
                    SELECT c.id, c.item_id, c.source_kind, c.source_label, c.chunk_text, c.chunk_order,
                           i.title, i.venue, i.year, i.doi, i.tags_json
                    FROM literature_chunks c
                    JOIN literature_items i ON i.id = c.item_id
                    WHERE c.project_id = ?
                    ORDER BY c.updated_at DESC, c.chunk_order ASC
                    LIMIT 80
                    """,
                    (project_id,),
                ).fetchall()
                for row in chunk_rows:
                    if row["id"] in rag_chunk_ids:
                        continue
                    tags = json.loads(row["tags_json"]) if row["tags_json"] else []
                    score, matched = score_revision_match(
                        terms,
                        row["chunk_text"] or "",
                        row["title"] or "",
                        row["source_label"] or "",
                        row["venue"] or "",
                        " ".join(tags),
                    )
                    if score <= 0:
                        continue
                    matched_literature += 1
                    source_kind = row["source_kind"] or "metadata-abstract"
                    boost = 12 if source_kind in {"manual-fulltext", "oa-pdf-fulltext"} else 8
                    evidence.append(
                        {
                            "sourceKind": "literature",
                            "sourceId": row["id"],
                            "label": f"文献片段：{(row['title'] or '')[:42]}",
                            "excerpt": summarize_evidence_excerpt(row["chunk_text"] or ""),
                            "score": score + boost,
                            "metadata": {
                                "matchedTerms": matched,
                                "itemId": row["item_id"],
                                "chunkOrder": row["chunk_order"],
                                "chunkSourceKind": source_kind,
                                "chunkSourceLabel": row["source_label"] or "",
                                "venue": row["venue"] or "",
                                "year": row["year"],
                                "doi": row["doi"] or "",
                                "tags": tags,
                            },
                        }
                    )
            literature_rows = connection.execute(
                """
                SELECT id, title, abstract, venue, year, doi, tags_json
                FROM literature_items
                WHERE project_id = ?
                ORDER BY updated_at DESC
                LIMIT 30
                """,
                (project_id,),
            ).fetchall()
            for row in literature_rows:
                abstract = row["abstract"] or ""
                title_text = row["title"] or ""
                tags = json.loads(row["tags_json"]) if row["tags_json"] else []
                score, matched = score_revision_match(terms, title_text, abstract, row["venue"] or "", " ".join(tags))
                if score <= 0:
                    continue
                matched_literature += 1
                boost = 8 if action_type in {"expand", "comment-revision", "academic-rewrite"} else 3
                evidence.append(
                    {
                        "sourceKind": "literature",
                        "sourceId": row["id"],
                        "label": f"文献：{title_text[:48]}",
                        "excerpt": summarize_evidence_excerpt(abstract or title_text),
                        "score": score + boost,
                        "metadata": {
                            "matchedTerms": matched,
                            "venue": row["venue"] or "",
                            "year": row["year"],
                            "doi": row["doi"] or "",
                            "tags": tags,
                        },
                    }
                )
            if matched_literature == 0 and action_type in {"expand", "comment-revision", "academic-rewrite", "unify-terms"}:
                for row in literature_rows[:2]:
                    tags = json.loads(row["tags_json"]) if row["tags_json"] else []
                    title_text = row["title"] or ""
                    abstract = row["abstract"] or ""
                    evidence.append(
                        {
                            "sourceKind": "literature",
                            "sourceId": row["id"],
                            "label": f"文献候选：{title_text[:48]}",
                            "excerpt": summarize_evidence_excerpt(abstract or title_text),
                            "score": 5.0,
                            "metadata": {
                                "matchedTerms": [],
                                "fallback": True,
                                "venue": row["venue"] or "",
                                "year": row["year"],
                                "doi": row["doi"] or "",
                                "tags": tags,
                            },
                        }
                    )

        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        for item in evidence:
            key = (
                item.get("sourceKind") or item.get("source_kind") or "unknown",
                item.get("sourceId") or item.get("source_id") or item.get("label", ""),
            )
            current = deduped.get(key)
            if current is None or float(item.get("score", 0)) > float(current.get("score", 0)):
                deduped[key] = item
        ranked = sorted(deduped.values(), key=lambda item: (-float(item["score"]), item["label"]))
        return ranked[:8]

    def _build_evidence_strategy(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str | None,
        title: str,
        base_text: str,
        action_type: str,
        language: str,
        note: str,
        comment_context: str,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        recommended_query = " ".join(extract_revision_terms("\n".join(part for part in [title, base_text[:1800], note, comment_context] if part.strip()))[:6])
        query_warnings: list[str] = []
        imported_literature_count = 0
        imported_doi_count = 0
        if project_id:
            literature_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'literature_items'"
            ).fetchone()
            if literature_table is not None:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN trim(coalesce(doi, '')) != '' THEN 1 ELSE 0 END) AS doi_total
                    FROM literature_items
                    WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone()
                imported_literature_count = int(row["total"] or 0)
                imported_doi_count = int(row["doi_total"] or 0)
            try:
                from .literature import LiteratureService

                suggestion = LiteratureService(database_path=self.database_path).suggest_search_query(project_id)
                recommended_query = suggestion.get("query") or recommended_query
                query_warnings.extend(suggestion.get("warnings", []))
            except Exception:
                pass

        literature_evidence = [
            item
            for item in evidence
            if (item.get("source_kind") or item.get("sourceKind")) == "literature"
        ]
        local_rag_evidence = [
            item
            for item in literature_evidence
            if ((item.get("metadata") or {}).get("retrievalMode") == "local-hybrid-rag")
        ]
        source_order = ["openalex", "crossref", "semantic-scholar"]
        if language == "zh":
            source_order.extend(["cnki-manual-fallback", "google-scholar-manual-fallback"])
        else:
            source_order.append("google-scholar-manual-fallback")

        if action_type in {"shorten", "transition-polish", "translate-en-zh", "translate-zh-en", "reduce-aigc"}:
            mode = "local-only"
            reason = "当前动作以语言处理和表达整理为主，不应默认联网补证据。"
        elif imported_literature_count > 0:
            mode = "library-first"
            reason = "优先使用项目文献库，只有证据不足时才建议人工补检。"
        else:
            mode = "library-plus-live-suggest"
            reason = "当前项目文献库为空或证据较弱，应先给出检索建议，再由用户确认导入。"

        need_support = action_type in {"academic-rewrite", "expand", "comment-revision", "unify-terms"}
        needs_live_search_suggestion = bool(
            need_support
            and imported_literature_count == 0
            and not literature_evidence
        )
        should_block_auto_citation = imported_doi_count == 0 and not any(
            (item.get("metadata") or {}).get("doi") for item in literature_evidence
        )
        return {
            "mode": mode,
            "reason": reason,
            "recommendedQuery": recommended_query,
            "sourceOrder": source_order,
            "queryWarnings": list(dict.fromkeys(query_warnings)),
            "importedLiteratureCount": imported_literature_count,
            "importedDoiCount": imported_doi_count,
            "retrievedLiteratureEvidenceCount": len(literature_evidence),
            "localRagEvidenceCount": len(local_rag_evidence),
            "retrievalMode": "local-hybrid-rag" if local_rag_evidence else mode,
            "needsImportedEvidence": need_support,
            "needsLiveSearchSuggestion": needs_live_search_suggestion,
            "shouldBlockAutoCitation": should_block_auto_citation,
        }

    def _build_citation_audit(
        self,
        *,
        original_text: str,
        candidate_text: str,
        evidence: list[dict[str, Any]],
        evidence_strategy: dict[str, Any],
    ) -> dict[str, Any]:
        original_has_citations = has_citation_markers(original_text)
        candidate_has_citations = has_citation_markers(candidate_text)
        literature_evidence = [
            item
            for item in evidence
            if (item.get("source_kind") or item.get("sourceKind")) == "literature"
        ]
        verified_dois = [
            item
            for item in literature_evidence
            if ((item.get("metadata") or {}).get("doi") or "").strip()
        ]
        if candidate_has_citations and not literature_evidence:
            status = "unsupported-risk"
            recommended_action = "候选稿出现了引文样式，但当前没有可追溯文献证据，不能直接采用。"
        elif candidate_has_citations and literature_evidence and not verified_dois:
            status = "needs-verification"
            recommended_action = "候选稿涉及引文表达，但缺少 DOI 级校验，建议先校对元数据再采用。"
        elif candidate_has_citations and verified_dois:
            status = "supported"
            recommended_action = "候选稿有文献证据支撑，但仍应在导出前做一次引用格式核对。"
        elif not candidate_has_citations and evidence_strategy.get("needsImportedEvidence") and not literature_evidence:
            status = "evidence-gap"
            recommended_action = "当前改稿没有直接引文，但这一类动作更适合先补文献证据，再决定是否增强论证。"
        else:
            status = "not-needed"
            recommended_action = "当前候选稿没有新增明确引文风险，可以继续人工审阅。"
        return {
            "status": status,
            "originalHasCitationMarkers": original_has_citations,
            "candidateHasCitationMarkers": candidate_has_citations,
            "literatureEvidenceCount": len(literature_evidence),
            "verifiedDoiEvidenceCount": len(verified_dois),
            "recommendedAction": recommended_action,
            "evidenceIds": [
                item.get("source_id") or item.get("sourceId")
                for item in literature_evidence
                if (item.get("source_id") or item.get("sourceId"))
            ],
        }

    def _build_citation_verification(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str | None,
        candidate_text: str,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized_candidate = normalize_text(candidate_text)
        doi_mentions = extract_doi_mentions(normalized_candidate)
        author_year_mentions = extract_author_year_mentions(normalized_candidate)
        numeric_mentions = [
            {
                "text": match.group(0),
                "numbers": parse_numeric_citation_values(match.group(0).strip("[]")),
                "verified": False,
                "issues": [],
            }
            for match in NUMERIC_CITATION_PATTERN.finditer(normalized_candidate)
        ]

        if not doi_mentions and not author_year_mentions and not numeric_mentions:
            return {
                "status": "not-applicable",
                "verifiedMentionCount": 0,
                "unresolvedMentionCount": 0,
                "doiMentions": [],
                "authorYearMentions": [],
                "numericMentions": [],
                "matchedItems": [],
                "issues": [],
                "recommendedAction": "当前候选稿没有可核验的显式引用标记。",
            }

        literature_rows: list[sqlite3.Row] = []
        if project_id:
            literature_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'literature_items'"
            ).fetchone()
            if literature_table is not None:
                literature_rows = connection.execute(
                    """
                    SELECT id, title, authors_json, year, venue, doi, journal_title, zotero_item_key
                    FROM literature_items
                    WHERE project_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (project_id,),
                ).fetchall()

        items: list[dict[str, Any]] = []
        for row in literature_rows:
            authors = json.loads(row["authors_json"]) if row["authors_json"] else []
            author_keys: set[str] = set()
            for author in authors:
                author_keys.update(author_keys_for_name(str(author)))
            items.append(
                {
                    "itemId": row["id"],
                    "title": row["title"] or "",
                    "authors": authors,
                    "authorKeys": sorted(author_keys),
                    "year": int(row["year"]) if row["year"] else None,
                    "venue": row["journal_title"] or row["venue"] or "",
                    "doi": normalize_citation_doi(row["doi"] or ""),
                    "zoteroItemKey": row["zotero_item_key"] or "",
                }
            )

        reference_entries: list[dict[str, Any]] = []
        if project_id:
            section_rows = connection.execute(
                "SELECT title, current_text FROM sections WHERE project_id = ? ORDER BY order_index ASC",
                (project_id,),
            ).fetchall()
            for row in section_rows:
                if looks_like_reference_section(row["title"] or ""):
                    reference_entries.extend(parse_reference_entries(row["current_text"] or ""))
        if not reference_entries:
            reference_entries = parse_reference_entries(normalized_candidate)

        items_by_doi = {item["doi"]: item for item in items if item["doi"]}
        reference_entries_by_index = {entry["index"]: entry for entry in reference_entries}
        matched_items: dict[str, dict[str, Any]] = {}
        issues: list[str] = []
        verified_mentions = 0
        unresolved_mentions = 0

        def register_match(item: dict[str, Any], match_kind: str) -> None:
            existing = matched_items.get(item["itemId"])
            if existing is None:
                matched_items[item["itemId"]] = {
                    "itemId": item["itemId"],
                    "title": item["title"],
                    "authors": item["authors"],
                    "year": item["year"],
                    "venue": item["venue"],
                    "doi": item["doi"],
                    "zoteroItemKey": item["zoteroItemKey"],
                    "matchKinds": [match_kind],
                    "metadataCompleteness": {
                        "hasDoi": bool(item["doi"]),
                        "hasYear": item["year"] is not None,
                        "hasVenue": bool(item["venue"]),
                        "hasZoteroItemKey": bool(item["zoteroItemKey"]),
                    },
                }
            elif match_kind not in existing["matchKinds"]:
                existing["matchKinds"].append(match_kind)

        def match_reference_entry_to_item(entry: dict[str, Any] | None) -> dict[str, Any] | None:
            if not entry:
                return None
            if entry.get("doi"):
                matched = items_by_doi.get(entry["doi"])
                if matched is not None:
                    return matched
            entry_author_key = entry.get("authorKey") or ""
            entry_year = int(re.sub(r"[^0-9]", "", entry.get("year") or "")[:4]) if re.search(r"\d{4}", entry.get("year") or "") else None
            if entry_author_key and entry_year:
                candidates = [
                    item
                    for item in items
                    if item["year"] == entry_year
                    and any(
                        entry_author_key == key or entry_author_key in key or key in entry_author_key
                        for key in item["authorKeys"]
                    )
                ]
                if candidates:
                    candidates.sort(
                        key=lambda item: max(
                            (len(key) for key in item["authorKeys"] if entry_author_key in key or key in entry_author_key),
                            default=0,
                        ),
                        reverse=True,
                    )
                    return candidates[0]
            entry_title = entry.get("titleToken") or ""
            if entry_title:
                for item in items:
                    item_title = normalize_title_token(item["title"])
                    if item_title and (entry_title in item_title or item_title in entry_title):
                        return item
            return None

        doi_results: list[dict[str, Any]] = []
        for mention in doi_mentions:
            item = items_by_doi.get(mention["doi"])
            mention_issues: list[str] = []
            if item is None:
                unresolved_mentions += 1
                mention_issues.append("doi_not_found_in_project_library")
                issues.append("doi_not_found_in_project_library")
            else:
                verified_mentions += 1
                register_match(item, "doi")
            doi_results.append(
                {
                    "text": mention["text"],
                    "doi": mention["doi"],
                    "verified": item is not None,
                    "matchedItemId": item["itemId"] if item else None,
                    "title": item["title"] if item else "",
                    "zoteroItemKey": item["zoteroItemKey"] if item else "",
                    "issues": mention_issues,
                }
            )

        author_year_results: list[dict[str, Any]] = []
        for mention in author_year_mentions:
            mention_year = int(re.sub(r"[^0-9]", "", mention["year"])[:4]) if re.search(r"\d{4}", mention["year"]) else None
            candidates = [
                item
                for item in items
                if item["year"] == mention_year
                and any(
                    mention["authorKey"] == key
                    or mention["authorKey"] in key
                    or key in mention["authorKey"]
                    for key in item["authorKeys"]
                )
            ]
            candidates.sort(key=lambda item: max((len(key) for key in item["authorKeys"] if mention["authorKey"] in key or key in mention["authorKey"]), default=0), reverse=True)
            item = candidates[0] if candidates else None
            mention_issues: list[str] = []
            if item is None:
                unresolved_mentions += 1
                mention_issues.append("author_year_not_found_in_project_library")
                issues.append("author_year_not_found_in_project_library")
            else:
                verified_mentions += 1
                register_match(item, "author-year")
                if len(candidates) > 1:
                    mention_issues.append("author_year_match_ambiguous")
                    issues.append("author_year_match_ambiguous")
            author_year_results.append(
                {
                    "text": mention["text"],
                    "author": mention["author"],
                    "year": mention["year"],
                    "verified": item is not None,
                    "matchedItemId": item["itemId"] if item else None,
                    "title": item["title"] if item else "",
                    "zoteroItemKey": item["zoteroItemKey"] if item else "",
                    "issues": mention_issues,
                }
            )

        numeric_results: list[dict[str, Any]] = []
        for mention in numeric_mentions:
            resolved_entries: list[dict[str, Any]] = []
            mention_issues: list[str] = []
            verified = True
            for number in mention["numbers"]:
                reference_entry = reference_entries_by_index.get(number)
                matched_item = match_reference_entry_to_item(reference_entry)
                if reference_entry is None:
                    verified = False
                    mention_issues.append("reference_entry_not_found")
                    issues.append("reference_entry_not_found")
                elif matched_item is None:
                    verified = False
                    mention_issues.append("reference_entry_not_matched_to_project_library")
                    issues.append("reference_entry_not_matched_to_project_library")
                else:
                    register_match(matched_item, "numeric")
                resolved_entries.append(
                    {
                        "number": number,
                        "referenceFound": reference_entry is not None,
                        "referenceText": reference_entry["rawText"] if reference_entry else "",
                        "matchedItemId": matched_item["itemId"] if matched_item else None,
                        "title": matched_item["title"] if matched_item else "",
                        "zoteroItemKey": matched_item["zoteroItemKey"] if matched_item else "",
                    }
                )
            if not mention["numbers"]:
                verified = False
                mention_issues.append("numeric_reference_parse_failed")
                issues.append("numeric_reference_parse_failed")
            if verified:
                verified_mentions += 1
            else:
                unresolved_mentions += 1
            numeric_results.append(
                {
                    "text": mention["text"],
                    "numbers": mention["numbers"],
                    "verified": verified,
                    "resolvedEntries": resolved_entries,
                    "issues": list(dict.fromkeys(mention_issues)),
                }
            )

        if verified_mentions > 0 and unresolved_mentions == 0:
            status = "verified"
            recommended_action = "显式 DOI、作者年份或数字引用都能在项目文献库中对上，可以继续做人工终审。"
        elif verified_mentions > 0:
            status = "partially-verified"
            recommended_action = "部分引用已核验，但仍有未解析或歧义项，建议先人工校对再采用。"
        else:
            status = "unverified"
            recommended_action = "当前引用无法在项目文献库中完成核验，不应直接采用到正文。"

        literature_evidence_ids = {
            (item.get("metadata") or {}).get("itemId") or item.get("sourceId")
            for item in evidence
            if (item.get("source_kind") or item.get("sourceKind")) == "literature"
        }
        return {
            "status": status,
            "verifiedMentionCount": verified_mentions,
            "unresolvedMentionCount": unresolved_mentions,
            "doiMentions": doi_results,
            "authorYearMentions": author_year_results,
            "numericMentions": numeric_results,
            "referenceEntryCount": len(reference_entries),
            "matchedItems": list(matched_items.values()),
            "issues": list(dict.fromkeys(issues)),
            "recommendedAction": recommended_action,
            "evidenceLinkedItemCount": len([item for item in matched_items if item in literature_evidence_ids]),
        }

    def _create_job(self, connection: sqlite3.Connection, project_id: str, job_type: str, detail: str) -> str:
        job_id = new_id("job")
        now = utc_now()
        connection.execute("INSERT INTO jobs (id, project_id, job_type, status, detail, created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?)", (job_id, project_id, job_type, detail, now, now))
        connection.execute("UPDATE projects SET last_job_id = ? WHERE id = ?", (job_id, project_id))
        return job_id

    def _finish_job(self, connection: sqlite3.Connection, job_id: str, status: str, detail: str) -> None:
        connection.execute("UPDATE jobs SET status = ?, detail = ?, updated_at = ? WHERE id = ?", (status, detail, utc_now(), job_id))

    def _effective_comment_section(self, comment: sqlite3.Row) -> str | None:
        return comment["manual_override_section_id"] or comment["mapped_section_id"]

    def _serialize_project(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "type": row["type"],
            "language": row["language"],
            "sourceType": row["source_type"],
            "status": row["status"],
            "progressState": row["progress_state"],
            "nextAction": row["next_action"],
            "overview": row["overview"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "issueCount": row["issue_count"],
            "unresolvedCommentCount": row["unresolved_comment_count"],
            "pendingRevisionCount": row["pending_revision_count"],
            "fileId": row["file_id"],
            "lastJobId": row["last_job_id"],
            "deletedAt": row["deleted_at"] if "deleted_at" in row.keys() else None,
        }

    def _serialize_source_file_status(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "fileId": row["id"],
            "projectId": row["project_id"],
            "fileName": row["file_name"],
            "contentType": row["content_type"] or "application/octet-stream",
            "extension": Path(row["file_name"]).suffix.lower(),
            "parseStatus": row["parse_status"],
            "parseError": row["parse_error"],
        }

    def _serialize_section(self, row: sqlite3.Row, *, source_page: int | None = None) -> dict[str, Any]:
        return {
            "id": row["id"],
            "projectId": row["project_id"],
            "title": row["title"],
            "order": row["order_index"],
            "level": row["level"],
            "currentText": row["current_text"],
            "originalText": row["original_text"],
            "path": row["path"],
            "parseConfidence": row["parse_confidence"],
            "issueCount": row["issue_count"],
            "commentCount": row["comment_count"],
            "revisionState": row["revision_state"],
            "completionState": row["completion_state"],
            "sourcePage": source_page,
        }

    def _serialize_issue(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "projectId": row["project_id"],
            "sectionId": row["section_id"],
            "issueType": row["issue_type"],
            "severity": row["severity"],
            "title": row["title"],
            "detail": row["detail"],
            "suggestedAction": row["suggested_action"],
            "status": row["status"],
        }

    def _serialize_comment(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "projectId": row["project_id"],
            "rawComment": row["raw_comment"],
            "mappedSectionId": row["mapped_section_id"],
            "manualOverrideSectionId": row["manual_override_section_id"],
            "confidence": row["confidence"],
            "suggestedAction": row["suggested_action"],
            "status": row["status"],
            "createdAt": row["created_at"],
        }

    def _serialize_revision(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "projectId": row["project_id"],
            "sectionId": row["section_id"],
            "sourceType": row["source_type"],
            "sourceLabel": row["source_label"],
            "actionType": row["action_type"],
            "summary": row["summary"],
            "oldText": row["old_text"],
            "newText": row["new_text"],
            "promptVersion": row["prompt_version"],
            "model": row["model"],
            "createdAt": row["created_at"],
        }

    def _refresh_project_state(self, connection: sqlite3.Connection, project_id: str) -> None:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            return
        sections = connection.execute("SELECT * FROM sections WHERE project_id = ? ORDER BY order_index ASC", (project_id,)).fetchall()
        issues = connection.execute("SELECT * FROM issues WHERE project_id = ?", (project_id,)).fetchall()
        comments = connection.execute("SELECT * FROM reviewer_comments WHERE project_id = ?", (project_id,)).fetchall()
        revisions = connection.execute("SELECT * FROM revision_events WHERE project_id = ?", (project_id,)).fetchall()
        jobs = connection.execute("SELECT * FROM jobs WHERE project_id = ? ORDER BY updated_at DESC", (project_id,)).fetchall()

        issues_by_section: dict[str, int] = {}
        comments_by_section: dict[str, int] = {}
        revisions_by_section: dict[str, int] = {}
        for issue in issues:
            if issue["status"] == "open":
                issues_by_section[issue["section_id"]] = issues_by_section.get(issue["section_id"], 0) + 1
        for comment in comments:
            if comment["status"] != "done":
                effective_section = self._effective_comment_section(comment)
                if effective_section:
                    comments_by_section[effective_section] = comments_by_section.get(effective_section, 0) + 1
        for revision in revisions:
            revisions_by_section[revision["section_id"]] = revisions_by_section.get(revision["section_id"], 0) + 1

        pending_revision_count = 0
        for section in sections:
            issue_count = issues_by_section.get(section["id"], 0)
            comment_count = comments_by_section.get(section["id"], 0)
            revision_count = revisions_by_section.get(section["id"], 0)
            if issue_count > 0 or comment_count > 0:
                revision_state = "needs-attention"
            elif revision_count > 1:
                revision_state = "updated"
            else:
                revision_state = "clean"
            if issue_count == 0 and comment_count == 0 and revision_count > 0:
                completion_state = "ready"
            elif revision_count > 0 or issue_count > 0 or comment_count > 0:
                completion_state = "in-progress"
            else:
                completion_state = "not-started"
            if completion_state != "ready":
                pending_revision_count += 1
            connection.execute(
                "UPDATE sections SET issue_count = ?, comment_count = ?, revision_state = ?, completion_state = ? WHERE id = ?",
                (issue_count, comment_count, revision_state, completion_state, section["id"]),
            )

        open_issue_count = sum(1 for issue in issues if issue["status"] == "open")
        unresolved_comment_count = sum(1 for comment in comments if comment["status"] != "done")
        running_job = next((job for job in jobs if job["status"] == "running"), None)
        user_or_ai_revisions = sum(1 for revision in revisions if revision["action_type"] != "initial-import")
        language = project["language"]

        if running_job and running_job["job_type"] == "parse":
            status = "parsing"
            progress_state = "editing"
            next_action = "等待文档解析完成。" if language == "zh" else "Wait for document parsing to finish."
        elif not sections:
            status = "uploaded"
            progress_state = "editing"
            next_action = "先上传草稿或粘贴正文。" if language == "zh" else "Upload a draft or paste source text first."
        elif unresolved_comment_count > 0:
            status = "review-pending"
            progress_state = "needs-comments"
            next_comment = next((comment for comment in comments if comment["status"] != "done"), None)
            next_action = next_comment["suggested_action"] if next_comment else ("先消化导师意见，再回到章节编辑。" if language == "zh" else "Resolve reviewer comments before continuing revisions.")
        elif open_issue_count > 0:
            status = "revising"
            progress_state = "active-revision"
            next_issue = next((issue for issue in issues if issue["status"] == "open"), None)
            next_action = next_issue["suggested_action"] if next_issue else ("先处理高优先级诊断问题。" if language == "zh" else "Start with the highest-priority diagnostic issue.")
        else:
            status = "ready"
            progress_state = "editing"
            next_action = "当前文稿已可直接进入编辑器继续改稿。" if language == "zh" else "Open the editor and continue revising the current draft."

        connection.execute(
            """
            UPDATE projects
            SET status = ?, progress_state = ?, next_action = ?, updated_at = ?,
                issue_count = ?, unresolved_comment_count = ?, pending_revision_count = ?
            WHERE id = ?
            """,
            (status, progress_state, next_action, utc_now(), open_issue_count, unresolved_comment_count, pending_revision_count, project_id),
        )

    def _seed_demo_data_if_needed(self, *, owner_user_id: str | None = None) -> None:
        with self._connect() as connection:
            if owner_user_id:
                count = connection.execute("SELECT COUNT(*) FROM projects WHERE owner_user_id = ?", (owner_user_id,)).fetchone()[0]
            else:
                count = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            if count > 0:
                return
            for demo in DEMO_PROJECTS:
                project_id = new_id("project")
                connection.execute(
                    """
                    INSERT INTO projects (
                       id, title, type, language, source_type, status, progress_state,
                       next_action, overview, created_at, updated_at, file_id, last_job_id,
                       issue_count, unresolved_comment_count, pending_revision_count, owner_user_id
                    ) VALUES (?, ?, ?, ?, ?, 'diagnosed', 'active-revision', ?, ?, ?, ?, NULL, NULL, 0, 0, 0, ?)
                    """,
                    (
                        project_id,
                        demo.title,
                        demo.type,
                        demo.language,
                        demo.source_type,
                        "先看高优先级诊断，再决定从哪一章开始改。" if demo.language == "zh" else "Start with the highest-priority diagnostic issue.",
                        demo.overview,
                        demo.created_at,
                        demo.updated_at,
                        owner_user_id,
                    ),
                )
                section_ids: dict[str, str] = {}
                for index, section in enumerate(demo.sections):
                    section_id = new_id("section")
                    section_ids[section.title] = section_id
                    connection.execute(
                        """
                        INSERT INTO sections (
                          id, project_id, title, order_index, level, current_text, original_text,
                          path, parse_confidence, issue_count, comment_count, revision_state, completion_state
                        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 0, 0, 'clean', 'not-started')
                        """,
                        (section_id, project_id, section.title, index, section.text, section.text, str(index + 1), 0.98),
                    )
                    connection.execute(
                        """
                        INSERT INTO revision_events (
                          id, project_id, section_id, source_type, source_label, action_type,
                          summary, old_text, new_text, prompt_version, model, created_at
                        ) VALUES (?, ?, ?, 'system', ?, 'initial-import', ?, '', ?, ?, 'seed', ?)
                        """,
                        (
                            new_id("revision"),
                            project_id,
                            section_id,
                            "系统示例" if demo.language == "zh" else "System demo",
                            "导入示例项目并生成基础章节结构。" if demo.language == "zh" else "Imported the demo project and created the baseline section structure.",
                            section.text,
                            "system/demo-seed@1.0.0",
                            demo.created_at,
                        ),
                    )
                for issue in demo.issues:
                    connection.execute(
                        """
                        INSERT INTO issues (
                          id, project_id, section_id, issue_type, severity, title,
                          detail, suggested_action, status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                        """,
                        (new_id("issue"), project_id, section_ids[issue.section_title], issue.issue_type, issue.severity, issue.title, issue.detail, issue.suggested_action, demo.updated_at),
                    )
                for comment in demo.comments:
                    connection.execute(
                        """
                        INSERT INTO reviewer_comments (
                          id, project_id, raw_comment, mapped_section_id, manual_override_section_id,
                          confidence, suggested_action, status, created_at
                        ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
                        """,
                        (new_id("comment"), project_id, comment.raw_comment, section_ids[comment.mapped_section_title], comment.confidence, comment.suggested_action, comment.status, demo.updated_at),
                    )
                self._refresh_project_state(connection, project_id)
            connection.commit()

    def list_projects(self, scope: str = "active", owner_user_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if scope not in {"active", "trash", "all"}:
                scope = "active"
            where_clauses: list[str] = []
            if scope == "active":
                where_clauses.append("deleted_at IS NULL")
            elif scope == "trash":
                where_clauses.append("deleted_at IS NOT NULL")
            if owner_user_id:
                where_clauses.append("owner_user_id = ?")
            where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            order_clause = "ORDER BY COALESCE(deleted_at, updated_at) DESC, updated_at DESC"
            params: tuple[Any, ...] = (owner_user_id,) if owner_user_id else ()
            project_ids = [
                row["id"]
                for row in connection.execute(f"SELECT id FROM projects {where} {order_clause}", params).fetchall()
            ]
            for project_id in project_ids:
                self._refresh_project_state(connection, project_id)
            rows = connection.execute(f"SELECT * FROM projects {where} {order_clause}", params).fetchall()
            connection.commit()
            return [self._serialize_project(row) for row in rows]

    def get_project_bundle(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            self._maybe_upgrade_sections_from_source(connection, project_id)
            self._refresh_project_state(connection, project_id)
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None or project["deleted_at"]:
                raise KeyError(project_id)
            sections = connection.execute("SELECT * FROM sections WHERE project_id = ? ORDER BY order_index ASC", (project_id,)).fetchall()
            source_file_row = connection.execute(
                """
                SELECT * FROM source_files
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            section_page_map = self._map_section_pages_from_source(source_file_row, sections)
            issues = connection.execute("SELECT * FROM issues WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
            comments = connection.execute("SELECT * FROM reviewer_comments WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
            revisions = connection.execute("SELECT * FROM revision_events WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
            connection.commit()
            return {
                "project": self._serialize_project(project),
                "sections": [self._serialize_section(row, source_page=section_page_map.get(row["id"])) for row in sections],
                "issues": [self._serialize_issue(row) for row in issues],
                "comments": [self._serialize_comment(row) for row in comments],
                "revisions": [self._serialize_revision(row) for row in revisions],
            }

    def get_project_source_file(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            row = connection.execute(
                """
                SELECT * FROM source_files
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if row is None:
                return {"file": None}
            preview = self._build_source_preview_payload(row)
            return {
                "file": {
                    **self._serialize_source_file_status(row),
                    "previewKind": preview_kind_for_file(row["file_name"], row["content_type"]),
                    "contentUrl": f"/api/source-files/{row['id']}/content",
                    "viewerKind": preview["viewerKind"],
                    "previewUrl": preview["previewUrl"],
                    "previewStatus": preview["previewStatus"],
                    "previewMessage": preview["previewMessage"],
                }
            }

    def get_source_file_content(self, file_id: str) -> tuple[Path, str, str]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM source_files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                raise KeyError(file_id)
            try:
                resolved_path = materialize_storage_ref(
                    storage_ref=row["storage_path"],
                    file_name=row["file_name"],
                    temp_dir=UPLOAD_DIR / "_materialized",
                )
            except FileNotFoundError:
                raise KeyError(file_id)
            return resolved_path, row["content_type"] or "application/octet-stream", row["file_name"]

    def get_source_file_preview_content(self, file_id: str) -> tuple[Path, str, str]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM source_files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                raise KeyError(file_id)
            file_kind = preview_kind_for_file(row["file_name"], row["content_type"])
            if file_kind == "pdf":
                return self.get_source_file_content(file_id)
            if file_kind != "docx":
                raise KeyError(file_id)
            preview_path = self._ensure_docx_preview_pdf(row)
            if preview_path is None:
                raise KeyError(file_id)
            return preview_path, "application/pdf", f"{Path(row['file_name']).stem}-preview.pdf"

    def _build_source_preview_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        file_kind = preview_kind_for_file(row["file_name"], row["content_type"])
        if file_kind == "pdf":
            return {
                "viewerKind": "pdf",
                "previewUrl": f"/api/source-files/{row['id']}/preview",
                "previewStatus": "exact",
                "previewMessage": "原始 PDF 页面预览",
            }
        if file_kind == "docx":
            preview_path = self._ensure_docx_preview_pdf(row)
            if preview_path is not None:
                return {
                    "viewerKind": "pdf",
                    "previewUrl": f"/api/source-files/{row['id']}/preview",
                    "previewStatus": "converted",
                    "previewMessage": "DOCX 已转换为分页预览",
                }
            return {
                "viewerKind": "docx",
                "previewUrl": f"/api/source-files/{row['id']}/content",
                "previewStatus": "approximate",
                "previewMessage": "当前环境未检测到 LibreOffice，暂时使用近似 Word 预览。上传 PDF 可获得原页效果。",
            }
        if file_kind == "text":
            return {
                "viewerKind": "text",
                "previewUrl": f"/api/source-files/{row['id']}/content",
                "previewStatus": "plain-text",
                "previewMessage": "文本项目没有原始版式，当前显示纸张式预览。",
            }
        return {
            "viewerKind": "unsupported",
            "previewUrl": f"/api/source-files/{row['id']}/content",
            "previewStatus": "unsupported",
            "previewMessage": "当前文件格式暂不支持原页预览。",
        }

    def _map_section_pages_from_source(
        self,
        source_row: sqlite3.Row | None,
        sections: list[sqlite3.Row],
    ) -> dict[str, int]:
        if source_row is None or not sections or fitz is None:
            return {}
        try:
            preview_path, content_type, _ = self.get_source_file_preview_content(source_row["id"])
        except KeyError:
            return {}
        if content_type != "application/pdf":
            return {}

        def significant_tokens(text: str, *, limit: int) -> list[str]:
            tokens = [
                token.lower()
                for token in re.findall(r"[A-Za-z]{4,}|[\u4e00-\u9fff]{2,}", text)
                if token.lower()
                not in {"section", "study", "based", "using", "analysis", "results"}
            ]
            unique_tokens: list[str] = []
            for token in tokens:
                if token not in unique_tokens:
                    unique_tokens.append(token)
                if len(unique_tokens) >= limit:
                    break
            return unique_tokens

        def score_page(section: sqlite3.Row, page_text: str) -> int:
            title = (section["title"] or "").strip()
            body = normalize_text(section["current_text"] or "")
            lead_lines = [line.strip() for line in body.splitlines() if line.strip()]
            lead_line = compact_text(lead_lines[0]).lower() if lead_lines else ""
            lead_excerpt = compact_text(" ".join(lead_lines[:3]))[:220].lower()
            title_key = normalize_heading_key(title)
            score = 0
            if title_key and title_key in page_text:
                score += 14
            if lead_line and len(lead_line) >= 18 and lead_line in page_text:
                score += 10
            if lead_excerpt and len(lead_excerpt) >= 40 and lead_excerpt in page_text:
                score += 6
            score += 2 * sum(1 for token in significant_tokens(title_key, limit=6) if token in page_text)
            score += sum(1 for token in significant_tokens(lead_excerpt, limit=10) if token in page_text)
            return score

        try:
            document = fitz.open(preview_path)
            try:
                page_texts = [compact_text(normalize_text(page.get_text("text") or "")).lower() for page in document]
            finally:
                document.close()
        except Exception:
            return {}

        if not any(page_texts):
            return {}

        mapped_pages: dict[str, int] = {}
        start_index = 0
        for section in sections:
            best_index: int | None = None
            best_score = 0
            for page_index in range(start_index, len(page_texts)):
                score = score_page(section, page_texts[page_index])
                if score > best_score:
                    best_score = score
                    best_index = page_index
                if score >= 14:
                    break
            if best_index is None or best_score < 4:
                continue
            mapped_pages[section["id"]] = best_index + 1
            start_index = best_index
        return mapped_pages

    def _ensure_docx_preview_pdf(self, row: sqlite3.Row) -> Path | None:
        preview_path = PREVIEW_DIR / f"{row['id']}.pdf"
        if preview_path.exists():
            return preview_path
        soffice_binary = self._resolve_soffice_binary()
        if soffice_binary is None:
            return None
        try:
            source_path = materialize_storage_ref(
                storage_ref=row["storage_path"],
                file_name=row["file_name"],
                temp_dir=UPLOAD_DIR / "_materialized",
            )
        except FileNotFoundError:
            return None
        try:
            subprocess.run(
                [
                    str(soffice_binary),
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(PREVIEW_DIR),
                    str(source_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        generated_path = PREVIEW_DIR / f"{source_path.stem}.pdf"
        if generated_path.exists():
            generated_path.replace(preview_path)
            return preview_path
        return None

    def _resolve_soffice_binary(self) -> Path | None:
        candidates = [
            shutil.which("soffice"),
            shutil.which("soffice.exe"),
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path.exists():
                return path
        return None

    def _maybe_upgrade_sections_from_source(self, connection: sqlite3.Connection, project_id: str) -> None:
        sections = connection.execute(
            "SELECT title, current_text FROM sections WHERE project_id = ? ORDER BY order_index ASC",
            (project_id,),
        ).fetchall()
        if not sections:
            return
        generic_count = sum(1 for row in sections if GENERIC_SECTION_TITLE_RE.fullmatch((row["title"] or "").strip()))
        known_count = sum(
            1
            for row in sections
            if (row["title"] or "").strip() in set(KNOWN_SECTION_TITLES["zh"].values()) | set(KNOWN_SECTION_TITLES["en"].values())
        )
        suspicious_count = sum(
            1
            for row in sections
            if re.search(r"\d", (row["title"] or "").strip()) or len((row["title"] or "").strip()) > 28 or "," in (row["title"] or "")
        )
        normalized_titles = {normalize_heading_key((row["title"] or "").strip()) for row in sections if (row["title"] or "").strip()}
        hidden_heading_count = sum(
            1
            for row in sections
            if re.search(r"\b(?:[IVXLC]+\.\s+)?(INTRODUCTION|METHODOLOGY|METHODS|DISCUSSION|CONCLUSION|REFERENCES)\b", row["current_text"] or "", re.I)
        )
        structure_suspect = not any(key in normalized_titles for key in ("introduction", "methodology", "methods")) and hidden_heading_count > 0
        noisy_text_count = sum(
            1
            for row in sections
            if any(marker in (row["current_text"] or "") for marker in ("¦Ě", "Âμ", "Î¼", "芒聙聰", "盲聙聰"))
        )
        noisy_text_count += sum(
            1
            for row in sections
            if re.search(r"[\u25a1\u25a0\uFFFD]", row["current_text"] or "")
        )
        noisy_text_count += sum(1 for row in sections if "脦录" in (row["current_text"] or ""))
        if generic_count == 0 and not (known_count < 2 and suspicious_count >= max(2, len(sections) // 3)) and noisy_text_count == 0 and not structure_suspect:
            return
        source_row = connection.execute(
            """
            SELECT * FROM source_files
            WHERE project_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if source_row is None:
            return
        try:
            source_path = materialize_storage_ref(
                storage_ref=source_row["storage_path"],
                file_name=source_row["file_name"],
                temp_dir=UPLOAD_DIR / "_materialized",
            )
        except FileNotFoundError:
            return
        parsed_text, parse_confidence, parse_error = self._parse_source_bytes(
            file_name=source_row["file_name"],
            raw_bytes=source_path.read_bytes(),
            fallback_text="",
        )
        if not normalize_text(parsed_text):
            return
        source_label = f"文件导入：{source_row['file_name']}" if is_chinese(parsed_text) else f"File import: {source_row['file_name']}"
        self._replace_sections_with_text(
            connection,
            project_id=project_id,
            text=normalize_text(parsed_text),
            source_label=f"File import: {source_row['file_name']}",
            parse_confidence=parse_confidence,
        )
        connection.execute(
            "UPDATE source_files SET parse_status = ?, parse_error = ? WHERE id = ?",
            ("parsed" if not parse_error else "fallback", parse_error or None, source_row["id"]),
        )

    def delete_project(self, project_id: str, *, permanent: bool = False) -> dict[str, Any]:
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            if not permanent:
                now = utc_now()
                connection.execute(
                    "UPDATE projects SET deleted_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, project_id),
                )
                connection.commit()
                return {"deletedProjectId": project_id, "permanent": False}
            files = connection.execute("SELECT storage_path FROM source_files WHERE project_id = ?", (project_id,)).fetchall()
            existing_tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            if "literature_items" in existing_tables and "literature_attachments" in existing_tables:
                literature_rows = connection.execute("SELECT id FROM literature_items WHERE project_id = ?", (project_id,)).fetchall()
                literature_ids = [row["id"] for row in literature_rows]
                if literature_ids:
                    placeholders = ",".join("?" for _ in literature_ids)
                    connection.execute(f"DELETE FROM literature_attachments WHERE item_id IN ({placeholders})", literature_ids)
            for table in [
                "source_files",
                "sections",
                "issues",
                "reviewer_comments",
                "revision_candidates",
                "revision_requests",
                "revision_events",
                "retrieval_logs",
                "llm_runs",
                "jobs",
                "literature_search_runs",
                "literature_items",
                "literature_collections",
                "zotero_sync_events",
            ]:
                if table in existing_tables:
                    connection.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            connection.commit()

        for row in files:
            try:
                if is_remote_storage_ref(row["storage_path"] or ""):
                    continue
                path = Path(row["storage_path"])
                if path.exists() and UPLOAD_DIR.resolve() in path.resolve().parents:
                    path.unlink()
            except OSError:
                pass
        return {"deletedProjectId": project_id, "permanent": True}

    def restore_project(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            connection.execute(
                "UPDATE projects SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), project_id),
            )
            connection.commit()
        return {"restoredProjectId": project_id}

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return {"id": row["id"], "projectId": row["project_id"], "jobType": row["job_type"], "status": row["status"], "detail": row["detail"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    def _replace_sections_with_text(self, connection: sqlite3.Connection, *, project_id: str, text: str, source_label: str, parse_confidence: float) -> None:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            raise KeyError(project_id)
        sections = split_into_sections(text, project["language"])
        connection.execute("DELETE FROM sections WHERE project_id = ?", (project_id,))
        connection.execute("DELETE FROM issues WHERE project_id = ?", (project_id,))
        connection.execute("DELETE FROM revision_events WHERE project_id = ? AND action_type = 'initial-import'", (project_id,))
        for index, section in enumerate(sections):
            section_id = new_id("section")
            connection.execute(
                """
                INSERT INTO sections (
                  id, project_id, title, order_index, level, current_text, original_text,
                  path, parse_confidence, issue_count, comment_count, revision_state, completion_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'clean', 'not-started')
                """,
                (
                    section_id,
                    project_id,
                    section["title"],
                    index,
                    section.get("level", 1),
                    section["text"],
                    section["text"],
                    section["path"],
                    section.get("parse_confidence", parse_confidence),
                ),
            )
            connection.execute(
                """
                INSERT INTO revision_events (
                  id, project_id, section_id, source_type, source_label, action_type,
                  summary, old_text, new_text, prompt_version, model, created_at
                ) VALUES (?, ?, ?, 'system', ?, 'initial-import', ?, '', ?, 'system/ingest@1.0.0', 'parser', ?)
                """,
                (new_id("revision"), project_id, section_id, source_label, "导入新草稿并生成章节结构。" if project["language"] == "zh" else "Imported the draft and generated a section scaffold.", section["text"], utc_now()),
            )

    def _parse_source_bytes(self, *, file_name: str, raw_bytes: bytes, fallback_text: str) -> tuple[str, float, str]:
        extension = Path(file_name).suffix.lower()
        parsed_text = ""
        parse_confidence = 0.86
        parse_error = ""
        try:
            if extension == ".txt":
                parsed_text = raw_bytes.decode("utf-8", errors="ignore")
                parse_confidence = 0.98
            elif extension in {".doc", ".docx"}:
                parsed_text = extract_text_from_docx(raw_bytes)
                parse_confidence = 0.88
            elif extension == ".pdf":
                parsed_text = extract_text_from_pdf(raw_bytes)
                parse_confidence = 0.8
            else:
                raise ValueError("Unsupported file type")
        except Exception as exc:
            parse_error = str(exc)
            parsed_text = fallback_text.strip()
            parse_confidence = 0.52 if parsed_text else 0.0
        return parsed_text, parse_confidence, parse_error

    def create_project(
        self,
        *,
        title: str,
        doc_type: str,
        language: str,
        source_type: str,
        note: str,
        text: str,
        owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        project_id = new_id("project")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects (
                  id, title, type, language, source_type, status, progress_state, next_action,
                  overview, created_at, updated_at, file_id, last_job_id, issue_count,
                  unresolved_comment_count, pending_revision_count, owner_user_id
                ) VALUES (?, ?, ?, ?, ?, 'uploaded', 'editing', ?, ?, ?, ?, NULL, NULL, 0, 0, 0, ?)
                """,
                (project_id, title.strip(), doc_type, language, source_type if source_type in {"demo", "text", "file"} else "text", "先上传草稿或粘贴正文。" if language == "zh" else "Upload a draft or paste source text first.", note.strip() or ("新项目已创建，等待解析与编辑。" if language == "zh" else "New project created and waiting for parsing."), now, now),
            )
            connection.commit()
        if text.strip():
            self.ingest_plain_text(project_id=project_id, text=text, source_label="粘贴文本" if language == "zh" else "Pasted text")
        return self.get_project_bundle(project_id)

    def ingest_plain_text(self, *, project_id: str, text: str, source_label: str) -> dict[str, Any]:
        with self._connect() as connection:
            job_id = self._create_job(connection, project_id, "parse", "正在解析粘贴文本。")
            self._replace_sections_with_text(connection, project_id=project_id, text=normalize_text(text), source_label=source_label, parse_confidence=0.94)
            self._finish_job(connection, job_id, "completed", "文本解析完成。")
            self._refresh_project_state(connection, project_id)
            connection.commit()
        return self.get_project_bundle(project_id)

    def create_project(
        self,
        *,
        title: str,
        doc_type: str,
        language: str,
        source_type: str,
        note: str,
        text: str,
        owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        project_id = new_id("project")
        now = utc_now()
        normalized_source_type = source_type if source_type in {"demo", "text", "file"} else "text"
        next_action = "先上传草稿或粘贴正文。" if language == "zh" else "Upload a draft or paste source text first."
        overview = note.strip() or ("新项目已创建，等待解析与编辑。" if language == "zh" else "New project created and waiting for parsing.")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects (
                  id, title, type, language, source_type, status, progress_state, next_action,
                  overview, created_at, updated_at, file_id, last_job_id, issue_count,
                  unresolved_comment_count, pending_revision_count, owner_user_id
                ) VALUES (?, ?, ?, ?, ?, 'uploaded', 'editing', ?, ?, ?, ?, NULL, NULL, 0, 0, 0, ?)
                """,
                (
                    project_id,
                    title.strip(),
                    doc_type,
                    language,
                    normalized_source_type,
                    next_action,
                    overview,
                    now,
                    now,
                    owner_user_id,
                ),
            )
            connection.commit()
        if text.strip():
            self.ingest_plain_text(
                project_id=project_id,
                text=text,
                source_label="粘贴文本" if language == "zh" else "Pasted text",
            )
        return self.get_project_bundle(project_id)

    def upload_file(self, *, project_id: str, file_name: str, content_type: str, raw_bytes: bytes, fallback_text: str) -> dict[str, Any]:
        extension = Path(file_name).suffix.lower()
        storage_name = f"{uuid.uuid4().hex[:12]}{extension}"
        storage_path = UPLOAD_DIR / storage_name
        storage_ref = ""
        if blob_enabled():
            try:
                blob_info = upload_bytes_to_blob(
                    pathname=f"draftrefine/source-files/{storage_name}",
                    body=raw_bytes,
                    content_type=content_type or "application/octet-stream",
                )
                storage_ref = blob_info["url"]
            except Exception:
                storage_path.write_bytes(raw_bytes)
                storage_ref = str(storage_path)
        else:
            storage_path.write_bytes(raw_bytes)
            storage_ref = str(storage_path)

        return self.ingest_uploaded_file(
            project_id=project_id,
            storage_ref=storage_ref,
            file_name=file_name,
            content_type=content_type,
            fallback_text=fallback_text,
            raw_bytes=raw_bytes,
        )

    def ingest_uploaded_file(
        self,
        *,
        project_id: str,
        storage_ref: str,
        file_name: str,
        content_type: str,
        fallback_text: str,
        raw_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        extension = Path(file_name).suffix.lower()
        if extension not in {".txt", ".doc", ".docx", ".pdf"}:
            raise ValueError("Unsupported file type")
        if raw_bytes is None:
            source_path = materialize_storage_ref(
                storage_ref=storage_ref,
                file_name=file_name,
                temp_dir=UPLOAD_DIR / "_materialized",
            )
            raw_bytes = source_path.read_bytes()

        with self._connect() as connection:
            file_id = new_id("file")
            connection.execute(
                """
                INSERT INTO source_files (
                  id, project_id, file_name, content_type, storage_path, parse_status, parse_error, created_at
                ) VALUES (?, ?, ?, ?, ?, 'stored', NULL, ?)
                """,
                (file_id, project_id, file_name, content_type, storage_ref, utc_now()),
            )
            connection.execute("UPDATE projects SET file_id = ?, source_type = 'file' WHERE id = ?", (file_id, project_id))
            job_id = self._create_job(connection, project_id, "parse", f"正在解析文件 {file_name}")
            parsed_text = ""
            parse_confidence = 0.86
            parse_error = ""
            try:
                if extension == ".txt":
                    parsed_text = raw_bytes.decode("utf-8", errors="ignore")
                    parse_confidence = 0.98
                elif extension in {".doc", ".docx"}:
                    parsed_text = extract_text_from_docx(raw_bytes)
                    parse_confidence = 0.88
                elif extension == ".pdf":
                    parsed_text = extract_text_from_pdf(raw_bytes)
                    parse_confidence = 0.80
                else:
                    raise ValueError("Unsupported file type")
            except Exception as exc:
                parse_error = str(exc)
                parsed_text = fallback_text.strip()
                parse_confidence = 0.52 if parsed_text else 0.0

            if not normalize_text(parsed_text):
                connection.execute("UPDATE source_files SET parse_status = 'failed', parse_error = ? WHERE id = ?", (parse_error or "No text extracted", file_id))
                self._finish_job(connection, job_id, "failed", "未提取到可用文本，请粘贴正文继续。")
                self._refresh_project_state(connection, project_id)
                source_file_row = connection.execute("SELECT * FROM source_files WHERE id = ?", (file_id,)).fetchone()
                connection.commit()
                bundle = self.get_project_bundle(project_id)
                if source_file_row is not None:
                    bundle["uploadFile"] = self._serialize_source_file_status(source_file_row)
                return bundle

            self._replace_sections_with_text(connection, project_id=project_id, text=normalize_text(parsed_text), source_label=f"文件导入：{file_name}" if is_chinese(parsed_text) else f"File import: {file_name}", parse_confidence=parse_confidence)
            connection.execute("UPDATE source_files SET parse_status = ?, parse_error = ? WHERE id = ?", ("parsed" if not parse_error else "fallback", parse_error or None, file_id))
            self._finish_job(connection, job_id, "completed", "文件解析完成。" if not parse_error else "文件解析部分失败，已回退到粘贴文本。")
            self._refresh_project_state(connection, project_id)
            source_file_row = connection.execute("SELECT * FROM source_files WHERE id = ?", (file_id,)).fetchone()
            connection.commit()
        bundle = self.get_project_bundle(project_id)
        if source_file_row is not None:
            bundle["uploadFile"] = self._serialize_source_file_status(source_file_row)
        return bundle

    def diagnose_project(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            sections_rows = connection.execute("SELECT * FROM sections WHERE project_id = ? ORDER BY order_index ASC", (project_id,)).fetchall()
            if not sections_rows:
                raise ValueError("Project has no sections to diagnose.")
            sections = [self._serialize_section(row) for row in sections_rows]
            diagnose_sections = []
            for section in sections:
                normalized_text = normalize_text(section["currentText"] or "")
                excerpt = normalized_text[:1200]
                diagnose_sections.append(
                    {
                        "id": section["id"],
                        "title": section["title"],
                        "level": section["level"],
                        "textLength": len(normalized_text),
                        "excerpt": excerpt,
                    }
                )
            prompt = self._load_prompt("diagnose", project["language"], "diagnose")
            schema_hint = prompt.get("schema_hint") or json.dumps([{"section_id": "section-xxx", "issue_type": "structure", "severity": "high", "title": "issue title", "detail": "why it matters", "suggested_action": "next concrete step"}], ensure_ascii=False, indent=2)
            model_output, run_meta = self._call_provider_json(
                action_name="diagnose",
                prompt_version=prompt["version_tag"],
                system_prompt=prompt["system_prompt"],
                input_payload={"projectTitle": project["title"], "language": project["language"], "sections": diagnose_sections},
                schema_hint=schema_hint,
            )
            section_ids = {section["id"] for section in sections}
            if isinstance(model_output, list) and model_output:
                issues = [issue for issue in model_output if isinstance(issue, dict) and issue.get("section_id") in section_ids]
            else:
                issues = []
            if not issues:
                issues = heuristic_diagnostics(project["language"], sections)
            connection.execute("DELETE FROM issues WHERE project_id = ?", (project_id,))
            job_id = self._create_job(connection, project_id, "diagnose", "正在生成诊断报告。")
            for issue in issues:
                connection.execute(
                    """
                    INSERT INTO issues (
                      id, project_id, section_id, issue_type, severity, title,
                      detail, suggested_action, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (new_id("issue"), project_id, issue["section_id"], issue["issue_type"], issue["severity"], issue["title"], issue["detail"], issue["suggested_action"], utc_now()),
                )
            self._insert_llm_run(connection, project_id=project_id, section_id=None, action_name="diagnose", prompt_version=prompt["version_tag"], provider=run_meta["provider"], model=run_meta["model"], status=run_meta["status"], latency_ms=run_meta["latency_ms"], input_text=json.dumps(diagnose_sections, ensure_ascii=False), output_text=json.dumps(issues, ensure_ascii=False), error=run_meta["error"])
            self._finish_job(connection, job_id, "completed", "诊断报告已更新。")
            self._refresh_project_state(connection, project_id)
            connection.commit()
        return self.get_project_bundle(project_id)

    def _run_revision_agent(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str | None,
        section_id: str | None,
        title: str,
        language: str,
        action_type: str,
        source_type: str,
        base_text: str,
        target_text: str,
        selected_text: str,
        note: str,
        comment_id: str | None,
        comment_context: str,
        previous_candidate_text: str,
        model_profile: str = "normal",
    ) -> dict[str, Any]:
        request_id = self._start_revision_request(
            connection,
            project_id=project_id,
            section_id=section_id,
            action_type=action_type,
            source_type=source_type,
            input_text=base_text,
            note=note,
            selected_text=selected_text,
            comment_id=comment_id,
        )
        graph = RevisionGraph(
            state=RevisionState(
                request_id=request_id,
                project_id=project_id,
                section_id=section_id,
                title=title,
                language=language,
                action_type=action_type,
                base_text=base_text,
                target_text=target_text,
                selected_text=selected_text,
                note=note,
                comment_context=comment_context,
                previous_candidate_text=previous_candidate_text,
            ),
            retrieve=lambda query_text, _: self._collect_revision_evidence(
                connection,
                project_id=project_id,
                section_id=section_id,
                title=title,
                query_text=query_text,
                action_type=action_type,
                comment_context=comment_context,
            ),
            interpret_call=lambda action_name, payload, schema_hint: self._run_prompt_json(
                category="intent",
                language=language,
                action_name=action_name,
                input_payload=payload,
                schema_hint=schema_hint,
                model_profile=model_profile,
            ),
            writer_call=lambda action_name, payload, schema_hint: self._run_prompt_json(
                category="rewrite",
                language=language,
                action_name=action_name,
                input_payload=payload,
                schema_hint=schema_hint,
                model_profile=model_profile,
            ),
            review_call=lambda action_name, payload, schema_hint: self._run_prompt_json(
                category="review",
                language=language,
                action_name=action_name,
                input_payload=payload,
                schema_hint=schema_hint,
                model_profile=model_profile,
            ),
            heuristic_rewrite=heuristic_rewrite,
        )
        def persist_step_runs(step_runs: list[dict[str, Any]]) -> None:
            for step in step_runs:
                self._insert_llm_run(
                    connection,
                    project_id=project_id,
                    section_id=section_id,
                    action_name=step["action_name"],
                    prompt_version=step["prompt_version"],
                    provider=step["provider"],
                    model=step["model"],
                    status=step["status"],
                    latency_ms=step["latency_ms"],
                    input_text=step["input_text"],
                    output_text=step["output_text"],
                    error=step["error"],
                )

        try:
            result = graph.run()
        except ModelInvocationError as exc:
            trace = graph._trace_payload()
            trace["error"] = str(exc)
            trace["failedAttempts"] = exc.attempts
            persist_step_runs(trace["stepRuns"])
            persist_step_runs(exc.attempts)
            self._finish_revision_request(
                connection,
                request_id=request_id,
                status="failed",
                result_summary=str(exc),
                state=trace,
            )
            connection.commit()
            raise
        except RuntimeError as exc:
            trace = graph._trace_payload()
            trace["error"] = str(exc)
            persist_step_runs(trace["stepRuns"])
            self._finish_revision_request(
                connection,
                request_id=request_id,
                status="failed",
                result_summary=str(exc),
                state=trace,
            )
            connection.commit()
            raise

        evidence_strategy = self._build_evidence_strategy(
            connection,
            project_id=project_id,
            title=title,
            base_text=base_text,
            action_type=result["agentTrace"].get("effectiveActionType") or action_type,
            language=language,
            note=((result["agentTrace"].get("instructionPlan") or {}).get("normalizedInstruction") or note),
            comment_context=comment_context,
            evidence=result["evidence"],
        )
        citation_audit = self._build_citation_audit(
            original_text=target_text,
            candidate_text=result["candidateTargetText"],
            evidence=result["evidence"],
            evidence_strategy=evidence_strategy,
        )
        citation_verification = self._build_citation_verification(
            connection,
            project_id=project_id,
            candidate_text=result["candidateTargetText"],
            evidence=result["evidence"],
        )
        result["evidenceStrategy"] = evidence_strategy
        result["citationAudit"] = citation_audit
        result["citationVerification"] = citation_verification
        result["agentTrace"]["evidenceStrategy"] = evidence_strategy
        result["agentTrace"]["citationAudit"] = citation_audit
        result["agentTrace"]["citationVerification"] = citation_verification
        self._insert_retrieval_logs(
            connection,
            request_id=request_id,
            project_id=project_id,
            section_id=section_id,
            evidence=result["evidence"],
        )
        persist_step_runs(result["stepRuns"])
        self._finish_revision_request(
            connection,
            request_id=request_id,
            status="completed",
            result_summary=result["summary"],
            state=result["agentTrace"],
        )
        writer_step = next((step for step in reversed(result["stepRuns"]) if step["step"] == "writer"), None)
        result["requestId"] = request_id
        result["promptVersion"] = writer_step["prompt_version"] if writer_step else "rewrite/unknown"
        result["model"] = writer_step["model"] if writer_step else "unknown"
        result["provider"] = writer_step["provider"] if writer_step else "unknown"
        result["revisionMode"] = "pro" if model_profile == "pro" else "normal"
        result["agentTrace"]["revisionMode"] = result["revisionMode"]
        return result

    def revise_text(
        self,
        *,
        text: str,
        action_type: str,
        mode: str = "normal",
        project_id: str | None = None,
        title: str = "",
        note: str = "",
        selected_text: str = "",
        selection_start: int | None = None,
        selection_end: int | None = None,
        comment_id: str | None = None,
        previous_candidate_text: str = "",
    ) -> dict[str, Any]:
        if not normalize_text(text):
            raise ValueError("Text input cannot be empty.")
        if action_type == "custom-instruction" and not normalize_text(note):
            raise ValueError("Custom instruction cannot be empty.")
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone() if project_id else None
            if project_id and project is None:
                raise KeyError(project_id)
            matched_span = locate_selected_text(text, selected_text, selection_start, selection_end) if selected_text.strip() else None
            if selected_text.strip() and matched_span is None:
                raise ValueError("Selected text could not be matched in the provided input.")
            target_text = text[matched_span[0] : matched_span[1]] if matched_span else text
            model_profile = "pro" if mode == "pro" else "normal"
            project_language = project["language"] if project is not None else ("zh" if is_chinese(text) else "en")
            language = detect_revision_language(target_text or text, fallback=project_language)
            effective_title = title.strip() or (project["title"] if project is not None else ("未命名输入" if language == "zh" else "Untitled input"))
            comment_context = ""
            if comment_id:
                row = connection.execute(
                    "SELECT raw_comment FROM reviewer_comments WHERE id = ? AND project_id = ?",
                    (comment_id, project_id),
                ).fetchone()
                if row is None:
                    raise KeyError(comment_id)
                comment_context = row["raw_comment"]
            result = self._run_revision_agent(
                connection,
                project_id=project_id,
                section_id=None,
                title=effective_title,
                language=language,
                action_type=action_type,
                source_type="text-input",
                base_text=text,
                target_text=target_text,
                selected_text=selected_text.strip(),
                note=note.strip(),
                comment_id=comment_id,
                comment_context=comment_context,
                previous_candidate_text=previous_candidate_text.strip(),
                model_profile=model_profile,
            )
            candidate_text = (
                f"{text[:matched_span[0]]}{result['candidateTargetText']}{text[matched_span[1]:]}"
                if matched_span
                else result["candidateTargetText"]
            )
            if matched_span:
                diff_hints = [
                    {"side": "left", "start": matched_span[0], "end": matched_span[1], "kind": "changed"},
                    {
                        "side": "right",
                        "start": matched_span[0],
                        "end": matched_span[0] + len(result["candidateTargetText"]),
                        "kind": "changed",
                    },
                ]
            else:
                left_start, left_end, right_start, right_end = first_diff_span(text, candidate_text)
                diff_hints = [
                    {"side": "left", "start": left_start, "end": left_end, "kind": "changed"},
                    {"side": "right", "start": right_start, "end": right_end, "kind": "changed"},
                ]
            connection.commit()
            return {
                "requestId": result["requestId"],
                "projectId": project_id,
                "text": candidate_text,
                "summary": result["summary"],
                "actionType": action_type,
                "promptVersion": result["promptVersion"],
                "model": result["model"],
                "provider": result["provider"],
                "warnings": result["warnings"],
                "baseText": text,
                "selectedText": selected_text.strip(),
                "replacementText": result["candidateTargetText"],
                "selectionStart": matched_span[0] if matched_span else None,
                "selectionEnd": matched_span[1] if matched_span else None,
                "commentId": comment_id,
                "diffHints": diff_hints,
                "plan": result["plan"],
                "review": result["review"],
                "evidence": result["evidence"],
                "evidenceStrategy": result["evidenceStrategy"],
                "citationAudit": result["citationAudit"],
                "citationVerification": result["citationVerification"],
                "agentTrace": result["agentTrace"],
                "revisionMode": result.get("revisionMode", model_profile),
            }

    def verify_citations(self, *, project_id: str, text: str) -> dict[str, Any]:
        if not normalize_text(text):
            raise ValueError("Text input cannot be empty.")
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            evidence = self._collect_revision_evidence(
                connection,
                project_id=project_id,
                section_id=None,
                title=project["title"],
                query_text=text,
                action_type="academic-rewrite",
                comment_context="",
            )
            verification = self._build_citation_verification(
                connection,
                project_id=project_id,
                candidate_text=text,
                evidence=evidence,
            )
            audit = self._build_citation_audit(
                original_text="",
                candidate_text=text,
                evidence=evidence,
                evidence_strategy={
                    "needsImportedEvidence": True,
                    "importedDoiCount": sum(1 for item in verification["matchedItems"] if item.get("doi")),
                },
            )
            return {
                "projectId": project_id,
                "text": text,
                "evidence": evidence,
                "citationAudit": audit,
                "citationVerification": verification,
            }

    def format_citations(
        self,
        *,
        project_id: str,
        style: str = "gb7714",
        item_ids: list[str] | None = None,
        text: str = "",
        matched_only: bool = True,
    ) -> dict[str, Any]:
        style_key = style.strip().lower().replace(" ", "").replace("-", "")
        style_aliases = {
            "gb": "gb7714",
            "gb7714": "gb7714",
            "gbt7714": "gb7714",
            "gb/t7714": "gb7714",
            "apa": "apa",
            "ieee": "ieee",
        }
        style_key = style_aliases.get(style_key, style_key)
        if style_key not in {"gb7714", "apa", "ieee"}:
            raise ValueError("Unsupported citation style. Use gb7714, apa, or ieee.")

        requested_ids = [item_id for item_id in (item_ids or []) if normalize_text(item_id)]
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)

            has_literature = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'literature_items'"
            ).fetchone()
            if not has_literature:
                return {
                    "projectId": project_id,
                    "style": style_key,
                    "matchedOnly": matched_only,
                    "entries": [],
                    "bibliographyText": "",
                    "warnings": ["Project literature table is not initialized."],
                }

            selected_ids = requested_ids
            verification: dict[str, Any] | None = None
            if not selected_ids and matched_only and normalize_text(text):
                verification = self._build_citation_verification(
                    connection,
                    project_id=project_id,
                    candidate_text=text,
                    evidence=[],
                )
                selected_ids = [
                    item["itemId"]
                    for item in verification.get("matchedItems", [])
                    if normalize_text(item.get("itemId") or "")
                ]

            params: list[Any] = [project_id]
            where = "project_id = ?"
            if selected_ids:
                placeholders = ",".join("?" for _ in selected_ids)
                where += f" AND id IN ({placeholders})"
                params.extend(selected_ids)
            elif matched_only:
                return {
                    "projectId": project_id,
                    "style": style_key,
                    "matchedOnly": True,
                    "entries": [],
                    "bibliographyText": "",
                    "citationVerification": verification,
                    "warnings": ["No imported literature matched the provided text."],
                }

            rows = connection.execute(
                f"""
                SELECT id, title, authors_json, year, venue, doi, url, zotero_item_key
                FROM literature_items
                WHERE {where}
                ORDER BY updated_at DESC, title ASC
                """,
                params,
            ).fetchall()

            row_map = {row["id"]: row for row in rows}
            ordered_rows = [row_map[item_id] for item_id in selected_ids if item_id in row_map] if selected_ids else rows
            entries = []
            for index, row in enumerate(ordered_rows, start=1):
                try:
                    authors = json.loads(row["authors_json"]) if row["authors_json"] else []
                except json.JSONDecodeError:
                    authors = []
                item = {
                    "id": row["id"],
                    "title": row["title"],
                    "authors": authors,
                    "year": row["year"],
                    "venue": row["venue"] or "",
                    "doi": row["doi"] or "",
                    "url": row["url"] or "",
                    "zoteroItemKey": row["zotero_item_key"] or "",
                }
                entries.append(
                    {
                        "itemId": row["id"],
                        "style": style_key,
                        "formattedText": format_literature_reference(item, style_key, index),
                        "doi": row["doi"] or "",
                        "zoteroItemKey": row["zotero_item_key"] or "",
                    }
                )

            return {
                "projectId": project_id,
                "style": style_key,
                "matchedOnly": matched_only,
                "entries": entries,
                "bibliographyText": "\n".join(entry["formattedText"] for entry in entries),
                "citationVerification": verification,
                "warnings": [] if entries else ["No literature items were available for formatting."],
            }

    def scout_revision_literature(
        self,
        *,
        project_id: str,
        text: str,
        action_type: str,
        title: str = "",
        note: str = "",
        comment_id: str | None = None,
        sources: list[str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        if not normalize_text(text):
            raise ValueError("Text input cannot be empty.")
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            project_language = project["language"] or ("zh" if is_chinese(text) else "en")
            language = detect_revision_language(text, fallback=project_language)
            effective_title = title.strip() or project["title"] or ("未命名输入" if language == "zh" else "Untitled input")
            comment_context = ""
            if comment_id:
                row = connection.execute(
                    "SELECT raw_comment FROM reviewer_comments WHERE id = ? AND project_id = ?",
                    (comment_id, project_id),
                ).fetchone()
                if row is None:
                    raise KeyError(comment_id)
                comment_context = row["raw_comment"]
            existing_evidence = self._collect_revision_evidence(
                connection,
                project_id=project_id,
                section_id=None,
                title=effective_title,
                query_text=text,
                action_type=action_type,
                comment_context=comment_context,
            )
            evidence_strategy = self._build_evidence_strategy(
                connection,
                project_id=project_id,
                title=effective_title,
                base_text=text,
                action_type=action_type,
                language=language,
                note=note.strip(),
                comment_context=comment_context,
                evidence=existing_evidence,
            )
            citation_audit = self._build_citation_audit(
                original_text=text,
                candidate_text=text,
                evidence=existing_evidence,
                evidence_strategy=evidence_strategy,
            )
        from .literature import LiteratureService

        literature_service = LiteratureService(database_path=self.database_path)
        scout_query = evidence_strategy["recommendedQuery"] or normalize_text(effective_title)
        live_search = literature_service.search_literature(
            project_id=project_id,
            query=scout_query,
            sources=sources,
            limit=limit,
        )
        return {
            "projectId": project_id,
            "actionType": action_type,
            "title": effective_title,
            "language": language,
            "textPreview": summarize_evidence_excerpt(text, 220),
            "existingEvidence": existing_evidence,
            "evidenceStrategy": evidence_strategy,
            "citationAudit": citation_audit,
            "search": live_search,
            "confirmBeforeUse": {
                "mustImportBeforeUse": True,
                "reason": "联网检索结果默认只是候选证据，只有导入项目文献库后才会进入改稿检索链。",
                "eligibleForRevisionNow": False,
                "importEndpoint": f"/api/projects/{project_id}/literature/import",
                "rerunRevisionAfterImport": True,
                "suggestedSourceOrder": evidence_strategy["sourceOrder"],
            },
        }

    def request_revision(
        self,
        *,
        section_id: str,
        action_type: str,
        current_text: str,
        comment_ids: list[str],
        selected_text: str = "",
        selection_start: int | None = None,
        selection_end: int | None = None,
        comment_id: str | None = None,
        feedback: str = "",
        previous_candidate_text: str = "",
    ) -> dict[str, Any]:
        if action_type == "custom-instruction" and not normalize_text(feedback):
            raise ValueError("Custom instruction cannot be empty.")
        with self._connect() as connection:
            section = connection.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
            if section is None:
                raise KeyError(section_id)
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (section["project_id"],)).fetchone()
            base_text = current_text if current_text.strip() else section["current_text"]
            effective_comment_id = comment_id or (comment_ids[0] if len(comment_ids) == 1 else None)
            comment_context = ""
            if effective_comment_id:
                row = connection.execute(
                    "SELECT raw_comment FROM reviewer_comments WHERE id = ? AND project_id = ?",
                    (effective_comment_id, project["id"]),
                ).fetchone()
                if row is None:
                    raise KeyError(effective_comment_id)
                comment_context = row["raw_comment"]
            elif comment_ids:
                placeholders = ",".join("?" for _ in comment_ids)
                rows = connection.execute(f"SELECT raw_comment FROM reviewer_comments WHERE id IN ({placeholders})", tuple(comment_ids)).fetchall()
                comment_context = "\n".join(row["raw_comment"] for row in rows)

            matched_span = locate_selected_text(base_text, selected_text, selection_start, selection_end) if selected_text.strip() else None
            if selected_text.strip() and matched_span is None:
                raise ValueError("Selected text could not be matched in the current section. Please select a complete sentence or paragraph.")
            target_text = base_text[matched_span[0] : matched_span[1]] if matched_span else base_text
            result = self._run_revision_agent(
                connection,
                project_id=project["id"],
                section_id=section_id,
                title=section["title"],
                language=detect_revision_language(target_text or base_text, fallback=project["language"]),
                action_type=action_type,
                source_type="section-revision",
                base_text=base_text,
                target_text=target_text,
                selected_text=selected_text.strip(),
                note=feedback.strip(),
                comment_id=effective_comment_id,
                comment_context=comment_context,
                previous_candidate_text=previous_candidate_text.strip(),
            )
            replacement_text = result["candidateTargetText"]
            if matched_span:
                candidate_text = f"{base_text[:matched_span[0]]}{replacement_text}{base_text[matched_span[1]:]}"
                right_start = matched_span[0]
                right_end = matched_span[0] + len(replacement_text)
                diff_hints = [
                    {"side": "left", "start": matched_span[0], "end": matched_span[1], "kind": "changed"},
                    {"side": "right", "start": right_start, "end": right_end, "kind": "changed"},
                ]
            else:
                candidate_text = replacement_text
                left_start, left_end, right_start, right_end = first_diff_span(base_text, candidate_text)
                diff_hints = [
                    {"side": "left", "start": left_start, "end": left_end, "kind": "changed"},
                    {"side": "right", "start": right_start, "end": right_end, "kind": "changed"},
                ]
            candidate_id = new_id("candidate")
            connection.execute(
                """
                INSERT INTO revision_candidates (
                  id, project_id, section_id, action_type, request_id, base_text, selected_text,
                  replacement_text, selection_start, selection_end, comment_id,
                  candidate_text, summary, prompt_version, model, warnings_json,
                  evidence_json, plan_json, review_json, agent_trace_json,
                  status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    project["id"],
                    section_id,
                    action_type,
                    result["requestId"],
                    base_text,
                    selected_text.strip(),
                    replacement_text,
                    matched_span[0] if matched_span else None,
                    matched_span[1] if matched_span else None,
                    effective_comment_id,
                    candidate_text,
                    result["summary"],
                    result["promptVersion"],
                    result["model"],
                    json.dumps(result["warnings"], ensure_ascii=False),
                    json.dumps(result["evidence"], ensure_ascii=False),
                    json.dumps(result["plan"], ensure_ascii=False),
                    json.dumps(result["review"], ensure_ascii=False),
                    json.dumps(result["agentTrace"], ensure_ascii=False),
                    "pending",
                    utc_now(),
                ),
            )
            connection.commit()
            return {
                "id": candidate_id,
                "projectId": project["id"],
                "sectionId": section_id,
                "text": candidate_text,
                "summary": result["summary"],
                "actionType": action_type,
                "promptVersion": result["promptVersion"],
                "model": result["model"],
                "provider": result["provider"],
                "warnings": result["warnings"],
                "baseText": base_text,
                "selectedText": selected_text.strip(),
                "replacementText": replacement_text,
                "selectionStart": matched_span[0] if matched_span else None,
                "selectionEnd": matched_span[1] if matched_span else None,
                "commentId": effective_comment_id,
                "diffHints": diff_hints,
                "requestId": result["requestId"],
                "plan": result["plan"],
                "review": result["review"],
                "evidence": result["evidence"],
                "evidenceStrategy": result["evidenceStrategy"],
                "citationAudit": result["citationAudit"],
                "citationVerification": result["citationVerification"],
                "agentTrace": result["agentTrace"],
            }

    def accept_revision_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            candidate = connection.execute("SELECT * FROM revision_candidates WHERE id = ?", (candidate_id,)).fetchone()
            if candidate is None:
                raise KeyError(candidate_id)
            section = connection.execute("SELECT * FROM sections WHERE id = ?", (candidate["section_id"],)).fetchone()
            connection.execute("UPDATE sections SET current_text = ? WHERE id = ?", (candidate["candidate_text"], section["id"]))
            connection.execute(
                """
                INSERT INTO revision_events (
                  id, project_id, section_id, source_type, source_label, action_type,
                  summary, old_text, new_text, prompt_version, model, created_at
                ) VALUES (?, ?, ?, 'ai', 'AI 改写', ?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id("revision"), candidate["project_id"], section["id"], candidate["action_type"], candidate["summary"], section["current_text"], candidate["candidate_text"], candidate["prompt_version"], candidate["model"], utc_now()),
            )
            connection.execute("UPDATE revision_candidates SET status = 'accepted' WHERE id = ?", (candidate_id,))
            connection.execute("UPDATE issues SET status = 'resolved' WHERE project_id = ? AND section_id = ? AND status = 'open'", (candidate["project_id"], section["id"]))
            if candidate["action_type"] == "comment-revision" and candidate["comment_id"]:
                connection.execute(
                    """
                    UPDATE reviewer_comments
                    SET status = 'done'
                    WHERE id = ? AND project_id = ?
                    """,
                    (candidate["comment_id"], candidate["project_id"]),
                )
            self._refresh_project_state(connection, candidate["project_id"])
            connection.commit()
        return self.get_project_bundle(candidate["project_id"])

    def reject_revision_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            candidate = connection.execute("SELECT * FROM revision_candidates WHERE id = ?", (candidate_id,)).fetchone()
            if candidate is None:
                raise KeyError(candidate_id)
            connection.execute("UPDATE revision_candidates SET status = 'rejected' WHERE id = ?", (candidate_id,))
            self._refresh_project_state(connection, candidate["project_id"])
            connection.commit()
        return self.get_project_bundle(candidate["project_id"])

    def revise_revision_candidate(self, candidate_id: str, feedback: str) -> dict[str, Any]:
        with self._connect() as connection:
            candidate = connection.execute("SELECT * FROM revision_candidates WHERE id = ?", (candidate_id,)).fetchone()
            if candidate is None:
                raise KeyError(candidate_id)
            section_id = candidate["section_id"]
            action_type = candidate["action_type"]
            current_text = candidate["base_text"] or connection.execute("SELECT current_text FROM sections WHERE id = ?", (section_id,)).fetchone()["current_text"]
            selected_text = candidate["selected_text"] or ""
            selection_start = candidate["selection_start"]
            selection_end = candidate["selection_end"]
            comment_id = candidate["comment_id"]
            previous_candidate_text = candidate["candidate_text"]
            connection.execute("UPDATE revision_candidates SET status = 'superseded' WHERE id = ?", (candidate_id,))
            connection.commit()
        return self.request_revision(
            section_id=section_id,
            action_type=action_type,
            current_text=current_text,
            comment_ids=[comment_id] if comment_id else [],
            selected_text=selected_text,
            selection_start=selection_start,
            selection_end=selection_end,
            comment_id=comment_id,
            feedback=feedback,
            previous_candidate_text=previous_candidate_text,
        )

    def save_manual_edit(self, *, section_id: str, new_text: str) -> dict[str, Any]:
        with self._connect() as connection:
            section = connection.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
            if section is None:
                raise KeyError(section_id)
            if normalize_text(section["current_text"]) == normalize_text(new_text):
                return self.get_project_bundle(section["project_id"])
            connection.execute("UPDATE sections SET current_text = ? WHERE id = ?", (new_text, section_id))
            connection.execute(
                """
                INSERT INTO revision_events (
                  id, project_id, section_id, source_type, source_label, action_type,
                  summary, old_text, new_text, prompt_version, model, created_at
                ) VALUES (?, ?, ?, 'user', '用户手改', 'manual-edit', ?, ?, ?, 'user/manual@1.0.0', 'human', ?)
                """,
                (new_id("revision"), section["project_id"], section_id, "记录一次手动编辑，用户文本继续作为当前主权版本。", section["current_text"], new_text, utc_now()),
            )
            connection.execute("UPDATE issues SET status = 'resolved' WHERE project_id = ? AND section_id = ? AND status = 'open'", (section["project_id"], section_id))
            self._refresh_project_state(connection, section["project_id"])
            connection.commit()
        return self.get_project_bundle(section["project_id"])

    def restore_revision(self, revision_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            revision = connection.execute("SELECT * FROM revision_events WHERE id = ?", (revision_id,)).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            section = connection.execute("SELECT * FROM sections WHERE id = ?", (revision["section_id"],)).fetchone()
            if normalize_text(section["current_text"]) == normalize_text(revision["new_text"]):
                return self.get_project_bundle(revision["project_id"])
            connection.execute("UPDATE sections SET current_text = ? WHERE id = ?", (revision["new_text"], section["id"]))
            connection.execute(
                """
                INSERT INTO revision_events (
                  id, project_id, section_id, source_type, source_label, action_type,
                  summary, old_text, new_text, prompt_version, model, created_at
                ) VALUES (?, ?, ?, 'system', '系统恢复', 'restore-version', ?, ?, ?, 'system/restore@1.0.0', 'system', ?)
                """,
                (new_id("revision"), revision["project_id"], section["id"], "从版本历史恢复为当前文本。", section["current_text"], revision["new_text"], utc_now()),
            )
            self._refresh_project_state(connection, revision["project_id"])
            connection.commit()
        return self.get_project_bundle(revision["project_id"])

    def import_comment(self, *, project_id: str, raw_comment: str) -> dict[str, Any]:
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            sections = [self._serialize_section(row) for row in connection.execute("SELECT * FROM sections WHERE project_id = ? ORDER BY order_index ASC", (project_id,)).fetchall()]
            prompt = self._load_prompt("comment-map", project["language"], "comment-map")
            schema_hint = prompt.get("schema_hint") or json.dumps({"mapped_section_id": "section-xxx", "confidence": 0.85, "suggested_action": "what to do next"}, ensure_ascii=False, indent=2)
            model_output, run_meta = self._call_provider_json(action_name="comment-map", prompt_version=prompt["version_tag"], system_prompt=prompt["system_prompt"], input_payload={"rawComment": raw_comment, "language": project["language"], "sections": sections}, schema_hint=schema_hint)
            heuristic_map = heuristic_comment_map(raw_comment, sections)
            section_ids = {section["id"] for section in sections}
            if heuristic_map.get("confidence", 0) >= 0.9:
                mapped = heuristic_map
            elif isinstance(model_output, dict) and model_output.get("mapped_section_id") in section_ids:
                mapped = model_output
            else:
                mapped = heuristic_map
            connection.execute(
                """
                INSERT INTO reviewer_comments (
                  id, project_id, raw_comment, mapped_section_id, manual_override_section_id,
                  confidence, suggested_action, status, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'pending', ?)
                """,
                (new_id("comment"), project_id, raw_comment, mapped["mapped_section_id"], float(mapped.get("confidence", 0.7)), mapped["suggested_action"], utc_now()),
            )
            self._insert_llm_run(connection, project_id=project_id, section_id=mapped.get("mapped_section_id"), action_name="comment-map", prompt_version=prompt["version_tag"], provider=run_meta["provider"], model=run_meta["model"], status=run_meta["status"], latency_ms=run_meta["latency_ms"], input_text=raw_comment, output_text=json.dumps(mapped, ensure_ascii=False), error=run_meta["error"])
            self._refresh_project_state(connection, project_id)
            connection.commit()
        return self.get_project_bundle(project_id)

    def remap_comment(self, *, comment_id: str, section_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            comment = connection.execute("SELECT * FROM reviewer_comments WHERE id = ?", (comment_id,)).fetchone()
            if comment is None:
                raise KeyError(comment_id)
            connection.execute("UPDATE reviewer_comments SET manual_override_section_id = ?, confidence = 0.97 WHERE id = ?", (section_id, comment_id))
            self._refresh_project_state(connection, comment["project_id"])
            connection.commit()
        return self.get_project_bundle(comment["project_id"])

    def update_comment_status(self, *, comment_id: str, status: str) -> dict[str, Any]:
        with self._connect() as connection:
            comment = connection.execute("SELECT * FROM reviewer_comments WHERE id = ?", (comment_id,)).fetchone()
            if comment is None:
                raise KeyError(comment_id)
            connection.execute("UPDATE reviewer_comments SET status = ? WHERE id = ?", (status, comment_id))
            self._refresh_project_state(connection, comment["project_id"])
            connection.commit()
        return self.get_project_bundle(comment["project_id"])

    def update_issue_status(self, *, issue_id: str, status: str) -> dict[str, Any]:
        with self._connect() as connection:
            issue = connection.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if issue is None:
                raise KeyError(issue_id)
            connection.execute("UPDATE issues SET status = ? WHERE id = ?", (status, issue_id))
            self._refresh_project_state(connection, issue["project_id"])
            connection.commit()
        return self.get_project_bundle(issue["project_id"])

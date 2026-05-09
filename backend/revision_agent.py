from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
from time import perf_counter
from typing import Any, Callable


def normalize_text_block(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def compact_text_block(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def chinese_overlap_ratio(source: str, candidate: str) -> float:
    source_chars = set(re.findall(r"[\u4e00-\u9fff]", source))
    candidate_chars = set(re.findall(r"[\u4e00-\u9fff]", candidate))
    if not source_chars:
        return 1.0
    return len(source_chars & candidate_chars) / len(source_chars)


def detect_text_language(text: str, fallback: str = "en") -> str:
    normalized = normalize_text_block(text)
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


def summarize_excerpt(text: str, limit: int = 180) -> str:
    compact = compact_text_block(text)
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit].rstrip()}..."


@dataclass
class RevisionUnit:
    id: str
    start: int
    end: int
    text: str
    kind: str = "paragraph"


def batch_units(units: list[RevisionUnit], max_chars: int = 900, max_units: int = 2) -> list[list[RevisionUnit]]:
    if not units:
        return []
    groups: list[list[RevisionUnit]] = []
    current: list[RevisionUnit] = []
    current_chars = 0
    for unit in units:
        proposed = current_chars + len(unit.text)
        if current and (proposed > max_chars or len(current) >= max_units):
            groups.append(current)
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += len(unit.text)
    if current:
        groups.append(current)
    return groups


def split_revision_units(text: str) -> list[RevisionUnit]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    units: list[RevisionUnit] = []
    position = 0
    unit_index = 0
    matches = list(re.finditer(r"\n\s*\n", normalized))
    for match in matches:
        chunk = normalized[position:match.start()]
        if chunk.strip():
            units.append(
                RevisionUnit(
                    id=f"unit-{unit_index}",
                    start=position,
                    end=match.start(),
                    text=chunk,
                    kind="paragraph",
                )
            )
            unit_index += 1
        position = match.end()
    tail = normalized[position:]
    if tail.strip():
        units.append(
            RevisionUnit(
                id=f"unit-{unit_index}",
                start=position,
                end=len(normalized),
                text=tail,
                kind="paragraph",
            )
        )
    if units:
        return units
    fallback = normalized.strip()
    if not fallback:
        return []
    return [RevisionUnit(id="unit-0", start=0, end=len(normalized), text=fallback, kind="paragraph")]


def json_safe_dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return repr(value)


@dataclass
class EvidenceSnippet:
    source_kind: str
    source_id: str | None
    label: str
    excerpt: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepRun:
    step: str
    action_name: str
    prompt_version: str
    provider: str
    model: str
    status: str
    latency_ms: int
    error: str | None
    input_text: str
    output_text: str


@dataclass
class RevisionState:
    request_id: str
    project_id: str | None
    section_id: str | None
    title: str
    language: str
    action_type: str
    base_text: str
    target_text: str
    selected_text: str
    note: str
    comment_context: str
    previous_candidate_text: str
    effective_action_type: str = ""
    instruction_plan: dict[str, Any] = field(default_factory=dict)
    rewrite_mode: str = ""
    execution_lane: str = "fast"
    risk_level: str = "medium"
    needs_retrieval: bool = False
    must_preserve: list[str] = field(default_factory=list)
    doc_profile: dict[str, Any] = field(default_factory=dict)
    units: list[RevisionUnit] = field(default_factory=list)
    target_batches: list[list[RevisionUnit]] = field(default_factory=list)
    evidence: list[EvidenceSnippet] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    patches: list[dict[str, Any]] = field(default_factory=list)
    candidate_target_text: str = ""
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    loop_count: int = 0
    step_runs: list[StepRun] = field(default_factory=list)


class RevisionGraph:
    def __init__(
        self,
        *,
        state: RevisionState,
        retrieve: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
        interpret_call: Callable[[str, dict[str, Any], str], tuple[Any | None, dict[str, Any]]] | None,
        writer_call: Callable[[str, dict[str, Any], str], tuple[Any | None, dict[str, Any]]],
        review_call: Callable[[str, dict[str, Any], str], tuple[Any | None, dict[str, Any]]],
        heuristic_rewrite: Callable[[str, str, str, str], dict[str, Any]],
    ) -> None:
        self.state = state
        self.retrieve = retrieve
        self.interpret_call = interpret_call
        self.writer_call = writer_call
        self.review_call = review_call
        self.heuristic_rewrite = heuristic_rewrite

    def run(self) -> dict[str, Any]:
        self._interpret()
        self._analyze()
        if self.state.execution_lane == "full":
            self._retrieve()
        else:
            self.state.evidence = []
        self._plan()
        self._write_and_review_with_repair()
        return self._serialize()

    def _effective_action_type(self) -> str:
        return self.state.effective_action_type or self.state.action_type

    def _translation_target_language(self, action_type: str | None = None) -> str | None:
        effective_action = action_type or self._effective_action_type()
        if effective_action == "translate-en-zh":
            return "zh"
        if effective_action == "translate-zh-en":
            return "en"
        return None

    def _lane_for_action(self, action_type: str) -> str:
        return "full" if action_type in {"academic-rewrite", "expand", "comment-revision", "custom-instruction"} else "fast"

    def _risk_for_action(self, action_type: str) -> str:
        if action_type in {"expand", "comment-revision"}:
            return "high"
        if action_type in {"academic-rewrite", "unify-terms", "custom-instruction", "reduce-aigc", "translate-en-zh", "translate-zh-en"}:
            return "medium"
        return "low"

    def _default_must_preserve(self, action_type: str) -> list[str]:
        rules = [
            "preserve_core_claim",
            "do_not_invent_citations",
            "stay_within_source_scope",
        ]
        if action_type != "expand":
            rules.append("avoid_new_facts_without_support")
        return rules

    def _coerce_string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _interpret(self) -> None:
        selected = bool(self.state.selected_text.strip())
        self.state.rewrite_mode = "targeted" if selected else "document"
        self.state.effective_action_type = self.state.action_type

        if self.state.action_type == "custom-instruction":
            self._interpret_custom_instruction()

        effective_action = self._effective_action_type()
        default_lane = self._lane_for_action(effective_action)
        default_risk = self._risk_for_action(effective_action)
        custom_plan = self.state.instruction_plan or {}

        execution_lane = str(custom_plan.get("executionLane") or default_lane).strip().lower()
        self.state.execution_lane = execution_lane if execution_lane in {"fast", "full"} else default_lane

        if "needsRetrieval" in custom_plan:
            self.state.needs_retrieval = bool(custom_plan.get("needsRetrieval"))
        else:
            self.state.needs_retrieval = self.state.execution_lane == "full"

        risk_level = str(custom_plan.get("riskLevel") or default_risk).strip().lower()
        self.state.risk_level = risk_level if risk_level in {"low", "medium", "high"} else default_risk

        self.state.must_preserve = self._default_must_preserve(effective_action)
        for item in custom_plan.get("mustPreserve", []):
            rule = str(item).strip()
            if rule and rule not in self.state.must_preserve:
                self.state.must_preserve.append(rule)

    def _interpret_custom_instruction(self) -> None:
        if not self.interpret_call:
            raise RuntimeError("Custom instruction analysis is unavailable.")

        raw_note = self.state.note.strip()
        if not raw_note:
            raise RuntimeError("Please enter a specific revision request before running the custom action.")

        payload = {
            "language": self.state.language,
            "title": self.state.title,
            "targetText": self.state.target_text,
            "fullTargetText": self.state.base_text,
            "selectedText": self.state.selected_text,
            "commentContext": self.state.comment_context,
            "userRequest": raw_note,
            "previousCandidateText": self.state.previous_candidate_text,
        }
        schema_hint = (
            '{"normalized_instruction":"short normalized request",'
            '"canonical_action":"academic-rewrite",'
            '"execution_lane":"full",'
            '"needs_retrieval":true,'
            '"risk_level":"medium",'
            '"writer_brief":["instruction 1","instruction 2"],'
            '"must_preserve":["constraint"],'
            '"review_focus":["check 1"],'
            '"retrieval_query":"query string"}'
        )
        model_output, run_meta = self.interpret_call("custom-instruction", payload, schema_hint)

        prompt_version = run_meta.get("promptVersion", "intent/unknown")
        provider = run_meta.get("provider", "unknown")
        model = run_meta.get("model", "unknown")
        latency_ms = int(run_meta.get("latencyMs", run_meta.get("latency_ms", 0)))

        def reject(error: str, output: Any) -> None:
            self.state.step_runs.append(
                StepRun(
                    step="intent",
                    action_name="custom-instruction-intent",
                    prompt_version=prompt_version,
                    provider=provider,
                    model=model,
                    status="rejected",
                    latency_ms=latency_ms,
                    error=error,
                    input_text=raw_note,
                    output_text=json_safe_dump(output),
                )
            )

        if not isinstance(model_output, dict):
            reject("Intent planner returned an invalid payload.", model_output)
            raise RuntimeError("The system could not interpret your custom revision request.")

        normalized_instruction = str(model_output.get("normalized_instruction") or "").strip()
        canonical_action = str(model_output.get("canonical_action") or "").strip()
        writer_brief = self._coerce_string_list(model_output.get("writer_brief"))
        allowed_actions = {
            "academic-rewrite",
            "shorten",
            "expand",
            "unify-terms",
            "comment-revision",
            "transition-polish",
            "translate-en-zh",
            "translate-zh-en",
            "reduce-aigc",
        }
        if not normalized_instruction or canonical_action not in allowed_actions or not writer_brief:
            reject("Intent planner returned an incomplete custom-instruction plan.", model_output)
            raise RuntimeError("The system could not turn your request into a stable revision plan.")

        execution_lane = str(model_output.get("execution_lane") or "").strip().lower()
        risk_level = str(model_output.get("risk_level") or "").strip().lower()
        retrieval_query = str(model_output.get("retrieval_query") or normalized_instruction).strip()
        self.state.instruction_plan = {
            "normalizedInstruction": normalized_instruction,
            "canonicalAction": canonical_action,
            "executionLane": execution_lane if execution_lane in {"fast", "full"} else self._lane_for_action(canonical_action),
            "needsRetrieval": bool(model_output.get("needs_retrieval"))
            if "needs_retrieval" in model_output
            else self._lane_for_action(canonical_action) == "full",
            "riskLevel": risk_level if risk_level in {"low", "medium", "high"} else self._risk_for_action(canonical_action),
            "writerBrief": writer_brief,
            "mustPreserve": self._coerce_string_list(model_output.get("must_preserve")),
            "reviewFocus": self._coerce_string_list(model_output.get("review_focus")),
            "retrievalQuery": retrieval_query or normalized_instruction,
        }
        self.state.effective_action_type = canonical_action
        self.state.step_runs.append(
            StepRun(
                step="intent",
                action_name="custom-instruction-intent",
                prompt_version=prompt_version,
                provider=provider,
                model=model,
                status=run_meta.get("status", "completed"),
                latency_ms=latency_ms,
                error=run_meta.get("error"),
                input_text=raw_note,
                output_text=json_safe_dump(self.state.instruction_plan),
            )
        )

    def _analyze(self) -> None:
        normalized = normalize_text_block(self.state.target_text)
        self.state.units = split_revision_units(self.state.target_text)
        self.state.doc_profile = {
            "characterCount": len(normalized),
            "paragraphCount": len(self.state.units),
            "containsChinese": contains_chinese(self.state.target_text),
            "hasReferenceMarkers": bool(
                re.search(r"(\[[0-9]{1,3}\]|\([^)]+\d{4}[^)]*\)|doi\s*[:?\s]*10\.)", self.state.target_text, re.I)
            ),
        }

    def _retrieve(self) -> None:
        if not self.state.needs_retrieval:
            self.state.evidence = []
            return
        query = "\n".join(
            part
            for part in [
                self.state.title,
                self.state.target_text[:1200],
                self.state.comment_context,
                str((self.state.instruction_plan or {}).get("retrievalQuery") or self.state.note),
            ]
            if part.strip()
        )
        raw_items = self.retrieve(
            query,
            {
                "requestId": self.state.request_id,
                "projectId": self.state.project_id,
                "sectionId": self.state.section_id,
                "actionType": self._effective_action_type(),
                "language": self.state.language,
            },
        )
        self.state.evidence = [
            EvidenceSnippet(
                source_kind=item.get("sourceKind", "unknown"),
                source_id=item.get("sourceId"),
                label=item.get("label", "Untitled evidence"),
                excerpt=item.get("excerpt", ""),
                score=float(item.get("score", 0)),
                metadata=item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {},
            )
            for item in raw_items
        ]

    def _plan(self) -> None:
        units = self.state.units or [
            RevisionUnit(id="unit-0", start=0, end=len(self.state.target_text), text=self.state.target_text)
        ]
        if self.state.selected_text.strip():
            batches = [[RevisionUnit(id="selected-0", start=0, end=len(self.state.target_text), text=self.state.target_text)]]
            strategy = "single-span"
        elif len(units) == 1:
            batches = [units]
            strategy = "single-block"
        else:
            batches = batch_units(units, max_chars=900, max_units=2)
            strategy = "paragraph-batches"

        self.state.target_batches = batches
        literature_evidence = [snippet for snippet in self.state.evidence if snippet.source_kind == "literature"]
        local_rag_evidence = [
            snippet for snippet in literature_evidence if snippet.metadata.get("retrievalMode") == "local-hybrid-rag"
        ]
        agent_roles = self._agent_roles()
        self.state.plan = {
            "strategy": strategy,
            "executionLane": self.state.execution_lane,
            "riskLevel": self.state.risk_level,
            "effectiveActionType": self._effective_action_type(),
            "needsRetrieval": self.state.needs_retrieval,
            "mustPreserve": list(self.state.must_preserve),
            "instructionPlan": self.state.instruction_plan,
            "agentRoles": agent_roles,
            "evidenceGate": {
                "requiresEvidence": self.state.needs_retrieval,
                "literatureEvidenceCount": len(literature_evidence),
                "localRagEvidenceCount": len(local_rag_evidence),
                "allowsNewCitation": bool(literature_evidence),
            },
            "targetCount": len(batches),
            "targets": [
                {
                    "batchId": f"batch-{index}",
                    "unitIds": [unit.id for unit in batch],
                    "goal": self._goal_label(),
                    "textPreview": summarize_excerpt("".join(unit.text for unit in batch), 140),
                }
                for index, batch in enumerate(batches)
            ],
        }

    def _agent_roles(self) -> list[dict[str, str]]:
        prefix: list[dict[str, str]] = []
        if self.state.instruction_plan:
            prefix.append(
                {
                    "name": "instruction-analyst",
                    "job": "turn a free-form user request into a bounded revision plan before writing",
                }
            )
        if self.state.execution_lane == "full":
            return prefix + [
                {"name": "planner", "job": "decide scope, risk, and rewrite batches"},
                {
                    "name": "retriever",
                    "job": "retrieve project sections, comments, revision memory, and local RAG literature evidence",
                },
                {"name": "writer", "job": "rewrite only the selected/current text with evidence constraints"},
                {"name": "guardrail-review", "job": "reject meaning drift, unsupported citations, and over-expansion"},
                {"name": "citation-verifier", "job": "verify explicit DOI, author-year, and numeric citations after writing"},
            ]
        return prefix + [
            {"name": "planner", "job": "decide scope, risk, and rewrite batches"},
            {"name": "writer", "job": "rewrite only the selected/current text with local context constraints"},
            {"name": "guardrail-review", "job": "reject meaning drift, unsupported citations, and over-expansion"},
        ]

    def _goal_label(self) -> str:
        labels = {
            "academic-rewrite": "Academic rewrite",
            "shorten": "Shorten and tighten",
            "expand": "Expand and clarify",
            "unify-terms": "Unify terminology",
            "comment-revision": "Revise for comment",
            "transition-polish": "Polish transition",
            "translate-en-zh": "Translate into Chinese",
            "translate-zh-en": "Translate into English",
            "reduce-aigc": "Reduce formulaic tone",
            "custom-instruction": "Follow custom revision request",
        }
        if self.state.action_type == "custom-instruction":
            return str((self.state.instruction_plan or {}).get("normalizedInstruction") or labels["custom-instruction"])
        return labels.get(self._effective_action_type(), "Revise text")

    def _write_and_review_with_repair(self) -> None:
        max_attempts = 3 if self.state.execution_lane == "full" else 2
        repair_instruction = ""
        final_error = ""

        for attempt in range(1, max_attempts + 1):
            self.state.loop_count = attempt
            self.state.patches = []
            self.state.summary = ""
            self.state.candidate_target_text = ""
            try:
                self._write_target(attempt_index=attempt, repair_instruction=repair_instruction)
                self._review_candidate()
                return
            except RuntimeError as exc:
                if exc.__class__.__name__ == "ModelInvocationError":
                    raise
                final_error = str(exc)
                repair_instruction = self._repair_instruction_for_error(final_error)
                self.state.warnings.append(self._retry_note(attempt, max_attempts, final_error))
                if attempt >= max_attempts:
                    break

        raise RuntimeError(self._final_user_error(final_error))

    def _retry_note(self, attempt: int, max_attempts: int, error: str) -> str:
        lowered = error.lower()
        if attempt >= max_attempts:
            return "The request was retried several times but still did not yield a stable revision."
        if "unchanged" in lowered:
            return f"Attempt {attempt} produced almost no visible revision. Tightening the next pass."
        if "allowed revision range" in lowered or "over_expansion" in lowered:
            return f"Attempt {attempt} expanded beyond the allowed scope. The next pass will stay closer to the source."
        if "citation" in lowered:
            return f"Attempt {attempt} introduced unsupported citation-style content. The next pass will stay citation-safe."
        if "stable revision plan" in lowered or "interpret" in lowered:
            return f"Attempt {attempt} did not yield a stable custom instruction plan. Retrying with a stricter interpretation."
        return f"Attempt {attempt} did not pass validation. Retrying with a more conservative rewrite."

    def _repair_instruction_for_error(self, error: str) -> str:
        lowered = error.lower()
        if "unchanged" in lowered:
            return (
                "Do not return the source text unchanged. Rewrite at sentence level, "
                "make visible but bounded improvements, and keep the same meaning."
            )
        if "allowed revision range" in lowered or "over_expansion" in lowered:
            return (
                "Reduce expansion sharply. Keep the revision close to the source length, "
                "preserve the original meaning, and avoid adding new claims."
            )
        if "language" in lowered:
            return "Keep the output in the same language as the source text."
        if "citation" in lowered:
            return (
                "Do not introduce new citations, DOI, or reference markers unless they are already present "
                "and directly supported by provided evidence."
            )
        if "custom instruction" in lowered or "stable revision plan" in lowered or "interpret" in lowered:
            return (
                "Use the structured instruction plan exactly. Keep the rewrite bounded, concrete, and faithful to the source."
            )
        if "invalid rewrite payload" in lowered:
            return "Return strict JSON with candidate_text, summary, and warnings only."
        return (
            "Tighten the rewrite. Preserve the original claim, stay close to the source scope, "
            "and produce a complete but conservative revision."
        )

    def _final_user_error(self, error: str) -> str:
        lowered = error.lower()
        if "unchanged" in lowered:
            return "The model kept returning text with no meaningful change."
        if "allowed revision range" in lowered or "over_expansion" in lowered:
            return "The rewrite kept drifting too far from the source scope."
        if "citation" in lowered:
            return "The rewrite introduced unsupported citation-style content."
        if "stable revision plan" in lowered or "interpret" in lowered:
            return "The system could not build a stable plan from the custom instruction."
        return "The model could not produce a stable rewrite for the current request."

    def _write_target(self, *, attempt_index: int = 1, repair_instruction: str = "") -> None:
        if not self.state.target_batches:
            self.state.candidate_target_text = self.state.target_text
            self.state.summary = ""
            return

        rebuilt_parts: list[str] = []
        cursor = 0
        summaries: list[str] = []
        patch_warnings: list[str] = []
        self.state.patches = []

        for index, batch in enumerate(self.state.target_batches):
            start = batch[0].start
            end = batch[-1].end
            original_chunk = self.state.target_text[start:end]
            payload = {
                "language": self.state.language,
                "title": self.state.title,
                "actionType": self._effective_action_type(),
                "targetText": original_chunk,
                "fullTargetText": self.state.target_text,
                "commentContext": self.state.comment_context,
                "instructionPlan": self.state.instruction_plan,
                "evidence": [asdict(snippet) for snippet in self.state.evidence[:6]],
                "constraints": self.state.must_preserve,
                "batchIndex": index,
                "batchCount": len(self.state.target_batches),
                "executionLane": self.state.execution_lane,
                "attemptIndex": attempt_index,
                "repairInstruction": repair_instruction,
            }
            if self.state.action_type == "custom-instruction" and self.state.previous_candidate_text:
                payload["previousCandidateText"] = self.state.previous_candidate_text
            schema_hint = (
                '{"candidate_text":"rewritten text for this batch",'
                '"summary":"brief change summary",'
                '"warnings":["optional warning"]}'
            )
            model_output, run_meta = self.writer_call(self.state.action_type, payload, schema_hint)
            candidate_payload = self._parse_writer_output(model_output, original_chunk, run_meta)
            rebuilt_parts.append(self.state.target_text[cursor:start])
            rebuilt_parts.append(candidate_payload["text"])
            cursor = end
            summaries.append(candidate_payload["summary"])
            patch_warnings.extend(candidate_payload["warnings"])
            self.state.patches.append(
                {
                    "batchId": f"batch-{index}",
                    "start": start,
                    "end": end,
                    "originalText": original_chunk,
                    "candidateText": candidate_payload["text"],
                    "summary": candidate_payload["summary"],
                }
            )

        rebuilt_parts.append(self.state.target_text[cursor:])
        self.state.candidate_target_text = "".join(rebuilt_parts)
        self.state.summary = "; ".join(summary for summary in dict.fromkeys(summaries) if summary)
        self.state.warnings = patch_warnings

    def _parse_writer_output(self, model_output: Any, original_chunk: str, run_meta: dict[str, Any]) -> dict[str, Any]:
        if not (isinstance(model_output, dict) and isinstance(model_output.get("candidate_text"), str) and model_output["candidate_text"].strip()):
            self.state.step_runs.append(
                StepRun(
                    step="writer",
                    action_name=run_meta.get("actionName", self.state.action_type),
                    prompt_version=run_meta.get("promptVersion", "rewrite/unknown"),
                    provider=run_meta.get("provider", "unknown"),
                    model=run_meta.get("model", "unknown"),
                    status="rejected",
                    latency_ms=int(run_meta.get("latencyMs", run_meta.get("latency_ms", 0))),
                    error="Model returned an invalid rewrite payload.",
                    input_text=original_chunk,
                    output_text=json_safe_dump(model_output),
                )
            )
            raise RuntimeError("The model returned an invalid rewrite payload.")

        payload: dict[str, Any] = {
            "text": str(model_output["candidate_text"]).strip(),
            "summary": str(model_output.get("summary") or ""),
            "warnings": [str(item) for item in model_output.get("warnings", []) if str(item).strip()],
        }

        unsafe_reason = self._unsafe_writer_reason(original_chunk, payload["text"])
        if unsafe_reason:
            self.state.step_runs.append(
                StepRun(
                    step="writer",
                    action_name=run_meta.get("actionName", self.state.action_type),
                    prompt_version=run_meta.get("promptVersion", "rewrite/unknown"),
                    provider=run_meta.get("provider", "unknown"),
                    model=run_meta.get("model", "unknown"),
                    status="rejected",
                    latency_ms=int(run_meta.get("latencyMs", run_meta.get("latency_ms", 0))),
                    error=unsafe_reason,
                    input_text=original_chunk,
                    output_text=payload["text"],
                )
            )
            raise RuntimeError(unsafe_reason)

        if normalize_text_block(original_chunk) == normalize_text_block(payload["text"]):
            self.state.step_runs.append(
                StepRun(
                    step="writer",
                    action_name=run_meta.get("actionName", self.state.action_type),
                    prompt_version=run_meta.get("promptVersion", "rewrite/unknown"),
                    provider=run_meta.get("provider", "unknown"),
                    model=run_meta.get("model", "unknown"),
                    status="rejected",
                    latency_ms=int(run_meta.get("latencyMs", run_meta.get("latency_ms", 0))),
                    error="Model returned unchanged text.",
                    input_text=original_chunk,
                    output_text=payload["text"],
                )
            )
            raise RuntimeError("Model returned unchanged text.")

        self.state.step_runs.append(
            StepRun(
                step="writer",
                action_name=run_meta.get("actionName", self.state.action_type),
                prompt_version=run_meta.get("promptVersion", "rewrite/unknown"),
                provider=run_meta.get("provider", "unknown"),
                model=run_meta.get("model", "unknown"),
                status=run_meta.get("status", "completed"),
                latency_ms=int(run_meta.get("latencyMs", run_meta.get("latency_ms", 0))),
                error=run_meta.get("error"),
                input_text=original_chunk,
                output_text=payload["text"],
            )
        )
        if not payload.get("summary"):
            payload["summary"] = self._goal_label()
        if not isinstance(payload.get("warnings"), list):
            payload["warnings"] = []
        return payload

    def _unsafe_writer_reason(self, source_text: str, candidate_text: str) -> str:
        normalized_source = normalize_text_block(source_text)
        normalized_candidate = normalize_text_block(candidate_text)
        effective_action = self._effective_action_type()
        source_language = detect_text_language(normalized_source, fallback=self.state.language)
        target_language = self._translation_target_language(effective_action)
        candidate_language = detect_text_language(normalized_candidate, fallback=target_language or source_language)
        if not normalized_candidate:
            return "Writer returned empty text."
        if target_language:
            if candidate_language != target_language:
                return "Writer did not produce the requested target language."
        elif source_language != candidate_language:
            return "Writer changed the language of the source text."
        if re.search(r"placeholder|xxx|todo", normalized_candidate, re.I):
            return "Writer returned placeholder content."
        if not target_language and effective_action != "expand" and len(normalized_source) < 200 and chinese_overlap_ratio(normalized_source, normalized_candidate) < 0.18:
            return "Writer drifted too far from the source meaning."
        if effective_action != "expand" and len(normalized_candidate) > max(160, len(normalized_source) * 3):
            return "Writer expanded the text beyond the allowed revision range."
        return ""

    def _review_candidate(self) -> None:
        started = perf_counter()
        review = self._heuristic_review()
        latency_ms = int((perf_counter() - started) * 1000)
        self.state.step_runs.append(
            StepRun(
                step="review",
                action_name="revision-review",
                prompt_version="review/local-guardrails@1.0.0",
                provider="deterministic",
                model="local-guardrails",
                status="completed" if review.get("passed") else "rejected",
                latency_ms=latency_ms,
                error=None if review.get("passed") else review.get("repairInstruction") or "Review rejected candidate.",
                input_text=self.state.target_text,
                output_text=self.state.candidate_target_text,
            )
        )
        self.state.review = review
        self.state.warnings = list(dict.fromkeys([*self.state.warnings, *review.get("warnings", [])]))
        if not review.get("passed", True):
            raise RuntimeError(review.get("repairInstruction") or "Review rejected candidate.")

    def _heuristic_review(self) -> dict[str, Any]:
        original = normalize_text_block(self.state.target_text)
        candidate = normalize_text_block(self.state.candidate_target_text)
        effective_action = self._effective_action_type()
        issues: list[str] = []
        warnings: list[str] = []
        source_language = detect_text_language(original, fallback=self.state.language)
        target_language = self._translation_target_language(effective_action)
        candidate_language = detect_text_language(candidate, fallback=target_language or source_language)

        if target_language:
            if candidate_language != target_language:
                issues.append("candidate_translation_language_mismatch")
        elif source_language != candidate_language:
            issues.append("candidate_language_drift")
        if not target_language and effective_action != "expand" and len(original) < 240 and chinese_overlap_ratio(original, candidate) < 0.18:
            issues.append("candidate_meaning_drift")
        if effective_action != "expand" and len(candidate) > max(180, len(original) * 2.4):
            issues.append("candidate_over_expansion")
        if re.search(r"(\[[0-9]{1,3}\]|\([^)]+\d{4}[^)]*\)|doi\s*[:?\s]*10\.)", candidate, re.I) and not re.search(
            r"(\[[0-9]{1,3}\]|\([^)]+\d{4}[^)]*\)|doi\s*[:?\s]*10\.)",
            original,
            re.I,
        ):
            if not any(snippet.source_kind == "literature" for snippet in self.state.evidence):
                issues.append("candidate_unsupported_citation")
            else:
                warnings.append("Candidate introduced citation-style text. Verify the citation against imported literature.")
        if effective_action in {"expand", "comment-revision"} and not any(
            snippet.source_kind == "literature" for snippet in self.state.evidence
        ):
            warnings.append("No imported literature evidence was retrieved. Human review is recommended before using the revision.")

        return {
            "passed": not issues,
            "issues": issues,
            "warnings": warnings,
            "repairInstruction": "Keep the rewrite closer to the source meaning, avoid unsupported citations, and stay within scope."
            if issues
            else "",
        }

    def _trace_payload(self) -> dict[str, Any]:
        role_sequence = [role["name"] for role in self.state.plan.get("agentRoles", [])]
        return {
            "rewriteMode": self.state.rewrite_mode,
            "executionLane": self.state.execution_lane,
            "riskLevel": self.state.risk_level,
            "effectiveActionType": self._effective_action_type(),
            "instructionPlan": self.state.instruction_plan,
            "roleSequence": role_sequence,
            "docProfile": self.state.doc_profile,
            "loopCount": self.state.loop_count,
            "stepRuns": [asdict(run) for run in self.state.step_runs],
            "patches": list(self.state.patches),
        }

    def _serialize(self) -> dict[str, Any]:
        trace = self._trace_payload()
        return {
            "requestId": self.state.request_id,
            "summary": self.state.summary or self._goal_label(),
            "warnings": list(dict.fromkeys(self.state.warnings)),
            "candidateTargetText": self.state.candidate_target_text,
            "replacementText": self.state.candidate_target_text,
            "plan": self.state.plan,
            "review": self.state.review,
            "evidence": [asdict(snippet) for snippet in self.state.evidence],
            "agentTrace": trace,
            "stepRuns": trace["stepRuns"],
        }

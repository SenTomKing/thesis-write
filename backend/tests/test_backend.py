from __future__ import annotations

import os
import shutil
import unittest
from uuid import uuid4
from pathlib import Path
from unittest.mock import patch

from backend.literature import LiteratureService
from backend.service import BackendService, ModelInvocationError, split_into_sections


class BackendServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_tmp_root = Path(__file__).resolve().parent / ".tmp"
        self.workspace_tmp_root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.workspace_tmp_root / f"draftrefine-test-{uuid4().hex}.sqlite3"
        os.environ.pop("DRAFTREFINE_DATABASE_PATH", None)
        os.environ["DRAFTREFINE_DEEPSEEK_API_KEY"] = ""
        os.environ["DRAFTREFINE_QWEN_API_KEY"] = ""
        self.service = BackendService(database_path=self.database_path)
        self.provider_calls: list[dict[str, object]] = []
        self.provider_patcher = patch.object(
            BackendService,
            "_call_provider_json",
            autospec=True,
            side_effect=self._fake_call_provider_json,
        )
        self.provider_patcher.start()

    def tearDown(self) -> None:
        self.provider_patcher.stop()
        if self.database_path.exists():
            self.database_path.unlink()
        storage_dir = self.workspace_tmp_root / f"{self.database_path.stem}_literature"
        if storage_dir.exists():
            shutil.rmtree(storage_dir, ignore_errors=True)

    def _run_meta(self, *, provider: str = "mock-provider", model: str = "mock-model") -> dict[str, object]:
        return {
            "provider": provider,
            "model": model,
            "status": "completed",
            "latency_ms": 12,
            "error": None,
        }

    def _fake_rewrite_text(self, action_name: str, target_text: str, language: str, comment_context: str) -> str:
        rewritten = target_text
        if action_name == "academic-rewrite":
            rewritten = rewritten.replace("kind of broad", "partially broad")
            rewritten = rewritten.replace("比较松散", "结构相对松散")
            rewritten = rewritten.replace("不够学术", "学术论证力度不足")
            if rewritten == target_text:
                rewritten = (
                    f"从学术表达的角度看，{rewritten}"
                    if language == "zh"
                    else f"From an academic writing perspective, {rewritten}"
                )
        elif action_name == "shorten":
            rewritten = rewritten.replace("kind of ", "").replace("比较", "")
            if rewritten == target_text:
                rewritten = rewritten.strip().rstrip(".") + "." if language != "zh" else rewritten.strip()
        elif action_name == "expand":
            addition = (
                " 这一表述进一步解释了论证链条与研究动机之间的关系。"
                if language == "zh"
                else " This revision adds the missing rationale and clarifies the argument chain."
            )
            rewritten = f"{rewritten}{addition}"
        elif action_name == "unify-terms":
            rewritten = rewritten.replace("LST", "land surface temperature (LST)")
            rewritten = rewritten.replace("地表温度", "地表温度（LST）")
            if rewritten == target_text:
                rewritten = (
                    rewritten.replace("method", "methodology")
                    if language != "zh"
                    else rewritten.replace("方法", "研究方法")
                )
        elif action_name == "comment-revision":
            prefix = "根据导师意见，" if language == "zh" else "In response to the supervisor comment, "
            rewritten = f"{prefix}{rewritten}"
            if comment_context.strip():
                rewritten += (
                    f" 重点处理：{comment_context.strip()[:32]}。"
                    if language == "zh"
                    else f" This directly addresses: {comment_context.strip()[:48]}."
                )
        elif action_name == "transition-polish":
            prefix = "因此，" if language == "zh" else "Therefore, "
            rewritten = f"{prefix}{rewritten[:1].lower() + rewritten[1:]}" if rewritten else rewritten

        return rewritten

    def _fake_call_provider_json(
        self,
        _service: BackendService,
        *,
        action_name: str,
        prompt_version: str,
        system_prompt: str,
        input_payload: dict,
        schema_hint: str,
    ) -> tuple[object, dict[str, object]]:
        self.provider_calls.append(
            {
                "action_name": action_name,
                "prompt_version": prompt_version,
                "input_payload": input_payload,
            }
        )
        if action_name == "diagnose":
            sections = input_payload.get("sections", [])
            target = sections[0] if sections else {"id": "section-1"}
            return (
                [
                    {
                        "section_id": target["id"],
                        "issue_type": "structure",
                        "severity": "medium",
                        "title": "Need tighter structure",
                        "detail": "The section can be tightened for clearer academic flow.",
                        "suggested_action": "Start with one focused revision pass.",
                    }
                ],
                self._run_meta(provider="mock-diagnose", model="diagnose-stub"),
            )

        if action_name == "comment-map":
            raw_comment = str(input_payload.get("rawComment", ""))
            sections = list(input_payload.get("sections", []))
            selected = sections[0] if sections else {"id": "section-1"}
            if len(sections) > 1 and ("question" in raw_comment.lower() or "问题" in raw_comment):
                selected = sections[1]
            return (
                {
                    "mapped_section_id": selected["id"],
                    "confidence": 0.93,
                    "suggested_action": "Run one targeted revision pass.",
                },
                self._run_meta(provider="mock-comment-map", model="comment-map-stub"),
            )

        if action_name == "custom-instruction" and "intent/" in prompt_version:
            user_request = str(input_payload.get("userRequest", ""))
            lowered = user_request.lower()
            if any(token in user_request for token in ["精简", "压缩", "缩短"]) or "short" in lowered:
                canonical_action = "shorten"
            elif any(token in user_request for token in ["扩写", "补充", "展开"]) or "expand" in lowered:
                canonical_action = "expand"
            elif any(token in user_request for token in ["术语", "统一"]) or "term" in lowered:
                canonical_action = "unify-terms"
            elif any(token in user_request for token in ["过渡", "衔接"]) or "transition" in lowered:
                canonical_action = "transition-polish"
            else:
                canonical_action = "academic-rewrite"
            lane = "full" if canonical_action in {"academic-rewrite", "expand", "comment-revision"} else "fast"
            return (
                {
                    "normalized_instruction": "Tighten the tone, reduce repetition, and keep the original claim intact.",
                    "canonical_action": canonical_action,
                    "execution_lane": lane,
                    "needs_retrieval": lane == "full",
                    "risk_level": "medium" if lane == "full" else "low",
                    "writer_brief": [
                        "Keep the original claim, numbers, and formula references intact.",
                        "Make the wording more review-ready and easier to defend.",
                    ],
                    "must_preserve": ["preserve_claim_scope", "preserve_equations_and_numbers"],
                    "review_focus": ["check meaning drift", "check unsupported citations"],
                    "retrieval_query": "review-ready academic rewrite",
                },
                self._run_meta(provider="mock-intent", model="intent-stub"),
            )

        effective_action = action_name
        if action_name == "custom-instruction":
            instruction_plan = input_payload.get("instructionPlan", {}) if isinstance(input_payload, dict) else {}
            effective_action = str(instruction_plan.get("canonicalAction") or input_payload.get("actionType") or "academic-rewrite")

        rewritten = self._fake_rewrite_text(
            action_name=effective_action,
            target_text=str(input_payload.get("targetText", "")),
            language=str(input_payload.get("language", "en")),
            comment_context=str(input_payload.get("commentContext", "")),
        )
        return (
            {
                "candidate_text": rewritten,
                "summary": f"{effective_action} applied",
                "warnings": [],
            },
            self._run_meta(provider="mock-rewrite", model="rewrite-stub"),
        )

    def test_create_project_from_text_generates_sections_without_auto_diagnose(self) -> None:
        bundle = self.service.create_project(
            title="Interview validation project",
            doc_type="thesis",
            language="en",
            source_type="text",
            note="Validate the revise workflow",
            text="The first paragraph introduces the research background.\n\nThe second paragraph frames the research question.\n\nThe third paragraph explains the method.",
        )

        self.assertEqual(bundle["project"]["title"], "Interview validation project")
        self.assertGreaterEqual(len(bundle["sections"]), 3)
        self.assertEqual(len(bundle["issues"]), 0)
        self.assertEqual(bundle["project"]["status"], "ready")

    def test_revision_accept_and_restore_flow(self) -> None:
        bundle = self.service.create_project(
            title="English draft",
            doc_type="journal-article",
            language="en",
            source_type="text",
            note="Tighten one section",
            text="This introduction is kind of broad.\n\nThe methods section needs validation logic.",
        )
        section_id = bundle["sections"][0]["id"]
        candidate = self.service.request_revision(
            section_id=section_id,
            action_type="academic-rewrite",
            current_text=bundle["sections"][0]["currentText"],
            comment_ids=[],
        )
        accepted = self.service.accept_revision_candidate(candidate["id"])
        original_revision_id = next(
            revision["id"]
            for revision in accepted["revisions"]
            if revision["actionType"] == "initial-import" and revision["sectionId"] == section_id
        )
        restored = self.service.restore_revision(original_revision_id)

        self.assertEqual(accepted["sections"][0]["currentText"], candidate["text"])
        self.assertTrue(any(revision["actionType"] == "restore-version" for revision in restored["revisions"]))
        self.assertEqual(restored["sections"][0]["currentText"], bundle["sections"][0]["currentText"])

    def test_selection_revision_replaces_only_selected_text(self) -> None:
        bundle = self.service.create_project(
            title="Selection draft",
            doc_type="journal-article",
            language="en",
            source_type="text",
            note="Test selected rewrite",
            text="This introduction is kind of broad. The method is stable.",
        )
        section = bundle["sections"][0]
        start = section["currentText"].index("kind of broad")
        candidate = self.service.request_revision(
            section_id=section["id"],
            action_type="academic-rewrite",
            current_text=section["currentText"],
            comment_ids=[],
            selected_text="kind of broad",
            selection_start=start,
            selection_end=start + len("kind of broad"),
        )

        self.assertIn("partially broad", candidate["text"])
        self.assertIn("The method is stable.", candidate["text"])
        self.assertEqual(candidate["replacementText"], "partially broad")
        self.assertEqual(candidate["selectionStart"], start)

    def test_fast_lane_revision_marks_fast_execution_lane(self) -> None:
        bundle = self.service.create_project(
            title="Fast lane draft",
            doc_type="journal-article",
            language="en",
            source_type="text",
            note="Validate fast lane",
            text="This introduction is kind of broad.",
        )

        candidate = self.service.request_revision(
            section_id=bundle["sections"][0]["id"],
            action_type="shorten",
            current_text=bundle["sections"][0]["currentText"],
            comment_ids=[],
        )

        self.assertEqual(candidate["agentTrace"]["executionLane"], "fast")
        self.assertNotIn("retriever", candidate["agentTrace"]["roleSequence"])

    def test_english_revision_in_zh_project_uses_english_prompt_and_ignores_previous_candidate(self) -> None:
        bundle = self.service.create_project(
            title="中文项目",
            doc_type="thesis",
            language="zh",
            source_type="text",
            note="Validate per-request language routing",
            text=(
                "Driven by these concerns, this study proposes a two-channel based SW-TES method. "
                "Operating independently of any auxiliary LSE data, this method facilitates the direct acquisition "
                "of LST from satellite-based TIR observations."
            ),
        )

        section = bundle["sections"][0]
        candidate = self.service.request_revision(
            section_id=section["id"],
            action_type="academic-rewrite",
            current_text=section["currentText"],
            comment_ids=[],
            previous_candidate_text="基于上述考虑，本研究提出了……",
        )

        writer_call = next(call for call in self.provider_calls if call["action_name"] == "academic-rewrite")
        self.assertTrue(str(writer_call["prompt_version"]).startswith("rewrite/default.en@"))
        self.assertEqual(writer_call["input_payload"]["language"], "en")
        self.assertNotIn("previousCandidateText", writer_call["input_payload"])
        self.assertIn("From an academic writing perspective", candidate["text"])
        self.assertNotIn("本研究提出", candidate["text"])

    def test_failed_model_call_marks_revision_request_failed(self) -> None:
        bundle = self.service.create_project(
            title="Failure draft",
            doc_type="journal-article",
            language="en",
            source_type="text",
            note="Validate failed request persistence",
            text="This introduction is kind of broad.",
        )
        section_id = bundle["sections"][0]["id"]

        self.provider_patcher.stop()
        try:
            with patch.object(
                BackendService,
                "_call_provider_json",
                autospec=True,
                side_effect=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    ModelInvocationError(
                        "Model invocation failed.",
                        attempts=[
                            {
                                "action_name": "academic-rewrite",
                                "prompt_version": "rewrite/mock@1.0.0",
                                "provider": "mock-provider",
                                "model": "mock-model",
                                "status": "failed",
                                "latency_ms": 25,
                                "error": "connection reset",
                                "input_text": "input",
                                "output_text": "",
                            }
                        ],
                    )
                ),
            ):
                with self.assertRaises(ModelInvocationError):
                    self.service.request_revision(
                        section_id=section_id,
                        action_type="academic-rewrite",
                        current_text=bundle["sections"][0]["currentText"],
                        comment_ids=[],
                    )
        finally:
            self.provider_patcher.start()

        with self.service._connect() as connection:
            request_row = connection.execute(
                "SELECT status, result_summary FROM revision_requests ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            llm_run_row = connection.execute(
                "SELECT status, provider, model, error FROM llm_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

        self.assertEqual(request_row["status"], "failed")
        self.assertIn("Model invocation failed", request_row["result_summary"])
        self.assertEqual(llm_run_row["status"], "failed")
        self.assertEqual(llm_run_row["provider"], "mock-provider")
        self.assertEqual(llm_run_row["model"], "mock-model")
        self.assertIn("connection reset", llm_run_row["error"])

    def test_selection_revision_rejects_unmatched_text(self) -> None:
        bundle = self.service.create_project(
            title="Selection validation",
            doc_type="journal-article",
            language="en",
            source_type="text",
            note="Test invalid selected rewrite",
            text="This introduction is kind of broad.",
        )
        with self.assertRaises(ValueError):
            self.service.request_revision(
                section_id=bundle["sections"][0]["id"],
                action_type="academic-rewrite",
                current_text=bundle["sections"][0]["currentText"],
                comment_ids=[],
                selected_text="not in this section",
            )

    def test_reject_revision_does_not_update_current_text(self) -> None:
        bundle = self.service.create_project(
            title="Reject draft",
            doc_type="journal-article",
            language="en",
            source_type="text",
            note="Test reject",
            text="This introduction is kind of broad.",
        )
        section = bundle["sections"][0]
        candidate = self.service.request_revision(
            section_id=section["id"],
            action_type="academic-rewrite",
            current_text=section["currentText"],
            comment_ids=[],
        )
        rejected = self.service.reject_revision_candidate(candidate["id"])

        self.assertEqual(rejected["sections"][0]["currentText"], section["currentText"])
        self.assertFalse(any(revision["actionType"] == "academic-rewrite" for revision in rejected["revisions"]))

    def test_accept_comment_revision_marks_only_that_comment_done(self) -> None:
        bundle = self.service.create_project(
            title="Comment revision",
            doc_type="thesis",
            language="en",
            source_type="text",
            note="Test comment-specific revision",
            text="The first paragraph introduces the background.\n\nThe second paragraph explains the research question.",
        )
        project_id = bundle["project"]["id"]
        first = self.service.import_comment(project_id=project_id, raw_comment="The background section needs a tighter academic tone.")
        second = self.service.import_comment(project_id=project_id, raw_comment="The research question needs more focus.")
        comment_id = first["comments"][0]["id"]
        section_id = first["comments"][0]["mappedSectionId"]
        section = next(section for section in first["sections"] if section["id"] == section_id)
        candidate = self.service.request_revision(
            section_id=section_id,
            action_type="comment-revision",
            current_text=section["currentText"],
            comment_ids=[comment_id],
            comment_id=comment_id,
        )
        accepted = self.service.accept_revision_candidate(candidate["id"])

        comments_by_id = {comment["id"]: comment for comment in accepted["comments"]}
        self.assertEqual(comments_by_id[comment_id]["status"], "done")
        self.assertEqual(comments_by_id[second["comments"][0]["id"]]["status"], "pending")

    def test_import_comment_maps_to_section(self) -> None:
        bundle = self.service.create_project(
            title="Comment mapping",
            doc_type="thesis",
            language="en",
            source_type="text",
            note="Map one supervisor comment",
            text="The first paragraph introduces the background.\n\nThe second paragraph explains the research question.\n\nThe third paragraph explains the discussion.",
        )
        project_id = bundle["project"]["id"]
        comment_bundle = self.service.import_comment(
            project_id=project_id,
            raw_comment="The research question section is still too loose and should be compressed into two sentences.",
        )

        self.assertIsNotNone(comment_bundle["comments"][0]["mappedSectionId"])
        self.assertEqual(comment_bundle["comments"][0]["status"], "pending")

    def test_section_revision_returns_agent_plan_review_and_evidence(self) -> None:
        bundle = self.service.create_project(
            title="中文改稿项目",
            doc_type="thesis",
            language="zh",
            source_type="text",
            note="验证 agent 改稿工作流",
            text="研究背景比较松散，而且这个问题写得不够学术。\n\n方法部分需要保持原意。",
        )
        project_id = bundle["project"]["id"]
        literature = LiteratureService(database_path=self.database_path)
        literature.import_literature(
            project_id,
            items=[
                {
                    "title": "研究背景段落的学术表达与结构收束",
                    "authors": ["张三"],
                    "year": 2024,
                    "venue": "学位与写作研究",
                    "doi": "10.1000/zh-background",
                    "url": "https://example.org/background",
                    "abstract": "该文讨论中文论文研究背景段落如何收束主张、统一术语并避免口语化表达。",
                    "source": "manual",
                    "sources": ["manual"],
                    "language": "zh",
                    "citationCount": 6,
                    "openAccessStatus": "metadata-only",
                    "tags": ["academic-writing", "research-background"],
                }
            ],
        )

        candidate = self.service.request_revision(
            section_id=bundle["sections"][0]["id"],
            action_type="expand",
            current_text=bundle["sections"][0]["currentText"],
            comment_ids=[],
        )

        self.assertIn("plan", candidate)
        self.assertIn("review", candidate)
        self.assertIn("evidence", candidate)
        self.assertIn("requestId", candidate)
        self.assertIn("evidenceStrategy", candidate)
        self.assertIn("citationAudit", candidate)
        self.assertIn("citationVerification", candidate)
        self.assertTrue(candidate["review"]["passed"])
        self.assertTrue(any(item["source_kind"] == "literature" for item in candidate["evidence"]))
        self.assertEqual(candidate["evidenceStrategy"]["sourceOrder"][0], "openalex")
        self.assertIn(candidate["citationAudit"]["status"], {"not-needed", "supported", "needs-verification"})
        self.assertIn(candidate["citationVerification"]["status"], {"not-applicable", "verified", "partially-verified", "unverified"})

    def test_direct_revision_agent_persists_request_and_retrieval_logs(self) -> None:
        bundle = self.service.create_project(
            title="直接文本改稿",
            doc_type="thesis",
            language="zh",
            source_type="text",
            note="验证纯文本 agent 入口",
            text="研究背景部分需要一个项目上下文。",
        )
        project_id = bundle["project"]["id"]
        literature = LiteratureService(database_path=self.database_path)
        literature.import_literature(
            project_id,
            items=[
                {
                    "title": "中文论文研究背景改稿策略",
                    "authors": ["李四"],
                    "year": 2025,
                    "venue": "研究生写作评论",
                    "doi": "10.1000/direct-agent",
                    "url": "https://example.org/direct-agent",
                    "abstract": "文章总结了研究背景写作中的学术化改写、术语统一和段落收束策略。",
                    "source": "manual",
                    "sources": ["manual"],
                    "language": "zh",
                    "citationCount": 4,
                    "openAccessStatus": "metadata-only",
                    "tags": ["academic-writing", "revision"],
                }
            ],
        )

        result = self.service.revise_text(
            project_id=project_id,
            text="本研究的研究背景比较松散，而且这个问题写得不够学术。",
            action_type="academic-rewrite",
            note="请保留原意，并参考用户导入文献库。",
        )

        self.assertTrue(result["review"]["passed"])
        self.assertTrue(any(item["source_kind"] == "literature" for item in result["evidence"]))
        self.assertIn("agentTrace", result)
        self.assertIn("evidenceStrategy", result)
        self.assertIn("citationAudit", result)
        self.assertIn("citationVerification", result)
        self.assertEqual(result["evidenceStrategy"]["sourceOrder"][0], "openalex")
        with self.service._connect() as connection:
            request_count = connection.execute("SELECT COUNT(*) AS total FROM revision_requests").fetchone()["total"]
            retrieval_count = connection.execute("SELECT COUNT(*) AS total FROM retrieval_logs").fetchone()["total"]
        self.assertGreaterEqual(request_count, 1)
        self.assertGreaterEqual(retrieval_count, 1)

    def test_custom_instruction_is_analyzed_before_rewrite(self) -> None:
        result = self.service.revise_text(
            text="This introduction is kind of broad and repeats the same claim several times.",
            action_type="custom-instruction",
            title="Custom instruction draft",
            note="Make it more formal and shorten repeated phrases, but keep the original claim unchanged.",
        )

        self.assertEqual(result["agentTrace"]["effectiveActionType"], "shorten")
        self.assertIn("instructionPlan", result["agentTrace"])
        intent_call = next(call for call in self.provider_calls if str(call["prompt_version"]).startswith("intent/custom-instruction"))
        rewrite_call = next(call for call in self.provider_calls if str(call["prompt_version"]).startswith("rewrite/custom-instruction"))
        self.assertIn("userRequest", intent_call["input_payload"])
        self.assertIn("instructionPlan", rewrite_call["input_payload"])
        self.assertNotIn("userNote", rewrite_call["input_payload"])
        self.assertTrue(result["text"].strip())

    def test_custom_instruction_requires_note(self) -> None:
        with self.assertRaisesRegex(ValueError, "Custom instruction cannot be empty"):
            self.service.revise_text(
                text="This paragraph needs revision.",
                action_type="custom-instruction",
                note="",
            )

    def test_local_hybrid_rag_indexes_and_feeds_revision_agent(self) -> None:
        bundle = self.service.create_project(
            title="LST revision project",
            doc_type="journal-article",
            language="en",
            source_type="text",
            note="Validate local RAG evidence for revision",
            text="The discussion should explain land surface temperature retrieval limitations.",
        )
        project_id = bundle["project"]["id"]
        literature = LiteratureService(database_path=self.database_path)
        imported = literature.import_literature(
            project_id,
            items=[
                {
                    "title": "Split-window retrieval of land surface temperature and emissivity",
                    "authors": ["Hall F"],
                    "year": 2024,
                    "venue": "Remote Sensing Methods",
                    "doi": "10.1000/local-rag",
                    "url": "https://example.org/local-rag",
                    "abstract": "Split-window algorithms estimate land surface temperature and emissivity from thermal infrared observations.",
                    "source": "manual",
                    "sources": ["manual"],
                    "language": "en",
                    "citationCount": 18,
                    "openAccessStatus": "metadata-only",
                    "tags": ["land-surface-temperature", "split-window", "emissivity"],
                }
            ],
        )
        indexed = literature.index_item_text(
            project_id=project_id,
            item_id=imported["items"][0]["id"],
            text=(
                "Split-window land surface temperature retrieval depends on paired thermal infrared bands. "
                "Uncertainty increases when emissivity assumptions are weak, so discussion sections should "
                "separate algorithmic limits from empirical validation results."
            ),
            source_label="local-test-fulltext",
        )

        rag = literature.search_project_evidence(
            project_id=project_id,
            query="split-window land surface temperature emissivity retrieval limitations",
            limit=3,
        )
        rag_status = literature.get_project_rag_status(project_id)
        result = self.service.revise_text(
            project_id=project_id,
            text="The discussion mentions LST retrieval but does not explain why emissivity assumptions matter.",
            action_type="expand",
            note="Use imported literature evidence if relevant.",
        )

        self.assertGreaterEqual(indexed["chunkCount"], 1)
        self.assertTrue(rag_status["ready"])
        self.assertEqual(rag_status["retrievalMode"], "local-hybrid-rag")
        self.assertGreaterEqual(rag_status["vectorCount"], rag_status["chunkCount"])
        self.assertEqual(rag["retrievalMode"], "local-hybrid-rag")
        self.assertGreaterEqual(len(rag["evidence"]), 1)
        self.assertEqual(rag["evidence"][0]["metadata"]["retrievalMode"], "local-hybrid-rag")
        self.assertGreaterEqual(result["evidenceStrategy"]["localRagEvidenceCount"], 1)
        self.assertIn("retriever", result["agentTrace"]["roleSequence"])
        self.assertTrue(
            any((item.get("metadata") or {}).get("retrievalMode") == "local-hybrid-rag" for item in result["evidence"])
        )

    def test_verify_citations_matches_project_library_metadata(self) -> None:
        bundle = self.service.create_project(
            title="Citation verification",
            doc_type="thesis",
            language="zh",
            source_type="text",
            note="Verify citation metadata against project literature",
            text="研究背景需要引用可核验的项目文献。",
        )
        project_id = bundle["project"]["id"]
        literature = LiteratureService(database_path=self.database_path)
        imported = literature.import_literature(
            project_id,
            items=[
                {
                    "title": "研究背景中的理论组织",
                    "authors": ["张三"],
                    "year": 2024,
                    "venue": "学术写作研究",
                    "doi": "10.1000/citation-check",
                    "url": "https://example.org/citation-check",
                    "abstract": "用于测试引用核验。",
                    "source": "manual",
                    "sources": ["manual"],
                    "language": "zh",
                    "citationCount": 5,
                    "openAccessStatus": "metadata-only",
                    "tags": ["citation", "verification"],
                }
            ],
        )

        verification = self.service.verify_citations(
            project_id=project_id,
            text="已有研究如张三（2024）所示，理论脉络的组织会影响背景论证质量，详见 doi:10.1000/citation-check。",
        )

        self.assertEqual(verification["citationVerification"]["status"], "verified")
        self.assertEqual(verification["citationVerification"]["verifiedMentionCount"], 2)
        self.assertEqual(verification["citationVerification"]["doiMentions"][0]["matchedItemId"], imported["items"][0]["id"])
        self.assertEqual(verification["citationVerification"]["authorYearMentions"][0]["matchedItemId"], imported["items"][0]["id"])
        self.assertEqual(verification["citationVerification"]["matchedItems"][0]["doi"], "10.1000/citation-check")

    def test_verify_numeric_citations_against_reference_list(self) -> None:
        bundle = self.service.create_project(
            title="Numeric citation verification",
            doc_type="journal-article",
            language="en",
            source_type="text",
            note="Verify bracketed citations against references",
            text=(
                "# Introduction\n"
                "The argument cites a numbered reference.\n\n"
                "# References\n"
                "[1] Zhang San. Theory organization in research background. "
                "Academic Writing Studies, 2024. doi:10.1000/numeric-check."
            ),
        )
        project_id = bundle["project"]["id"]
        literature = LiteratureService(database_path=self.database_path)
        imported = literature.import_literature(
            project_id,
            items=[
                {
                    "title": "Theory organization in research background",
                    "authors": ["Zhang San"],
                    "year": 2024,
                    "venue": "Academic Writing Studies",
                    "doi": "10.1000/numeric-check",
                    "url": "https://example.org/numeric-check",
                    "abstract": "A test item for numeric citation verification.",
                    "source": "manual",
                    "sources": ["manual"],
                    "language": "en",
                    "citationCount": 5,
                    "openAccessStatus": "metadata-only",
                    "tags": ["citation", "numeric"],
                }
            ],
        )

        verification = self.service.verify_citations(
            project_id=project_id,
            text="Prior work supports the claim [1].",
        )["citationVerification"]

        self.assertEqual(verification["status"], "verified")
        self.assertEqual(verification["referenceEntryCount"], 1)
        self.assertTrue(verification["numericMentions"][0]["verified"])
        self.assertEqual(
            verification["numericMentions"][0]["resolvedEntries"][0]["matchedItemId"],
            imported["items"][0]["id"],
        )
        self.assertIn("numeric", verification["matchedItems"][0]["matchKinds"])

    def test_format_citations_exports_matched_bibliography(self) -> None:
        bundle = self.service.create_project(
            title="Citation formatter",
            doc_type="thesis",
            language="en",
            source_type="text",
            note="Format imported references",
            text="The draft will export a bibliography.",
        )
        project_id = bundle["project"]["id"]
        literature = LiteratureService(database_path=self.database_path)
        imported = literature.import_literature(
            project_id,
            items=[
                {
                    "title": "Traceable academic revision",
                    "authors": ["Li Ming", "Wang Yue"],
                    "year": 2025,
                    "venue": "Journal of Academic Writing",
                    "doi": "10.1000/format-check",
                    "url": "https://example.org/format-check",
                    "abstract": "A test item for citation formatting.",
                    "source": "manual",
                    "sources": ["manual"],
                    "language": "en",
                    "citationCount": 3,
                    "openAccessStatus": "metadata-only",
                    "tags": ["citation", "format"],
                }
            ],
        )

        gb = self.service.format_citations(
            project_id=project_id,
            style="gb7714",
            text="This claim cites doi:10.1000/format-check.",
            matched_only=True,
        )
        ieee = self.service.format_citations(
            project_id=project_id,
            style="ieee",
            item_ids=[imported["items"][0]["id"]],
            matched_only=False,
        )

        self.assertEqual(len(gb["entries"]), 1)
        self.assertIn("Traceable academic revision", gb["bibliographyText"])
        self.assertIn("DOI:10.1000/format-check", gb["bibliographyText"])
        self.assertTrue(ieee["bibliographyText"].startswith("[1]"))

    def test_action_specific_prompt_registry_lookup(self) -> None:
        action_prompt = self.service._load_prompt("rewrite", "zh", "academic-rewrite")
        fallback_prompt = self.service._load_prompt("rewrite", "zh", "unknown-action")

        self.assertIn("rewrite/academic-rewrite.zh@", action_prompt["version_tag"])
        self.assertIn("schema_hint", action_prompt)
        self.assertIn("rewrite/default.zh@", fallback_prompt["version_tag"])

    def test_revision_literature_scout_requires_import_before_use(self) -> None:
        bundle = self.service.create_project(
            title="文献侦察项目",
            doc_type="thesis",
            language="zh",
            source_type="text",
            note="准备做按证据增强的改稿",
            text="研究背景部分需要更扎实的文献支撑和更明确的理论脉络。",
        )
        project_id = bundle["project"]["id"]
        fake_search = {
            "run": {
                "id": "lit-run-test",
                "projectId": project_id,
                "query": "研究背景 理论脉络 学术写作",
                "sources": ["openalex", "crossref", "semantic-scholar"],
                "status": "completed",
                "totalFound": 1,
                "dedupedCount": 1,
                "warnings": [],
                "createdAt": "2026-04-22T00:00:00Z",
                "updatedAt": "2026-04-22T00:00:00Z",
            },
            "candidates": [
                {
                    "id": "candidate-live-1",
                    "title": "研究背景写作中的理论脉络构建",
                    "authors": ["张三"],
                    "year": 2024,
                    "venue": "学术写作研究",
                    "doi": "10.1000/live-scout",
                    "url": "https://example.org/live-scout",
                    "abstract": "讨论如何在研究背景中组织理论脉络并增强论证支撑。",
                    "source": "openalex",
                    "sources": ["openalex", "crossref"],
                    "language": "zh",
                    "citationCount": 8,
                    "openAccessStatus": "metadata-only",
                    "pdfUrl": "",
                    "zoteroItemKey": None,
                    "tags": ["academic-writing", "background"],
                }
            ],
        }

        with patch("backend.literature.LiteratureService.search_literature", return_value=fake_search):
            result = self.service.scout_revision_literature(
                project_id=project_id,
                text="研究背景部分需要更扎实的文献支撑和更明确的理论脉络。",
                action_type="expand",
                note="先给我联网找候选文献，但不要直接用于改稿。",
            )

        self.assertEqual(result["projectId"], project_id)
        self.assertTrue(result["confirmBeforeUse"]["mustImportBeforeUse"])
        self.assertFalse(result["confirmBeforeUse"]["eligibleForRevisionNow"])
        self.assertEqual(result["confirmBeforeUse"]["importEndpoint"], f"/api/projects/{project_id}/literature/import")
        self.assertEqual(result["search"]["run"]["id"], "lit-run-test")
        self.assertEqual(result["search"]["candidates"][0]["doi"], "10.1000/live-scout")
        self.assertEqual(result["evidenceStrategy"]["sourceOrder"][0], "openalex")

    def test_revision_agent_uses_indexed_literature_chunks(self) -> None:
        bundle = self.service.create_project(
            title="Chunk RAG 项目",
            doc_type="thesis",
            language="zh",
            source_type="text",
            note="验证文献 chunk 检索进入改稿证据",
            text="研究背景需要补充关于理论脉络和论证支撑的参考文献。",
        )
        project_id = bundle["project"]["id"]
        literature = LiteratureService(database_path=self.database_path)
        imported = literature.import_literature(
            project_id,
            items=[
                {
                    "title": "研究背景中的理论脉络组织",
                    "authors": ["张三"],
                    "year": 2024,
                    "venue": "学术写作研究",
                    "doi": "10.1000/chunk-rag",
                    "url": "https://example.org/chunk-rag",
                    "abstract": "这是一条较短的摘要。",
                    "source": "manual",
                    "sources": ["manual"],
                    "language": "zh",
                    "citationCount": 3,
                    "openAccessStatus": "metadata-only",
                    "tags": ["academic-writing", "background"],
                }
            ],
        )
        item_id = imported["items"][0]["id"]
        indexed = literature.index_item_text(
            project_id=project_id,
            item_id=item_id,
            text="理论脉络的清晰组织能够帮助研究背景建立从研究问题到方法设计的论证支撑，并减少口语化表达。",
            source_label="manual-fulltext",
        )

        result = self.service.revise_text(
            project_id=project_id,
            text="研究背景部分需要更明确的理论脉络和更扎实的论证支撑。",
            action_type="expand",
            note="优先参考已导入文献的正文片段。",
        )

        self.assertGreaterEqual(indexed["chunkCount"], 1)
        self.assertTrue(
            any(
                item["source_kind"] == "literature"
                and item.get("metadata", {}).get("chunkSourceKind") == "manual-fulltext"
                for item in result["evidence"]
            )
        )
        self.assertEqual(result["evidenceStrategy"]["mode"], "library-first")


    def test_txt_upload_preserves_markdown_headings(self) -> None:
        bundle = self.service.create_project(
            title="Heading upload",
            doc_type="thesis",
            language="zh",
            source_type="file",
            note="Upload a txt draft",
            text="",
        )
        uploaded = self.service.upload_file(
            project_id=bundle["project"]["id"],
            file_name="draft.txt",
            content_type="text/plain",
            raw_bytes=("# \u6458\u8981\n\u672c\u6587\u662f\u4e00\u6bb5\u6458\u8981\u3002\n\n# \u7ed3\u8bba\n\u9700\u8981\u6536\u675f\u7814\u7a76\u8d21\u732e\u3002").encode("utf-8"),
            fallback_text="",
        )

        self.assertEqual([section["title"] for section in uploaded["sections"]], ["\u6458\u8981", "\u7ed3\u8bba"])
        preview = self.service.get_project_source_file(bundle["project"]["id"])["file"]
        self.assertEqual(preview["previewKind"], "text")
        self.assertEqual(preview["viewerKind"], "text")
        self.assertEqual(preview["previewStatus"], "plain-text")
        path, content_type, file_name = self.service.get_source_file_content(preview["fileId"])
        self.assertTrue(path.exists())
        self.assertEqual(content_type, "text/plain")
        self.assertEqual(file_name, "draft.txt")

    def test_split_into_sections_prefers_academic_headings_over_generic_numbers(self) -> None:
        sections = split_into_sections(
            "Page 1 of 15\n1\n2\nABSTRACT\nThis paper introduces a hybrid retrieval method.\n\nI. INTRODUCTION\nThe introduction motivates the study.\n\nII. METHODS\nThe method section explains the workflow.",
            "en",
        )

        titles = [section["title"] for section in sections]
        self.assertEqual(titles[:3], ["Abstract", "Introduction", "Methods"])
        self.assertFalse(any(title.lower().startswith("section ") for title in titles))

    def test_delete_project_removes_related_records(self) -> None:
        bundle = self.service.create_project(
            title="Delete me",
            doc_type="thesis",
            language="zh",
            source_type="text",
            note="Delete workflow",
            text="第一段。\n\n第二段。",
        )
        project_id = bundle["project"]["id"]
        deleted = self.service.delete_project(project_id)

        self.assertEqual(deleted["deletedProjectId"], project_id)
        with self.assertRaises(KeyError):
            self.service.get_project_bundle(project_id)


class FakeZotero:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def create_collections(self, collections: list[dict]) -> dict:
        return {"successful": {"0": {"key": "COLL1234", "data": {"key": "COLL1234"}}}}

    def item_template(self, item_type: str) -> dict:
        return {"itemType": item_type}

    def create_items(self, items: list[dict]) -> dict:
        self.items.extend(items)
        return {"successful": {"0": {"key": "ITEM1234", "data": {"key": "ITEM1234"}}}}


class LiteratureServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_tmp_root = Path(__file__).resolve().parent / ".tmp"
        self.workspace_tmp_root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.workspace_tmp_root / f"literature-test-{uuid4().hex}.sqlite3"
        os.environ.pop("DRAFTREFINE_DATABASE_PATH", None)
        os.environ["DRAFTREFINE_DEEPSEEK_API_KEY"] = ""
        os.environ["DRAFTREFINE_QWEN_API_KEY"] = ""
        self.provider_patcher = patch.object(
            BackendService,
            "_call_provider_json",
            autospec=True,
            side_effect=self._fake_call_provider_json,
        )
        self.provider_patcher.start()
        self.backend = BackendService(database_path=self.database_path)
        bundle = self.backend.create_project(
            title="Literature validation",
            doc_type="thesis",
            language="zh",
            source_type="text",
            note="Validate literature workflow",
            text="本研究讨论城市热环境与遥感指标。\n\n第二节解释多源数据融合。",
        )
        self.project_id = bundle["project"]["id"]

    def tearDown(self) -> None:
        self.provider_patcher.stop()
        if self.database_path.exists():
            self.database_path.unlink()
        storage_dir = self.workspace_tmp_root / f"{self.database_path.stem}_literature"
        if storage_dir.exists():
            shutil.rmtree(storage_dir, ignore_errors=True)

    def _fake_call_provider_json(
        self,
        _service: BackendService,
        *,
        action_name: str,
        prompt_version: str,
        system_prompt: str,
        input_payload: dict,
        schema_hint: str,
    ) -> tuple[object, dict[str, object]]:
        if action_name == "diagnose":
            sections = input_payload.get("sections", [])
            target = sections[0] if sections else {"id": "section-1"}
            return (
                [
                    {
                        "section_id": target["id"],
                        "issue_type": "structure",
                        "severity": "medium",
                        "title": "Need tighter structure",
                        "detail": "The section can be tightened for clearer academic flow.",
                        "suggested_action": "Start with one focused revision pass.",
                    }
                ],
                {
                    "provider": "mock-diagnose",
                    "model": "diagnose-stub",
                    "status": "completed",
                    "latency_ms": 12,
                    "error": None,
                },
            )
        if action_name == "comment-map":
            sections = list(input_payload.get("sections", []))
            target = sections[0] if sections else {"id": "section-1"}
            return (
                {
                    "mapped_section_id": target["id"],
                    "confidence": 0.91,
                    "suggested_action": "Run one targeted revision pass.",
                },
                {
                    "provider": "mock-comment-map",
                    "model": "comment-map-stub",
                    "status": "completed",
                    "latency_ms": 12,
                    "error": None,
                },
            )
        return (
            {},
            {
                "provider": "mock-provider",
                "model": "mock-model",
                "status": "completed",
                "latency_ms": 12,
                "error": None,
            },
        )

    def test_literature_search_dedupes_openalex_and_crossref(self) -> None:
        def fake_http_json(url: str, headers: dict[str, str] | None = None) -> dict:
            if "openalex" in url:
                return {
                    "results": [
                        {
                            "title": "Urban thermal environment mapping",
                            "type": "article",
                            "publication_year": 2024,
                            "doi": "https://doi.org/10.1000/example",
                            "cited_by_count": 12,
                            "authorships": [{"author": {"display_name": "Jane Wang"}}],
                            "primary_location": {
                                "pdf_url": "https://example.org/open.pdf",
                                "source": {
                                    "id": "https://openalex.org/S123",
                                    "display_name": "Remote Sensing",
                                    "type": "journal",
                                    "issn": ["0034-4257"],
                                    "issn_l": "0034-4257",
                                    "host_organization_name": "Elsevier",
                                    "is_in_doaj": False,
                                    "is_core": True,
                                },
                            },
                            "open_access": {"is_oa": True, "oa_url": "https://example.org/open.pdf"},
                            "abstract_inverted_index": {"Urban": [0], "thermal": [1], "mapping": [2]},
                        }
                    ]
                }
            if "crossref" in url:
                return {
                    "message": {
                        "items": [
                            {
                                "title": ["Urban thermal environment mapping"],
                                "type": "journal-article",
                                "DOI": "10.1000/example",
                                "author": [{"given": "Jane", "family": "Wang"}],
                                "issued": {"date-parts": [[2024]]},
                                "container-title": ["Remote Sensing"],
                                "URL": "https://doi.org/10.1000/example",
                                "is-referenced-by-count": 18,
                            }
                        ]
                    }
                }
            return {"data": []}

        literature = LiteratureService(database_path=self.database_path, http_json=fake_http_json)
        response = literature.search_literature(self.project_id, "urban thermal mapping", ["openalex", "crossref"], limit=10)

        self.assertEqual(response["run"]["dedupedCount"], 1)
        self.assertEqual(response["candidates"][0]["doi"], "10.1000/example")
        self.assertEqual(response["candidates"][0]["openAccessStatus"], "open")
        self.assertEqual(set(response["candidates"][0]["sources"]), {"openalex", "crossref"})
        self.assertEqual(response["candidates"][0]["publicationType"], "journal-article")
        self.assertEqual(response["candidates"][0]["issnL"], "0034-4257")
        self.assertIn("期刊论文", response["candidates"][0]["qualitySignals"])

    def test_long_chinese_query_filters_single_acronym_matches(self) -> None:
        def fake_http_json(url: str, headers: dict[str, str] | None = None) -> dict:
            if "openalex" in url:
                return {
                    "results": [
                        {
                            "title": "Effective Hyperon-Nucleon Interaction Derived from Nijmegen OBE Model",
                            "type": "article",
                            "publication_year": 1985,
                            "doi": "https://doi.org/10.1000/nuclear-obe",
                            "cited_by_count": 56,
                            "authorships": [{"author": {"display_name": "Y. Yamamoto"}}],
                            "primary_location": {"source": {"display_name": "Physics Journal", "type": "journal"}},
                            "open_access": {"is_oa": False},
                            "abstract_inverted_index": {"OBE": [0], "model": [1]},
                        },
                        {
                            "title": "基于OBE理念的小学体育课程思政教学理念与实践路径研究",
                            "type": "article",
                            "publication_year": 2024,
                            "doi": "https://doi.org/10.1000/obe-pe-course",
                            "cited_by_count": 3,
                            "authorships": [{"author": {"display_name": "李明"}}],
                            "primary_location": {"source": {"display_name": "体育教学研究", "type": "journal"}},
                            "open_access": {"is_oa": False},
                            "abstract_inverted_index": {"小学": [0], "体育": [1], "课程": [2], "思政": [3], "OBE": [4]},
                        },
                    ]
                }
            return {"message": {"items": []}, "data": []}

        literature = LiteratureService(database_path=self.database_path, http_json=fake_http_json)
        response = literature.search_literature(
            self.project_id,
            "基于OBE理念的小学体育课程思政教学理念与实践路径研究",
            ["openalex"],
            limit=10,
        )

        titles = [item["title"] for item in response["candidates"]]
        self.assertIn("基于OBE理念的小学体育课程思政教学理念与实践路径研究", titles)
        self.assertNotIn("Effective Hyperon-Nucleon Interaction Derived from Nijmegen OBE Model", titles)

    def test_suggest_query_uses_uploaded_article_text(self) -> None:
        bundle = self.backend.create_project(
            title="基于OBE理念的小学体育课程思政教学理念与实践路径研究",
            doc_type="thesis",
            language="zh",
            source_type="text",
            note="Generate literature query",
            text="本文围绕OBE理念、小学体育课程思政、教学实践路径展开研究。\n\n研究重点包括体育课程思政的目标设计、教学评价与实践路径。",
        )
        literature = LiteratureService(database_path=self.database_path)
        suggestion = literature.suggest_search_query(bundle["project"]["id"])
        terms = [item["term"] for item in suggestion["terms"]]

        self.assertTrue(any("OBE" in term for term in terms))
        self.assertIn("课程思政", terms)
        self.assertIn("小学体育", terms)
        self.assertIn("实践路径", terms)
        self.assertIn("课程思政", suggestion["query"])

    def test_import_fetch_fulltext_and_download_bundle(self) -> None:
        literature = LiteratureService(database_path=self.database_path)
        imported = literature.import_literature(
            self.project_id,
            items=[
                {
                    "id": "candidate-1",
                    "title": "Open paper for urban heat",
                    "authors": ["Jane Wang"],
                    "year": 2025,
                    "venue": "Open Journal",
                    "doi": "10.1000/open",
                    "url": "https://example.org/open",
                    "abstract": "Open access evidence.",
                    "source": "openalex",
                    "sources": ["openalex"],
                    "language": "en",
                    "citationCount": 5,
                    "openAccessStatus": "open",
                    "pdfUrl": "https://example.org/open.pdf",
                    "tags": ["开放全文可获取"],
                }
            ],
        )
        item_id = imported["items"][0]["id"]
        with patch.object(LiteratureService, "_download_binary", return_value=b"%PDF-1.4 fake"), patch.object(
            LiteratureService,
            "_extract_pdf_text",
            return_value="Urban heat mitigation evidence from an open-access PDF supports revising the background section.",
        ):
            attachment = literature.fetch_open_fulltext(self.project_id, item_id)
        batch = literature.fetch_open_fulltext_batch(self.project_id, [item_id])
        library = literature.list_project_literature(self.project_id)

        self.assertEqual(attachment["attachment"]["status"], "downloaded")
        self.assertTrue(attachment["attachment"]["localPath"])
        self.assertGreaterEqual(attachment["indexing"]["chunkCount"], 1)
        self.assertTrue(
            any(chunk["sourceKind"] == "oa-pdf-fulltext" for chunk in library["chunks"])
        )
        self.assertEqual(batch["downloadedCount"], 1)
        self.assertTrue(batch["downloadUrl"])

    def test_fetch_open_fulltext_falls_back_to_linked_attachment_on_download_failure(self) -> None:
        literature = LiteratureService(database_path=self.database_path)
        imported = literature.import_literature(
            self.project_id,
            items=[
                {
                    "id": "candidate-oa-fallback",
                    "title": "Fallback open paper",
                    "authors": ["Jane Wang"],
                    "year": 2025,
                    "venue": "Open Journal",
                    "doi": "10.1000/open-fallback",
                    "url": "https://example.org/open-fallback",
                    "abstract": "Open access evidence that still fails to download.",
                    "source": "openalex",
                    "sources": ["openalex"],
                    "language": "en",
                    "citationCount": 2,
                    "openAccessStatus": "open",
                    "pdfUrl": "https://example.org/open-fallback.pdf",
                    "tags": ["fallback"],
                }
            ],
        )

        with patch.object(LiteratureService, "_download_binary", side_effect=RuntimeError("download failed")):
            attachment = literature.fetch_open_fulltext(self.project_id, imported["items"][0]["id"])

        self.assertEqual(attachment["attachment"]["status"], "linked")
        self.assertEqual(attachment["attachment"]["localPath"], "")
        self.assertIsNone(attachment["indexing"])
        self.assertIn("download failed", attachment["warning"])

    def test_non_open_fulltext_is_rejected(self) -> None:
        literature = LiteratureService(database_path=self.database_path)
        imported = literature.import_literature(
            self.project_id,
            items=[
                {
                    "id": "candidate-2",
                    "title": "Metadata only paper",
                    "authors": [],
                    "year": 2023,
                    "venue": "Closed Journal",
                    "doi": "",
                    "url": "https://example.org/meta",
                    "abstract": "",
                    "source": "crossref",
                    "sources": ["crossref"],
                    "language": "en",
                    "citationCount": 0,
                    "openAccessStatus": "metadata-only",
                    "pdfUrl": "",
                    "tags": ["仅元数据"],
                }
            ],
        )

        with self.assertRaises(ValueError):
            literature.fetch_open_fulltext(self.project_id, imported["items"][0]["id"])



if __name__ == "__main__":
    unittest.main()

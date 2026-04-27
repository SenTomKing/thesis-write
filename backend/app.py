from __future__ import annotations

import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import os

from .literature import LiteratureService
from .service import BackendService

service = BackendService()
literature_service = LiteratureService(service.database_path)


class ProjectCreateRequest(BaseModel):
    title: str = Field(min_length=1)
    type: str
    language: str
    sourceType: str
    note: str = ""
    text: str = ""


class RewriteRequest(BaseModel):
    actionType: str
    currentText: str
    commentIds: list[str] = Field(default_factory=list)
    selectedText: str = ""
    selectionStart: int | None = None
    selectionEnd: int | None = None
    commentId: str | None = None
    feedback: str = ""
    previousCandidateText: str = ""


class AgentRevisionRequest(BaseModel):
    text: str = Field(min_length=1)
    actionType: str
    projectId: str | None = None
    title: str = ""
    note: str = ""
    selectedText: str = ""
    selectionStart: int | None = None
    selectionEnd: int | None = None
    commentId: str | None = None
    previousCandidateText: str = ""


class AgentLiteratureScoutRequest(BaseModel):
    projectId: str
    text: str = Field(min_length=1)
    actionType: str
    title: str = ""
    note: str = ""
    commentId: str | None = None
    sources: list[str] = Field(default_factory=list)
    limit: int = 8


class CitationVerifyRequest(BaseModel):
    projectId: str
    text: str = Field(min_length=1)


class CitationFormatRequest(BaseModel):
    projectId: str
    style: str = "gb7714"
    itemIds: list[str] = Field(default_factory=list)
    text: str = ""
    matchedOnly: bool = True


class ManualEditRequest(BaseModel):
    newText: str


class CandidateReviseRequest(BaseModel):
    feedback: str = Field(min_length=1)


class CommentImportRequest(BaseModel):
    projectId: str
    rawComment: str = Field(min_length=1)


class CommentRemapRequest(BaseModel):
    sectionId: str


class StatusUpdateRequest(BaseModel):
    status: str


class LiteratureSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)
    limit: int = 12


class LiteratureImportRequest(BaseModel):
    runId: str | None = None
    itemIds: list[str] = Field(default_factory=list)
    items: list[dict] = Field(default_factory=list)


class LiteratureIndexTextRequest(BaseModel):
    text: str = Field(min_length=1)
    sourceLabel: str = "manual-fulltext"


class LiteratureRagSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = 8


class LiteratureBatchFetchRequest(BaseModel):
    itemIds: list[str] = Field(default_factory=list)


app = FastAPI(title="DraftRefine API", version="0.2.0")


def configured_allowed_origins() -> list[str]:
    raw = (os.getenv("DRAFTREFINE_ALLOWED_ORIGINS") or "").strip()
    if not raw:
        return [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


def upstream_failure(exc: RuntimeError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


@app.get("/api/projects")
def list_projects(scope: str = Query(default="active")) -> list[dict]:
    return service.list_projects(scope=scope)


@app.get("/api/projects/{project_id}/bundle")
def get_project_bundle(project_id: str) -> dict:
    try:
        return service.get_project_bundle(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None


@app.get("/api/projects/{project_id}/source-file")
def get_project_source_file(project_id: str) -> dict:
    try:
        return service.get_project_source_file(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None


@app.get("/api/source-files/{file_id}/content")
def get_source_file_content(file_id: str) -> FileResponse:
    try:
        path, content_type, file_name = service.get_source_file_content(file_id)
        return FileResponse(path, media_type=content_type, filename=file_name)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source file not found.") from None


@app.get("/api/source-files/{file_id}/preview")
def get_source_file_preview(file_id: str) -> FileResponse:
    try:
        path, content_type, file_name = service.get_source_file_preview_content(file_id)
        return FileResponse(path, media_type=content_type, headers={"Content-Disposition": f'inline; filename="{file_name}"'})
    except KeyError:
        raise HTTPException(status_code=404, detail="Preview file not found.") from None


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, permanent: bool = Query(default=False)) -> dict:
    try:
        return service.delete_project(project_id, permanent=permanent)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None


@app.post("/api/projects/{project_id}/restore")
def restore_project(project_id: str) -> dict:
    try:
        return service.restore_project(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    try:
        return service.get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found.") from None


@app.post("/api/projects")
def create_project(payload: ProjectCreateRequest) -> dict:
    return service.create_project(
        title=payload.title,
        doc_type=payload.type,
        language=payload.language,
        source_type=payload.sourceType,
        note=payload.note,
        text=payload.text,
    )


@app.post("/api/projects/{project_id}/files")
async def upload_project_file(project_id: str, file: UploadFile = File(...), fallbackText: str = "") -> dict:
    try:
        raw_bytes = await file.read()
        return service.upload_file(
            project_id=project_id,
            file_name=file.filename or "draft.bin",
            content_type=file.content_type or "application/octet-stream",
            raw_bytes=raw_bytes,
            fallback_text=fallbackText,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/diagnose")
def diagnose_project(project_id: str) -> dict:
    try:
        return service.diagnose_project(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc


@app.post("/api/sections/{section_id}/rewrite")
def rewrite_section(section_id: str, payload: RewriteRequest) -> dict:
    try:
        return service.request_revision(
            section_id=section_id,
            action_type=payload.actionType,
            current_text=payload.currentText,
            comment_ids=payload.commentIds,
            selected_text=payload.selectedText,
            selection_start=payload.selectionStart,
            selection_end=payload.selectionEnd,
            comment_id=payload.commentId,
            feedback=payload.feedback,
            previous_candidate_text=payload.previousCandidateText,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Section not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="后端处理改写请求时发生异常，请重试。") from exc


@app.post("/api/agent/revise")
def revise_text(payload: AgentRevisionRequest) -> dict:
    try:
        return service.revise_text(
            text=payload.text,
            action_type=payload.actionType,
            project_id=payload.projectId,
            title=payload.title,
            note=payload.note,
            selected_text=payload.selectedText,
            selection_start=payload.selectionStart,
            selection_end=payload.selectionEnd,
            comment_id=payload.commentId,
            previous_candidate_text=payload.previousCandidateText,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Project or comment not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="后端处理改写请求时发生异常，请重试。") from exc


@app.post("/api/agent/literature-scout")
def scout_revision_literature(payload: AgentLiteratureScoutRequest) -> dict:
    try:
        return service.scout_revision_literature(
            project_id=payload.projectId,
            text=payload.text,
            action_type=payload.actionType,
            title=payload.title,
            note=payload.note,
            comment_id=payload.commentId,
            sources=payload.sources,
            limit=payload.limit,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Project or comment not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agent/verify-citations")
def verify_citations(payload: CitationVerifyRequest) -> dict:
    try:
        return service.verify_citations(project_id=payload.projectId, text=payload.text)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agent/format-citations")
def format_citations(payload: CitationFormatRequest) -> dict:
    try:
        return service.format_citations(
            project_id=payload.projectId,
            style=payload.style,
            item_ids=payload.itemIds,
            text=payload.text,
            matched_only=payload.matchedOnly,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sections/{section_id}/manual-edit")
def save_manual_edit(section_id: str, payload: ManualEditRequest) -> dict:
    try:
        return service.save_manual_edit(section_id=section_id, new_text=payload.newText)
    except KeyError:
        raise HTTPException(status_code=404, detail="Section not found.") from None


@app.post("/api/comments/import")
def import_comment(payload: CommentImportRequest) -> dict:
    try:
        return service.import_comment(project_id=payload.projectId, raw_comment=payload.rawComment)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc


@app.post("/api/comments/{comment_id}/remap")
def remap_comment(comment_id: str, payload: CommentRemapRequest) -> dict:
    try:
        return service.remap_comment(comment_id=comment_id, section_id=payload.sectionId)
    except KeyError:
        raise HTTPException(status_code=404, detail="Comment not found.") from None


@app.post("/api/comments/{comment_id}/status")
def update_comment_status(comment_id: str, payload: StatusUpdateRequest) -> dict:
    try:
        return service.update_comment_status(comment_id=comment_id, status=payload.status)
    except KeyError:
        raise HTTPException(status_code=404, detail="Comment not found.") from None


@app.post("/api/issues/{issue_id}/status")
def update_issue_status(issue_id: str, payload: StatusUpdateRequest) -> dict:
    try:
        return service.update_issue_status(issue_id=issue_id, status=payload.status)
    except KeyError:
        raise HTTPException(status_code=404, detail="Issue not found.") from None


@app.post("/api/revisions/{candidate_id}/accept")
def accept_revision(candidate_id: str) -> dict:
    try:
        return service.accept_revision_candidate(candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Revision candidate not found.") from None


@app.post("/api/revisions/{candidate_id}/reject")
def reject_revision(candidate_id: str) -> dict:
    try:
        return service.reject_revision_candidate(candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Revision candidate not found.") from None


@app.post("/api/revisions/{candidate_id}/revise")
def revise_revision(candidate_id: str, payload: CandidateReviseRequest) -> dict:
    try:
        return service.revise_revision_candidate(candidate_id, payload.feedback)
    except KeyError:
        raise HTTPException(status_code=404, detail="Revision candidate not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc


@app.post("/api/versions/{revision_id}/restore")
def restore_version(revision_id: str) -> dict:
    try:
        return service.restore_revision(revision_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Revision not found.") from None


@app.post("/api/projects/{project_id}/literature/search")
def search_literature(project_id: str, payload: LiteratureSearchRequest) -> dict:
    try:
        return literature_service.search_literature(
            project_id=project_id,
            query=payload.query,
            sources=payload.sources,
            limit=payload.limit,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/literature")
def list_literature(project_id: str) -> dict:
    try:
        return literature_service.list_project_literature(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None


@app.get("/api/projects/{project_id}/literature/rag-status")
def get_literature_rag_status(project_id: str) -> dict:
    try:
        return literature_service.get_project_rag_status(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None


@app.get("/api/projects/{project_id}/literature/suggest-query")
def suggest_literature_query(project_id: str) -> dict:
    try:
        return literature_service.suggest_search_query(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None


@app.post("/api/projects/{project_id}/literature/import")
def import_literature(project_id: str, payload: LiteratureImportRequest) -> dict:
    try:
        return literature_service.import_literature(
            project_id=project_id,
            run_id=payload.runId,
            item_ids=payload.itemIds,
            items=payload.items,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Project or search run not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/literature/{item_id}/fetch-open-fulltext")
def fetch_open_fulltext(project_id: str, item_id: str) -> dict:
    try:
        return literature_service.fetch_open_fulltext(project_id=project_id, item_id=item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Literature item not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/literature/fetch-open-fulltext-batch")
def fetch_open_fulltext_batch(project_id: str, payload: LiteratureBatchFetchRequest) -> dict:
    try:
        return literature_service.fetch_open_fulltext_batch(project_id=project_id, item_ids=payload.itemIds)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project or literature item not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/literature/attachments/{attachment_id}/download")
def download_literature_attachment(project_id: str, attachment_id: str) -> FileResponse:
    try:
        path, content_type, file_name = literature_service.get_attachment_download(project_id=project_id, attachment_id=attachment_id)
        return FileResponse(path, media_type=content_type, filename=file_name)
    except KeyError:
        raise HTTPException(status_code=404, detail="Attachment not found.") from None


@app.get("/api/projects/{project_id}/literature/download-bundles/{bundle_name}")
def download_literature_bundle(project_id: str, bundle_name: str) -> FileResponse:
    try:
        path, content_type, file_name = literature_service.get_download_bundle(project_id=project_id, bundle_name=bundle_name)
        return FileResponse(path, media_type=content_type, filename=file_name)
    except KeyError:
        raise HTTPException(status_code=404, detail="Download bundle not found.") from None


@app.post("/api/projects/{project_id}/literature/{item_id}/index-text")
def index_literature_text(project_id: str, item_id: str, payload: LiteratureIndexTextRequest) -> dict:
    try:
        return literature_service.index_item_text(
            project_id=project_id,
            item_id=item_id,
            text=payload.text,
            source_label=payload.sourceLabel,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Project or literature item not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/literature/rag-search")
def search_literature_evidence(project_id: str, payload: LiteratureRagSearchRequest) -> dict:
    try:
        return literature_service.search_project_evidence(
            project_id=project_id,
            query=payload.query,
            limit=payload.limit,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/literature/search-runs/{run_id}")
def get_literature_search_run(run_id: str) -> dict:
    try:
        return literature_service.get_search_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Search run not found.") from None


def main() -> None:
    uvicorn.run("backend.app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()

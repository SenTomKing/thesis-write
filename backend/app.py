from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .literature import LiteratureService
from .service import BackendService

SESSION_COOKIE_NAME = "draftrefine_session"

service = BackendService()
literature_service = LiteratureService(service.database_path)


class AuthRegisterRequest(BaseModel):
    email: str = Field(min_length=3)
    username: str = Field(min_length=3)
    password: str = Field(min_length=8)
    inviteCode: str = ""


class AuthLoginRequest(BaseModel):
    identifier: str = Field(min_length=3)
    password: str = Field(min_length=8)


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
    mode: str = "normal"
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


class ProjectFileIngestRequest(BaseModel):
    storageRef: str = Field(min_length=1)
    fileName: str = Field(min_length=1)
    contentType: str = "application/octet-stream"
    fallbackText: str = ""


app = FastAPI(title="DraftRefine API", version="0.3.0")


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


def upstream_failure(exc: RuntimeError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


def session_cookie_max_age_seconds() -> int:
    return max(86400, service._session_days() * 24 * 60 * 60)


def is_secure_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.lower() == "https"


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=is_secure_request(request),
        samesite="lax",
        max_age=session_cookie_max_age_seconds(),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=is_secure_request(request),
        samesite="lax",
    )


def current_user(request: Request) -> dict[str, Any] | None:
    return service.get_user_by_session(request.cookies.get(SESSION_COOKIE_NAME))


def require_current_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录。")
    return user


def require_project_access(request: Request, project_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    try:
        return service.ensure_project_access(project_id, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None


def require_source_file_access(request: Request, file_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    try:
        return service.ensure_source_file_access(file_id, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="文件不存在。") from None


def require_section_access(request: Request, section_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    try:
        return service.ensure_section_access(section_id, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="章节不存在。") from None


def require_comment_access(request: Request, comment_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    try:
        return service.ensure_comment_access(comment_id, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="意见不存在。") from None


def require_issue_access(request: Request, issue_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    try:
        return service.ensure_issue_access(issue_id, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="问题不存在。") from None


def require_candidate_access(request: Request, candidate_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    try:
        return service.ensure_revision_candidate_access(candidate_id, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="改写候选不存在。") from None


def require_revision_access(request: Request, revision_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    try:
        return service.ensure_revision_access(revision_id, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="版本不存在。") from None


def require_job_access(request: Request, job_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    try:
        return service.ensure_job_access(job_id, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在。") from None


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth/status")
def auth_status() -> dict[str, Any]:
    return service.auth_status()


@app.post("/api/auth/register")
def register_auth(payload: AuthRegisterRequest, request: Request) -> JSONResponse:
    try:
        user, token = service.register_user(
            email=payload.email,
            username=payload.username,
            password=payload.password,
            invite_code=payload.inviteCode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = JSONResponse({"user": user})
    set_session_cookie(response, token, request)
    return response


@app.post("/api/auth/login")
def login_auth(payload: AuthLoginRequest, request: Request) -> JSONResponse:
    try:
        user, token = service.login_user(identifier=payload.identifier, password=payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = JSONResponse({"user": user})
    set_session_cookie(response, token, request)
    return response


@app.post("/api/auth/logout")
def logout_auth(request: Request) -> JSONResponse:
    service.delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({"ok": True})
    clear_session_cookie(response, request)
    return response


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    user = require_current_user(request)
    return {"user": user}


@app.get("/api/projects")
def list_projects(request: Request, scope: str = Query(default="active")) -> list[dict]:
    user = require_current_user(request)
    return service.list_projects(scope=scope, owner_user_id=user["id"])


@app.get("/api/projects/{project_id}/bundle")
def get_project_bundle(request: Request, project_id: str) -> dict:
    require_project_access(request, project_id)
    try:
        return service.get_project_bundle(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None


@app.get("/api/projects/{project_id}/source-file")
def get_project_source_file(request: Request, project_id: str) -> dict:
    require_project_access(request, project_id)
    try:
        return service.get_project_source_file(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None


@app.get("/api/source-files/{file_id}/content")
def get_source_file_content(request: Request, file_id: str) -> FileResponse:
    require_source_file_access(request, file_id)
    try:
        path, content_type, file_name = service.get_source_file_content(file_id)
        return FileResponse(path, media_type=content_type, filename=file_name)
    except KeyError:
        raise HTTPException(status_code=404, detail="文件不存在。") from None


@app.get("/api/source-files/{file_id}/preview")
def get_source_file_preview(request: Request, file_id: str) -> FileResponse:
    require_source_file_access(request, file_id)
    try:
        path, content_type, file_name = service.get_source_file_preview_content(file_id)
        return FileResponse(path, media_type=content_type, headers={"Content-Disposition": f'inline; filename="{file_name}"'})
    except KeyError:
        raise HTTPException(status_code=404, detail="预览不存在。") from None


@app.delete("/api/projects/{project_id}")
def delete_project(request: Request, project_id: str, permanent: bool = Query(default=False)) -> dict:
    require_project_access(request, project_id)
    try:
        return service.delete_project(project_id, permanent=permanent)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None


@app.post("/api/projects/{project_id}/restore")
def restore_project(request: Request, project_id: str) -> dict:
    require_project_access(request, project_id)
    try:
        return service.restore_project(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None


@app.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict:
    require_job_access(request, job_id)
    try:
        return service.get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在。") from None


@app.post("/api/projects")
def create_project(request: Request, payload: ProjectCreateRequest) -> dict:
    user = require_current_user(request)
    return service.create_project(
        title=payload.title,
        doc_type=payload.type,
        language=payload.language,
        source_type=payload.sourceType,
        note=payload.note,
        text=payload.text,
        owner_user_id=user["id"],
    )


@app.post("/api/projects/{project_id}/files")
async def upload_project_file(request: Request, project_id: str, file: UploadFile = File(...), fallbackText: str = "") -> dict:
    require_project_access(request, project_id)
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
        raise HTTPException(status_code=404, detail="项目不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/files/ingest")
def ingest_project_file(request: Request, project_id: str, payload: ProjectFileIngestRequest) -> dict:
    require_project_access(request, project_id)
    try:
        return service.ingest_uploaded_file(
            project_id=project_id,
            storage_ref=payload.storageRef,
            file_name=payload.fileName,
            content_type=payload.contentType,
            fallback_text=payload.fallbackText,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/diagnose")
def diagnose_project(request: Request, project_id: str) -> dict:
    require_project_access(request, project_id)
    try:
        return service.diagnose_project(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc


@app.post("/api/sections/{section_id}/rewrite")
def rewrite_section(request: Request, section_id: str, payload: RewriteRequest) -> dict:
    require_section_access(request, section_id)
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
        raise HTTPException(status_code=404, detail="章节不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc


@app.post("/api/agent/revise")
def revise_text(request: Request, payload: AgentRevisionRequest) -> dict:
    user = require_current_user(request)
    if payload.projectId:
        try:
            service.ensure_project_access(payload.projectId, user["id"])
        except KeyError:
            raise HTTPException(status_code=404, detail="项目不存在。") from None
    if payload.commentId:
        try:
            service.ensure_comment_access(payload.commentId, user["id"])
        except KeyError:
            raise HTTPException(status_code=404, detail="意见不存在。") from None
    try:
        return service.revise_text(
            text=payload.text,
            action_type=payload.actionType,
            mode=payload.mode,
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
        raise HTTPException(status_code=404, detail="项目或意见不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc


@app.post("/api/agent/literature-scout")
def scout_revision_literature(request: Request, payload: AgentLiteratureScoutRequest) -> dict:
    require_project_access(request, payload.projectId)
    if payload.commentId:
        require_comment_access(request, payload.commentId)
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
        raise HTTPException(status_code=404, detail="项目或意见不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agent/verify-citations")
def verify_citations(request: Request, payload: CitationVerifyRequest) -> dict:
    require_project_access(request, payload.projectId)
    try:
        return service.verify_citations(project_id=payload.projectId, text=payload.text)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agent/format-citations")
def format_citations(request: Request, payload: CitationFormatRequest) -> dict:
    require_project_access(request, payload.projectId)
    try:
        return service.format_citations(
            project_id=payload.projectId,
            style=payload.style,
            item_ids=payload.itemIds,
            text=payload.text,
            matched_only=payload.matchedOnly,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sections/{section_id}/manual-edit")
def save_manual_edit(request: Request, section_id: str, payload: ManualEditRequest) -> dict:
    require_section_access(request, section_id)
    try:
        return service.save_manual_edit(section_id=section_id, new_text=payload.newText)
    except KeyError:
        raise HTTPException(status_code=404, detail="章节不存在。") from None


@app.post("/api/comments/import")
def import_comment(request: Request, payload: CommentImportRequest) -> dict:
    require_project_access(request, payload.projectId)
    try:
        return service.import_comment(project_id=payload.projectId, raw_comment=payload.rawComment)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc


@app.post("/api/comments/{comment_id}/remap")
def remap_comment(request: Request, comment_id: str, payload: CommentRemapRequest) -> dict:
    require_comment_access(request, comment_id)
    try:
        service.ensure_section_access(payload.sectionId, require_current_user(request)["id"])
        return service.remap_comment(comment_id=comment_id, section_id=payload.sectionId)
    except KeyError:
        raise HTTPException(status_code=404, detail="意见或章节不存在。") from None


@app.post("/api/comments/{comment_id}/status")
def update_comment_status(request: Request, comment_id: str, payload: StatusUpdateRequest) -> dict:
    require_comment_access(request, comment_id)
    try:
        return service.update_comment_status(comment_id=comment_id, status=payload.status)
    except KeyError:
        raise HTTPException(status_code=404, detail="意见不存在。") from None


@app.post("/api/issues/{issue_id}/status")
def update_issue_status(request: Request, issue_id: str, payload: StatusUpdateRequest) -> dict:
    require_issue_access(request, issue_id)
    try:
        return service.update_issue_status(issue_id=issue_id, status=payload.status)
    except KeyError:
        raise HTTPException(status_code=404, detail="问题不存在。") from None


@app.post("/api/revisions/{candidate_id}/accept")
def accept_revision(request: Request, candidate_id: str) -> dict:
    require_candidate_access(request, candidate_id)
    try:
        return service.accept_revision_candidate(candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="改写候选不存在。") from None


@app.post("/api/revisions/{candidate_id}/reject")
def reject_revision(request: Request, candidate_id: str) -> dict:
    require_candidate_access(request, candidate_id)
    try:
        return service.reject_revision_candidate(candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="改写候选不存在。") from None


@app.post("/api/revisions/{candidate_id}/revise")
def revise_revision(request: Request, candidate_id: str, payload: CandidateReviseRequest) -> dict:
    require_candidate_access(request, candidate_id)
    try:
        return service.revise_revision_candidate(candidate_id, payload.feedback)
    except KeyError:
        raise HTTPException(status_code=404, detail="改写候选不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise upstream_failure(exc) from exc


@app.post("/api/versions/{revision_id}/restore")
def restore_version(request: Request, revision_id: str) -> dict:
    require_revision_access(request, revision_id)
    try:
        return service.restore_revision(revision_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="版本不存在。") from None


@app.post("/api/projects/{project_id}/literature/search")
def search_literature(request: Request, project_id: str, payload: LiteratureSearchRequest) -> dict:
    require_project_access(request, project_id)
    try:
        return literature_service.search_literature(
            project_id=project_id,
            query=payload.query,
            sources=payload.sources,
            limit=payload.limit,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/literature")
def list_literature(request: Request, project_id: str) -> dict:
    require_project_access(request, project_id)
    try:
        return literature_service.list_project_literature(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None


@app.get("/api/projects/{project_id}/literature/rag-status")
def get_literature_rag_status(request: Request, project_id: str) -> dict:
    require_project_access(request, project_id)
    try:
        return literature_service.get_project_rag_status(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None


@app.get("/api/projects/{project_id}/literature/suggest-query")
def suggest_literature_query(request: Request, project_id: str) -> dict:
    require_project_access(request, project_id)
    try:
        return literature_service.suggest_search_query(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None


@app.post("/api/projects/{project_id}/literature/import")
def import_literature(request: Request, project_id: str, payload: LiteratureImportRequest) -> dict:
    require_project_access(request, project_id)
    try:
        return literature_service.import_literature(
            project_id=project_id,
            run_id=payload.runId,
            item_ids=payload.itemIds,
            items=payload.items,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="项目或检索记录不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/literature/{item_id}/fetch-open-fulltext")
def fetch_open_fulltext(request: Request, project_id: str, item_id: str) -> dict:
    require_project_access(request, project_id)
    try:
        return literature_service.fetch_open_fulltext(project_id=project_id, item_id=item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="文献不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/literature/fetch-open-fulltext-batch")
def fetch_open_fulltext_batch(request: Request, project_id: str, payload: LiteratureBatchFetchRequest) -> dict:
    require_project_access(request, project_id)
    try:
        return literature_service.fetch_open_fulltext_batch(project_id=project_id, item_ids=payload.itemIds)
    except KeyError:
        raise HTTPException(status_code=404, detail="项目或文献不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/literature/attachments/{attachment_id}/download")
def download_literature_attachment(request: Request, project_id: str, attachment_id: str) -> FileResponse:
    require_project_access(request, project_id)
    try:
        path, content_type, file_name = literature_service.get_attachment_download(project_id=project_id, attachment_id=attachment_id)
        return FileResponse(path, media_type=content_type, filename=file_name)
    except KeyError:
        raise HTTPException(status_code=404, detail="附件不存在。") from None


@app.get("/api/projects/{project_id}/literature/download-bundles/{bundle_name}")
def download_literature_bundle(request: Request, project_id: str, bundle_name: str) -> FileResponse:
    require_project_access(request, project_id)
    try:
        path, content_type, file_name = literature_service.get_download_bundle(project_id=project_id, bundle_name=bundle_name)
        return FileResponse(path, media_type=content_type, filename=file_name)
    except KeyError:
        raise HTTPException(status_code=404, detail="下载包不存在。") from None


@app.post("/api/projects/{project_id}/literature/{item_id}/index-text")
def index_literature_text(request: Request, project_id: str, item_id: str, payload: LiteratureIndexTextRequest) -> dict:
    require_project_access(request, project_id)
    try:
        return literature_service.index_item_text(
            project_id=project_id,
            item_id=item_id,
            text=payload.text,
            source_label=payload.sourceLabel,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="项目或文献不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/literature/rag-search")
def search_literature_evidence(request: Request, project_id: str, payload: LiteratureRagSearchRequest) -> dict:
    require_project_access(request, project_id)
    try:
        return literature_service.search_project_evidence(
            project_id=project_id,
            query=payload.query,
            limit=payload.limit,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="项目不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/literature/search-runs/{run_id}")
def get_literature_search_run(request: Request, run_id: str) -> dict:
    user = require_current_user(request)
    try:
        run = literature_service.get_search_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="检索记录不存在。") from None
    try:
        service.ensure_project_access(run["projectId"], user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="检索记录不存在。") from None
    return run


def main() -> None:
    import uvicorn

    uvicorn.run("backend.app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()

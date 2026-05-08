import {
  Project,
  ProjectBundle,
  RevisionCandidate,
  AgentRevisionResult,
  LiteratureSearchResult,
  LiteratureLibrary,
  FulltextFetchResult,
  FulltextBatchFetchResult,
  RagStatus,
  RagSearchResult,
  CitationFormatResult,
  CitationVerification,
  CitationAudit,
  SourceFilePreview,
  UserProfile,
  AuthStatus,
} from '../types';

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');

class ApiError extends Error {
  constructor(public status: number, public data: any) {
    super(data?.detail || 'API Error');
  }
}

type UploadPhase = 'uploading' | 'processing' | 'completed';

type UploadHandlers = {
  onProgress?: (percent: number, phase: UploadPhase) => void;
  timeoutMs?: number;
};

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function fetchWithRetry(url: string, options: RequestInit, attempts = 2): Promise<Response> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const signal = options.signal ?? (typeof AbortSignal !== 'undefined' && 'timeout' in AbortSignal ? AbortSignal.timeout(45000) : undefined);
      return await fetch(url, { ...options, signal, credentials: 'include' });
    } catch (error) {
      lastError = error;
      if (attempt < attempts) {
        await delay(400 * attempt);
        continue;
      }
    }
  }
  throw lastError;
}

function humanizeDetail(detail: string): string {
  const text = String(detail || '').trim();
  if (!text) return '请求失败，请稍后重试。';
  if (/Failed to fetch/i.test(text)) {
    return '请求没有送达，请确认服务仍在运行后重试。';
  }
  if (/timed?\s*out|read operation timed out|timeout/i.test(text)) {
    return '处理超时，请稍后重试。';
  }
  if (/Writer expanded the text beyond the allowed revision range/i.test(text)) {
    return '这次改动范围过大，请换成更具体的动作或缩小改写目标。';
  }
  if (/Model returned unchanged text/i.test(text)) {
    return '这次没有生成有效改写，请重试，或补充更明确的修改要求。';
  }
  if (/invalid rewrite payload/i.test(text)) {
    return '这次改写没有生成可用结果，请重试。';
  }
  if (/No extractable text/i.test(text)) {
    return 'PDF 已拿到，但暂时没能抽取出可用于检索的正文；你仍然可以下载原文。';
  }
  if (/Server failed while handling the uploaded file/i.test(text)) {
    return '服务端在保存或解析上传文件时失败。请稍后重试；如果持续失败，请查看部署日志。';
  }
  return text;
}

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const url = `${API_BASE}${endpoint}`;
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  };

  // Remove Content-Type if it's FormData to allow browser to set boundary
  if (options.body instanceof FormData) {
    // @ts-ignore
    delete headers['Content-Type'];
  }

  let response: Response;
  try {
    response = await fetchWithRetry(url, { ...options, headers });
  } catch (error: any) {
    throw new ApiError(0, { detail: humanizeDetail(error?.message || 'Failed to fetch') });
  }
  
  if (!response.ok) {
    let errData;
    try {
      errData = await response.json();
    } catch (e) {
      errData = { detail: response.statusText };
    }
    errData = { ...errData, detail: humanizeDetail(errData?.detail || response.statusText) };
    if (response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new Event('draftrefine:unauthorized'));
    }
    throw new ApiError(response.status, errData);
  }

  return response.json();
}

function uploadFileRequest<T>(
  endpoint: string,
  formData: FormData,
  handlers: UploadHandlers = {}
): Promise<T> {
  const url = `${API_BASE}${endpoint}`;
  const timeoutMs = handlers.timeoutMs ?? 180000;

  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.timeout = timeoutMs;
    xhr.withCredentials = true;
    xhr.setRequestHeader('Accept', 'application/json');

    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable || !handlers.onProgress) return;
      const percent = Math.max(6, Math.min(72, Math.round((event.loaded / event.total) * 72)));
      handlers.onProgress(percent, 'uploading');
    };

    xhr.onreadystatechange = () => {
      if (!handlers.onProgress) return;
      if (xhr.readyState === XMLHttpRequest.HEADERS_RECEIVED) {
        handlers.onProgress(82, 'processing');
      } else if (xhr.readyState === XMLHttpRequest.LOADING) {
        handlers.onProgress(90, 'processing');
      }
    };

    xhr.onerror = () => {
      reject(new ApiError(0, { detail: humanizeDetail('Failed to fetch') }));
    };

    xhr.ontimeout = () => {
      reject(
        new ApiError(0, {
          detail: humanizeDetail('Upload timed out while the server was parsing the file.'),
        })
      );
    };

    xhr.onload = () => {
      let payload: any = null;
      try {
        payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        payload = { detail: xhr.responseText || xhr.statusText };
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        handlers.onProgress?.(100, 'completed');
        resolve(payload as T);
        return;
      }

      reject(
        new ApiError(xhr.status, {
          ...payload,
          detail: humanizeDetail(payload?.detail || xhr.statusText || 'Server failed while handling the uploaded file.'),
        })
      );
      if (xhr.status === 401 && typeof window !== 'undefined') {
        window.dispatchEvent(new Event('draftrefine:unauthorized'));
      }
    };

    xhr.send(formData);
  });
}

export const api = {
  health: () => request<{status: string}>('/health'),

  auth: {
    status: () => request<AuthStatus>('/auth/status'),
    me: () => request<{ user: UserProfile }>('/auth/me'),
    register: (data: { email: string; username: string; password: string; inviteCode: string }) =>
      request<{ user: UserProfile }>('/auth/register', { method: 'POST', body: JSON.stringify(data) }),
    login: (data: { identifier: string; password: string }) =>
      request<{ user: UserProfile }>('/auth/login', { method: 'POST', body: JSON.stringify(data) }),
    logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  },
  
  projects: {
    list: (scope: 'active' | 'trash' | 'all' = 'active') => request<Project[]>(`/projects?scope=${scope}`),
    create: (data: { title: string, type: string, language: string, sourceType: string, text?: string }) => 
      request<ProjectBundle>('/projects', { method: 'POST', body: JSON.stringify(data) }),
    getBundle: (projectId: string) => request<ProjectBundle>(`/projects/${projectId}/bundle`),
    getSourceFile: (projectId: string) => request<{ file: SourceFilePreview }>(`/projects/${projectId}/source-file`),
    delete: (projectId: string, permanent = false) =>
      request<{ deletedProjectId: string; permanent: boolean }>(`/projects/${projectId}${permanent ? '?permanent=true' : ''}`, { method: 'DELETE' }),
    restore: (projectId: string) => request<{ restoredProjectId: string }>(`/projects/${projectId}/restore`, { method: 'POST' }),
    uploadFiles: (projectId: string, file: File, fallbackText?: string, handlers?: UploadHandlers) => {
      const formData = new FormData();
      formData.append('file', file);
      if (fallbackText) formData.append('fallbackText', fallbackText);
      return uploadFileRequest<ProjectBundle & { uploadFile?: SourceFilePreview }>(`/projects/${projectId}/files`, formData, handlers);
    },
    ingestUploadedFile: (
      projectId: string,
      data: { storageRef: string; fileName: string; contentType?: string; fallbackText?: string }
    ) =>
      request<ProjectBundle>(`/projects/${projectId}/files/ingest`, {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    diagnose: (projectId: string) => request<ProjectBundle>(`/projects/${projectId}/diagnose`, { method: 'POST' }),
  },
  
  sections: {
    rewrite: (sectionId: string, data: { actionType: string, currentText: string, feedback?: string, commentId?: string | null, previousCandidateText?: string }) => 
      request<RevisionCandidate>(`/sections/${sectionId}/rewrite`, { method: 'POST', body: JSON.stringify(data) }),
    manualEdit: (sectionId: string, data: { newText: string }) => 
      request<ProjectBundle>(`/sections/${sectionId}/manual-edit`, { method: 'POST', body: JSON.stringify(data) }),
  },

  agent: {
    revise: (data: any) => request<AgentRevisionResult>('/agent/revise', { method: 'POST', body: JSON.stringify(data) }),
    verifyCitations: (projectId: string, text: string) =>
      request<{ projectId: string; text: string; evidence: any[]; citationAudit: CitationAudit; citationVerification: CitationVerification }>(
        '/agent/verify-citations',
        { method: 'POST', body: JSON.stringify({ projectId, text }) }
      ),
    formatCitations: (data: { projectId: string; style: string; itemIds?: string[]; text?: string; matchedOnly?: boolean }) =>
      request<CitationFormatResult>('/agent/format-citations', { method: 'POST', body: JSON.stringify(data) }),
  },
  
  revisions: {
    accept: (candidateId: string) => request<ProjectBundle>(`/revisions/${candidateId}/accept`, { method: 'POST' }),
    reject: (candidateId: string) => request<ProjectBundle>(`/revisions/${candidateId}/reject`, { method: 'POST' }),
    revise: (candidateId: string, feedback: string) => request<RevisionCandidate>(`/revisions/${candidateId}/revise`, { method: 'POST', body: JSON.stringify({ feedback }) }),
  },
  
  comments: {
    import: (projectId: string, rawComment: string) => request<ProjectBundle>(`/comments/import`, { method: 'POST', body: JSON.stringify({ projectId, rawComment }) }),
    remap: (commentId: string, sectionId: string) => request<ProjectBundle>(`/comments/${commentId}/remap`, { method: 'POST', body: JSON.stringify({ sectionId }) }),
    updateStatus: (commentId: string, status: string) => request<ProjectBundle>(`/comments/${commentId}/status`, { method: 'POST', body: JSON.stringify({ status }) })
  },

  literature: {
    search: (projectId: string, data: { query: string; sources?: string[]; limit?: number }) =>
      request<LiteratureSearchResult>(`/projects/${projectId}/literature/search`, { method: 'POST', body: JSON.stringify(data) }),
    list: (projectId: string) => request<LiteratureLibrary>(`/projects/${projectId}/literature`),
    suggestQuery: (projectId: string) => request<{ query: string; terms: any[]; sourceSections: string[]; warnings: string[]; generatedAt: string }>(`/projects/${projectId}/literature/suggest-query`),
    import: (projectId: string, data: { runId?: string | null; itemIds?: string[]; items?: any[] }) =>
      request<{ items: any[]; fulltextResults?: FulltextFetchResult[]; library?: LiteratureLibrary }>(
        `/projects/${projectId}/literature/import`,
        { method: 'POST', body: JSON.stringify(data) }
      ),
    indexText: (projectId: string, itemId: string, text: string, sourceLabel = 'manual-fulltext') =>
      request<{ itemId: string; chunkCount: number; chunks: any[] }>(`/projects/${projectId}/literature/${itemId}/index-text`, { method: 'POST', body: JSON.stringify({ text, sourceLabel }) }),
    ragStatus: (projectId: string) => request<RagStatus>(`/projects/${projectId}/literature/rag-status`),
    ragSearch: (projectId: string, query: string, limit = 8) =>
      request<RagSearchResult>(`/projects/${projectId}/literature/rag-search`, { method: 'POST', body: JSON.stringify({ query, limit }) }),
    fetchOpenFulltext: (projectId: string, itemId: string) =>
      request<FulltextFetchResult>(`/projects/${projectId}/literature/${itemId}/fetch-open-fulltext`, { method: 'POST' }),
    fetchOpenFulltextBatch: (projectId: string, itemIds: string[]) =>
      request<FulltextBatchFetchResult>(`/projects/${projectId}/literature/fetch-open-fulltext-batch`, { method: 'POST', body: JSON.stringify({ itemIds }) }),
  }
};

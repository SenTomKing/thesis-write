# DraftRefine 前端对接 Agent 技术报告

## 1. 当前状态

后端已经具备可供前端直接调用的本地 MVP agent 能力：

- 项目创建、文件上传、章节解析、诊断。
- 独立文本改稿和章节改稿。
- 候选改稿的接受、拒绝、反馈再修改。
- 导师意见导入、映射、重映射、状态更新。
- 项目文献库、文献搜索、文献导入、开放全文抓取、手动全文索引。
- 本地 hybrid RAG 检索。
- DOI、作者年份、数字引用 `[1]` 核验。
- GB/T 7714、APA、IEEE 参考文献格式化。
- 版本历史和版本恢复。

旧前端已经删除：

- `src/`
- `dist/`

后端保留：

- `backend/`
- `promptfoo/`
- `docs/`
- `.env`
- `.env.example`
- `start-draftrefine.bat`

## 2. 后端运行方式

后端启动：

```cmd
cd /d F:\Fucking\thesis-refine-hub
python -m backend.app
```

默认地址：

```text
http://127.0.0.1:8000
```

健康检查：

```http
GET /api/health
```

返回：

```json
{
  "status": "ok"
}
```

## 3. 前端技术边界

新前端必须把后端作为唯一业务数据源。

不得使用：

- 旧 mock 数据。
- 旧 `mock-ai` 流程。
- 前端自行拼接 prompt。
- 前端自行调用 DeepSeek。
- 前端自行伪造文献证据。
- 前端直接修改 SQLite 文件。

前端只负责：

- 调用 FastAPI。
- 管理页面状态。
- 展示 agent 返回结果。
- 触发接受、拒绝、再修改。
- 展示文献库、RAG、引用核验结果。

所有 agent 逻辑都在后端。

## 4. 基础数据模型

前端建议按以下 TypeScript 类型建模。字段可以先宽松处理，但关键字段必须保留。

```ts
type Project = {
  id: string;
  title: string;
  type: string;
  language: "zh" | "en" | string;
  status: string;
  sourceType: string;
  progressState?: string;
  nextAction?: string;
  issueCount?: number;
  unresolvedCommentCount?: number;
  pendingRevisionCount?: number;
  updatedAt?: string;
};
```

```ts
type DocumentSection = {
  id: string;
  projectId: string;
  title: string;
  currentText: string;
  originalText: string;
  orderIndex: number;
  path?: string;
  issueCount?: number;
  commentCount?: number;
  revisionState?: string;
  completionState?: string;
};
```

```ts
type RevisionCandidate = {
  id: string;
  projectId: string;
  sectionId?: string;
  text: string;
  summary: string;
  warnings: string[];
  actionType: string;
  promptVersion?: string;
  model?: string;
  provider?: string;
  baseText?: string;
  selectedText?: string;
  replacementText?: string;
  evidence?: EvidenceItem[];
  evidenceStrategy?: EvidenceStrategy;
  citationAudit?: CitationAudit;
  citationVerification?: CitationVerification;
  agentTrace?: AgentTrace;
};
```

```ts
type EvidenceItem = {
  sourceKind: string;
  sourceId?: string;
  label: string;
  excerpt: string;
  score: number;
  metadata?: Record<string, unknown>;
};
```

```ts
type EvidenceStrategy = {
  mode: string;
  reason: string;
  recommendedQuery: string;
  sourceOrder: string[];
  queryWarnings: string[];
  importedLiteratureCount: number;
  importedDoiCount: number;
  retrievedLiteratureEvidenceCount: number;
  localRagEvidenceCount: number;
  retrievalMode: string;
  needsImportedEvidence: boolean;
  needsLiveSearchSuggestion: boolean;
  shouldBlockAutoCitation: boolean;
};
```

```ts
type AgentTrace = {
  rewriteMode: string;
  riskLevel: string;
  roleSequence: string[];
  docProfile: Record<string, unknown>;
  loopCount: number;
  stepRuns: AgentStepRun[];
  patches: unknown[];
  evidenceStrategy?: EvidenceStrategy;
  citationAudit?: CitationAudit;
  citationVerification?: CitationVerification;
};
```

```ts
type AgentStepRun = {
  step: string;
  action_name: string;
  prompt_version: string;
  provider: string;
  model: string;
  status: string;
  latency_ms: number;
  error?: string | null;
  input_text?: string;
  output_text?: string;
};
```

```ts
type ReviewerComment = {
  id: string;
  projectId: string;
  rawComment: string;
  mappedSectionId?: string | null;
  manualOverrideSectionId?: string | null;
  confidence: number;
  suggestedAction: string;
  status: "pending" | "in_progress" | "done" | string;
  createdAt?: string;
};
```

```ts
type LiteratureItem = {
  id: string;
  projectId: string;
  title: string;
  authors: string[];
  year?: number;
  venue?: string;
  doi?: string;
  url?: string;
  abstract?: string;
  source?: string;
  sources?: string[];
  language?: string;
  citationCount?: number;
  openAccessStatus?: string;
  pdfUrl?: string;
  zoteroItemKey?: string | null;
  tags?: string[];
};
```

```ts
type RagStatus = {
  projectId: string;
  retrievalMode: "local-hybrid-rag" | string;
  embeddingModel: string;
  itemCount: number;
  chunkCount: number;
  fulltextChunkCount: number;
  vectorCount: number;
  missingVectorCount: number;
  ready: boolean;
};
```

```ts
type CitationAudit = {
  status: "not-needed" | "supported" | "needs-verification" | "unsupported-risk" | "evidence-gap" | string;
  originalHasCitationMarkers: boolean;
  candidateHasCitationMarkers: boolean;
  literatureEvidenceCount: number;
  verifiedDoiEvidenceCount: number;
  recommendedAction: string;
  evidenceIds: string[];
};
```

```ts
type CitationVerification = {
  status: "not-applicable" | "verified" | "partially-verified" | "unverified" | string;
  verifiedMentionCount: number;
  unresolvedMentionCount: number;
  doiMentions: unknown[];
  authorYearMentions: unknown[];
  numericMentions: unknown[];
  matchedItems: unknown[];
  issues: string[];
  recommendedAction: string;
  referenceEntryCount?: number;
};
```

## 5. 项目 API

### 5.1 项目列表

```http
GET /api/projects
```

返回：

```ts
Project[]
```

### 5.2 项目 bundle

```http
GET /api/projects/{project_id}/bundle
```

返回：

```ts
type ProjectBundle = {
  project: Project;
  sections: DocumentSection[];
  issues: unknown[];
  comments: ReviewerComment[];
  revisions: unknown[];
  candidates?: RevisionCandidate[];
};
```

前端使用：

- 项目详情页初始化。
- 接受候选后刷新。
- 拒绝候选后刷新。
- 版本恢复后刷新。

### 5.3 创建项目

```http
POST /api/projects
```

请求：

```json
{
  "title": "论文题目",
  "type": "thesis",
  "language": "zh",
  "sourceType": "text",
  "note": "补充说明",
  "text": "用户粘贴文本"
}
```

返回：

```ts
ProjectBundle
```

### 5.4 上传项目文件

```http
POST /api/projects/{project_id}/files
Content-Type: multipart/form-data
```

字段：

- `file`: 上传文件。
- `fallbackText`: 可选，解析失败时使用。

返回：

```ts
ProjectBundle
```

### 5.5 删除项目

```http
DELETE /api/projects/{project_id}
```

返回：

```json
{
  "deleted": true,
  "projectId": "project-xxx"
}
```

### 5.6 诊断项目

```http
POST /api/projects/{project_id}/diagnose
```

返回：

```ts
ProjectBundle
```

## 6. 改稿 Agent API

## 6.1 独立文本改稿

适合前端最小可用版本。无需依赖章节 ID。

```http
POST /api/agent/revise
```

请求：

```json
{
  "projectId": "project-xxx",
  "text": "需要改写的段落",
  "actionType": "academic-rewrite",
  "title": "研究背景",
  "note": "保持原意，不新增事实",
  "selectedText": "",
  "selectionStart": null,
  "selectionEnd": null,
  "commentId": null,
  "previousCandidateText": ""
}
```

`actionType` 可用值：

- `academic-rewrite`
- `shorten`
- `expand`
- `unify-terms`
- `comment-revision`
- `transition-polish`

返回重点字段：

```ts
type AgentRevisionResult = {
  projectId?: string;
  text: string;
  summary: string;
  actionType: string;
  promptVersion: string;
  model: string;
  provider: string;
  warnings: string[];
  baseText: string;
  selectedText: string;
  replacementText: string;
  plan: unknown;
  review: unknown;
  evidence: EvidenceItem[];
  evidenceStrategy: EvidenceStrategy;
  citationAudit: CitationAudit;
  citationVerification: CitationVerification;
  agentTrace: AgentTrace;
};
```

注意：

- `text` 是最终候选文本。
- `baseText` 是输入基线。
- 前端不要把 `text` 自动覆盖用户输入。
- 如果用户点击“采用”，前端自己更新输入框即可；独立文本改稿没有 candidate id。

## 6.2 章节改稿

适合项目章节工作流。

```http
POST /api/sections/{section_id}/rewrite
```

请求：

```json
{
  "actionType": "expand",
  "currentText": "当前章节文本",
  "commentIds": [],
  "selectedText": "",
  "selectionStart": null,
  "selectionEnd": null,
  "commentId": null,
  "feedback": "",
  "previousCandidateText": ""
}
```

返回：

```ts
RevisionCandidate
```

注意：

- 返回的 `id` 是后续接受、拒绝、再修改的关键。
- 章节改稿必须通过 accept 才能写入当前章节。

### 6.3 接受候选

```http
POST /api/revisions/{candidate_id}/accept
```

返回：

```ts
ProjectBundle
```

前端处理：

- 替换本地 bundle。
- 清空当前 candidate。

### 6.4 拒绝候选

```http
POST /api/revisions/{candidate_id}/reject
```

返回：

```ts
ProjectBundle
```

前端处理：

- 不修改当前章节文本。
- 清空当前 candidate。

### 6.5 反馈再修改

```http
POST /api/revisions/{candidate_id}/revise
```

请求：

```json
{
  "feedback": "这版太长，请缩短并保留关键证据"
}
```

返回：

```ts
RevisionCandidate
```

前端处理：

- 用新 candidate 替换旧 candidate。
- 不覆盖当前章节文本。

### 6.6 手动保存章节

```http
POST /api/sections/{section_id}/manual-edit
```

请求：

```json
{
  "newText": "用户手动编辑后的章节文本"
}
```

返回：

```ts
ProjectBundle
```

## 7. 导师意见 API

### 7.1 导入导师意见

```http
POST /api/comments/import
```

请求：

```json
{
  "projectId": "project-xxx",
  "rawComment": "导师原始意见"
}
```

返回：

```ts
ProjectBundle
```

### 7.2 手动重映射章节

```http
POST /api/comments/{comment_id}/remap
```

请求：

```json
{
  "sectionId": "section-xxx"
}
```

返回：

```ts
ProjectBundle
```

### 7.3 更新意见状态

```http
POST /api/comments/{comment_id}/status
```

请求：

```json
{
  "status": "done"
}
```

可用状态：

- `pending`
- `in_progress`
- `done`

返回：

```ts
ProjectBundle
```

## 8. 文献库 API

### 8.1 搜索文献

```http
POST /api/projects/{project_id}/literature/search
```

请求：

```json
{
  "query": "land surface temperature emissivity retrieval",
  "sources": ["openalex", "crossref", "semantic-scholar"],
  "limit": 12
}
```

返回：

```ts
type LiteratureSearchResult = {
  run: unknown;
  candidates: LiteratureItem[];
};
```

注意：

- 搜索结果只是候选。
- 候选必须导入后才能进入项目文献库。

### 8.2 导入文献

```http
POST /api/projects/{project_id}/literature/import
```

请求方式 1：从 search run 导入

```json
{
  "runId": "lit-run-xxx",
  "itemIds": ["candidate-1", "candidate-2"],
  "items": []
}
```

请求方式 2：直接导入外部传入条目

```json
{
  "runId": null,
  "itemIds": [],
  "items": [
    {
      "title": "文献标题",
      "authors": ["作者A"],
      "year": 2024,
      "venue": "期刊名",
      "doi": "10.xxxx/xxxx",
      "url": "https://example.org",
      "abstract": "摘要",
      "source": "manual",
      "sources": ["manual"],
      "language": "zh",
      "citationCount": 0,
      "openAccessStatus": "metadata-only",
      "tags": []
    }
  ]
}
```

返回：

```ts
{
  items: LiteratureItem[];
}
```

### 8.3 获取项目文献库

```http
GET /api/projects/{project_id}/literature
```

返回：

```ts
{
  items: LiteratureItem[];
  attachments: unknown[];
  chunks: unknown[];
  syncEvents: unknown[];
}
```

### 8.4 粘贴全文并建立本地 RAG 索引

```http
POST /api/projects/{project_id}/literature/{item_id}/index-text
```

请求：

```json
{
  "text": "用户有权使用的文献全文、摘要、引言或相关片段",
  "sourceLabel": "manual-fulltext"
}
```

返回：

```json
{
  "itemId": "lit-xxx",
  "chunkCount": 3,
  "chunks": []
}
```

注意：

- 这是本地 RAG 的主要入口。
- 成功后应刷新 `rag-status`。

### 8.5 抓取开放全文

```http
POST /api/projects/{project_id}/literature/{item_id}/fetch-open-fulltext
```

返回：

```ts
{
  item: LiteratureItem;
  attachment: unknown;
  indexing?: {
    itemId: string;
    sourceKind: string;
    chunkCount: number;
  };
}
```

注意：

- 只支持明确 open-access PDF。
- 失败时后端会返回 400。

### 8.6 RAG 状态

```http
GET /api/projects/{project_id}/literature/rag-status
```

返回：

```ts
RagStatus
```

前端处理：

- `ready=true`：可以显示本地 RAG 可用。
- `ready=false`：提示用户导入文献或粘贴全文。
- `missingVectorCount>0`：可以提示索引不完整，但后端检索时会自动 backfill。

### 8.7 RAG 检索

```http
POST /api/projects/{project_id}/literature/rag-search
```

请求：

```json
{
  "query": "研究背景 理论脉络 学术表达",
  "limit": 8
}
```

返回：

```ts
{
  projectId: string;
  query: string;
  retrievalMode: "local-hybrid-rag";
  embeddingModel: string;
  queryTerms: string[];
  backfilledVectorCount: number;
  candidateChunkCount: number;
  evidence: EvidenceItem[];
}
```

## 9. 引用核验 API

### 9.1 核验正文引用

```http
POST /api/agent/verify-citations
```

请求：

```json
{
  "projectId": "project-xxx",
  "text": "已有研究表明该方法有效 [1]，详见 doi:10.xxxx/xxxx。"
}
```

返回：

```ts
{
  projectId: string;
  text: string;
  evidence: EvidenceItem[];
  citationAudit: CitationAudit;
  citationVerification: CitationVerification;
}
```

状态处理：

- `verified`：全部显式引用匹配项目文献库。
- `partially-verified`：部分匹配。
- `unverified`：没有匹配。
- `not-applicable`：没有显式引用标记。

### 9.2 参考文献格式化

```http
POST /api/agent/format-citations
```

请求：

```json
{
  "projectId": "project-xxx",
  "style": "gb7714",
  "itemIds": [],
  "text": "正文或候选稿",
  "matchedOnly": true
}
```

`style` 支持：

- `gb7714`
- `apa`
- `ieee`

返回：

```ts
{
  projectId: string;
  style: string;
  matchedOnly: boolean;
  entries: {
    itemId: string;
    style: string;
    formattedText: string;
    doi: string;
    zoteroItemKey: string;
  }[];
  bibliographyText: string;
  citationVerification?: CitationVerification;
  warnings: string[];
}
```

## 10. Zotero API

```http
POST /api/projects/{project_id}/literature/sync-zotero
```

请求：

```json
{
  "itemIds": ["lit-xxx", "lit-yyy"]
}
```

返回：

```ts
{
  events: unknown[];
  items: LiteratureItem[];
}
```

注意：

- 需要后端 `.env` 配置 Zotero API key 和 user id。
- 前端只负责选择条目并触发同步。

## 11. 版本历史 API

### 11.1 读取版本历史

版本历史在：

```http
GET /api/projects/{project_id}/bundle
```

返回的 `revisions` 字段中。

### 11.2 恢复版本

```http
POST /api/versions/{revision_id}/restore
```

返回：

```ts
ProjectBundle
```

## 12. 前端状态流

推荐全局状态：

```ts
type AppState = {
  projects: Project[];
  activeProjectId: string | null;
  bundle: ProjectBundle | null;
  activeSectionId: string | null;
  sourceText: string;
  candidate: RevisionCandidate | null;
  agentResult: AgentRevisionResult | null;
  ragStatus: RagStatus | null;
  loading: Record<string, boolean>;
  error: string | null;
};
```

章节改稿状态流：

```text
bundle.sections[n].currentText
  -> POST /api/sections/{section_id}/rewrite
  -> candidate
  -> accept/reject/revise
  -> refresh bundle
```

独立文本改稿状态流：

```text
sourceText
  -> POST /api/agent/revise
  -> agentResult.text
  -> user manually adopts or edits
```

文献 RAG 状态流：

```text
search literature
  -> import literature
  -> optional index-text / fetch-open-fulltext
  -> GET rag-status
  -> agent revise uses local RAG automatically
```

## 13. 错误处理

后端常见错误：

- `400`：请求内容不合法、文本为空、文件解析失败、非开放 PDF 等。
- `404`：项目、章节、候选、评论、文献条目不存在。
- `500`：未捕获后端错误。

前端处理规则：

- 不要吞掉错误。
- 显示 `detail` 字段。
- 生成候选失败时不要清空原文。
- 接受失败时不要假设已保存。
- 文献导入失败时不要把候选加入本地状态。

错误响应通常为：

```json
{
  "detail": "Project not found."
}
```

## 14. 前端最小实现顺序

1. 实现 API client。
2. 接 `GET /api/health`。
3. 接项目列表和创建项目。
4. 接 `POST /api/agent/revise`，完成独立文本改稿。
5. 展示 `summary`、`warnings`、`evidenceStrategy`、`citationAudit`、`citationVerification`。
6. 接项目 bundle 和章节选择。
7. 接章节改稿、接受、拒绝、再修改。
8. 接文献搜索、导入、文献列表。
9. 接 `index-text`、`rag-status`、`rag-search`。
10. 接导师意见。
11. 接引用核验和参考文献格式化。
12. 接版本恢复。

## 15. 必须遵守的技术规则

- 前端不保存最终业务数据，刷新后以 `GET /api/projects/{project_id}/bundle` 为准。
- 未接受的 candidate 不能覆盖 section currentText。
- 独立文本改稿没有 candidate id，章节改稿才有 candidate id。
- 文献搜索结果必须导入后才能被 RAG 使用。
- RAG 可用性以 `rag-status.ready` 为准。
- `citationAudit.status` 不等于 `not-needed` 或 `supported` 时，前端必须提示用户。
- `citationVerification.status` 为 `unverified` 时，前端不能显示“引用已核验”。
- 前端不处理 prompt，不展示 prompt 编辑器。
- 前端不调用模型供应商 API。
- 前端不直接访问 SQLite。

## 16. 本地开发注意事项

因为旧前端已删除，新前端创建前：

- `npm run dev` 会失败或启动空项目。
- 后端测试不受影响。
- 后端可单独启动。

后端测试命令：

```cmd
cd /d F:\Fucking\thesis-refine-hub
python -m unittest discover backend/tests
```

当前后端测试基线：

```text
25 tests OK
```

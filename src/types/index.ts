export type Project = {
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
  deletedAt?: string | null;
};

export type DocumentSection = {
  id: string;
  projectId: string;
  title: string;
  currentText: string;
  originalText: string;
  orderIndex: number;
  path?: string;
  sourcePage?: number;
  issueCount?: number;
  commentCount?: number;
  revisionState?: string;
  completionState?: string;
};

export type SourceFilePreview = {
  fileId: string;
  projectId: string;
  fileName: string;
  contentType?: string;
  extension?: string;
  previewKind?: string;
  contentUrl?: string;
  viewerKind?: string;
  previewUrl?: string;
  previewStatus?: string;
  previewMessage?: string;
  parseStatus?: string;
  parseError?: string | null;
};

export type EvidenceItem = {
  sourceKind: string;
  sourceId?: string;
  label: string;
  excerpt: string;
  score: number;
  metadata?: Record<string, unknown>;
};

export type EvidenceStrategy = {
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

export type CitationAudit = {
  status: "not-needed" | "supported" | "needs-verification" | "unsupported-risk" | "evidence-gap" | string;
  originalHasCitationMarkers: boolean;
  candidateHasCitationMarkers: boolean;
  literatureEvidenceCount: number;
  verifiedDoiEvidenceCount: number;
  recommendedAction: string;
  evidenceIds: string[];
};

export type CitationVerification = {
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

export type AgentStepRun = {
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

export type AgentTrace = {
  rewriteMode: string;
  executionLane?: 'fast' | 'full' | string;
  riskLevel: string;
  effectiveActionType?: string;
  instructionPlan?: Record<string, unknown>;
  roleSequence: string[];
  docProfile: Record<string, unknown>;
  loopCount: number;
  stepRuns: AgentStepRun[];
  patches: unknown[];
  evidenceStrategy?: EvidenceStrategy;
  citationAudit?: CitationAudit;
  citationVerification?: CitationVerification;
};

export type RewriteProgress = {
  lane: 'fast' | 'full';
  actionType: string;
  phase: string;
  stepIndex: number;
  totalSteps: number;
  percent: number;
};

export type RevisionCandidate = {
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

export type ReviewerComment = {
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

export type ProjectIssue = {
  id: string;
  projectId: string;
  sectionId?: string;
  severity: "high" | "medium" | "low" | string;
  category: string;
  description: string;
  suggestion: string;
  status: "open" | "resolved" | string;
}

export type ProjectBundle = {
  project: Project;
  sections: DocumentSection[];
  issues: ProjectIssue[];
  comments: ReviewerComment[];
  revisions: unknown[];
  candidates?: RevisionCandidate[];
};

export type AgentRevisionResult = {
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

export type LiteratureItem = {
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
  tags?: string[];
  relevanceScore?: number;
  matchReason?: string;
  qualitySignals?: string[];
  qualityStatus?: string;
  journalTitle?: string;
};

export type LiteratureAttachment = {
  id: string;
  itemId: string;
  kind: string;
  url?: string;
  localPath?: string;
  accessType?: string;
  status?: string;
  createdAt?: string;
  downloadUrl?: string;
};

export type LiteratureRun = {
  id: string;
  projectId: string;
  query: string;
  sources: string[];
  status: string;
  totalFound: number;
  dedupedCount: number;
  warnings: string[];
  createdAt: string;
  updatedAt: string;
};

export type LiteratureSearchResult = {
  run: LiteratureRun;
  candidates: LiteratureItem[];
};

export type LiteratureLibrary = {
  items: LiteratureItem[];
  attachments: LiteratureAttachment[];
  chunks: unknown[];
};

export type RagStatus = {
  projectId: string;
  retrievalMode: string;
  embeddingModel: string;
  itemCount: number;
  chunkCount: number;
  fulltextChunkCount: number;
  vectorCount: number;
  missingVectorCount: number;
  ready: boolean;
};

export type RagSearchResult = {
  projectId: string;
  query: string;
  retrievalMode: string;
  embeddingModel: string;
  queryTerms: string[];
  backfilledVectorCount: number;
  candidateChunkCount: number;
  evidence: EvidenceItem[];
};

export type CitationFormatResult = {
  projectId: string;
  style: string;
  matchedOnly: boolean;
  entries: Array<{
    itemId: string;
    style: string;
    formattedText: string;
    doi: string;
  }>;
  bibliographyText: string;
  citationVerification?: CitationVerification;
  warnings: string[];
};

export type FulltextFetchResult = {
  itemId: string;
  title: string;
  status: 'downloaded' | 'linked' | 'skipped' | 'failed';
  warning?: string;
  error?: string;
  downloadUrl?: string;
  attachment?: LiteratureAttachment | null;
  chunkCount?: number;
};

export type FulltextBatchFetchResult = {
  results: FulltextFetchResult[];
  downloadedCount: number;
  linkedCount: number;
  failedCount: number;
  skippedCount: number;
  downloadUrl?: string;
  fileCount?: number;
};

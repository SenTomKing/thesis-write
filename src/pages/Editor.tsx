import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  AlertCircle,
  ArrowLeft,
  Check,
  Copy,
  FileText,
  Library,
  MessageSquare,
  RefreshCcw,
  Sparkles,
  Wand2,
  X,
} from 'lucide-react';
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { api } from '../api/client';
import { useAppStore } from '../store';
import { Button } from '../components/ui/Button';
import { Card } from '../components/ui/Card';
import type { AgentRevisionResult, EvidenceItem, RewriteProgress, SourceFilePreview } from '../types';
import './Editor.css';

GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

type RightTab = 'rewrite' | 'reference' | 'trace' | 'comments';
type CenterView = 'review' | 'draft';

type ActionConfig = {
  type: string;
  label: string;
  desc: string;
};

const ACTIONS: ActionConfig[] = [
  { type: 'academic-rewrite', label: '学术化改写', desc: '提升语气与表达规范。' },
  { type: 'shorten', label: '精简表达', desc: '压缩重复内容，保留核心信息。' },
  { type: 'expand', label: '扩展论述', desc: '补足必要说明与论证。' },
  { type: 'transition-polish', label: '补强过渡', desc: '让段落衔接更自然。' },
  { type: 'unify-terms', label: '统一术语', desc: '减少术语漂移与口径不一。' },
];

const FAST_ACTIONS = new Set(['shorten', 'transition-polish', 'unify-terms']);

const PROGRESS_PHASES: Record<'fast' | 'full', Array<{ label: string; ratio: number }>> = {
  fast: [
    { label: '准备输入文本', ratio: 0.2 },
    { label: '生成改写结果', ratio: 0.76 },
    { label: '校验改写结果', ratio: 0.95 },
  ],
  full: [
    { label: '理解改写任务', ratio: 0.14 },
    { label: '检索相关证据', ratio: 0.42 },
    { label: '生成改写结果', ratio: 0.8 },
    { label: '校验改写结果', ratio: 0.96 },
  ],
};

function wait(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function userFacingMessage(message?: string | null) {
  const text = String(message || '').trim();
  if (!text) return '';
  if (/Failed to fetch/i.test(text)) return '请求没有送达到本地后端，请确认后端服务仍在运行。';
  if (/timed?\s*out|timeout/i.test(text)) return '模型响应超时，本次改写已中止，请稍后重试。';
  if (/unchanged/i.test(text)) return '本次改写没有形成有效差异，请换一个动作或补充更明确的原文。';
  if (/allowed revision range|over_expansion/i.test(text)) return '本次改写偏离原文过大，系统已收回。';
  if (/citation/i.test(text) && /unverified|unsupported/i.test(text)) return '本次改写涉及引用，但当前证据不足，请先补充文献。';
  if (/stable revision plan|interpret/i.test(text)) return '系统没有稳定理解你的自定义要求，请换一种更具体的说法。';
  if (/model invocation/i.test(text) || /service unavailable/i.test(text)) return '模型服务暂时不可用，请稍后再试。';
  return text;
}

function laneForAction(actionType: string): 'fast' | 'full' {
  return FAST_ACTIONS.has(actionType) ? 'fast' : 'full';
}

function createLocalProgressDriver(
  actionType: string,
  setProgress: React.Dispatch<React.SetStateAction<RewriteProgress | null>>
) {
  const lane = laneForAction(actionType);
  const phases = PROGRESS_PHASES[lane];
  const startedAt = Date.now();
  const targetDurationMs = lane === 'full' ? 18000 : 9000;

  setProgress({
    lane,
    actionType,
    phase: phases[0].label,
    stepIndex: 1,
    totalSteps: phases.length,
    percent: 8,
  });

  const timer = window.setInterval(() => {
    const elapsed = Date.now() - startedAt;
    const normalized = Math.min(1, elapsed / targetDurationMs);
    const eased = 1 - Math.exp(-normalized * 1.9);
    const percent = Math.min(92, Math.round(8 + eased * 84));
    let phaseIndex = phases.findIndex((phase) => percent <= phase.ratio * 100);
    if (phaseIndex === -1) phaseIndex = phases.length - 1;

    setProgress({
      lane,
      actionType,
      phase: phases[phaseIndex].label,
      stepIndex: phaseIndex + 1,
      totalSteps: phases.length,
      percent,
    });
  }, 260);

  return {
    async complete() {
      window.clearInterval(timer);
      setProgress({
        lane,
        actionType,
        phase: '改写结果已返回',
        stepIndex: phases.length,
        totalSteps: phases.length,
        percent: 100,
      });
      await wait(180);
      setProgress(null);
    },
    fail() {
      window.clearInterval(timer);
      setProgress(null);
    },
  };
}

function absoluteApiUrl(path?: string | null) {
  if (!path) return '';
  if (/^https?:\/\//i.test(path)) return path;
  return `http://127.0.0.1:8000${path.startsWith('/') ? path : `/${path}`}`;
}

function toPlainString(value: unknown) {
  if (value === null || value === undefined) return '';
  return String(value).trim();
}

function toStringArray(value: unknown) {
  if (!Array.isArray(value)) return [];
  return value.map((item) => toPlainString(item)).filter(Boolean);
}

function evidenceKindLabel(sourceKind: string) {
  switch (sourceKind) {
    case 'literature':
      return '文献';
    case 'project-section':
      return '项目章节';
    case 'reviewer-comment':
      return '导师意见';
    case 'revision-memory':
      return '历史改稿';
    default:
      return '参考依据';
  }
}

function citationStatusLabel(status?: string | null) {
  switch (status) {
    case 'verified':
      return '已核验';
    case 'partially-verified':
      return '部分核验';
    case 'unverified':
      return '待核验';
    case 'unsupported-risk':
      return '引用风险';
    case 'evidence-gap':
      return '证据不足';
    case 'supported':
      return '有文献支撑';
    case 'needs-verification':
      return '需补核验';
    case 'not-needed':
    case 'not-applicable':
      return '无需核验';
    default:
      return toPlainString(status) || '未核验';
  }
}

function evidenceFacts(item: EvidenceItem) {
  const metadata = item.metadata || {};
  const facts: string[] = [];
  const venue = toPlainString(metadata.venue);
  const year = toPlainString(metadata.year);
  const doi = toPlainString(metadata.doi);
  const chunkSourceLabel = toPlainString(metadata.chunkSourceLabel);
  const chunkSourceKind = toPlainString(metadata.chunkSourceKind);
  const itemId = toPlainString(metadata.itemId);
  const matchedTerms = toStringArray(metadata.matchedTerms).slice(0, 4);

  if (venue) facts.push(venue);
  if (year) facts.push(year);
  if (doi) facts.push(`DOI ${doi}`);
  if (chunkSourceLabel) {
    facts.push(chunkSourceLabel);
  } else if (chunkSourceKind) {
    facts.push(chunkSourceKind);
  }
  if (matchedTerms.length) facts.push(`命中 ${matchedTerms.join(' / ')}`);
  if (item.sourceKind === 'literature' && itemId) facts.push(`条目 ${itemId}`);
  return facts;
}

function buildEvidenceSummary(candidate: AgentRevisionResult | null) {
  if (!candidate) return '';
  const strategy = candidate.evidenceStrategy;
  const audit = candidate.citationAudit;
  const parts: string[] = [];
  const totalEvidence = candidate.evidence?.length || 0;
  const literatureCount = strategy?.retrievedLiteratureEvidenceCount || 0;
  const ragCount = strategy?.localRagEvidenceCount || 0;
  const doiCount = audit?.verifiedDoiEvidenceCount || 0;

  if (totalEvidence > 0) parts.push(`本次改写参考了 ${totalEvidence} 条项目依据`);
  if (literatureCount > 0) parts.push(`其中 ${literatureCount} 条来自文献库`);
  if (ragCount > 0) parts.push(`${ragCount} 条来自已导入全文`);
  if (doiCount > 0) parts.push(`${doiCount} 条带 DOI 核验`);

  return parts.join('，');
}

function cleanEvidenceTitle(raw: string) {
  return raw
    .replace(/^RAG文献[:：]?\s*/i, '')
    .replace(/^文献[:：]?\s*/i, '')
    .replace(/^历史改稿[:：]?\s*/i, '')
    .replace(/^项目章节[:：]?\s*/i, '')
    .trim();
}

function evidenceDisplayTitle(item: EvidenceItem) {
  const metadata = item.metadata || {};
  return (
    toPlainString(metadata.title) ||
    cleanEvidenceTitle(item.label) ||
    evidenceKindLabel(item.sourceKind)
  );
}

function evidenceDisplaySubtitle(item: EvidenceItem) {
  const metadata = item.metadata || {};
  const parts: string[] = [];
  const authors = toStringArray(metadata.authors).slice(0, 3);
  const venue = toPlainString(metadata.venue);
  const year = toPlainString(metadata.year);
  const doi = toPlainString(metadata.doi);

  if (authors.length) {
    parts.push(authors.join(' / '));
  }
  if (venue && year) {
    parts.push(`${venue} · ${year}`);
  } else if (venue || year) {
    parts.push(venue || year);
  }
  if (doi) {
    parts.push(`DOI ${doi}`);
  }
  return parts;
}

function evidenceSupportReason(item: EvidenceItem) {
  const metadata = item.metadata || {};
  const matchedTerms = toStringArray(metadata.matchedTerms).slice(0, 6);
  const summary = toPlainString(metadata.summary);
  if (matchedTerms.length) {
    return `本轮命中的支撑点：${matchedTerms.join(' / ')}`;
  }
  if (summary) {
    return summary;
  }
  return '';
}

function evidenceLocationLabel(item: EvidenceItem) {
  const metadata = item.metadata || {};
  const chunkSourceLabel = toPlainString(metadata.chunkSourceLabel);
  const chunkSourceKind = toPlainString(metadata.chunkSourceKind);
  const retrievalMode = toPlainString(metadata.retrievalMode);

  if (chunkSourceLabel) return chunkSourceLabel;
  if (chunkSourceKind) return chunkSourceKind;
  if (retrievalMode) return retrievalMode;

  switch (item.sourceKind) {
    case 'literature':
      return '文献库元数据';
    case 'project-section':
      return '项目章节上下文';
    case 'reviewer-comment':
      return '导师意见任务';
    case 'revision-memory':
      return '历史改稿记录';
    default:
      return '项目内证据';
  }
}

function evidenceStatLabel(item: EvidenceItem) {
  const confidence = toPlainString(item.metadata?.confidence);
  if (confidence) return `置信度 ${confidence}`;
  return `证据分 ${item.score.toFixed(1)}`;
}

function EvidenceDisclosure({ item, open = false }: { item: EvidenceItem; open?: boolean }) {
  const subtitle = evidenceDisplaySubtitle(item);
  const supportReason = evidenceSupportReason(item);
  const facts = evidenceFacts(item);

  return (
    <details className="evidence-disclosure" open={open}>
      <summary className="evidence-disclosure__summary">
        <div className="evidence-disclosure__summary-main">
          <div className="evidence-disclosure__title-row">
            <strong>{evidenceDisplayTitle(item)}</strong>
            <span className="evidence-chip evidence-chip--muted">{evidenceKindLabel(item.sourceKind)}</span>
            <span className="evidence-chip">{evidenceStatLabel(item)}</span>
          </div>
          {subtitle.length ? <div className="evidence-disclosure__subtitle">{subtitle.join(' · ')}</div> : null}
          {supportReason ? <div className="evidence-disclosure__reason">{supportReason}</div> : null}
        </div>
      </summary>
      <div className="evidence-disclosure__body">
        <div className="evidence-disclosure__meta">
          <div>
            <span>证据位置</span>
            <strong>{evidenceLocationLabel(item)}</strong>
          </div>
          {facts.length ? (
            <div>
              <span>可核验信息</span>
              <strong>{facts.slice(0, 3).join(' · ')}</strong>
            </div>
          ) : null}
        </div>
        <div className="evidence-disclosure__excerpt">
          <span>证据摘录</span>
          <p>{item.excerpt}</p>
        </div>
      </div>
    </details>
  );
}

function ReferenceTextPreview({ text }: { text: string }) {
  return (
    <div className="reference-text-preview">
      <pre>{text || '当前项目没有可供参考的原文文本。'}</pre>
    </div>
  );
}

function PdfPagePreview({
  url,
  pageNumber,
  onPageCount,
}: {
  url: string;
  pageNumber: number;
  onPageCount?: (count: number) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [message, setMessage] = useState('');

  useEffect(() => {
    let cancelled = false;
    let activeRender: { cancel?: () => void; promise?: Promise<unknown> } | null = null;

    const render = async () => {
      if (!url || !canvasRef.current) return;
      setStatus('loading');
      setMessage('');

      try {
        const loadingTask = getDocument(url);
        const pdf = await loadingTask.promise;
        if (cancelled) {
          pdf.destroy();
          return;
        }

        onPageCount?.(pdf.numPages);
        const safePageNumber = Math.min(Math.max(pageNumber, 1), pdf.numPages);
        const page = await pdf.getPage(safePageNumber);
        if (cancelled) {
          pdf.destroy();
          return;
        }

        const baseViewport = page.getViewport({ scale: 1 });
        const wrapperWidth = wrapperRef.current?.clientWidth || 920;
        const targetWidth = Math.max(720, Math.min(1100, wrapperWidth - 32));
        const scale = Math.max(1.05, Math.min(2.2, targetWidth / baseViewport.width));
        const viewport = page.getViewport({ scale });
        const canvas = canvasRef.current;
        if (!canvas) {
          pdf.destroy();
          return;
        }

        const context = canvas.getContext('2d');
        if (!context) {
          pdf.destroy();
          return;
        }

        const devicePixelRatio = window.devicePixelRatio || 1;
        canvas.width = Math.floor(viewport.width * devicePixelRatio);
        canvas.height = Math.floor(viewport.height * devicePixelRatio);
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        context.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);

        activeRender = page.render({ canvas, canvasContext: context, viewport });
        await activeRender.promise;
        pdf.destroy();

        if (cancelled) return;
        setStatus('ready');
      } catch (renderError) {
        if (cancelled) return;
        setStatus('error');
        setMessage(
          userFacingMessage(renderError instanceof Error ? renderError.message : String(renderError)) ||
            '当前页暂时无法渲染。'
        );
      }
    };

    void render();
    return () => {
      cancelled = true;
      activeRender?.cancel?.();
    };
  }, [onPageCount, pageNumber, url]);

  return (
    <div className="pdf-preview-shell" ref={wrapperRef}>
      {status !== 'ready' ? (
        <div className={`pdf-preview-placeholder ${status === 'error' ? 'pdf-preview-placeholder--error' : ''}`}>
          <div className="spinner"></div>
          <p>{status === 'error' ? message : '正在加载原稿页面...'}</p>
        </div>
      ) : null}
      <div className={`pdf-preview-canvas-shell ${status !== 'ready' ? 'is-hidden' : ''}`}>
        <canvas ref={canvasRef} className="pdf-preview-canvas" />
      </div>
    </div>
  );
}

export const Editor: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const requestedSectionId = searchParams.get('section');

  const {
    bundle,
    loadProject,
    loading,
    error: bundleError,
    activeSectionId,
    setActiveSection,
  } = useAppStore();

  const [activeRightTab, setActiveRightTab] = useState<RightTab>('rewrite');
  const [activeCenterView, setActiveCenterView] = useState<CenterView>('review');
  const [workspaceText, setWorkspaceText] = useState('');
  const [workspaceCandidate, setWorkspaceCandidate] = useState<AgentRevisionResult | null>(null);
  const [candidateContext, setCandidateContext] = useState<{ actionType: string; commentId?: string | null } | null>(null);
  const [feedback, setFeedback] = useState('');
  const [customInstruction, setCustomInstruction] = useState('');
  const [requestProgress, setRequestProgress] = useState<RewriteProgress | null>(null);
  const [requestError, setRequestError] = useState('');
  const [requestLoading, setRequestLoading] = useState(false);
  const [sourceFile, setSourceFile] = useState<SourceFilePreview | null>(null);
  const [previewPage, setPreviewPage] = useState(1);
  const [previewPageCount, setPreviewPageCount] = useState(0);
  const [copyState, setCopyState] = useState<'idle' | 'done' | 'error'>('idle');
  const sidepanelHeaderRef = useRef<HTMLDivElement>(null);
  const [sidepanelHeaderHeight, setSidepanelHeaderHeight] = useState(52);

  useEffect(() => {
    const el = sidepanelHeaderRef.current;
    if (!el) return;
    const measure = () => setSidepanelHeaderHeight(el.offsetHeight);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [requestLoading, requestError, activeRightTab]);

  useEffect(() => {
    if (id && (!bundle || bundle.project.id !== id)) {
      void loadProject(id);
    }
  }, [bundle, id, loadProject]);

  useEffect(() => {
    if (!bundle) return;
    const validSectionIds = new Set(bundle.sections.map((section) => section.id));
    if (requestedSectionId && validSectionIds.has(requestedSectionId) && requestedSectionId !== activeSectionId) {
      setActiveSection(requestedSectionId);
      return;
    }
    const fallbackSectionId = bundle.sections[0]?.id ?? null;
    if (requestedSectionId && !validSectionIds.has(requestedSectionId) && fallbackSectionId) {
      setActiveSection(fallbackSectionId);
      navigate(`/editor/${bundle.project.id}?section=${fallbackSectionId}`, { replace: true });
      return;
    }
    if ((!activeSectionId || !validSectionIds.has(activeSectionId)) && fallbackSectionId) {
      setActiveSection(fallbackSectionId);
      if (requestedSectionId !== fallbackSectionId) {
        navigate(`/editor/${bundle.project.id}?section=${fallbackSectionId}`, { replace: true });
      }
    }
  }, [activeSectionId, bundle, navigate, requestedSectionId, setActiveSection]);

  useEffect(() => {
    let cancelled = false;
    if (!bundle?.project.id) {
      setSourceFile(null);
      return;
    }
    void api.projects
      .getSourceFile(bundle.project.id)
      .then((response) => {
        if (cancelled) return;
        setSourceFile(response.file);
      })
      .catch(() => {
        if (cancelled) return;
        setSourceFile(null);
      });
    return () => {
      cancelled = true;
    };
  }, [bundle?.project.id]);

  const activeSection = useMemo(
    () => bundle?.sections.find((section) => section.id === activeSectionId) || null,
    [activeSectionId, bundle]
  );

  const activeComments = useMemo(() => {
    if (!bundle || !activeSectionId) return [];
    return bundle.comments.filter(
      (comment) => (comment.manualOverrideSectionId || comment.mappedSectionId) === activeSectionId
    );
  }, [activeSectionId, bundle]);

  const candidateWarnings = useMemo(
    () => (workspaceCandidate?.warnings || []).map((item) => userFacingMessage(item)).filter(Boolean),
    [workspaceCandidate?.warnings]
  );
  const evidenceSummary = useMemo(() => buildEvidenceSummary(workspaceCandidate), [workspaceCandidate]);
  const rankedEvidence = useMemo(
    () => [...(workspaceCandidate?.evidence || [])].sort((left, right) => right.score - left.score),
    [workspaceCandidate?.evidence]
  );
  const literatureEvidence = useMemo(
    () => rankedEvidence.filter((item) => item.sourceKind === 'literature'),
    [rankedEvidence]
  );
  const projectEvidence = useMemo(
    () => rankedEvidence.filter((item) => item.sourceKind !== 'literature'),
    [rankedEvidence]
  );
  const evidenceHighlights = useMemo(() => rankedEvidence.slice(0, 3), [rankedEvidence]);

  const sourcePreviewUrl = absoluteApiUrl(sourceFile?.previewUrl || sourceFile?.contentUrl);
  const canShowPdfPreview = sourceFile?.viewerKind === 'pdf' && Boolean(sourcePreviewUrl);
  const hasWorkspaceDraft = Boolean(
    workspaceText.trim() || workspaceCandidate || customInstruction.trim() || feedback.trim()
  );

  useEffect(() => {
    setPreviewPage(activeSection?.sourcePage || 1);
    setPreviewPageCount(0);
    setWorkspaceText('');
    setWorkspaceCandidate(null);
    setCandidateContext(null);
    setFeedback('');
    setCustomInstruction('');
    setRequestProgress(null);
    setRequestError('');
    setCopyState('idle');
  }, [activeSection?.id, activeSection?.sourcePage]);

  useEffect(() => {
    if (!canShowPdfPreview && activeCenterView === 'review') {
      setActiveCenterView('draft');
    }
  }, [activeCenterView, canShowPdfPreview]);

  const runRevision = async (actionType: string, options?: { commentId?: string | null; note?: string }) => {
    if (!bundle || !activeSection) return;
    const text = workspaceText.trim();
    if (!text) {
      setRequestError('先把你要修改的原文粘贴到左侧输入框。');
      return;
    }

    const note = (options?.note ?? '').trim();
    if (actionType === 'custom-instruction' && !note) {
      setRequestError('先输入你希望系统如何修改这段文字。');
      return;
    }

    const driver = createLocalProgressDriver(actionType, setRequestProgress);
    setRequestLoading(true);
    setRequestError('');
    setCopyState('idle');

    try {
      const result = await api.agent.revise({
        projectId: bundle.project.id,
        title: `${bundle.project.title} / ${activeSection.title}`,
        text,
        actionType,
        note,
        commentId: options?.commentId ?? null,
        previousCandidateText: '',
      });
      setWorkspaceCandidate(result);
      setCandidateContext({ actionType, commentId: options?.commentId ?? null });
      setActiveCenterView('draft');
      setActiveRightTab('rewrite');
      await driver.complete();
    } catch (err: any) {
      driver.fail();
      setRequestError(userFacingMessage(err?.message || '本次改写未能完成。'));
    } finally {
      setRequestLoading(false);
    }
  };

  const handleFeedbackRevision = async () => {
    if (!bundle || !workspaceCandidate || !feedback.trim()) return;
    const driver = createLocalProgressDriver('custom-instruction', setRequestProgress);
    setRequestLoading(true);
    setRequestError('');
    setCopyState('idle');

    try {
      const result = await api.agent.revise({
        projectId: bundle.project.id,
        title: bundle.project.title,
        text: workspaceCandidate.baseText || workspaceText,
        actionType: 'custom-instruction',
        note: feedback.trim(),
        commentId: candidateContext?.commentId ?? null,
        previousCandidateText: workspaceCandidate.text,
      });
      setWorkspaceCandidate(result);
      setCandidateContext({ actionType: 'custom-instruction', commentId: candidateContext?.commentId ?? null });
      setActiveCenterView('draft');
      setFeedback('');
      await driver.complete();
    } catch (err: any) {
      driver.fail();
      setRequestError(userFacingMessage(err?.message || '本次改写未能完成。'));
    } finally {
      setRequestLoading(false);
    }
  };

  const handleAcceptCandidate = () => {
    if (!workspaceCandidate) return;
    setWorkspaceText(workspaceCandidate.text);
    setWorkspaceCandidate(null);
    setCandidateContext(null);
    setFeedback('');
    setRequestError('');
  };

  const handleRejectCandidate = () => {
    setWorkspaceCandidate(null);
    setCandidateContext(null);
    setFeedback('');
    setRequestError('');
  };

  const handleCopyCandidate = async () => {
    if (!workspaceCandidate?.text) return;
    try {
      await navigator.clipboard.writeText(workspaceCandidate.text);
      setCopyState('done');
      await wait(1200);
      setCopyState('idle');
    } catch {
      setCopyState('error');
      setRequestError('浏览器没有完成复制，请手动选中右侧结果。');
    }
  };

  const handleSwitchSection = (nextSectionId: string) => {
    if (!activeSection || nextSectionId === activeSection.id) return;
    if (hasWorkspaceDraft && !window.confirm('切换章节会清空当前粘贴文本和候选结果，确认继续吗？')) {
      return;
    }
    setActiveSection(nextSectionId);
    navigate(`/editor/${bundle?.project.id}?section=${nextSectionId}`, { replace: true });
  };

  if (loading.bundle) {
    return (
      <div className="editor-loading">
        <div className="spinner"></div>
        <p>正在加载编辑器...</p>
      </div>
    );
  }

  if (!bundle || !activeSection) {
    return <div className="editor-error-banner">{userFacingMessage(bundleError) || '无法加载当前章节。'}</div>;
  }

  const currentStatusText = requestLoading ? requestProgress?.phase || '正在处理' : requestError;
  const outputText = workspaceCandidate?.text || '';

  return (
    <div className="editor-shell">
      <header className="editor-header">
        <div className="editor-header__left">
          <Button variant="ghost" onClick={() => navigate(`/`)}>
            <ArrowLeft size={18} className="mr-2" />
            返回诊断
          </Button>
          <div className="editor-header__titles">
            <h2>{bundle.project.title}</h2>
            <span>{activeSection.title}</span>
          </div>
        </div>

        <div className="editor-header__right">
          <Button variant="outline" onClick={() => navigate(`/literature/${bundle.project.id}`)}>
            <Library size={16} className="mr-2" />
            文献库
          </Button>
        </div>
      </header>

      <div className="editor-workspace">
        <aside className="editor-sections">
          <div className="editor-sections__title">章节导航</div>
          <div className="editor-sections__list">
            {bundle.sections.map((section) => (
              <button
                key={section.id}
                type="button"
                className={`editor-section-chip ${section.id === activeSectionId ? 'active' : ''}`}
                onClick={() => handleSwitchSection(section.id)}
              >
                <div className="editor-section-chip__title">{section.title}</div>
                <div className="editor-section-chip__meta">
                  {section.issueCount || 0} 个问题 · {section.commentCount || 0} 条意见
                </div>
              </button>
            ))}
          </div>
        </aside>

        <main className="editor-main">
          <div className="editor-main__switcher">
            <button
              type="button"
              className={activeCenterView === 'review' ? 'active' : ''}
              onClick={() => setActiveCenterView('review')}
              disabled={!canShowPdfPreview}
            >
              审阅页面
            </button>
            <button
              type="button"
              className={activeCenterView === 'draft' ? 'active' : ''}
              onClick={() => setActiveCenterView('draft')}
            >
              文本页面
            </button>
          </div>

          {activeCenterView === 'review' ? (
            <section className="reference-stage">
              <div className="reference-stage__top">
                <div>
                  <div className="reference-stage__eyebrow">原文参考</div>
                  <h3>{activeSection.title}</h3>
                </div>
                <div className="reference-stage__meta">
                  {activeSection.sourcePage ? <span>原稿第 {activeSection.sourcePage} 页</span> : null}
                  <span>{activeComments.length} 条导师意见</span>
                </div>
              </div>

              {canShowPdfPreview ? (
                <div className="reference-stage__preview-shell">
                  <div className="reference-stage__preview-toolbar">
                    <div className="reference-stage__preview-info">
                      <strong>原始 PDF</strong>
                      <span>
                        第 {previewPage} 页{previewPageCount ? ` / 共 ${previewPageCount} 页` : ''}
                      </span>
                    </div>
                    <div className="reference-stage__preview-actions">
                      <button
                        type="button"
                        onClick={() => setPreviewPage((current) => Math.max(1, current - 1))}
                        disabled={previewPage <= 1}
                      >
                        上一页
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          setPreviewPage((current) => (previewPageCount ? Math.min(previewPageCount, current + 1) : current + 1))
                        }
                        disabled={previewPageCount > 0 && previewPage >= previewPageCount}
                      >
                        下一页
                      </button>
                      <a href={`${sourcePreviewUrl}#page=${previewPage}`} target="_blank" rel="noreferrer">
                        新窗口打开
                      </a>
                    </div>
                  </div>
                  <PdfPagePreview
                    key={`${activeSection.id}-${previewPage}-${sourcePreviewUrl}`}
                    url={sourcePreviewUrl}
                    pageNumber={previewPage}
                    onPageCount={setPreviewPageCount}
                  />
                </div>
              ) : (
                <ReferenceTextPreview text={activeSection.currentText} />
              )}
            </section>
          ) : (
            <section className="workspace-stage">
              <div className="workspace-pane">
                <div className="workspace-pane__header">
                  <div>
                    <div className="workspace-pane__eyebrow">输入区</div>
                    <h3>粘贴你要修改的原文</h3>
                  </div>
                  <button
                    type="button"
                    className="workspace-pane__ghost"
                    onClick={() => {
                      setWorkspaceText('');
                      setWorkspaceCandidate(null);
                      setCandidateContext(null);
                      setFeedback('');
                      setRequestError('');
                    }}
                  >
                    清空
                  </button>
                </div>
                <p className="workspace-pane__hint">从原稿复制一段或几段文字到这里，系统只改这部分，不再自动读取整章。</p>
                <textarea
                  className="workspace-pane__textarea"
                  value={workspaceText}
                  onChange={(event) => {
                    const next = event.target.value;
                    setWorkspaceText(next);
                    setRequestError('');
                    if (workspaceCandidate && next !== workspaceCandidate.baseText) {
                      setWorkspaceCandidate(null);
                      setCandidateContext(null);
                      setFeedback('');
                    }
                  }}
                  placeholder="把你要修改的文字粘贴到这里。建议一次处理 1-3 段。"
                />
              </div>

              <div className="workspace-pane">
                <div className="workspace-pane__header">
                  <div>
                    <div className="workspace-pane__eyebrow">输出区</div>
                    <h3>{workspaceCandidate ? '候选改写结果' : '等待生成结果'}</h3>
                  </div>
                  <div className="workspace-pane__header-actions">
                    <button
                      type="button"
                      className="workspace-pane__ghost"
                      onClick={() => void handleCopyCandidate()}
                      disabled={!workspaceCandidate?.text}
                    >
                      <Copy size={14} />
                      {copyState === 'done' ? '已复制' : '复制结果'}
                    </button>
                    <button
                      type="button"
                      className="workspace-pane__ghost"
                      onClick={handleRejectCandidate}
                      disabled={!workspaceCandidate}
                    >
                      <X size={14} />
                      清空结果
                    </button>
                  </div>
                </div>

                {workspaceCandidate ? (
                  <div className="workspace-result-meta">
                    <span>{workspaceCandidate.summary}</span>
                    <span>{workspaceCandidate.agentTrace?.executionLane === 'full' ? '深度链路' : '快速链路'}</span>
                    {evidenceSummary ? <span>{evidenceSummary}</span> : null}
                    {workspaceCandidate.citationVerification?.status ? (
                      <span>{citationStatusLabel(workspaceCandidate.citationVerification.status)}</span>
                    ) : null}
                  </div>
                ) : (
                  <p className="workspace-pane__hint">右侧会显示本轮候选改写。接受后会把结果回填到左侧输入框。</p>
                )}

                <textarea
                  className="workspace-pane__textarea workspace-pane__textarea--output"
                  value={outputText}
                  readOnly
                  placeholder="选择一个动作后，这里会返回候选改写结果。"
                />

                {candidateWarnings.length ? (
                  <Card className="editor-warning-card">
                    <div className="editor-warning-card__title">
                      <AlertCircle size={16} />
                      需要确认
                    </div>
                    <ul className="editor-warning-list">
                      {candidateWarnings.map((warning, index) => (
                        <li key={`${warning}-${index}`}>{warning}</li>
                      ))}
                    </ul>
                  </Card>
                ) : null}

                <div className="workspace-result-actions">
                  <Button variant="outline" onClick={handleRejectCandidate} disabled={!workspaceCandidate}>
                    <X size={16} className="mr-2" />
                    拒绝
                  </Button>
                  <Button onClick={handleAcceptCandidate} disabled={!workspaceCandidate}>
                    <Check size={16} className="mr-2" />
                    接受并回填输入区
                  </Button>
                </div>
              </div>
            </section>
          )}
        </main>

        <aside className="editor-sidepanel">
          <div className="editor-sidepanel__header" ref={sidepanelHeaderRef}>
          {requestLoading || requestError ? (
            <Card className={`request-status-card ${requestError && !requestLoading ? 'request-status-card--error' : ''}`}>
              <div className="request-status-card__title">
                {requestLoading ? '正在生成候选结果' : '本次改写未完成'}
              </div>
              <div className="request-status-card__subtitle">{currentStatusText}</div>
              {requestProgress ? (
                <>
                  <div className="request-status-card__bar">
                    <span style={{ width: `${requestProgress.percent}%` }} />
                  </div>
                  <div className="request-status-card__percent">{requestProgress.percent}%</div>
                </>
              ) : null}
            </Card>
          ) : null}

          <div className="sidepanel-tabs">
            <button
              type="button"
              className={activeRightTab === 'rewrite' ? 'active' : ''}
              onClick={() => setActiveRightTab('rewrite')}
            >
              改写
            </button>
            <button
              type="button"
              className={activeRightTab === 'reference' ? 'active' : ''}
              onClick={() => setActiveRightTab('reference')}
            >
              参考
            </button>
            <button
              type="button"
              className={activeRightTab === 'comments' ? 'active' : ''}
              onClick={() => setActiveRightTab('comments')}
            >
              意见
            </button>
            <button
              type="button"
              className={activeRightTab === 'trace' ? 'active' : ''}
              onClick={() => setActiveRightTab('trace')}
            >
              轨迹
            </button>
          </div>

          </div>
          <div className="sidepanel-body" style={{ height: `calc(100vh - 72px - ${sidepanelHeaderHeight}px)` }}>
            {activeRightTab === 'rewrite' ? (
              <>
                <Card className="sidepanel-card">
                  <h3 className="sidepanel-card__title">
                    <Sparkles size={18} />
                    标准动作
                  </h3>
                  <div className="action-grid">
                    {ACTIONS.map((action) => (
                      <button
                        key={action.type}
                        type="button"
                        className="action-button"
                        disabled={requestLoading}
                        onClick={() => void runRevision(action.type)}
                      >
                        <span className="action-button__label">{action.label}</span>
                        <span className="action-button__desc">{action.desc}</span>
                      </button>
                    ))}
                  </div>
                </Card>

                <Card className="sidepanel-card">
                  <h3 className="sidepanel-card__title">
                    <Wand2 size={18} />
                    自定义要求
                  </h3>
                  <textarea
                    className="feedback-textarea"
                    value={customInstruction}
                    onChange={(event) => setCustomInstruction(event.target.value)}
                    placeholder="输入你的修改要求"
                  />
                  <Button
                    onClick={() => void runRevision('custom-instruction', { note: customInstruction })}
                    isLoading={requestLoading}
                    disabled={!workspaceText.trim() || !customInstruction.trim()}
                  >
                    按这个要求改写
                  </Button>
                </Card>

                {workspaceCandidate ? (
                  <Card className="sidepanel-card">
                    <h3 className="sidepanel-card__title">
                      <RefreshCcw size={18} />
                      再改一轮
                    </h3>
                    <textarea
                      className="feedback-textarea"
                      value={feedback}
                      onChange={(event) => setFeedback(event.target.value)}
                      placeholder="继续说明你希望如何调整当前结果"
                    />
                    <div className="candidate-actions">
                      <Button onClick={handleFeedbackRevision} isLoading={requestLoading} disabled={!feedback.trim()}>
                        再修改一次
                      </Button>
                      <Button variant="outline" onClick={handleAcceptCandidate}>
                        接受
                      </Button>
                      <Button variant="outline" onClick={handleRejectCandidate}>
                        拒绝
                      </Button>
                    </div>
                  </Card>
                ) : null}
              </>
            ) : null}

            {activeRightTab === 'reference' ? (
              <>
                {workspaceCandidate?.evidenceStrategy ? (
                  <>
                    {/* ── Trust Banner ── */}
                    <div className={`ref-trust-banner ref-trust-banner--${
                      workspaceCandidate.citationVerification?.status === 'verified' || workspaceCandidate.citationVerification?.status === 'not-needed' || workspaceCandidate.citationVerification?.status === 'not-applicable'
                        ? 'safe'
                        : workspaceCandidate.citationVerification?.status === 'partially-verified' || workspaceCandidate.citationVerification?.status === 'needs-verification'
                        ? 'caution'
                        : workspaceCandidate.citationVerification?.status === 'unsupported-risk' || workspaceCandidate.citationVerification?.status === 'evidence-gap'
                        ? 'risk'
                        : 'neutral'
                    }`}>
                      <div className="ref-trust-banner__icon">
                        {workspaceCandidate.citationVerification?.status === 'verified' || workspaceCandidate.citationVerification?.status === 'not-needed' || workspaceCandidate.citationVerification?.status === 'not-applicable'
                          ? <Check size={20} />
                          : workspaceCandidate.citationVerification?.status === 'unsupported-risk' || workspaceCandidate.citationVerification?.status === 'evidence-gap'
                          ? <AlertCircle size={20} />
                          : <FileText size={20} />}
                      </div>
                      <div className="ref-trust-banner__text">
                        <strong>{citationStatusLabel(workspaceCandidate.citationVerification?.status)}</strong>
                        <span>{workspaceCandidate.citationAudit?.recommendedAction || evidenceSummary || '系统已完成证据检索与核验。'}</span>
                      </div>
                    </div>

                    {/* ── Stats Overview ── */}
                    <div className="ref-stats-row">
                      <div className="ref-stat">
                        <span className="ref-stat__num">{workspaceCandidate.evidence?.length || 0}</span>
                        <span className="ref-stat__label">引用证据</span>
                      </div>
                      <div className="ref-stat">
                        <span className="ref-stat__num">{workspaceCandidate.evidenceStrategy.retrievedLiteratureEvidenceCount}</span>
                        <span className="ref-stat__label">文献来源</span>
                      </div>
                      <div className="ref-stat">
                        <span className="ref-stat__num">{workspaceCandidate.evidenceStrategy.localRagEvidenceCount}</span>
                        <span className="ref-stat__label">全文索引</span>
                      </div>
                      <div className="ref-stat">
                        <span className="ref-stat__num">{workspaceCandidate.citationAudit?.verifiedDoiEvidenceCount || 0}</span>
                        <span className="ref-stat__label">DOI 核验</span>
                      </div>
                    </div>

                    {/* ── Literature Evidence ── */}
                    {literatureEvidence.length > 0 && (
                      <div className="ref-section">
                        <div className="ref-section__header">
                          <Library size={16} />
                          <span>文献支撑</span>
                          <span className="ref-section__count">{literatureEvidence.length}</span>
                        </div>
                        <div className="ref-evidence-list">
                          {literatureEvidence.map((item, index) => {
                            const subtitle = evidenceDisplaySubtitle(item);
                            const reason = evidenceSupportReason(item);
                            const relevance = Math.min(100, Math.round(item.score));
                            return (
                              <details key={`${item.sourceId || item.label}-lit-${index}`} className="ref-evidence-card" open={index === 0}>
                                <summary className="ref-evidence-card__summary">
                                  <div className="ref-evidence-card__head">
                                    <strong className="ref-evidence-card__title">{evidenceDisplayTitle(item)}</strong>
                                    <div className="ref-evidence-card__relevance">
                                      <div className="ref-relevance-bar">
                                        <div className="ref-relevance-bar__fill" style={{ width: `${relevance}%` }} />
                                      </div>
                                      <span className="ref-relevance-score">{item.score.toFixed(1)}</span>
                                    </div>
                                  </div>
                                  {subtitle.length > 0 && <div className="ref-evidence-card__subtitle">{subtitle.join(' · ')}</div>}
                                  {reason && <div className="ref-evidence-card__reason">{reason}</div>}
                                </summary>
                                <div className="ref-evidence-card__body">
                                  <div className="ref-evidence-card__meta-grid">
                                    <div className="ref-evidence-card__meta-item">
                                      <span>来源类型</span>
                                      <strong>{evidenceKindLabel(item.sourceKind)}</strong>
                                    </div>
                                    <div className="ref-evidence-card__meta-item">
                                      <span>证据位置</span>
                                      <strong>{evidenceLocationLabel(item)}</strong>
                                    </div>
                                  </div>
                                  <div className="ref-evidence-card__excerpt">
                                    <span className="ref-evidence-card__excerpt-label">摘录原文</span>
                                    <p>{item.excerpt}</p>
                                  </div>
                                </div>
                              </details>
                            );
                          })}
                        </div>
                      </div>
                    )}

                    {/* ── Project Context Evidence ── */}
                    {projectEvidence.length > 0 && (
                      <div className="ref-section">
                        <div className="ref-section__header">
                          <FileText size={16} />
                          <span>项目内上下文</span>
                          <span className="ref-section__count">{projectEvidence.length}</span>
                        </div>
                        <div className="ref-evidence-list">
                          {projectEvidence.map((item, index) => {
                            const subtitle = evidenceDisplaySubtitle(item);
                            const reason = evidenceSupportReason(item);
                            const relevance = Math.min(100, Math.round(item.score));
                            return (
                              <details key={`${item.sourceId || item.label}-proj-${index}`} className="ref-evidence-card ref-evidence-card--project" open={index === 0}>
                                <summary className="ref-evidence-card__summary">
                                  <div className="ref-evidence-card__head">
                                    <strong className="ref-evidence-card__title">{evidenceDisplayTitle(item)}</strong>
                                    <div className="ref-evidence-card__relevance">
                                      <div className="ref-relevance-bar">
                                        <div className="ref-relevance-bar__fill ref-relevance-bar__fill--project" style={{ width: `${relevance}%` }} />
                                      </div>
                                      <span className="ref-relevance-score">{item.score.toFixed(1)}</span>
                                    </div>
                                  </div>
                                  {subtitle.length > 0 && <div className="ref-evidence-card__subtitle">{subtitle.join(' · ')}</div>}
                                  {reason && <div className="ref-evidence-card__reason">{reason}</div>}
                                </summary>
                                <div className="ref-evidence-card__body">
                                  <div className="ref-evidence-card__meta-grid">
                                    <div className="ref-evidence-card__meta-item">
                                      <span>来源类型</span>
                                      <strong>{evidenceKindLabel(item.sourceKind)}</strong>
                                    </div>
                                    <div className="ref-evidence-card__meta-item">
                                      <span>证据位置</span>
                                      <strong>{evidenceLocationLabel(item)}</strong>
                                    </div>
                                  </div>
                                  <div className="ref-evidence-card__excerpt">
                                    <span className="ref-evidence-card__excerpt-label">摘录原文</span>
                                    <p>{item.excerpt}</p>
                                  </div>
                                </div>
                              </details>
                            );
                          })}
                        </div>
                      </div>
                    )}

                    {rankedEvidence.length === 0 && (
                      <div className="sidepanel-empty">本次改写没有命中项目内证据。可以先导入文献或补充 RAG 索引。</div>
                    )}
                  </>
                ) : (
                  <div className="sidepanel-empty">生成候选后，这里会展示改写引用了哪些文献和项目上下文，帮助你判断结果是否可信。</div>
                )}
              </>
            ) : null}

            {activeRightTab === 'comments' ? (
              <Card className="sidepanel-card">
                <h3 className="sidepanel-card__title">
                  <MessageSquare size={18} />
                  导师意见
                </h3>
                <div className="comment-list">
                  {activeComments.length === 0 ? (
                    <div className="sidepanel-empty">当前章节没有待处理意见。</div>
                  ) : (
                    activeComments.map((comment) => (
                      <div key={comment.id} className="comment-item">
                        <div className="comment-item__meta">
                          <span className="comment-item__status">{comment.status}</span>
                          <span>{comment.suggestedAction}</span>
                        </div>
                        <p>{comment.rawComment}</p>
                        <Button
                          variant="outline"
                          size="sm"
                          fullWidth
                          onClick={() => void runRevision('comment-revision', { commentId: comment.id })}
                          isLoading={requestLoading}
                          disabled={!workspaceText.trim()}
                        >
                          按这条意见改写
                        </Button>
                      </div>
                    ))
                  )}
                </div>
              </Card>
            ) : null}

            {activeRightTab === 'trace' ? (
              <Card className="sidepanel-card">
                <h3 className="sidepanel-card__title">
                  <FileText size={18} />
                  执行轨迹
                </h3>
                {workspaceCandidate?.agentTrace?.stepRuns?.length ? (
                  <>
                    <div className="trace-summary">
                      <span>{workspaceCandidate.agentTrace.executionLane === 'full' ? '深度链路' : '快速链路'}</span>
                      <span>{workspaceCandidate.agentTrace.effectiveActionType || workspaceCandidate.actionType}</span>
                    </div>
                    <div className="trace-list">
                      {workspaceCandidate.agentTrace.stepRuns.map((step, index) => (
                        <div key={`${step.step}-${index}`} className="trace-item">
                          <div className="trace-item__top">
                            <strong>{step.step}</strong>
                            <span>{step.status}</span>
                          </div>
                          <div className="trace-item__bottom">{step.latency_ms} ms</div>
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="sidepanel-empty">生成候选后，这里会显示本轮处理轨迹。</div>
                )}
              </Card>
            ) : null}
          </div>
        </aside>
      </div>
    </div>
  );
};

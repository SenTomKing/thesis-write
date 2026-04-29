import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  AlertCircle,
  ArrowLeft,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Download,
  ExternalLink,
  FileSearch,
  Library,
  Quote,
  Search,
  Upload,
} from 'lucide-react';
import { Button } from '../components/ui/Button';
import { Card } from '../components/ui/Card';
import { api } from '../api/client';
import {
  CitationFormatResult,
  FulltextBatchFetchResult,
  LiteratureAttachment,
  LiteratureItem,
  LiteratureSearchResult,
  RagSearchResult,
} from '../types';
import './Literature.css';

const API_ORIGIN = 'http://127.0.0.1:8000';
const DEFAULT_SOURCES = ['openalex', 'crossref', 'semantic-scholar'];

type ActiveView = 'library' | 'search';
type DetailSection = 'fulltext' | 'citation' | 'evidence';
type OperationKind = 'bootstrap' | 'search' | 'import' | 'fetch' | 'batch-fetch' | 'index' | 'evidence' | 'citation';
type OperationProgress = {
  kind: OperationKind;
  title: string;
  detail: string;
  percent: number;
};

const OPERATION_PRESETS: Record<
  OperationKind,
  { title: string; duration: number; phases: Array<{ detail: string; ratio: number }> }
> = {
  bootstrap: {
    title: '正在加载文献库',
    duration: 4200,
    phases: [
      { detail: '读取项目文献与全文附件', ratio: 0.45 },
      { detail: '刷新候选检索词与文献状态', ratio: 0.9 },
    ],
  },
  search: {
    title: '正在检索候选文献',
    duration: 9000,
    phases: [
      { detail: '查询开放学术源', ratio: 0.38 },
      { detail: '去重并筛选相关结果', ratio: 0.88 },
    ],
  },
  import: {
    title: '正在导入项目文献库',
    duration: 18000,
    phases: [
      { detail: '写入选中文献条目', ratio: 0.28 },
      { detail: '尝试抓取开放全文并建立索引', ratio: 0.82 },
      { detail: '刷新项目文献库', ratio: 0.95 },
    ],
  },
  fetch: {
    title: '正在获取全文',
    duration: 16000,
    phases: [
      { detail: '检查开放全文链接', ratio: 0.18 },
      { detail: '下载并提取 PDF 正文', ratio: 0.74 },
      { detail: '写入本地索引并刷新状态', ratio: 0.95 },
    ],
  },
  'batch-fetch': {
    title: '正在批量获取全文',
    duration: 22000,
    phases: [
      { detail: '检查选中文献的开放全文链接', ratio: 0.16 },
      { detail: '批量下载、提取并建立索引', ratio: 0.82 },
      { detail: '刷新项目文献库', ratio: 0.95 },
    ],
  },
  index: {
    title: '正在写入全文文本',
    duration: 8000,
    phases: [
      { detail: '切分全文片段', ratio: 0.46 },
      { detail: '建立本地检索索引', ratio: 0.92 },
    ],
  },
  evidence: {
    title: '正在检索证据片段',
    duration: 6500,
    phases: [
      { detail: '读取本地文献向量与片段', ratio: 0.46 },
      { detail: '计算相关度并整理命中片段', ratio: 0.92 },
    ],
  },
  citation: {
    title: '正在生成参考文献',
    duration: 7000,
    phases: [
      { detail: '匹配文献条目与引用信息', ratio: 0.48 },
      { detail: '按格式输出参考文献', ratio: 0.92 },
    ],
  },
};

function wait(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function createOperationProgressDriver(
  kind: OperationKind,
  setProgress: React.Dispatch<React.SetStateAction<OperationProgress | null>>
) {
  const preset = OPERATION_PRESETS[kind];
  const startedAt = Date.now();

  setProgress({
    kind,
    title: preset.title,
    detail: preset.phases[0].detail,
    percent: 8,
  });

  const timer = window.setInterval(() => {
    const elapsed = Date.now() - startedAt;
    const normalized = Math.min(1, elapsed / preset.duration);
    const eased = 1 - Math.exp(-normalized * 2);
    const percent = Math.min(92, Math.round(8 + eased * 84));
    let phaseIndex = preset.phases.findIndex((phase) => percent <= phase.ratio * 100);
    if (phaseIndex === -1) phaseIndex = preset.phases.length - 1;

    setProgress({
      kind,
      title: preset.title,
      detail: preset.phases[phaseIndex].detail,
      percent,
    });
  }, 240);

  return {
    async complete(detail = '操作已完成') {
      window.clearInterval(timer);
      setProgress({
        kind,
        title: preset.title,
        detail,
        percent: 100,
      });
      await wait(260);
      setProgress(null);
    },
    fail() {
      window.clearInterval(timer);
      setProgress(null);
    },
  };
}

function toAbsoluteUrl(url?: string) {
  if (!url) return '';
  if (/^https?:\/\//i.test(url)) return url;
  return `${API_ORIGIN}${url}`;
}

function formatAuthors(authors: string[]) {
  if (!authors?.length) return '作者待确认';
  if (authors.length <= 3) return authors.join('、');
  return `${authors.slice(0, 3).join('、')} 等`;
}

function getItemAttachment(itemId: string, attachments: LiteratureAttachment[]) {
  return attachments.find((attachment) => attachment.itemId === itemId && attachment.kind === 'pdf') || null;
}

function getItemAccessLabel(item: LiteratureItem, attachment: LiteratureAttachment | null) {
  if (attachment?.localPath) return '全文已下载';
  if (item.openAccessStatus === 'open' && item.pdfUrl) return '可获取全文';
  if (attachment?.url) return '已记录全文链接';
  return '仅元数据';
}

function summarizeBatchResult(result: FulltextBatchFetchResult) {
  const parts = [
    result.downloadedCount ? `已下载 ${result.downloadedCount} 篇` : '',
    result.linkedCount ? `仅保留链接 ${result.linkedCount} 篇` : '',
    result.failedCount ? `失败 ${result.failedCount} 篇` : '',
  ].filter(Boolean);
  return parts.join('，') || '没有新的全文可获取';
}

export const Literature: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [activeView, setActiveView] = useState<ActiveView>('library');
  const [query, setQuery] = useState('');
  const [sources, setSources] = useState<string[]>(DEFAULT_SOURCES);
  const [searchResult, setSearchResult] = useState<LiteratureSearchResult | null>(null);
  const [library, setLibrary] = useState<LiteratureItem[]>([]);
  const [attachments, setAttachments] = useState<LiteratureAttachment[]>([]);
  const [selectedSearchIds, setSelectedSearchIds] = useState<string[]>([]);
  const [selectedLibraryIds, setSelectedLibraryIds] = useState<string[]>([]);
  const [activeLibraryId, setActiveLibraryId] = useState<string | null>(null);
  const [activeCandidateId, setActiveCandidateId] = useState<string | null>(null);
  const [citationText, setCitationText] = useState('');
  const [citationResult, setCitationResult] = useState<CitationFormatResult | null>(null);
  const [indexText, setIndexText] = useState('');
  const [evidenceQuery, setEvidenceQuery] = useState('');
  const [evidenceResult, setEvidenceResult] = useState<RagSearchResult | null>(null);
  const [loadingKey, setLoadingKey] = useState<string | null>(null);
  const [operationProgress, setOperationProgress] = useState<OperationProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [openSections, setOpenSections] = useState<Record<DetailSection, boolean>>({
    fulltext: true,
    citation: false,
    evidence: false,
  });

  const activeLibraryItem = useMemo(
    () => library.find((item) => item.id === activeLibraryId) || null,
    [activeLibraryId, library]
  );

  const activeCandidateItem = useMemo(
    () => searchResult?.candidates.find((item) => item.id === activeCandidateId) || null,
    [activeCandidateId, searchResult]
  );

  const detailItem = activeView === 'library' ? activeLibraryItem : activeCandidateItem;
  const detailAttachment = useMemo(
    () => (detailItem ? getItemAttachment(detailItem.id, attachments) : null),
    [attachments, detailItem]
  );

  const metrics = useMemo(() => {
    const downloaded = attachments.filter((attachment) => Boolean(attachment.localPath)).length;
    const linked = attachments.filter((attachment) => Boolean(attachment.url) && !attachment.localPath).length;
    const openAccess = library.filter((item) => item.openAccessStatus === 'open' && item.pdfUrl).length;
    return {
      total: library.length,
      downloaded,
      linked,
      openAccess,
    };
  }, [attachments, library]);

  const loadLibrary = async (projectId: string) => {
    const [suggestion, libraryData] = await Promise.all([
      api.literature.suggestQuery(projectId),
      api.literature.list(projectId),
    ]);
    setQuery((current) => current || suggestion.query);
    setEvidenceQuery((current) => current || suggestion.query);
    setLibrary(libraryData.items);
    setAttachments(libraryData.attachments);
    setActiveLibraryId((current) => current || libraryData.items[0]?.id || null);
  };

  useEffect(() => {
    if (!id) return;
    void (async () => {
      const driver = createOperationProgressDriver('bootstrap', setOperationProgress);
      try {
        setLoadingKey('bootstrap');
        await loadLibrary(id);
        setError(null);
        await driver.complete('文献库已加载');
      } catch (err: any) {
        driver.fail();
        setError(err.message || '加载文献库失败');
      } finally {
        setLoadingKey(null);
      }
    })();
  }, [id]);

  useEffect(() => {
    if (library.length === 0) {
      setActiveLibraryId(null);
      return;
    }
    if (!activeLibraryId || !library.some((item) => item.id === activeLibraryId)) {
      setActiveLibraryId(library[0].id);
    }
  }, [activeLibraryId, library]);

  const toggleSource = (source: string) => {
    setSources((current) =>
      current.includes(source) ? current.filter((item) => item !== source) : [...current, source]
    );
  };

  const toggleSection = (section: DetailSection) => {
    setOpenSections((current) => ({ ...current, [section]: !current[section] }));
  };

  const selectAll = () => {
    if (activeView === 'search') {
      setSelectedSearchIds(searchResult?.candidates.map((item) => item.id) || []);
      return;
    }
    setSelectedLibraryIds(library.map((item) => item.id));
  };

  const clearSelection = () => {
    if (activeView === 'search') {
      setSelectedSearchIds([]);
      return;
    }
    setSelectedLibraryIds([]);
  };

  const toggleSelected = (itemId: string, selectedIds: string[], setSelected: React.Dispatch<React.SetStateAction<string[]>>) => {
    setSelected(selectedIds.includes(itemId) ? selectedIds.filter((value) => value !== itemId) : [...selectedIds, itemId]);
  };

  const handleSearch = async () => {
    if (!id || !query.trim()) return;
    const driver = createOperationProgressDriver('search', setOperationProgress);
    try {
      setLoadingKey('search');
      const result = await api.literature.search(id, {
        query: query.trim(),
        sources,
        limit: 12,
      });
      setSearchResult(result);
      setSelectedSearchIds(result.candidates.map((item) => item.id));
      setActiveCandidateId(result.candidates[0]?.id || null);
      setActiveView('search');
      setNotice(`${result.candidates.length} 条候选结果已更新。`);
      setError(null);
      await driver.complete(`已返回 ${result.candidates.length} 条候选文献`);
    } catch (err: any) {
      driver.fail();
      setError(err.message || '检索失败');
      setNotice(null);
    } finally {
      setLoadingKey(null);
    }
  };

  const handleImport = async (itemIds?: string[]) => {
    if (!id || !searchResult) return;
    const targetIds = itemIds?.length ? itemIds : selectedSearchIds;
    if (!targetIds.length) return;
    const driver = createOperationProgressDriver('import', setOperationProgress);

    try {
      setLoadingKey('import');
      const response = await api.literature.import(id, {
        runId: searchResult.run.id,
        itemIds: targetIds,
      });
      await loadLibrary(id);
      setActiveView('library');
      const fulltextResults = response.fulltextResults || [];
      const downloadedCount = fulltextResults.filter((item: any) => item.status === 'downloaded').length;
      setNotice(
        downloadedCount > 0
          ? `Imported ${targetIds.length} papers and auto-filled ${downloadedCount} full texts.`
          : `Imported ${targetIds.length} papers.`
      );
      setError(null);
      await driver.complete(
        downloadedCount > 0
          ? `Imported ${targetIds.length} papers and auto-filled ${downloadedCount} full texts.`
          : `Imported ${targetIds.length} papers.`
      );
    } catch (err: any) {
      driver.fail();
      setError(err.message || 'Import failed');
      setNotice(null);
    } finally {
      setLoadingKey(null);
    }
  };

  const triggerDownload = (relativeUrl?: string) => {
    const absoluteUrl = toAbsoluteUrl(relativeUrl);
    if (!absoluteUrl) return;
    window.location.assign(absoluteUrl);
  };

  const handleFetchSingle = async (itemId: string) => {
    if (!id) return;
    const driver = createOperationProgressDriver('fetch', setOperationProgress);
    try {
      setLoadingKey(`fetch:${itemId}`);
      const result = await api.literature.fetchOpenFulltext(id, itemId);
      await loadLibrary(id);
      if (result.downloadUrl) {
        triggerDownload(result.downloadUrl);
        setNotice('Full text downloaded and browser download started.');
      } else if (result.warning) {
        setNotice(result.warning);
      } else {
        setNotice('Saved the source link for this full text.');
      }
      setError(null);
      await driver.complete(
        result.chunkCount && result.chunkCount > 0
          ? `Full text indexed with ${result.chunkCount} retrievable chunks`
          : 'Full-text fetch finished'
      );
    } catch (err: any) {
      driver.fail();
      setError(err.message || 'Full-text fetch failed');
      setNotice(null);
    } finally {
      setLoadingKey(null);
    }
  };

  const handleFetchBatch = async () => {
    if (!id) return;
    const itemIds = selectedLibraryIds.length ? selectedLibraryIds : activeLibraryItem ? [activeLibraryItem.id] : [];
    if (!itemIds.length) return;
    const driver = createOperationProgressDriver('batch-fetch', setOperationProgress);

    try {
      setLoadingKey('batch-fetch');
      const result = await api.literature.fetchOpenFulltextBatch(id, itemIds);
      await loadLibrary(id);
      if (result.downloadUrl) {
        triggerDownload(result.downloadUrl);
      }
      setNotice(summarizeBatchResult(result));
      setError(null);
      await driver.complete(
        result.downloadedCount > 0
          ? `Downloaded ${result.downloadedCount} full texts and refreshed the library`
          : 'Batch full-text fetch finished'
      );
    } catch (err: any) {
      driver.fail();
      setError(err.message || 'Batch full-text fetch failed');
      setNotice(null);
    } finally {
      setLoadingKey(null);
    }
  };

  const handleIndexText = async () => {
    if (!id || !activeLibraryItem || !indexText.trim()) return;
    const driver = createOperationProgressDriver('index', setOperationProgress);
    try {
      setLoadingKey('index');
      await api.literature.indexText(id, activeLibraryItem.id, indexText.trim());
      await loadLibrary(id);
      setIndexText('');
      setNotice('Indexed full-text content into the project library for rewriting and evidence retrieval.');
      setError(null);
      await driver.complete('Full-text content indexed locally');
    } catch (err: any) {
      driver.fail();
      setError(err.message || 'Full-text indexing failed');
      setNotice(null);
    } finally {
      setLoadingKey(null);
    }
  };

  const handleEvidenceSearch = async () => {
    if (!id || !evidenceQuery.trim()) return;
    const driver = createOperationProgressDriver('evidence', setOperationProgress);
    try {
      setLoadingKey('evidence');
      const result = await api.literature.ragSearch(id, evidenceQuery.trim(), 6);
      setEvidenceResult(result);
      setError(null);
      await driver.complete(`Matched ${result.evidence.length} evidence snippets`);
    } catch (err: any) {
      driver.fail();
      setError(err.message || 'Evidence search failed');
    } finally {
      setLoadingKey(null);
    }
  };

  const handleFormatCitations = async () => {
    if (!id) return;
    const targetIds =
      selectedLibraryIds.length > 0
        ? selectedLibraryIds
        : activeLibraryItem
          ? [activeLibraryItem.id]
          : [];
    if (!targetIds.length) return;
    const driver = createOperationProgressDriver('citation', setOperationProgress);

    try {
      setLoadingKey('citation');
      const result = await api.agent.formatCitations({
        projectId: id,
        style: 'gb7714',
        itemIds: targetIds,
        text: citationText.trim() || undefined,
        matchedOnly: false,
      });
      setCitationResult(result);
      setError(null);
      await driver.complete(`Generated ${result.entries.length} bibliography entries`);
    } catch (err: any) {
      driver.fail();
      setError(err.message || 'Citation formatting failed');
    } finally {
      setLoadingKey(null);
    }
  };

  return (
    <div className="literature-page animate-fade-in">
      <header className="page-header literature-header">
        <div>
          <h1 className="page-title">项目文献库</h1>
          <p className="page-description">检索文献、导入项目、尽量补全文，并把可用全文留在系统里供改写使用。</p>
        </div>
        <div className="literature-header-actions">
          <Button variant="ghost" onClick={() => navigate(`/editor/${id}`)}>
            <ArrowLeft size={16} className="mr-2" />
            返回诊断
          </Button>
          <Button variant="outline" onClick={() => navigate(`/editor/${id}`)}>
            <BookOpen size={16} className="mr-2" />
            打开编辑器
          </Button>
        </div>
      </header>

      <div className="page-body literature-body">
        <Card className="literature-toolbar">
          <div className="literature-toolbar__top">
            <div className="literature-searchbox">
              <Search size={18} className="literature-searchbox__icon" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="按关键词、研究问题、DOI 或论文标题检索"
              />
            </div>
            <Button onClick={handleSearch} isLoading={loadingKey === 'search'}>
              开始检索
            </Button>
          </div>

          <div className="literature-toolbar__bottom">
            <div className="source-pills">
              {DEFAULT_SOURCES.map((source) => (
                <button
                  key={source}
                  type="button"
                  className={`source-pill ${sources.includes(source) ? 'active' : ''}`}
                  onClick={() => toggleSource(source)}
                >
                  {source}
                </button>
              ))}
            </div>
            <div className="toolbar-note">导入后会自动尝试获取开放全文，能下载就直接入库并保留下载文件。</div>
          </div>
        </Card>

        {error ? (
          <div className="error-banner">
            <AlertCircle size={18} />
            <span>{error}</span>
          </div>
        ) : null}

        {notice ? (
          <div className="notice-banner">
            <CheckCircle2 size={18} />
            <span>{notice}</span>
          </div>
        ) : null}

        {operationProgress ? (
          <Card className="operation-progress-card">
            <div className="operation-progress-card__header">
              <strong>{operationProgress.title}</strong>
              <span>{operationProgress.percent}%</span>
            </div>
            <div className="operation-progress-card__detail">{operationProgress.detail}</div>
            <div className="operation-progress-card__track">
              <div
                className="operation-progress-card__fill"
                style={{ width: `${operationProgress.percent}%` }}
              />
            </div>
          </Card>
        ) : null}

        <section className="literature-metrics">
          <div className="metric-card">
            <span>已入库文献</span>
            <strong>{metrics.total}</strong>
          </div>
          <div className="metric-card">
            <span>全文已下载</span>
            <strong>{metrics.downloaded}</strong>
          </div>
          <div className="metric-card">
            <span>已记录全文链接</span>
            <strong>{metrics.linked}</strong>
          </div>
          <div className="metric-card">
            <span>开放获取候选</span>
            <strong>{metrics.openAccess}</strong>
          </div>
        </section>

        <section className="literature-switcher">
          <button
            type="button"
            className={`switch-chip ${activeView === 'library' ? 'active' : ''}`}
            onClick={() => setActiveView('library')}
          >
            项目文献库
            <span>{library.length}</span>
          </button>
          <button
            type="button"
            className={`switch-chip ${activeView === 'search' ? 'active' : ''}`}
            onClick={() => setActiveView('search')}
          >
            候选结果
            <span>{searchResult?.candidates.length || 0}</span>
          </button>
        </section>

        <section className="literature-stage">
          <Card className="literature-list-pane">
            <div className="literature-list-pane__header">
              <div>
                <h2>{activeView === 'library' ? '已导入文献' : '待确认结果'}</h2>
                <p>
                  {activeView === 'library'
                    ? '先选择条目，再批量获取能拿到的全文。'
                    : '先筛掉不相关结果，再导入项目文献库。'}
                </p>
              </div>
              <div className="literature-list-pane__actions">
                <Button variant="outline" size="sm" onClick={selectAll}>
                  全部选择
                </Button>
                <Button variant="outline" size="sm" onClick={clearSelection}>
                  全部取消
                </Button>
                {activeView === 'library' ? (
                  <Button
                    size="sm"
                    onClick={handleFetchBatch}
                    disabled={library.length === 0}
                    isLoading={loadingKey === 'batch-fetch'}
                  >
                    <Download size={14} className="mr-2" />
                    获取可获取全文
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    onClick={() => void handleImport()}
                    disabled={!searchResult || selectedSearchIds.length === 0}
                    isLoading={loadingKey === 'import'}
                  >
                    <Upload size={14} className="mr-2" />
                    导入到项目文献库
                  </Button>
                )}
              </div>
            </div>

            {loadingKey === 'bootstrap' ? (
              <div className="empty-box">正在加载文献库...</div>
            ) : activeView === 'library' ? (
              library.length === 0 ? (
                <div className="empty-box">文献库还是空的。先检索并导入几篇相关论文。</div>
              ) : (
                <div className="literature-list">
                  {library.map((item, index) => {
                    const attachment = getItemAttachment(item.id, attachments);
                    return (
                      <div
                        key={item.id}
                        className={`literature-row animate-slide-up ${activeLibraryId === item.id ? 'active' : ''}`}
                        style={{ animationDelay: `${index * 0.03}s`, opacity: 0 }}
                        onClick={() => setActiveLibraryId(item.id)}
                      >
                        <div className="literature-row__check">
                          <input
                            type="checkbox"
                            checked={selectedLibraryIds.includes(item.id)}
                            onChange={(event) => {
                              event.stopPropagation();
                              toggleSelected(item.id, selectedLibraryIds, setSelectedLibraryIds);
                            }}
                            onClick={(event) => event.stopPropagation()}
                          />
                        </div>
                        <div className="literature-row__body">
                          <div className="literature-row__title">{item.title}</div>
                          <div className="literature-row__meta">
                            <span>{formatAuthors(item.authors)}</span>
                            <span>{item.year || '年份待确认'}</span>
                            <span>{item.venue || '来源待确认'}</span>
                          </div>
                          <div className="literature-row__tags">
                            <span className="tag-chip">{getItemAccessLabel(item, attachment)}</span>
                            {item.doi ? <span className="tag-chip">DOI</span> : null}
                            {attachment?.downloadUrl ? <span className="tag-chip tag-chip--success">可下载</span> : null}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )
            ) : !searchResult ? (
              <div className="empty-box">输入检索词后，这里会显示候选结果。</div>
            ) : (
              <div className="literature-list">
                {searchResult.candidates.map((item, index) => (
                  <div
                    key={item.id}
                    className={`literature-row animate-slide-up ${activeCandidateId === item.id ? 'active' : ''}`}
                    style={{ animationDelay: `${index * 0.03}s`, opacity: 0 }}
                    onClick={() => setActiveCandidateId(item.id)}
                  >
                    <div className="literature-row__check">
                      <input
                        type="checkbox"
                        checked={selectedSearchIds.includes(item.id)}
                        onChange={(event) => {
                          event.stopPropagation();
                          toggleSelected(item.id, selectedSearchIds, setSelectedSearchIds);
                        }}
                        onClick={(event) => event.stopPropagation()}
                      />
                    </div>
                    <div className="literature-row__body">
                      <div className="literature-row__title">{item.title}</div>
                      <div className="literature-row__meta">
                        <span>{formatAuthors(item.authors)}</span>
                        <span>{item.year || '年份待确认'}</span>
                        <span>{item.venue || '来源待确认'}</span>
                      </div>
                      <div className="literature-row__tags">
                        <span className="tag-chip">{item.openAccessStatus === 'open' ? '可获取全文' : '仅元数据'}</span>
                        {(item.sources || [item.source]).filter(Boolean).map((source) => (
                          <span className="tag-chip" key={`${item.id}-${source}`}>
                            {source}
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <aside className="literature-detail-pane">
            <Card className="literature-detail-card">
              {!detailItem ? (
                <div className="detail-empty">
                  <h3>选择一条文献</h3>
                  <p>右侧会显示来源、摘要、全文操作和参考文献工具。</p>
                </div>
              ) : (
                <>
                  <div className="literature-detail-card__header">
                    <div className="detail-eyebrow">{activeView === 'library' ? '项目文献' : '候选文献'}</div>
                    <h3>{detailItem.title}</h3>
                    <div className="detail-meta">
                      <span>{formatAuthors(detailItem.authors)}</span>
                      <span>{detailItem.year || '年份待确认'}</span>
                      <span>{detailItem.venue || '来源待确认'}</span>
                    </div>
                    <div className="detail-tags">
                      <span className="tag-chip">{getItemAccessLabel(detailItem, detailAttachment)}</span>
                      {detailItem.doi ? <span className="tag-chip">{detailItem.doi}</span> : null}
                    </div>
                  </div>

                  <div className="detail-actions">
                    {detailItem.url ? (
                      <a className="detail-link" href={toAbsoluteUrl(detailItem.url)} target="_blank" rel="noreferrer">
                        <ExternalLink size={14} />
                        查看来源
                      </a>
                    ) : null}

                    {activeView === 'search' ? (
                      <Button onClick={() => void handleImport([detailItem.id])} isLoading={loadingKey === 'import'}>
                        <Upload size={14} className="mr-2" />
                        导入这篇
                      </Button>
                    ) : (
                      <div className="detail-action-group">
                        {detailAttachment?.downloadUrl ? (
                          <Button variant="outline" onClick={() => triggerDownload(detailAttachment.downloadUrl)}>
                            <Download size={14} className="mr-2" />
                            下载全文
                          </Button>
                        ) : (
                          <Button
                            variant="outline"
                            onClick={() => void handleFetchSingle(detailItem.id)}
                            isLoading={loadingKey === `fetch:${detailItem.id}`}
                            disabled={!detailItem.pdfUrl}
                          >
                            <Download size={14} className="mr-2" />
                            获取全文
                          </Button>
                        )}
                      </div>
                    )}
                  </div>

                  {detailItem.abstract ? (
                    <section className="detail-section detail-section--static">
                      <div className="detail-section__header detail-section__header--static">
                        <h4>摘要</h4>
                      </div>
                      <p className="detail-abstract">{detailItem.abstract}</p>
                    </section>
                  ) : null}

                  {activeView === 'library' ? (
                    <>
                      <section className="detail-section">
                        <button type="button" className="detail-section__header" onClick={() => toggleSection('fulltext')}>
                          <h4>补充全文</h4>
                          {openSections.fulltext ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                        </button>
                        {openSections.fulltext ? (
                          <div className="detail-section__body">
                            <p>如果自动获取不到全文，可以把 PDF 提取文本或关键段落粘贴进来。</p>
                            <textarea
                              className="detail-textarea"
                              value={indexText}
                              onChange={(event) => setIndexText(event.target.value)}
                              placeholder="粘贴全文、摘要或方法与结果的关键段落。"
                            />
                            <Button onClick={handleIndexText} isLoading={loadingKey === 'index'}>
                              <Library size={16} className="mr-2" />
                              保存到文献库
                            </Button>
                          </div>
                        ) : null}
                      </section>

                      <section className="detail-section">
                        <button type="button" className="detail-section__header" onClick={() => toggleSection('citation')}>
                          <h4>参考文献输出</h4>
                          {openSections.citation ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                        </button>
                        {openSections.citation ? (
                          <div className="detail-section__body">
                            <p>按当前选中文献输出 GB/T 7714 条目，可选粘贴正文片段辅助格式化。</p>
                            <textarea
                              className="detail-textarea detail-textarea--compact"
                              value={citationText}
                              onChange={(event) => setCitationText(event.target.value)}
                              placeholder="可选：粘贴正文片段，帮助匹配更接近实际引用。"
                            />
                            <Button onClick={handleFormatCitations} isLoading={loadingKey === 'citation'}>
                              <Quote size={16} className="mr-2" />
                              生成参考文献
                            </Button>
                            {citationResult ? (
                              <pre className="detail-output">
                                {citationResult.bibliographyText || '当前没有可生成的条目。'}
                              </pre>
                            ) : null}
                          </div>
                        ) : null}
                      </section>

                      <section className="detail-section">
                        <button type="button" className="detail-section__header" onClick={() => toggleSection('evidence')}>
                          <h4>证据检索</h4>
                          {openSections.evidence ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                        </button>
                        {openSections.evidence ? (
                          <div className="detail-section__body">
                            <p>从已入库全文里找支持某个论点或方法描述的片段。</p>
                            <textarea
                              className="detail-textarea detail-textarea--compact"
                              value={evidenceQuery}
                              onChange={(event) => setEvidenceQuery(event.target.value)}
                              placeholder="输入想验证的论点、方法或实验描述。"
                            />
                            <Button onClick={handleEvidenceSearch} isLoading={loadingKey === 'evidence'}>
                              <FileSearch size={16} className="mr-2" />
                              查找证据
                            </Button>
                            {evidenceResult?.evidence?.length ? (
                              <div className="evidence-list">
                                {evidenceResult.evidence.map((item, index) => (
                                  <div key={`${item.label}-${index}`} className="evidence-card">
                                    <div className="evidence-card__top">
                                      <strong>{item.label}</strong>
                                      <span>{item.score.toFixed(2)}</span>
                                    </div>
                                    <p>{item.excerpt}</p>
                                  </div>
                                ))}
                              </div>
                            ) : null}
                          </div>
                        ) : null}
                      </section>
                    </>
                  ) : null}
                </>
              )}
            </Card>
          </aside>
        </section>
      </div>
    </div>
  );
};

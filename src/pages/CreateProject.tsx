import React, { useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../store';
import { api } from '../api/client';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { AlertTriangle, CheckCircle2, FileText, FileUp, Loader2, UploadCloud, X } from 'lucide-react';
import './CreateProject.css';

const SUPPORTED_EXTENSIONS = ['.pdf', '.docx', '.txt'];
const IS_DEPLOYED_SERVERLESS = Boolean(import.meta.env.PROD && !import.meta.env.VITE_API_BASE_URL);
const SAFE_SERVERLESS_UPLOAD_BYTES = 4 * 1024 * 1024;

type UploadOutcome = {
  projectId: string;
  fileName: string;
  parseStatus: string;
  parseError?: string | null;
  sectionCount: number;
};

function getExtension(fileName: string): string {
  const lower = fileName.toLowerCase();
  const dotIndex = lower.lastIndexOf('.');
  return dotIndex >= 0 ? lower.slice(dotIndex) : '';
}

function isSupportedFile(file: File): boolean {
  return SUPPORTED_EXTENSIONS.includes(getExtension(file.name));
}

function defaultTitleFromFile(file: File): string {
  return file.name.replace(/\.[^.]+$/, '').trim();
}

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function uploadMessageFromStatus(parseStatus: string, parseError?: string | null): string {
  if (parseStatus === 'parsed') return '文件解析完成，章节已生成。';
  if (parseStatus === 'fallback') return parseError ? `文件解析不完整，已改用补充正文继续：${parseError}` : '文件解析不完整，已改用补充正文继续。';
  if (parseStatus === 'failed') return parseError || '未提取到可用文本，请补充正文后重试。';
  return '文件已上传。';
}

export const CreateProject: React.FC = () => {
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const { createProject, loading, error, setError } = useAppStore();

  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [fallbackText, setFallbackText] = useState('');
  const [activeTab, setActiveTab] = useState<'paste' | 'upload'>('paste');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadStage, setUploadStage] = useState('');
  const [uploadProgress, setUploadProgress] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [pendingProjectId, setPendingProjectId] = useState<string | null>(null);
  const [uploadOutcome, setUploadOutcome] = useState<UploadOutcome | null>(null);

  const submitLoading = loading.create || uploading;
  const activeError = error;

  const uploadSummary = useMemo(() => {
    if (!selectedFile) return null;
    return `${selectedFile.name} · ${formatFileSize(selectedFile.size)}`;
  }, [selectedFile]);

  const resetUploadState = () => {
    setSelectedFile(null);
    setUploadStage('');
    setUploadProgress(0);
    setUploadOutcome(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  const selectFile = (file: File | null) => {
    if (!file) return;
    if (!isSupportedFile(file)) {
      setError('仅支持上传 PDF、DOCX 或 TXT 文件。');
      return;
    }
    setError(null);
    setUploadOutcome(null);
    setUploadProgress(0);
    setSelectedFile(file);
    setTitle((current) => current.trim() || defaultTitleFromFile(file));
  };

  const handleFileInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    selectFile(file);
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0] ?? null;
    selectFile(file);
  };

  const performUpload = async (projectId: string, file: File) => {
    if (IS_DEPLOYED_SERVERLESS && file.size > SAFE_SERVERLESS_UPLOAD_BYTES) {
      throw new Error('当前线上上传建议控制在 4MB 内。更大的 PDF / DOCX 请先压缩、转 TXT，或在本地版本中上传。');
    }

    setUploadStage(`正在上传并解析 ${file.name}…`);
    setUploadProgress(6);
    return api.projects.uploadFiles(projectId, file, fallbackText.trim() || undefined, {
      timeoutMs: 180000,
      onProgress: (percent, phase) => {
        setUploadProgress((current) => Math.max(current, percent));
        if (phase === 'uploading') {
          return;
        }
        if (phase === 'processing') {
          setUploadStage(`æ–‡ä»¶å·²ä¸Šä¼ ï¼Œæ­£åœ¨è§£æž ${file.name}`);
          return;
        }
        setUploadStage('æ–‡ä»¶è§£æžå®Œæˆï¼Œæ­£åœ¨è¿›å…¥è¯Šæ–­é¡µã€‚');
      },
    });
  };

  const handleUploadFlow = async (trimmedTitle: string) => {
    if (!selectedFile) {
      setError('请先选择一个 PDF、DOCX 或 TXT 文件。');
      return;
    }

    try {
      setError(null);
      setUploading(true);
      setUploadProgress(4);
      setUploadOutcome(null);

      let projectId = pendingProjectId;
      if (!projectId) {
        setUploadStage('正在创建项目…');
        const bundle = await createProject({
          title: trimmedTitle,
          type: 'thesis',
          language: 'zh',
          sourceType: 'file',
          text: '',
        });
        projectId = bundle.project.id;
        setPendingProjectId(projectId);
      }

      const uploaded = await performUpload(projectId, selectedFile);
      const fileMeta = uploaded.uploadFile;
      const outcome: UploadOutcome = {
        projectId: uploaded.project.id,
        fileName: fileMeta?.fileName || selectedFile.name,
        parseStatus: fileMeta?.parseStatus || 'unknown',
        parseError: fileMeta?.parseError || null,
        sectionCount: uploaded.sections.length,
      };
      setUploadOutcome(outcome);

      if (outcome.parseStatus === 'failed' || uploaded.sections.length === 0) {
        setPendingProjectId(uploaded.project.id);
        setError(uploadMessageFromStatus(outcome.parseStatus, outcome.parseError));
        setUploadStage('文件已上传，但暂未生成可诊断章节。请补充正文后重新上传。');
        return;
      }

      setPendingProjectId(null);
      setUploadProgress(100);
      navigate(`/diagnose/${uploaded.project.id}`, {
        state: {
          uploadResult: outcome,
        },
      });
    } catch (submitError) {
      console.error(submitError);
      const message = submitError instanceof Error ? submitError.message : '上传或解析失败，请稍后重试。';
      setError(message);
      setUploadStage('');
      setUploadProgress(0);
    } finally {
      setUploading(false);
      if (!pendingProjectId) {
        setUploadStage('');
      }
    }
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    const trimmedTitle = title.trim();
    if (!trimmedTitle) {
      setError('请输入项目标题。');
      return;
    }

    if (activeTab === 'paste') {
      if (!text.trim()) {
        setError('请粘贴要诊断或改写的正文。');
        return;
      }
      try {
        const bundle = await createProject({
          title: trimmedTitle,
          type: 'thesis',
          language: 'zh',
          sourceType: 'text',
          text,
        });
        navigate(`/diagnose/${bundle.project.id}`);
      } catch (submitError) {
        console.error(submitError);
      }
      return;
    }

    await handleUploadFlow(trimmedTitle);
  };

  return (
    <div className="animate-fade-in create-project-page">
      <header className="page-header">
        <h1 className="page-title">新建改稿项目</h1>
        <p className="page-description">支持直接粘贴正文，或上传 PDF / DOCX / TXT 草稿后自动解析。</p>
      </header>

      <div className="page-body">
        <Card className="create-form-card">
          <form onSubmit={handleSubmit}>
            <div className="form-group">
              <label htmlFor="project-title">项目标题</label>
              <input
                id="project-title"
                type="text"
                placeholder="例如：第四章结果分析修订"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                className="form-input"
                autoFocus
              />
            </div>

            <div className="tabs">
              <button
                type="button"
                className={`tab ${activeTab === 'paste' ? 'active' : ''}`}
                onClick={() => {
                  setActiveTab('paste');
                  setError(null);
                  setUploadOutcome(null);
                }}
              >
                <FileText size={18} />
                直接粘贴文本
              </button>
              <button
                type="button"
                className={`tab ${activeTab === 'upload' ? 'active' : ''}`}
                onClick={() => {
                  setActiveTab('upload');
                  setError(null);
                }}
              >
                <UploadCloud size={18} />
                上传文件
              </button>
            </div>

            <div className="tab-content">
              {activeTab === 'paste' ? (
                <div className="form-group">
                  <textarea
                    placeholder="把需要诊断或改写的正文直接粘贴到这里。"
                    value={text}
                    onChange={(event) => setText(event.target.value)}
                    className="form-textarea"
                  />
                </div>
              ) : (
                <div className="upload-panel">
                  <div
                    className={`upload-zone ${dragging ? 'dragging' : ''} ${selectedFile ? 'has-file' : ''}`}
                    onDragEnter={(event) => {
                      event.preventDefault();
                      setDragging(true);
                    }}
                    onDragOver={(event) => {
                      event.preventDefault();
                      setDragging(true);
                    }}
                    onDragLeave={(event) => {
                      event.preventDefault();
                      if (event.currentTarget === event.target) {
                        setDragging(false);
                      }
                    }}
                    onDrop={handleDrop}
                    onClick={() => fileInputRef.current?.click()}
                  >
                    <div className="upload-icon-container">
                      {uploading ? <Loader2 size={40} className="upload-icon spinning" /> : <UploadCloud size={40} className="upload-icon" />}
                    </div>
                    <h3>{selectedFile ? '文件已就绪，可直接创建并解析' : '点击或拖拽文件到这里'}</h3>
                    <p>支持 PDF、DOCX、TXT。上传后由后端解析正文并生成章节。</p>
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
                      className="file-input"
                      onChange={handleFileInputChange}
                    />
                  </div>

                  {selectedFile ? (
                    <div className="selected-file-card">
                      <div className="selected-file-meta">
                        <div className="selected-file-name">
                          <FileUp size={18} />
                          <span>{selectedFile.name}</span>
                        </div>
                        <span className="selected-file-size">{formatFileSize(selectedFile.size)}</span>
                      </div>
                      <div className="selected-file-actions">
                        <span className="selected-file-hint">
                          {pendingProjectId ? '当前会继续上传到已创建项目，不会重复创建。' : '创建项目后会立即上传并解析。'}
                        </span>
                        <button type="button" className="selected-file-clear" onClick={resetUploadState}>
                          <X size={16} />
                          移除
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="upload-help-list">
                      <div className="upload-help-item">PDF：适合已有排版稿，解析质量取决于原文件文本层。</div>
                      <div className="upload-help-item">DOCX：适合可编辑论文稿，通常能拿到更完整正文。</div>
                      <div className="upload-help-item">TXT：适合已清洗的纯文本稿。</div>
                      {IS_DEPLOYED_SERVERLESS ? <div className="upload-help-item">线上环境建议单个文件控制在 4MB 内，过大的稿件请先压缩或转 TXT。</div> : null}
                    </div>
                  )}

                  <div className="form-group form-group-compact">
                    <label htmlFor="fallback-text">解析失败时的补充正文（可选）</label>
                    <textarea
                      id="fallback-text"
                      placeholder="如果 PDF / DOCX 解析失败，系统会优先使用这里的文本继续创建章节。"
                      value={fallbackText}
                      onChange={(event) => setFallbackText(event.target.value)}
                      className="form-textarea form-textarea-compact"
                    />
                  </div>

                  {uploadSummary ? <div className="upload-status-text">{uploadSummary}</div> : null}
                  {uploadStage ? <div className="upload-stage">{uploadStage}</div> : null}
                  {(uploading || uploadProgress > 0) ? (
                    <div className="upload-progress-block" aria-live="polite">
                      <div className="upload-progress-track">
                        <div className="upload-progress-fill" style={{ width: `${uploadProgress}%` }} />
                      </div>
                      <div className="upload-progress-meta">
                        <span>{uploading ? '上传与解析进度' : '上传状态'}</span>
                        <span>{uploadProgress}%</span>
                      </div>
                    </div>
                  ) : null}

                  {uploadOutcome ? (
                    <div className={`upload-outcome-card ${uploadOutcome.parseStatus === 'failed' ? 'error' : 'success'}`}>
                      <div className="upload-outcome-header">
                        {uploadOutcome.parseStatus === 'failed' ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}
                        <span>{uploadMessageFromStatus(uploadOutcome.parseStatus, uploadOutcome.parseError)}</span>
                      </div>
                      <div className="upload-outcome-meta">
                        <span>文件：{uploadOutcome.fileName}</span>
                        <span>章节数：{uploadOutcome.sectionCount}</span>
                      </div>
                    </div>
                  ) : null}
                </div>
              )}
            </div>

            {activeError ? <div className="form-error">{activeError}</div> : null}

            <div className="form-actions">
              <Button type="button" variant="ghost" onClick={() => navigate('/')}>
                取消
              </Button>
              <Button type="submit" isLoading={submitLoading}>
                {activeTab === 'paste'
                  ? '创建并诊断项目'
                  : pendingProjectId
                    ? '继续上传并重试解析'
                    : '创建、上传并解析'}
              </Button>
            </div>
          </form>
        </Card>
      </div>
    </div>
  );
};

import React, { useEffect } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { AlertTriangle, CheckCircle, ChevronRight, FileText } from 'lucide-react';
import { useAppStore } from '../store';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import './Diagnostics.css';

type UploadResultState = {
  fileName: string;
  parseStatus: string;
  parseError?: string | null;
  sectionCount: number;
};

export const Diagnostics: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { bundle, loadProject, loading, error } = useAppStore();
  const uploadResult = (location.state as { uploadResult?: UploadResultState } | null)?.uploadResult;

  useEffect(() => {
    if (id && (!bundle || bundle.project.id !== id)) {
      loadProject(id);
    }
  }, [id, bundle, loadProject]);

  if (loading.bundle) {
    return (
      <div className="loading-state">
        <div className="spinner"></div>
        <p>正在加载诊断结果…</p>
      </div>
    );
  }

  if (error || !bundle) {
    return <div className="error-banner">加载失败：{error}</div>;
  }

  const { project, sections, issues, comments } = bundle;

  return (
    <div className="animate-fade-in diagnostics-page">
      <header className="page-header flex-between">
        <div>
          <h1 className="page-title">{project.title} - 诊断报告</h1>
          <p className="page-description">系统已完成章节拆分与诊断，你可以从问题或章节入口继续改稿。</p>
        </div>
        <Button onClick={() => navigate(`/editor/${project.id}`)}>进入编辑器开始改稿</Button>
      </header>

      <div className="page-body">
        {uploadResult ? (
          <Card className={`upload-feedback-card ${uploadResult.parseStatus === 'fallback' ? 'warning' : 'success'}`}>
            <div className="upload-feedback-header">
              {uploadResult.parseStatus === 'fallback' ? <AlertTriangle size={18} /> : <CheckCircle size={18} />}
              <span>
                {uploadResult.parseStatus === 'fallback'
                  ? '文件已导入，但原文件解析不完整，系统已改用补充正文生成章节。'
                  : '文件上传并解析完成，当前项目已进入诊断阶段。'}
              </span>
            </div>
            <div className="upload-feedback-meta">
              <span>文件：{uploadResult.fileName}</span>
              <span>章节数：{uploadResult.sectionCount}</span>
              {uploadResult.parseError ? <span>解析备注：{uploadResult.parseError}</span> : null}
            </div>
          </Card>
        ) : null}

        <div className="diag-grid">
          <div className="diag-col">
            <Card className="summary-card">
              <h2 className="card-title">诊断概览</h2>
              <div className="stats-row">
                <div className="stat-box danger">
                  <span className="stat-num">{project.issueCount || issues?.length || 0}</span>
                  <span className="stat-label">待处理问题</span>
                </div>
                <div className="stat-box warning">
                  <span className="stat-num">{project.unresolvedCommentCount || comments?.length || 0}</span>
                  <span className="stat-label">导师意见</span>
                </div>
                <div className="stat-box success">
                  <span className="stat-num">{sections.length}</span>
                  <span className="stat-label">已解析章节</span>
                </div>
              </div>
            </Card>

            <Card className="sections-card">
              <h2 className="card-title">章节结构</h2>
              <div className="section-list">
                {sections.map((section) => (
                  <div key={section.id} className="section-item">
                    <div className="sec-info">
                      <FileText size={16} className="text-secondary" />
                      <span className="sec-title">{section.title}</span>
                    </div>
                    <div className="sec-meta">
                      {section.issueCount ? <span className="badge danger-badge">{section.issueCount} 问题</span> : null}
                      <Button variant="ghost" size="sm" onClick={() => navigate(`/editor/${project.id}?section=${section.id}`)}>
                        前往 <ChevronRight size={14} />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <div className="diag-col">
            <Card className="issues-card">
              <h2 className="card-title">具体诊断问题</h2>
              {!issues || issues.length === 0 ? (
                <div className="empty-issues">
                  <CheckCircle size={40} className="text-success mb-4" />
                  <p>目前没有发现明显结构问题，可以直接进入改稿。</p>
                </div>
              ) : (
                <div className="issue-list">
                  {issues.map((issue: any) => (
                    <div key={issue.id} className={`issue-item severity-${issue.severity}`}>
                      <div className="issue-header">
                        <AlertTriangle size={16} />
                        <span className="issue-category">{issue.category}</span>
                      </div>
                      <p className="issue-desc">{issue.description}</p>
                      <div className="issue-action">
                        <span className="issue-suggestion">建议：{issue.suggestion}</span>
                        <Button variant="outline" size="sm" onClick={() => navigate(`/editor/${project.id}?section=${issue.sectionId}`)}>
                          去修改
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
};

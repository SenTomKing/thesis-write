import React, { useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useAppStore } from '../store';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { FileText, AlertTriangle, CheckCircle, ChevronRight, MessageSquare } from 'lucide-react';
import './Diagnostics.css';

export const Diagnostics: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { bundle, loadProject, loading, error } = useAppStore();

  useEffect(() => {
    if (id && (!bundle || bundle.project.id !== id)) {
      loadProject(id);
    }
  }, [id, bundle, loadProject]);

  if (loading.bundle) {
    return <div className="loading-state"><div className="spinner"></div><p>正在加载诊断报告...</p></div>;
  }

  if (error || !bundle) {
    return <div className="error-banner">加载失败: {error}</div>;
  }

  const { project, sections, issues, comments } = bundle;

  return (
    <div className="animate-fade-in diagnostics-page">
      <header className="page-header flex-between">
        <div>
          <h1 className="page-title">{project.title} - 诊断报告</h1>
          <p className="page-description">我们分析了您的草稿并发现了以下建议修改点</p>
        </div>
        <Button onClick={() => navigate(`/editor/${project.id}`)}>
          进入编辑器开始改稿
        </Button>
      </header>

      <div className="page-body">
        <div className="diag-grid">
          {/* Left Column: Summary & Sections */}
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
                {sections.map(sec => (
                  <div key={sec.id} className="section-item">
                    <div className="sec-info">
                      <FileText size={16} className="text-secondary" />
                      <span className="sec-title">{sec.title}</span>
                    </div>
                    <div className="sec-meta">
                      {sec.issueCount ? <span className="badge danger-badge">{sec.issueCount} 问题</span> : null}
                      <Button variant="ghost" size="sm" onClick={() => navigate(`/editor/${project.id}?section=${sec.id}`)}>
                        前往 <ChevronRight size={14} />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          {/* Right Column: Detailed Issues */}
          <div className="diag-col">
            <Card className="issues-card">
              <h2 className="card-title">具体诊断问题</h2>
              {(!issues || issues.length === 0) ? (
                <div className="empty-issues">
                  <CheckCircle size={40} className="text-success mb-4" />
                  <p>太棒了，目前没有发现明显问题！</p>
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
                        <span className="issue-suggestion">建议: {issue.suggestion}</span>
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

import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertCircle, ArrowRight, Clock3, FileText, Library, Plus, RotateCcw, Trash2 } from 'lucide-react';
import { api } from '../api/client';
import { useAppStore } from '../store';
import { Project } from '../types';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import './Dashboard.css';

function formatStatus(status: string) {
  const labels: Record<string, string> = {
    draft: '待改稿',
    uploaded: '已上传',
    parsing: '解析中',
    diagnosed: '已诊断',
    revising: '改写中',
    'review-pending': '待确认',
    ready: '就绪',
  };
  return labels[status] || status;
}

function formatDate(value?: string) {
  if (!value) return '更新时间未知';
  try {
    return new Date(value).toLocaleDateString('zh-CN');
  } catch {
    return value;
  }
}

export const Dashboard: React.FC = () => {
  const navigate = useNavigate();
  const { loadProject } = useAppStore();

  const [projects, setProjects] = useState<Project[]>([]);
  const [trashCount, setTrashCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const loadLists = async () => {
    try {
      setLoading(true);
      const [active, trash] = await Promise.all([api.projects.list('active'), api.projects.list('trash')]);
      setProjects(active);
      setTrashCount(trash.length);
      setError(null);
    } catch (err: any) {
      setError(err.message || '项目列表加载失败。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadLists();
  }, []);

  const handleOpenProject = async (projectId: string) => {
    await loadProject(projectId);
    navigate(`/diagnose/${projectId}`);
  };

  const handleDelete = async (projectId: string) => {
    try {
      setBusyKey(`delete:${projectId}`);
      await api.projects.delete(projectId, false);
      await loadLists();
      setError(null);
    } catch (err: any) {
      setError(err.message || '项目删除失败。');
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div className="dashboard-page animate-fade-in">
      <header className="page-header dashboard-header">
        <div>
          <h1 className="page-title">工作台</h1>
          <p className="page-description">查看项目、进入改稿、管理文献库和回收站。</p>
        </div>
        <div className="dashboard-header-actions">
          <Button variant="outline" onClick={() => navigate('/trash')}>
            <Trash2 size={16} className="mr-2" />
            回收站
            {trashCount > 0 ? <span className="dashboard-header-count">{trashCount}</span> : null}
          </Button>
          <Button variant="outline" onClick={() => void loadLists()} isLoading={loading}>
            <RotateCcw size={16} className="mr-2" />
            刷新
          </Button>
          <Button onClick={() => navigate('/create')}>
            <Plus size={16} className="mr-2" />
            新建项目
          </Button>
        </div>
      </header>

      <div className="page-body dashboard-body">
        {error ? (
          <div className="error-banner">
            <AlertCircle size={18} />
            <span>{error}</span>
          </div>
        ) : null}

        <section className="projects-stage">
          <div className="projects-stage__header">
            <div>
              <h2>项目列表</h2>
              <p>{projects.length} 个项目，可继续诊断、改稿或管理文献。</p>
            </div>
          </div>

          {loading ? (
            <div className="dashboard-loading">正在加载项目...</div>
          ) : projects.length === 0 ? (
            <div className="dashboard-empty-state">
              <div className="dashboard-empty-state__icon">
                <FileText size={40} />
              </div>
              <h3>还没有项目</h3>
              <p>先新建项目，或者导入草稿文件后再进入诊断与改稿流程。</p>
              <Button onClick={() => navigate('/create')}>新建项目</Button>
            </div>
          ) : (
            <div className="projects-grid">
              {projects.map((project, index) => (
                <Card
                  key={project.id}
                  hoverable
                  className="project-card animate-slide-up"
                  style={{ animationDelay: `${index * 0.05}s`, opacity: 0 }}
                  onClick={() => void handleOpenProject(project.id)}
                >
                  <div className="project-card__top">
                    <div className="project-card__icon">
                      <FileText size={20} />
                    </div>
                    <div className="project-card__top-actions">
                      <span className="project-card__status">{formatStatus(project.status)}</span>
                      <button
                        type="button"
                        className="project-card__delete"
                        onClick={(event) => {
                          event.stopPropagation();
                          void handleDelete(project.id);
                        }}
                        aria-label="删除项目"
                        disabled={busyKey === `delete:${project.id}`}
                      >
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </div>

                  <h3 className="project-card__title">{project.title || '未命名项目'}</h3>

                  <div className="project-card__meta">
                    <div className="project-card__meta-row">
                      <AlertCircle size={14} />
                      <span>{project.issueCount || 0} 个待处理问题</span>
                    </div>
                    <div className="project-card__meta-row">
                      <Clock3 size={14} />
                      <span>{formatDate(project.updatedAt)}</span>
                    </div>
                  </div>

                  <div className="project-card__actions">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={(event) => {
                        event.stopPropagation();
                        navigate(`/literature/${project.id}`);
                      }}
                    >
                      <Library size={14} className="mr-2" />
                      文献库
                    </Button>
                  </div>

                  <div className="project-card__footer">
                    <span>继续处理</span>
                    <ArrowRight size={16} />
                  </div>
                </Card>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
};

import React, { useEffect, useState } from 'react';
import { AlertCircle, RotateCcw, Trash2 } from 'lucide-react';
import { api } from '../api/client';
import { Project } from '../types';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import './Trash.css';

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

function formatDate(value?: string | null) {
  if (!value) return '时间未知';
  try {
    return new Date(value).toLocaleDateString('zh-CN');
  } catch {
    return value;
  }
}

export const Trash: React.FC = () => {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const loadTrash = async () => {
    try {
      setLoading(true);
      const trash = await api.projects.list('trash');
      setProjects(trash);
      setError(null);
    } catch (err: any) {
      setError(err.message || '回收站加载失败。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadTrash();
  }, []);

  const handleRestore = async (projectId: string) => {
    try {
      setBusyKey(`restore:${projectId}`);
      await api.projects.restore(projectId);
      await loadTrash();
    } catch (err: any) {
      setError(err.message || '项目恢复失败。');
    } finally {
      setBusyKey(null);
    }
  };

  const handleDelete = async (projectId: string) => {
    try {
      setBusyKey(`purge:${projectId}`);
      await api.projects.delete(projectId, true);
      await loadTrash();
    } catch (err: any) {
      setError(err.message || '项目彻底删除失败。');
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div className="trash-page animate-fade-in">
      <header className="page-header trash-header">
        <div>
          <h1 className="page-title">回收站</h1>
          <p className="page-description">删除后的项目先进入回收站，可恢复或彻底删除。</p>
        </div>
        <Button variant="outline" onClick={() => void loadTrash()} isLoading={loading}>
          <RotateCcw size={16} className="mr-2" />
          刷新
        </Button>
      </header>

      <div className="page-body trash-body">
        {error ? (
          <div className="error-banner">
            <AlertCircle size={18} />
            <span>{error}</span>
          </div>
        ) : null}

        {loading ? (
          <div className="dashboard-loading">正在加载回收站...</div>
        ) : projects.length === 0 ? (
          <div className="dashboard-empty-state">
            <div className="dashboard-empty-state__icon">
              <Trash2 size={36} />
            </div>
            <h3>回收站为空</h3>
            <p>删除的项目会先出现在这里。</p>
          </div>
        ) : (
          <div className="trash-grid">
            {projects.map((project, index) => (
              <Card 
                key={project.id} 
                className="trash-card animate-slide-up"
                style={{ animationDelay: `${index * 0.05}s`, opacity: 0 }}
              >
                <div className="trash-card__top">
                  <div>
                    <h3 className="trash-card__title">{project.title || '未命名项目'}</h3>
                    <p className="trash-card__hint">删除后保留</p>
                  </div>
                  <span className="trash-card__status">{formatStatus(project.status)}</span>
                </div>

                <div className="trash-card__meta">
                  <span>删除时间</span>
                  <span>{formatDate(project.deletedAt || project.updatedAt)}</span>
                </div>

                <div className="trash-card__actions">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void handleRestore(project.id)}
                    isLoading={busyKey === `restore:${project.id}`}
                  >
                    恢复
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={() => void handleDelete(project.id)}
                    isLoading={busyKey === `purge:${project.id}`}
                  >
                    彻底删除
                  </Button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../store';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { UploadCloud, FileText } from 'lucide-react';
import './CreateProject.css';

export const CreateProject: React.FC = () => {
  const navigate = useNavigate();
  const { createProject, loading, error } = useAppStore();
  
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [activeTab, setActiveTab] = useState<'paste' | 'upload'>('paste');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title) return alert('请输入项目标题');
    if (activeTab === 'paste' && !text) return alert('请输入草稿内容');

    try {
      const bundle = await createProject({
        title,
        type: 'thesis',
        language: 'zh',
        sourceType: activeTab === 'paste' ? 'text' : 'file',
        text: activeTab === 'paste' ? text : undefined
      });
      navigate(`/diagnose/${bundle.project.id}`);
    } catch (err) {
      console.error(err);
    }
  };

  return (
    <div className="animate-fade-in create-project-page">
      <header className="page-header">
        <h1 className="page-title">新建改稿项目</h1>
        <p className="page-description">上传您的论文草稿以进行智能诊断与修改</p>
      </header>

      <div className="page-body">
        <Card className="create-form-card">
          <form onSubmit={handleSubmit}>
            <div className="form-group">
              <label>项目标题</label>
              <input 
                type="text" 
                placeholder="例如：第四章 研究结果与分析"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="form-input"
                autoFocus
              />
            </div>

            <div className="tabs">
              <button 
                type="button" 
                className={`tab ${activeTab === 'paste' ? 'active' : ''}`}
                onClick={() => setActiveTab('paste')}
              >
                <FileText size={18} /> 直接粘贴文本
              </button>
              <button 
                type="button" 
                className={`tab ${activeTab === 'upload' ? 'active' : ''}`}
                onClick={() => setActiveTab('upload')}
              >
                <UploadCloud size={18} /> 上传文档 (PDF/DOCX)
              </button>
            </div>

            <div className="tab-content">
              {activeTab === 'paste' ? (
                <div className="form-group">
                  <textarea 
                    placeholder="在此处粘贴您的草稿正文..."
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    className="form-textarea"
                  />
                </div>
              ) : (
                <div className="upload-zone">
                  <div className="upload-icon-container">
                    <UploadCloud size={40} className="upload-icon" />
                  </div>
                  <h3>点击或拖拽文件至此处</h3>
                  <p>支持 .pdf, .docx, .txt 格式 (当前仅展示UI)</p>
                  {/* Note: File upload logic requires api.projects.uploadFiles, kept simple here as requested per integration report minimally */}
                  <input type="file" className="file-input" disabled />
                </div>
              )}
            </div>

            {error && <div className="form-error">{error}</div>}

            <div className="form-actions">
              <Button type="button" variant="ghost" onClick={() => navigate('/')}>取消</Button>
              <Button type="submit" isLoading={loading.create}>
                创建并诊断项目
              </Button>
            </div>
          </form>
        </Card>
      </div>
    </div>
  );
};

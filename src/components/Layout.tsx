import React from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { FileText, FolderPlus, LayoutDashboard, LogOut, Trash2, UserRound } from 'lucide-react';
import { useAuth } from '../auth/AuthContext';
import './Layout.css';

function initialsForUser(username: string) {
  const trimmed = username.trim();
  return trimmed ? trimmed.slice(0, 1).toUpperCase() : 'U';
}

export const Layout: React.FC = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const { authStatus, user, logout } = useAuth();
  const isDemoMode = Boolean(authStatus?.demoMode);

  const navItems = [
    { path: '/', icon: <LayoutDashboard size={20} />, label: '工作台' },
    { path: '/create', icon: <FolderPlus size={20} />, label: '新建项目' },
    { path: '/trash', icon: <Trash2 size={20} />, label: '回收站' },
  ];

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="logo-box">
            <FileText size={24} className="logo-icon" />
          </div>
          <h1 className="logo-text">DraftRefine</h1>
        </div>

        <nav className="sidebar-nav">
          {navItems.map((item) => (
            <Link
              key={item.path}
              to={item.path}
              className={`nav-item ${location.pathname === item.path ? 'active' : ''}`}
            >
              {item.icon}
              <span>{item.label}</span>
            </Link>
          ))}
        </nav>

        <div className="sidebar-footer">
          {isDemoMode ? (
            <div className="demo-panel">
              <strong>公开演示版</strong>
              <span>可直接体验核心改稿流程</span>
            </div>
          ) : (
            <>
              <div className="account-panel">
                <div className="account-avatar">{initialsForUser(user?.username || '')}</div>
                <div className="account-copy">
                  <strong>{user?.username || '未登录'}</strong>
                  <span>{user?.email || ''}</span>
                </div>
              </div>
              <button
                className="nav-item ghost"
                type="button"
                onClick={async () => {
                  await logout();
                  navigate('/auth', { replace: true });
                }}
              >
                <LogOut size={20} />
                <span>退出登录</span>
              </button>
              <div className="account-note">
                <UserRound size={14} />
                <span>当前数据仅对本账号可见</span>
              </div>
            </>
          )}
        </div>
      </aside>

      <main className="main-content">
        <Outlet />
      </main>
    </div>
  );
};

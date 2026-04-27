import React from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { FileText, FolderPlus, LayoutDashboard, Settings, Trash2 } from 'lucide-react';
import './Layout.css';

export const Layout: React.FC = () => {
  const location = useLocation();

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
          <button className="nav-item ghost" type="button">
            <Settings size={20} />
            <span>设置</span>
          </button>
        </div>
      </aside>

      <main className="main-content">
        <Outlet />
      </main>
    </div>
  );
};

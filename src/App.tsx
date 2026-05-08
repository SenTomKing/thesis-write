import React from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { Layout } from './components/Layout';
import { AuthPage } from './pages/AuthPage';
import { Dashboard } from './pages/Dashboard';
import { CreateProject } from './pages/CreateProject';
import { Diagnostics } from './pages/Diagnostics';
import { Editor } from './pages/Editor';
import { Literature } from './pages/Literature';
import { Trash } from './pages/Trash';

const centeredShellStyle: React.CSSProperties = {
  minHeight: '100vh',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '24px',
  background:
    'radial-gradient(circle at top, rgba(124, 58, 237, 0.12), transparent 40%), linear-gradient(180deg, #faf8ff 0%, #f6f3ff 100%)',
};

const centeredCardStyle: React.CSSProperties = {
  width: 'min(420px, 100%)',
  padding: '28px 32px',
  borderRadius: '24px',
  background: 'rgba(255, 255, 255, 0.95)',
  boxShadow: '0 24px 60px rgba(15, 23, 42, 0.08)',
  border: '1px solid rgba(124, 58, 237, 0.12)',
  color: '#475569',
  fontSize: '0.95rem',
};

function LoadingScreen() {
  return (
    <div style={centeredShellStyle}>
      <div style={centeredCardStyle}>正在确认登录状态…</div>
    </div>
  );
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return <LoadingScreen />;
  }

  if (!user) {
    const from = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate to="/auth" replace state={{ from }} />;
  }

  return <>{children}</>;
}

function GuestOnly({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation() as ReturnType<typeof useLocation> & {
    state?: { from?: string };
  };

  if (loading) {
    return <LoadingScreen />;
  }

  if (user) {
    return <Navigate to={location.state?.from || '/'} replace />;
  }

  return <>{children}</>;
}

function AppRoutes() {
  return (
    <Routes>
      <Route
        path="/auth"
        element={
          <GuestOnly>
            <AuthPage />
          </GuestOnly>
        }
      />

      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Dashboard />} />
        <Route path="/create" element={<CreateProject />} />
        <Route path="/trash" element={<Trash />} />
        <Route path="/diagnose/:id" element={<Diagnostics />} />
        <Route path="/literature/:id" element={<Literature />} />
      </Route>

      <Route
        path="/editor/:id"
        element={
          <RequireAuth>
            <Editor />
          </RequireAuth>
        }
      />
    </Routes>
  );
}

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;

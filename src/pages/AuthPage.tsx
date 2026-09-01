import React, { useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { AlertCircle, KeyRound, LockKeyhole, Mail, UserRound } from 'lucide-react';
import { useAuth } from '../auth/AuthContext';
import './AuthPage.css';

type AuthTab = 'login' | 'register';

function titleForState(hasUsers: boolean) {
  return hasUsers ? '登录 DraftRefine' : '创建首个账号';
}

export const AuthPage: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation() as ReturnType<typeof useLocation> & {
    state?: { from?: string };
  };
  const { authStatus, login, register } = useAuth();

  const [activeTab, setActiveTab] = useState<AuthTab>(authStatus?.hasUsers ? 'login' : 'register');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loginForm, setLoginForm] = useState({ identifier: '', password: '' });
  const [registerForm, setRegisterForm] = useState({
    email: '',
    username: '',
    password: '',
    inviteCode: '',
  });

  const redirectTarget = location.state?.from || '/';
  const showInviteCode = authStatus?.registrationMode === 'invite-only';
  const pageTitle = titleForState(Boolean(authStatus?.hasUsers));
  const pageDescription = authStatus?.hasUsers
    ? '输入邮箱或用户名与密码后进入你的项目。'
    : '当前站点需要先创建首个账号，现有项目会自动归到这个账号名下。';

  const registerDisabled = useMemo(() => {
    return (
      !registerForm.email.trim() ||
      !registerForm.username.trim() ||
      registerForm.password.trim().length < 8 ||
      (showInviteCode && !registerForm.inviteCode.trim())
    );
  }, [registerForm, showInviteCode]);

  const handleLogin = async (event: React.FormEvent) => {
    event.preventDefault();
    if (loginForm.identifier.trim().length < 3) {
      setError('请输入至少 3 个字符的邮箱或用户名。');
      return;
    }
    if (loginForm.password.trim().length < 8) {
      setError('请输入至少 8 位密码。');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await login(loginForm);
      navigate(redirectTarget, { replace: true });
    } catch (err: any) {
      setError(err.message || '登录失败。');
    } finally {
      setBusy(false);
    }
  };

  const handleRegister = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await register(registerForm);
      navigate(redirectTarget, { replace: true });
    } catch (err: any) {
      setError(err.message || '注册失败。');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-copy">
          <div className="auth-brand">DraftRefine</div>
          <h1>{pageTitle}</h1>
          <p>{pageDescription}</p>
        </div>

        <div className="auth-tabs" role="tablist" aria-label="账号操作">
          <button
            type="button"
            className={activeTab === 'login' ? 'active' : ''}
            onClick={() => setActiveTab('login')}
          >
            登录
          </button>
          <button
            type="button"
            className={activeTab === 'register' ? 'active' : ''}
            onClick={() => setActiveTab('register')}
          >
            注册
          </button>
        </div>

        {error ? (
          <div className="auth-error">
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        ) : null}

        {activeTab === 'login' ? (
          <form className="auth-form" onSubmit={handleLogin}>
            <label className="auth-field">
              <span>邮箱或用户名</span>
              <div className="auth-input">
                <UserRound size={18} />
                <input
                  value={loginForm.identifier}
                  onChange={(event) =>
                    setLoginForm((current) => ({ ...current, identifier: event.target.value }))
                  }
                  placeholder="输入邮箱或用户名"
                  autoComplete="username"
                  minLength={3}
                  required
                />
              </div>
            </label>

            <label className="auth-field">
              <span>密码</span>
              <div className="auth-input">
                <LockKeyhole size={18} />
                <input
                  type="password"
                  value={loginForm.password}
                  onChange={(event) =>
                    setLoginForm((current) => ({ ...current, password: event.target.value }))
                  }
                  placeholder="输入密码"
                  autoComplete="current-password"
                  minLength={8}
                  required
                />
              </div>
            </label>

            <button className="auth-submit" type="submit" disabled={busy}>
              {busy ? '登录中…' : '登录'}
            </button>
          </form>
        ) : (
          <form className="auth-form" onSubmit={handleRegister}>
            <label className="auth-field">
              <span>邮箱</span>
              <div className="auth-input">
                <Mail size={18} />
                <input
                  type="email"
                  value={registerForm.email}
                  onChange={(event) =>
                    setRegisterForm((current) => ({ ...current, email: event.target.value }))
                  }
                  placeholder="name@example.com"
                  autoComplete="email"
                  required
                />
              </div>
            </label>

            <label className="auth-field">
              <span>用户名</span>
              <div className="auth-input">
                <UserRound size={18} />
                <input
                  value={registerForm.username}
                  onChange={(event) =>
                    setRegisterForm((current) => ({ ...current, username: event.target.value }))
                  }
                  placeholder="设置一个用户名"
                  autoComplete="username"
                  minLength={3}
                  required
                />
              </div>
            </label>

            <label className="auth-field">
              <span>密码</span>
              <div className="auth-input">
                <LockKeyhole size={18} />
                <input
                  type="password"
                  value={registerForm.password}
                  onChange={(event) =>
                    setRegisterForm((current) => ({ ...current, password: event.target.value }))
                  }
                  placeholder="至少 8 位"
                  autoComplete="new-password"
                  minLength={8}
                  required
                />
              </div>
            </label>

            {showInviteCode ? (
              <label className="auth-field">
                <span>邀请码</span>
                <div className="auth-input">
                  <KeyRound size={18} />
                  <input
                    value={registerForm.inviteCode}
                    onChange={(event) =>
                      setRegisterForm((current) => ({ ...current, inviteCode: event.target.value }))
                    }
                    placeholder="输入邀请码"
                    required
                  />
                </div>
              </label>
            ) : null}

            <button className="auth-submit" type="submit" disabled={busy || registerDisabled}>
              {busy ? '创建中…' : '创建账号'}
            </button>
          </form>
        )}
      </div>
    </div>
  );
};

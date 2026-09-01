import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import { useAppStore } from '../store';
import { AuthStatus, UserProfile } from '../types';

type LoginPayload = {
  identifier: string;
  password: string;
};

type RegisterPayload = {
  email: string;
  username: string;
  password: string;
  inviteCode: string;
};

type RecoveryPayload = {
  identifier: string;
  newPassword: string;
  recoveryCode: string;
};

type AuthContextValue = {
  user: UserProfile | null;
  authStatus: AuthStatus | null;
  loading: boolean;
  refresh: () => Promise<void>;
  login: (payload: LoginPayload) => Promise<UserProfile>;
  register: (payload: RegisterPayload) => Promise<UserProfile>;
  recover: (payload: RecoveryPayload) => Promise<UserProfile>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

function clearWorkspaceState() {
  useAppStore.getState().resetWorkspace();
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    setLoading(true);
    try {
      const status = await api.auth.status();
      setAuthStatus(status);
      try {
        const response = await api.auth.me();
        setUser(response.user);
      } catch (error: any) {
        if (error?.status !== 401) {
          throw error;
        }
        setUser(null);
        clearWorkspaceState();
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    const handleUnauthorized = () => {
      setUser(null);
      clearWorkspaceState();
    };
    window.addEventListener('draftrefine:unauthorized', handleUnauthorized);
    return () => window.removeEventListener('draftrefine:unauthorized', handleUnauthorized);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      authStatus,
      loading,
      refresh,
      login: async (payload) => {
        const response = await api.auth.login(payload);
        setUser(response.user);
        setAuthStatus((previous) =>
          previous
            ? { ...previous, hasUsers: true }
            : {
                hasUsers: true,
                registrationMode: 'invite-only',
                inviteConfigured: true,
              }
        );
        clearWorkspaceState();
        return response.user;
      },
      register: async (payload) => {
        const response = await api.auth.register(payload);
        setUser(response.user);
        setAuthStatus((previous) =>
          previous
            ? { ...previous, hasUsers: true }
            : {
                hasUsers: true,
                registrationMode: 'invite-only',
                inviteConfigured: true,
              }
        );
        clearWorkspaceState();
        return response.user;
      },
      recover: async (payload) => {
        const response = await api.auth.recover(payload);
        setUser(response.user);
        clearWorkspaceState();
        return response.user;
      },
      logout: async () => {
        try {
          await api.auth.logout();
        } finally {
          setUser(null);
          clearWorkspaceState();
        }
      },
    }),
    [authStatus, loading, user]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}

import { create } from 'zustand';
import { Project, ProjectBundle, RevisionCandidate, RewriteProgress } from '../types';
import { api } from '../api/client';

const FAST_ACTIONS = new Set(['shorten', 'transition-polish', 'unify-terms']);

const REWRITE_PHASES: Record<'fast' | 'full', Array<{ label: string; ratio: number }>> = {
  fast: [
    { label: '准备上下文', ratio: 0.18 },
    { label: '生成改写', ratio: 0.72 },
    { label: '整理结果', ratio: 0.92 },
  ],
  full: [
    { label: '准备上下文', ratio: 0.14 },
    { label: '检索证据', ratio: 0.38 },
    { label: '生成改写', ratio: 0.74 },
    { label: '校验结果', ratio: 0.92 },
  ],
};

function laneForAction(actionType: string): 'fast' | 'full' {
  return FAST_ACTIONS.has(actionType) ? 'fast' : 'full';
}

function startRewriteProgress(
  set: (partial: Partial<AppState> | ((state: AppState) => Partial<AppState>)) => void,
  actionType: string
): () => void {
  const lane = laneForAction(actionType);
  const phases = REWRITE_PHASES[lane];
  const startedAt = Date.now();
  const targetDurationMs = lane === 'full' ? 24000 : 12000;

  set({
    rewriteProgress: {
      lane,
      actionType,
      phase: phases[0].label,
      stepIndex: 1,
      totalSteps: phases.length,
      percent: 6,
    },
  });

  const timer = window.setInterval(() => {
    const elapsed = Date.now() - startedAt;
    const normalized = Math.min(1, elapsed / targetDurationMs);
    const eased = 1 - Math.exp(-normalized * 2.1);
    const percent = Math.min(88, Math.round(6 + eased * 82));
    let phaseIndex = phases.findIndex((phase) => percent <= phase.ratio * 100);
    if (phaseIndex === -1) phaseIndex = phases.length - 1;

    set({
      rewriteProgress: {
        lane,
        actionType,
        phase: phases[phaseIndex].label,
        stepIndex: phaseIndex + 1,
        totalSteps: phases.length,
        percent,
      },
    });
  }, 280);

  return () => {
    window.clearInterval(timer);
    set({ rewriteProgress: null });
  };
}

interface AppState {
  projects: Project[];
  activeProjectId: string | null;
  bundle: ProjectBundle | null;
  activeSectionId: string | null;
  candidate: RevisionCandidate | null;
  rewriteProgress: RewriteProgress | null;
  loading: Record<string, boolean>;
  error: string | null;
  resetWorkspace: () => void;

  fetchProjects: (scope?: 'active' | 'trash' | 'all') => Promise<void>;
  loadProject: (id: string) => Promise<void>;
  createProject: (data: unknown) => Promise<ProjectBundle>;
  deleteProject: (id: string, permanent?: boolean) => Promise<void>;
  restoreProject: (id: string) => Promise<void>;
  setActiveSection: (id: string | null) => void;
  diagnoseProject: () => Promise<void>;
  rewriteSection: (actionType: string, currentText: string, feedback?: string, commentId?: string | null) => Promise<void>;
  acceptCandidate: () => Promise<void>;
  rejectCandidate: () => Promise<void>;
  reviseCandidate: (feedback: string) => Promise<void>;
  manualSaveSection: (sectionId: string, newText: string) => Promise<void>;
  setError: (error: string | null) => void;
  clearCandidate: () => void;
  setLoading: (key: string, isLoading: boolean) => void;
}

export const useAppStore = create<AppState>((set, get) => ({
  projects: [],
  activeProjectId: null,
  bundle: null,
  activeSectionId: null,
  candidate: null,
  rewriteProgress: null,
  loading: {},
  error: null,

  setLoading: (key, isLoading) =>
    set((state) => ({
      loading: {
        ...state.loading,
        [key]: isLoading,
      },
    })),

  setError: (error) => set({ error }),

  resetWorkspace: () =>
    set({
      projects: [],
      activeProjectId: null,
      bundle: null,
      activeSectionId: null,
      candidate: null,
      rewriteProgress: null,
      loading: {},
      error: null,
    }),

  clearCandidate: () => set({ candidate: null, rewriteProgress: null }),

  fetchProjects: async (scope = 'active') => {
    try {
      get().setLoading(`projects:${scope}`, true);
      const projects = await api.projects.list(scope);
      set({ projects, error: null });
    } catch (err: any) {
      set({ error: err.message });
    } finally {
      get().setLoading(`projects:${scope}`, false);
    }
  },

  loadProject: async (id) => {
    try {
      get().setLoading('bundle', true);
      const bundle = await api.projects.getBundle(id);
      set({
        bundle,
        activeProjectId: id,
        activeSectionId: null,
        candidate: null,
        rewriteProgress: null,
        error: null,
      });
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      get().setLoading('bundle', false);
    }
  },

  createProject: async (data) => {
    try {
      get().setLoading('create', true);
      const bundle = await api.projects.create(data as any);
      set((state) => ({
        projects: [bundle.project, ...state.projects],
        bundle,
        activeProjectId: bundle.project.id,
        activeSectionId: bundle.sections[0]?.id ?? null,
        error: null,
      }));
      return bundle;
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      get().setLoading('create', false);
    }
  },

  deleteProject: async (id, permanent = false) => {
    try {
      get().setLoading(`delete:${id}`, true);
      await api.projects.delete(id, permanent);
      set((state) => ({
        projects: state.projects.filter((project) => project.id !== id),
        error: null,
      }));
      if (get().activeProjectId === id) {
        set({
          activeProjectId: null,
          bundle: null,
          activeSectionId: null,
          candidate: null,
          rewriteProgress: null,
        });
      }
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      get().setLoading(`delete:${id}`, false);
    }
  },

  restoreProject: async (id) => {
    try {
      get().setLoading(`restore:${id}`, true);
      await api.projects.restore(id);
      set({ error: null });
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      get().setLoading(`restore:${id}`, false);
    }
  },

  setActiveSection: (id) =>
    set({
      activeSectionId: id,
      candidate: null,
      rewriteProgress: null,
      error: null,
    }),

  diagnoseProject: async () => {
    const { activeProjectId } = get();
    if (!activeProjectId) return;
    try {
      get().setLoading('diagnose', true);
      const bundle = await api.projects.diagnose(activeProjectId);
      set({ bundle, error: null });
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      get().setLoading('diagnose', false);
    }
  },

  rewriteSection: async (actionType, currentText, feedback, commentId) => {
    const { activeSectionId, candidate } = get();
    if (!activeSectionId) return;
    const stopProgress = startRewriteProgress(set, actionType);
    try {
      get().setLoading('rewrite', true);
      set({ error: null });
      const nextCandidate = await api.sections.rewrite(activeSectionId, {
        actionType,
        currentText,
        feedback,
        commentId: commentId ?? null,
        previousCandidateText: candidate?.text || '',
      });
      set({ candidate: nextCandidate, error: null });
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      stopProgress();
      get().setLoading('rewrite', false);
    }
  },

  acceptCandidate: async () => {
    const { candidate } = get();
    if (!candidate) return;
    try {
      get().setLoading('accept', true);
      const bundle = await api.revisions.accept(candidate.id);
      set({ bundle, candidate: null, rewriteProgress: null, error: null });
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      get().setLoading('accept', false);
    }
  },

  rejectCandidate: async () => {
    const { candidate } = get();
    if (!candidate) return;
    try {
      get().setLoading('reject', true);
      await api.revisions.reject(candidate.id);
      set({ candidate: null, rewriteProgress: null, error: null });
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      get().setLoading('reject', false);
    }
  },

  reviseCandidate: async (feedback) => {
    const { candidate } = get();
    if (!candidate || !feedback.trim()) return;
    const stopProgress = startRewriteProgress(set, candidate.actionType);
    try {
      get().setLoading('reviseCandidate', true);
      set({ error: null });
      const nextCandidate = await api.revisions.revise(candidate.id, feedback.trim());
      set({ candidate: nextCandidate, error: null });
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      stopProgress();
      get().setLoading('reviseCandidate', false);
    }
  },

  manualSaveSection: async (sectionId, newText) => {
    try {
      get().setLoading('save', true);
      const bundle = await api.sections.manualEdit(sectionId, { newText });
      set({ bundle, error: null });
    } catch (err: any) {
      set({ error: err.message });
      throw err;
    } finally {
      get().setLoading('save', false);
    }
  },
}));

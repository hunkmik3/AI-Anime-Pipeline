import { create } from "zustand";

import {
  EMPTY_PROJECT_BIBLE,
  createProject as apiCreateProject,
  deleteProject as apiDeleteProject,
  getProject,
  getProjectBible,
  listProjects,
  patchProject,
  putProjectBible,
  type ProjectBible,
  type ProjectDTO,
  type ProjectDetailDTO,
} from "../api/client";

/**
 * Top-level project state. Owns the project list, the active project id,
 * and the Project Bible for the active project. Mid-Phase 3 it sits next
 * to the legacy ``useBoardStore`` (which is now a thin proxy onto the
 * shot-workflow store) and gradually consumers migrate over.
 */
interface ProjectState {
  projects: ProjectDTO[];
  currentProjectId: string | null;
  currentProject: ProjectDetailDTO | null;
  projectBible: ProjectBible | null;
  loading: boolean;
  error: string | null;

  loadProjects(): Promise<void>;
  selectProject(id: string | null): Promise<void>;
  createProject(name: string): Promise<ProjectDTO | null>;
  renameProject(id: string, name: string): Promise<void>;
  deleteProject(id: string): Promise<void>;
  refreshProjectDetail(): Promise<void>;
  loadBible(id: string): Promise<void>;
  saveBible(bible: ProjectBible): Promise<void>;
  clearError(): void;
}

const ACTIVE_PROJECT_KEY = "flowboard.activeProjectId";

function persistProjectId(id: string | null) {
  try {
    if (id === null) localStorage.removeItem(ACTIVE_PROJECT_KEY);
    else localStorage.setItem(ACTIVE_PROJECT_KEY, id);
  } catch {
    /* storage disabled — non-fatal */
  }
}

function loadPersistedProjectId(): string | null {
  try {
    const raw = localStorage.getItem(ACTIVE_PROJECT_KEY);
    return raw && raw.length > 0 ? raw : null;
  } catch {
    return null;
  }
}

function fillBible(partial: Partial<ProjectBible> | null | undefined): ProjectBible {
  return { ...EMPTY_PROJECT_BIBLE, ...(partial ?? {}) };
}

export const useProjectStore = create<ProjectState>((set, get) => ({
  projects: [],
  currentProjectId: null,
  currentProject: null,
  projectBible: null,
  loading: false,
  error: null,

  async loadProjects() {
    set({ loading: true, error: null });
    try {
      const projects = await listProjects();
      const persisted = loadPersistedProjectId();
      const fallback = projects[0]?.id ?? null;
      const nextId =
        (persisted && projects.find((p) => p.id === persisted)?.id) ||
        fallback;
      set({ projects, loading: false });
      if (nextId) {
        await get().selectProject(nextId);
      } else {
        set({ currentProjectId: null, currentProject: null, projectBible: null });
      }
    } catch (err) {
      set({ loading: false, error: err instanceof Error ? err.message : String(err) });
    }
  },

  async selectProject(id) {
    if (id === null) {
      set({ currentProjectId: null, currentProject: null, projectBible: null });
      persistProjectId(null);
      return;
    }
    if (id === get().currentProjectId && get().currentProject) {
      // Already loaded — fast path; consumers can call refreshProjectDetail
      // explicitly if they need fresh counts.
      return;
    }
    try {
      const [detail, bible] = await Promise.all([
        getProject(id),
        getProjectBible(id).catch(() => ({}) as Partial<ProjectBible>),
      ]);
      set({
        currentProjectId: id,
        currentProject: detail,
        projectBible: fillBible(bible),
      });
      persistProjectId(id);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  async createProject(name) {
    try {
      const project = await apiCreateProject({ name: name.trim() || "Untitled" });
      set((s) => ({ projects: [project, ...s.projects] }));
      await get().selectProject(project.id);
      return project;
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return null;
    }
  },

  async renameProject(id, name) {
    try {
      const updated = await patchProject(id, { name });
      set((s) => ({
        projects: s.projects.map((p) =>
          p.id === id ? { ...p, name: updated.name } : p,
        ),
        currentProject:
          s.currentProject && s.currentProject.id === id
            ? { ...s.currentProject, name: updated.name }
            : s.currentProject,
      }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  async deleteProject(id) {
    try {
      await apiDeleteProject(id);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return;
    }
    const remaining = get().projects.filter((p) => p.id !== id);
    set({ projects: remaining });
    if (get().currentProjectId === id) {
      if (remaining.length > 0) {
        await get().selectProject(remaining[0].id);
      } else {
        set({ currentProjectId: null, currentProject: null, projectBible: null });
        persistProjectId(null);
      }
    }
  },

  async refreshProjectDetail() {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    try {
      const detail = await getProject(currentProjectId);
      set({ currentProject: detail });
    } catch {
      /* non-fatal — counts will be stale until next reload */
    }
  },

  async loadBible(id) {
    try {
      const bible = await getProjectBible(id);
      set({ projectBible: fillBible(bible) });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  async saveBible(bible) {
    const { currentProjectId } = get();
    if (!currentProjectId) return;
    try {
      const saved = await putProjectBible(currentProjectId, bible);
      set({ projectBible: fillBible(saved) });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  clearError() {
    set({ error: null });
  },
}));

export { ACTIVE_PROJECT_KEY };

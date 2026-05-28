import { create } from "zustand";

import {
  createScene as apiCreateScene,
  deleteScene as apiDeleteScene,
  getScene,
  listScenes,
  patchScene,
  type SceneDTO,
  type SceneDetailDTO,
} from "../api/client";

interface SceneState {
  // Scene list keyed by projectId so toggling projects keeps both ready
  // without an extra fetch on every switch. Older entries are dropped when
  // the user switches projects to keep memory bounded.
  scenesByProject: Record<string, SceneDTO[]>;
  loadingProjectId: string | null;
  currentSceneId: string | null;
  currentScene: SceneDetailDTO | null;
  error: string | null;

  loadScenes(projectId: string): Promise<SceneDTO[]>;
  createScene(projectId: string, name: string): Promise<SceneDTO | null>;
  renameScene(id: string, name: string): Promise<void>;
  deleteScene(id: string): Promise<void>;
  selectScene(id: string | null): Promise<void>;
  // Replace scene list when the active project drops out from under us.
  resetForProject(projectId: string | null): void;
  clearError(): void;
}

export const useSceneStore = create<SceneState>((set, get) => ({
  scenesByProject: {},
  loadingProjectId: null,
  currentSceneId: null,
  currentScene: null,
  error: null,

  async loadScenes(projectId) {
    set({ loadingProjectId: projectId, error: null });
    try {
      const scenes = await listScenes(projectId);
      set((s) => ({
        scenesByProject: { ...s.scenesByProject, [projectId]: scenes },
        loadingProjectId: null,
      }));
      return scenes;
    } catch (err) {
      set({
        loadingProjectId: null,
        error: err instanceof Error ? err.message : String(err),
      });
      return [];
    }
  },

  async createScene(projectId, name) {
    try {
      const existing = get().scenesByProject[projectId] ?? [];
      const scene = await apiCreateScene(projectId, {
        name: name.trim() || "Untitled scene",
        order_index: existing.length,
      });
      set((s) => ({
        scenesByProject: {
          ...s.scenesByProject,
          [projectId]: [...(s.scenesByProject[projectId] ?? []), scene],
        },
      }));
      return scene;
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return null;
    }
  },

  async renameScene(id, name) {
    try {
      const updated = await patchScene(id, { name });
      set((s) => {
        const next: Record<string, SceneDTO[]> = {};
        for (const [pid, list] of Object.entries(s.scenesByProject)) {
          next[pid] = list.map((sc) =>
            sc.id === id ? { ...sc, name: updated.name } : sc,
          );
        }
        return {
          scenesByProject: next,
          currentScene:
            s.currentScene && s.currentScene.id === id
              ? { ...s.currentScene, name: updated.name }
              : s.currentScene,
        };
      });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  async deleteScene(id) {
    try {
      await apiDeleteScene(id);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return;
    }
    set((s) => {
      const next: Record<string, SceneDTO[]> = {};
      for (const [pid, list] of Object.entries(s.scenesByProject)) {
        next[pid] = list.filter((sc) => sc.id !== id);
      }
      const isActive = s.currentSceneId === id;
      return {
        scenesByProject: next,
        currentSceneId: isActive ? null : s.currentSceneId,
        currentScene: isActive ? null : s.currentScene,
      };
    });
  },

  async selectScene(id) {
    if (id === null) {
      set({ currentSceneId: null, currentScene: null });
      return;
    }
    if (id === get().currentSceneId && get().currentScene) {
      return;
    }
    try {
      const detail = await getScene(id);
      set({ currentSceneId: id, currentScene: detail });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  resetForProject(projectId) {
    if (projectId === null) {
      set({
        scenesByProject: {},
        currentSceneId: null,
        currentScene: null,
      });
      return;
    }
    set((s) => {
      // Drop scene lists for other projects to keep memory bounded once
      // the user has clearly moved away. Keep the currently-loaded project
      // and the new one until both are confirmed obsolete.
      const kept: Record<string, SceneDTO[]> = {};
      if (s.scenesByProject[projectId]) {
        kept[projectId] = s.scenesByProject[projectId];
      }
      return { scenesByProject: kept };
    });
  },

  clearError() {
    set({ error: null });
  },
}));

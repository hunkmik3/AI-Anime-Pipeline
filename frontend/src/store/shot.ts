import { create } from "zustand";

import {
  createShot as apiCreateShot,
  deleteShot as apiDeleteShot,
  getShot,
  listShots,
  patchShot,
  runShot as apiRunShot,
  cancelShot as apiCancelShot,
  type ShotDTO,
  type ShotStatus,
} from "../api/client";

interface ShotState {
  // Shot lists keyed by sceneId — same memory bound approach as
  // ``useSceneStore.scenesByProject``.
  shotsByScene: Record<string, ShotDTO[]>;
  loadingSceneId: string | null;
  currentShotId: string | null;
  currentShot: ShotDTO | null;
  error: string | null;

  loadShots(sceneId: string): Promise<ShotDTO[]>;
  createShot(sceneId: string, scriptText?: string): Promise<ShotDTO | null>;
  selectShot(id: string | null): Promise<void>;
  updateScriptText(id: string, scriptText: string): Promise<void>;
  setStatus(id: string, status: ShotStatus): Promise<void>;
  deleteShot(id: string): Promise<void>;
  runShot(id: string): Promise<void>;
  cancelShot(id: string): Promise<void>;
  resetForScene(sceneId: string | null): void;
  clearError(): void;
}

const ACTIVE_SHOT_KEY = "flowboard.activeShotId";
const LEGACY_BOARD_KEY = "flowboard.activeBoardId";

/**
 * Clear the pre-Phase-3 numeric ``flowboard.activeBoardId`` localStorage
 * key. Phase 1 swapped the DB to UUIDs; the old numeric value can't be
 * resolved against the new schema so we drop it on the first run after
 * upgrade. Idempotent — safe to call on every boot.
 */
export function migrateLegacyLocalStorage(): void {
  try {
    if (localStorage.getItem(LEGACY_BOARD_KEY) !== null) {
      localStorage.removeItem(LEGACY_BOARD_KEY);
    }
  } catch {
    /* storage disabled — non-fatal */
  }
}

export function persistActiveShotId(id: string | null) {
  try {
    if (id === null) localStorage.removeItem(ACTIVE_SHOT_KEY);
    else localStorage.setItem(ACTIVE_SHOT_KEY, id);
  } catch {
    /* ignore */
  }
}

export function loadPersistedShotId(): string | null {
  try {
    const raw = localStorage.getItem(ACTIVE_SHOT_KEY);
    return raw && raw.length > 0 ? raw : null;
  } catch {
    return null;
  }
}

export const useShotStore = create<ShotState>((set, get) => ({
  shotsByScene: {},
  loadingSceneId: null,
  currentShotId: null,
  currentShot: null,
  error: null,

  async loadShots(sceneId) {
    set({ loadingSceneId: sceneId, error: null });
    try {
      const shots = await listShots(sceneId);
      set((s) => ({
        shotsByScene: { ...s.shotsByScene, [sceneId]: shots },
        loadingSceneId: null,
      }));
      return shots;
    } catch (err) {
      set({
        loadingSceneId: null,
        error: err instanceof Error ? err.message : String(err),
      });
      return [];
    }
  },

  async createShot(sceneId, scriptText) {
    try {
      const existing = get().shotsByScene[sceneId] ?? [];
      const shot = await apiCreateShot(sceneId, {
        order_index: existing.length,
        script_text: scriptText ?? "",
      });
      set((s) => ({
        shotsByScene: {
          ...s.shotsByScene,
          [sceneId]: [...(s.shotsByScene[sceneId] ?? []), shot],
        },
      }));
      return shot;
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return null;
    }
  },

  async selectShot(id) {
    if (id === null) {
      set({ currentShotId: null, currentShot: null });
      persistActiveShotId(null);
      return;
    }
    if (id === get().currentShotId && get().currentShot) return;
    try {
      const shot = await getShot(id);
      set({ currentShotId: id, currentShot: shot });
      persistActiveShotId(id);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  async updateScriptText(id, scriptText) {
    try {
      const updated = await patchShot(id, { script_text: scriptText });
      set((s) => {
        const next: Record<string, ShotDTO[]> = {};
        for (const [sid, list] of Object.entries(s.shotsByScene)) {
          next[sid] = list.map((sh) =>
            sh.id === id ? { ...sh, script_text: updated.script_text } : sh,
          );
        }
        return {
          shotsByScene: next,
          currentShot:
            s.currentShot && s.currentShot.id === id
              ? { ...s.currentShot, script_text: updated.script_text }
              : s.currentShot,
        };
      });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  async setStatus(id, status) {
    try {
      const updated = await patchShot(id, { status });
      set((s) => ({
        currentShot:
          s.currentShot && s.currentShot.id === id
            ? { ...s.currentShot, status: updated.status }
            : s.currentShot,
      }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  async deleteShot(id) {
    try {
      await apiDeleteShot(id);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return;
    }
    set((s) => {
      const next: Record<string, ShotDTO[]> = {};
      for (const [sid, list] of Object.entries(s.shotsByScene)) {
        next[sid] = list.filter((sh) => sh.id !== id);
      }
      const isActive = s.currentShotId === id;
      return {
        shotsByScene: next,
        currentShotId: isActive ? null : s.currentShotId,
        currentShot: isActive ? null : s.currentShot,
      };
    });
  },

  async runShot(id) {
    try {
      const shot = await apiRunShot(id);
      set((s) => ({
        currentShot:
          s.currentShot && s.currentShot.id === id ? shot : s.currentShot,
      }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  async cancelShot(id) {
    try {
      const shot = await apiCancelShot(id);
      set((s) => ({
        currentShot:
          s.currentShot && s.currentShot.id === id ? shot : s.currentShot,
      }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  resetForScene(sceneId) {
    if (sceneId === null) {
      set({
        shotsByScene: {},
        currentShotId: null,
        currentShot: null,
      });
      return;
    }
    set((s) => {
      const kept: Record<string, ShotDTO[]> = {};
      if (s.shotsByScene[sceneId]) {
        kept[sceneId] = s.shotsByScene[sceneId];
      }
      return { shotsByScene: kept };
    });
  },

  clearError() {
    set({ error: null });
  },
}));

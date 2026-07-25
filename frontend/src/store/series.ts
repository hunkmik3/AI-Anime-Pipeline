import { create } from "zustand";

import {
  createSeries as apiCreateSeries,
  deleteSeries as apiDeleteSeries,
  listSeries,
  patchSeries,
  type SeriesDTO,
  type UnitLabel,
} from "../api/client";

/**
 * Phase 10: Series is the tier between Project and Episode/Chapter. Kept in its
 * own light store, keyed by project like the scene store, so switching projects
 * doesn't refetch and the project home page can group episodes under their
 * series client-side.
 */
interface SeriesState {
  byProject: Record<string, SeriesDTO[]>;
  loadingProjectId: string | null;
  error: string | null;

  loadSeries(projectId: string): Promise<SeriesDTO[]>;
  createSeries(
    projectId: string,
    input: { name: string; code?: string; unit_label?: UnitLabel },
  ): Promise<SeriesDTO | null>;
  renameSeries(
    projectId: string,
    id: string,
    patch: { name?: string; code?: string; unit_label?: UnitLabel },
  ): Promise<void>;
  deleteSeries(projectId: string, id: string): Promise<boolean>;
  clearError(): void;
}

export const useSeriesStore = create<SeriesState>((set) => ({
  byProject: {},
  loadingProjectId: null,
  error: null,

  async loadSeries(projectId) {
    set({ loadingProjectId: projectId, error: null });
    try {
      const rows = await listSeries(projectId);
      set((s) => ({
        byProject: { ...s.byProject, [projectId]: rows },
        loadingProjectId: null,
      }));
      return rows;
    } catch (err) {
      set({
        loadingProjectId: null,
        error: err instanceof Error ? err.message : String(err),
      });
      return [];
    }
  },

  async createSeries(projectId, input) {
    try {
      const row = await apiCreateSeries(projectId, input);
      set((s) => ({
        byProject: {
          ...s.byProject,
          [projectId]: [...(s.byProject[projectId] ?? []), row],
        },
      }));
      return row;
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return null;
    }
  },

  async renameSeries(projectId, id, patch) {
    try {
      const row = await patchSeries(id, patch);
      set((s) => ({
        byProject: {
          ...s.byProject,
          [projectId]: (s.byProject[projectId] ?? []).map((x) =>
            x.id === id ? row : x,
          ),
        },
      }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
    }
  },

  async deleteSeries(projectId, id) {
    try {
      await apiDeleteSeries(id);
    } catch (err) {
      // 409 (series still has episodes) is surfaced to the caller so it can
      // tell the user to move/delete episodes first.
      set({ error: err instanceof Error ? err.message : String(err) });
      return false;
    }
    set((s) => ({
      byProject: {
        ...s.byProject,
        [projectId]: (s.byProject[projectId] ?? []).filter((x) => x.id !== id),
      },
    }));
    return true;
  },

  clearError() {
    set({ error: null });
  },
}));

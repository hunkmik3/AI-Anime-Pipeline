import { create } from "zustand";

import {
  listVideoModels,
  type VideoModelDTO,
  type VideoModelsResponse,
} from "../api/client";

/**
 * Caches the backend's video model registry.
 *
 * The list is small (3 models at Phase 5 ship; grows linearly as Kling
 * / Hailuo etc. are added) and immutable across a session, so we fetch
 * once at app boot and keep it in memory. Any component that needs
 * capability info — VideoNode settings panel, project default selector,
 * worker-side dispatch UI — reads from this store.
 *
 * The store also tracks `loadError` so the dropdown can render a clear
 * "couldn't load models" fallback rather than freezing. This matches
 * how `useLlmProvidersStore` handles its parallel concern.
 */

interface VideoModelsState {
  defaultModelId: string | null;
  models: VideoModelDTO[];
  loaded: boolean;
  loading: boolean;
  loadError: string | null;

  load(force?: boolean): Promise<void>;
  /** Look up a model by id. Returns undefined when not registered. */
  getModel(modelId: string): VideoModelDTO | undefined;
}

export const useVideoModelsStore = create<VideoModelsState>((set, get) => ({
  defaultModelId: null,
  models: [],
  loaded: false,
  loading: false,
  loadError: null,

  async load(force = false) {
    if (get().loading) return;
    if (get().loaded && !force) return;
    set({ loading: true, loadError: null });
    try {
      const resp: VideoModelsResponse = await listVideoModels();
      set({
        defaultModelId: resp.default_model_id,
        models: resp.models,
        loaded: true,
        loading: false,
      });
    } catch (err) {
      set({
        loading: false,
        loadError: err instanceof Error ? err.message : String(err),
      });
    }
  },

  getModel(modelId) {
    return get().models.find((m) => m.model_id === modelId);
  },
}));

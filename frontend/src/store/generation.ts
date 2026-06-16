import { create } from "zustand";
import { ensureProjectFlowProject, createRequest, getRequest, patchNode } from "../api/client";
import { useShotWorkflowStore, type NodeStatus } from "./shotWorkflow";
import { useProjectStore } from "./project";
import { useSettingsStore } from "./settings";
import { useVideoModelsStore } from "./videoModels";
import { parseServerTimeMs } from "../utils/serverTime";

type PollEntry = { requestId: number; timerId: ReturnType<typeof setTimeout> | null };

interface GenerationState {
  active: Record<string, PollEntry>;
  openDialog: { rfId: string | null; prompt: string };
  openViewer: { rfId: string | null; idx: number };
  projectId: string | null;
  // Auto-detected from Flow's createProject response — used as the
  // default tier for every dispatch so the UI no longer needs to ask.
  // Null until the first successful project bootstrap.
  paygateTier: "PAYGATE_TIER_ONE" | "PAYGATE_TIER_TWO" | null;
  error: string | null;

  openGenerationDialog(rfId: string, prompt: string): void;
  closeGenerationDialog(): void;
  openResultViewer(rfId: string, idx?: number): void;
  closeResultViewer(): void;

  ensureProjectId(): Promise<string | null>;

  dispatchGeneration(
    rfId: string,
    opts: {
      prompt: string;
      aspectRatio?: string;
      paygateTier?: string;
      kind?: "image" | "video";
      sourceMediaId?: string;
      // Multi-source-image i2v: when the upstream image has N variants
      // we generate one video per variant. Backend sends N items in the
      // batchAsyncGenerate body so all are dispatched together.
      sourceMediaIds?: string[];
      variantCount?: number;
      // Per-variant prompts. When provided, each variant uses its own
      // prompt — required for batch auto-prompt to keep poses distinct
      // across the 4 generated images.
      prompts?: string[];
      // Phase 8.1.5/d: standalone custom refs uploaded in the video dialog
      // (not persisted to a node). image → reference_images; audio →
      // audio_ref_url; video → reference_videos.
      customRefs?: { mediaId: string; label: string | null; kind: "image" | "audio" | "video" }[];
    },
  ): Promise<void>;

  refineImage(
    rfId: string,
    opts: { prompt: string; refMediaIds?: string[]; aspectRatio?: string },
  ): Promise<void>;

  // Storyboard — see .omc/plans/storyboard-image-node.md.
  // dispatchStoryboard plans + dispatches all N shots in one request;
  // retryStoryboardShot re-runs a single failed shot (root → gen_image,
  // child → edit_image with parent.mediaId as base).
  dispatchStoryboard(
    rfId: string,
    opts: {
      shotCount: number; // 1..8
      narrativeSeed?: string;
      aspectRatio?: string;
      paygateTier?: string;
    },
  ): Promise<void>;

  retryStoryboardShot(rfId: string, shotIdx: number): Promise<void>;

  cancelGeneration(rfId: string): void;
  clearError(): void;
  // Phase 8.4 — transient success/info notice (e.g. "Frame extracted"),
  // rendered by the Toaster distinctly from errors.
  notice: string | null;
  setNotice(msg: string): void;
  clearNotice(): void;
}

// Walk the board to collect mediaIds of every upstream media-bearing node
// (character / image / visual_asset) feeding into this image-target node.
// All of these are passed to Flow as IMAGE_INPUT_TYPE_REFERENCE inputs so the
// new image is composed from them.
//
// Per-edge variant pinning: each edge from a multi-variant source
// remembers exactly WHICH variant feeds the downstream — stored on
// `edge.data.sourceVariantIdx`. Resolution rules per edge:
//   1. If the edge has a pinned `sourceVariantIdx` AND the source has
//      a `mediaIds[idx]` entry there → use it.
//   2. Else if the source has an active `mediaId` → use it
//      (single-variant case; or multi-variant where the user hasn't
//      pinned yet — variant 0 is the natural default).
//   3. Else if the source has a non-empty `mediaIds[]` → use index 0.
// One ref per edge means one Flow API call regardless of how many
// variants the upstream has — the user picks which variant feeds
// which downstream by clicking the variant tile (Stage 2 UX).
// ``master_shot`` (Phase 6) carries the scene's establishing-shot mediaId
// as ``data.mediaId`` once the MasterShotNode resolves it from the Scene
// Bible (see ``MasterShotNode.tsx``). Treated as a normal ref source so
// it ends up positionally in the wire payload; the backend's prompt
// synth labels it ``establishing_shot_ref`` at slot 1.
const REF_SOURCE_TYPES = new Set([
  "character",
  "image",
  "visual_asset",
  "Storyboard",
  "master_shot",
]);

/**
 * Phase 7: find the audio reference feeding a VideoNode. Returns the first
 * connected AudioRefNode's uploaded media_id, or undefined. Only honored by
 * Seedance 2.0 (r2v+audio) — the worker drops it with a warning otherwise.
 */
function collectUpstreamAudioMediaId(targetRfId: string): string | undefined {
  const { nodes, edges } = useShotWorkflowStore.getState();
  for (const e of edges) {
    if (e.target !== targetRfId) continue;
    const src = nodes.find((n) => n.id === e.source);
    if (src?.data.type === "audio_ref" && typeof src.data.audioMediaId === "string" && src.data.audioMediaId) {
      return src.data.audioMediaId;
    }
  }
  return undefined;
}

/**
 * DRIFT 1 fix: identity-anchor refs feeding a VideoNode in r2v mode.
 * Collects mediaIds from connected Character / VisualAsset / MasterShot
 * nodes in edge order (deduped). Used only when the model supports
 * multi-ref AND the VideoNode's manual reference list is empty — so a
 * canvas wired with character nodes feeds Seedance 2.0 r2v instead of
 * silently falling back to i2v. Excludes "image"/"storyboard" nodes:
 * those are generated stills (first_frame material), not identity anchors.
 */
const R2V_REF_TYPES = new Set(["character", "visual_asset", "master_shot"]);

/**
 * Phase 8.1.5: resolve a ref node's canonical media_id. Mirrors the backend
 * helper resolve_primary_media_id (agent/.../services/video/ref_ordering.py):
 * primary_variant_id ?? mediaId ?? first non-empty mediaIds entry.
 */
export function resolvePrimaryMediaId(
  data: Record<string, unknown> | undefined,
): string | undefined {
  const d = data ?? {};
  const primary = d.primary_variant_id;
  if (typeof primary === "string" && primary) return primary;
  const mid = d.mediaId;
  if (typeof mid === "string" && mid) return mid;
  const ids = d.mediaIds;
  if (Array.isArray(ids)) {
    const first = ids.find((m): m is string => typeof m === "string" && m.length > 0);
    if (first) return first;
  }
  return undefined;
}

/**
 * Phase 8.1: r2v refs WITH each ref node's user-assigned @image label.
 * The label drives positional ordering on the backend so the Nth
 * reference_image block matches @imageN in the pasted prompt. Edge order
 * is preserved (and dedup keeps the first occurrence's label).
 */
function collectUpstreamR2vRefsDetailed(
  targetRfId: string,
): { id: string; label: string | null }[] {
  const { nodes, edges } = useShotWorkflowStore.getState();
  const out: { id: string; label: string | null }[] = [];
  for (const e of edges) {
    if (e.target !== targetRfId) continue;
    const src = nodes.find((n) => n.id === e.source);
    if (!src || !R2V_REF_TYPES.has(src.data.type)) continue;
    // Phase 8.1.5: resolve the node's PRIMARY variant
    // (primary_variant_id ?? mediaId ?? mediaIds[0]) — must mirror the
    // backend resolve_primary_media_id (services/video/ref_ordering.py).
    const mid = resolvePrimaryMediaId(src.data);
    if (!mid) continue;
    if (out.some((r) => r.id === mid)) continue;
    const label =
      typeof src.data.reference_label === "string" && src.data.reference_label.trim()
        ? src.data.reference_label.trim()
        : null;
    out.push({ id: mid, label });
  }
  return out;
}

function collectUpstreamR2vRefs(targetRfId: string): string[] {
  return collectUpstreamR2vRefsDetailed(targetRfId).map((r) => r.id);
}

function collectUpstreamRefMediaIds(targetRfId: string): string[] {
  const { nodes, edges } = useShotWorkflowStore.getState();
  const ids: string[] = [];
  for (const e of edges) {
    if (e.target !== targetRfId) continue;
    const src = nodes.find((n) => n.id === e.source);
    if (!src || !REF_SOURCE_TYPES.has(src.data.type)) continue;

    const variants = Array.isArray(src.data.mediaIds) ? src.data.mediaIds : [];
    const pinned = (e.data?.sourceVariantIdx ?? null) as number | null;

    let chosen: string | null = null;
    if (
      pinned !== null
      && pinned >= 0
      && pinned < variants.length
      && typeof variants[pinned] === "string"
      && variants[pinned]
    ) {
      chosen = variants[pinned] as string;
    } else if (typeof src.data.mediaId === "string" && src.data.mediaId) {
      chosen = src.data.mediaId;
    } else if (variants.length > 0 && typeof variants[0] === "string" && variants[0]) {
      chosen = variants[0] as string;
    }

    if (chosen) ids.push(chosen);
  }
  return ids;
}

export const useGenerationStore = create<GenerationState>((set, get) => ({
  active: {},
  openDialog: { rfId: null, prompt: "" },
  openViewer: { rfId: null, idx: 0 },
  projectId: null,
  paygateTier: null,
  error: null,
  notice: null,

  setNotice(msg) {
    set({ notice: msg });
  },
  clearNotice() {
    set({ notice: null });
  },

  openGenerationDialog(rfId, prompt) {
    set({ openDialog: { rfId, prompt } });
  },

  closeGenerationDialog() {
    set({ openDialog: { rfId: null, prompt: "" } });
  },

  openResultViewer(rfId, idx = 0) {
    set({ openViewer: { rfId, idx } });
  },

  closeResultViewer() {
    set({ openViewer: { rfId: null, idx: 0 } });
  },

  async ensureProjectId() {
    const cached = get().projectId;
    if (cached !== null) return cached;
    // Resolve the active project (Flow-project binding lives at the
    // project level since Phase 4 — POST /api/projects/{id}/flow-project).
    const realProjectId = useProjectStore.getState().currentProjectId;
    if (!realProjectId) {
      set({ error: "no project loaded" });
      return null;
    }
    try {
      const proj = await ensureProjectFlowProject(realProjectId);
      set({ projectId: proj.flow_project_id });
      return proj.flow_project_id;
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return null;
    }
  },

  async dispatchGeneration(rfId, opts: {
    prompt: string;
    aspectRatio?: string;
    paygateTier?: string;
    kind?: "image" | "video";
    sourceMediaId?: string;
    sourceMediaIds?: string[];
    variantCount?: number;
    prompts?: string[];
    customRefs?: { mediaId: string; label: string | null; kind: "image" | "audio" | "video" }[];
  }) {
    const projectId = await get().ensureProjectId();
    if (projectId === null) return;

    const kind = opts.kind ?? "image";

    // Pre-flight tier gate is Flow-ONLY. Image gen always routes through Flow,
    // so it needs a known plan tier. Video gen only needs it on the Flow model
    // — a non-Flow model (Seedance via Avis/Dreamina) carries no Google Flow
    // plan, so we resolve the effective video model and skip the gate for it.
    // Without this, every Seedance dispatch was blocked with
    // `paygate_tier_unknown` whenever the Flow extension wasn't connected.
    let effectiveVideoModel: string | undefined;
    if (kind === "video") {
      const nodeForModel = useShotWorkflowStore.getState().nodes.find((n) => n.id === rfId);
      const projectSettings = useProjectStore.getState().currentProject?.settings;
      const nodeOverride = (nodeForModel?.data as { videoModelId?: string } | undefined)?.videoModelId;
      const projectDefault =
        typeof projectSettings === "object" && projectSettings !== null
          ? ((projectSettings as Record<string, unknown>).default_video_model as string | undefined)
          : undefined;
      effectiveVideoModel =
        nodeOverride ?? projectDefault ?? useVideoModelsStore.getState().defaultModelId ?? undefined;
    }
    const isFlowDispatch = kind !== "video" || effectiveVideoModel === "flow-default";

    const knownTier = opts.paygateTier ?? get().paygateTier;
    if (isFlowDispatch && !knownTier) {
      set({
        error: "Open Flow once so the extension can detect your plan, then retry. (See the Tier-unknown banner in the bottom-left.)",
      });
      useShotWorkflowStore.getState().updateNodeData(rfId, {
        status: "error",
        error: "paygate_tier_unknown",
      });
      return;
    }

    // Cancel existing poll for this node if any
    const existingEntry = get().active[rfId];
    if (existingEntry && existingEntry.timerId !== null) {
      clearTimeout(existingEntry.timerId);
    }

    // Optimistically update node — record variantCount so the placeholder
    // grid matches the eventual variant count even before generation finishes.
    const variantCount = Math.max(1, Math.min(opts.variantCount ?? 1, 4));
    useShotWorkflowStore.getState().updateNodeData(rfId, {
      status: "queued",
      prompt: opts.prompt,
      error: undefined,
      variantCount,
      mediaIds: undefined,
      mediaId: undefined,
    });

    // Create request (kind resolved above for the tier pre-flight)
    let reqDto;
    try {
      const nodeDbId = parseInt(rfId, 10);
      if (kind === "video") {
        const hasMulti =
          Array.isArray(opts.sourceMediaIds) && opts.sourceMediaIds.length > 0;
        // Phase 7: r2v / r2v+audio need no start frame — references (or an
        // audio ref) are enough. Only block when there's nothing to gen from.
        const nodeForGuard = useShotWorkflowStore.getState().nodes.find((n) => n.id === rfId);
        const guardRefs = Array.isArray(nodeForGuard?.data?.reference_image_ids)
          ? (nodeForGuard!.data.reference_image_ids as string[])
          : [];
        const guardAudio =
          (typeof nodeForGuard?.data?.audio_ref_media_id === "string" && nodeForGuard.data.audio_ref_media_id) ||
          collectUpstreamAudioMediaId(rfId);
        // Canvas identity-anchor nodes also count as "something to gen from"
        // (DRIFT 1): a Character->Video graph with no still is valid r2v.
        const guardCanvasRefs = collectUpstreamR2vRefs(rfId);
        if (
          !hasMulti &&
          !opts.sourceMediaId &&
          guardRefs.length === 0 &&
          guardCanvasRefs.length === 0 &&
          !guardAudio
        ) {
          useShotWorkflowStore.getState().updateNodeData(rfId, { status: "error", error: "no source media" });
          set({ error: "Video needs a source image, references (r2v), or an audio reference" });
          return;
        }
        // Model resolution: node.data.videoModelId override (Phase 5
        // per-node setting) > project.settings.default_video_model
        // (Phase 5 per-project) > backend default (currently flow-default).
        // Worker honors `model_id` and rejects unknown ids with a 422.
        const nodeForModel = useShotWorkflowStore.getState().nodes.find((n) => n.id === rfId);
        const projectSettings = useProjectStore.getState().currentProject?.settings;
        const nodeOverride = (nodeForModel?.data as { videoModelId?: string } | undefined)?.videoModelId;
        const projectDefault =
          typeof projectSettings === "object" && projectSettings !== null
            ? ((projectSettings as Record<string, unknown>).default_video_model as string | undefined)
            : undefined;
        // Fall back to the system default model (GET /api/video/models) when
        // neither the node nor the project pins one. Without this, a node on
        // the *system default* model resolved to `undefined` here, so the
        // capability lookup below missed → r2v references were silently
        // dropped → the backend hard-failed with `missing_first_frame_url`.
        const resolvedModelId =
          nodeOverride ?? projectDefault ?? useVideoModelsStore.getState().defaultModelId ?? undefined;

        const videoParams: Record<string, unknown> = {
          prompt: opts.prompt,
          project_id: projectId,
          // Canonical aspect_ratio literal per services/video/base.py.
          // Flow maps to its internal enum; Dreamina accepts only the
          // literal "16:9" / "9:16" / "1:1".
          aspect_ratio: opts.aspectRatio ?? "16:9",
          // Tier precedence: explicit caller arg > auto-detected from
          // Flow > TIER_ONE fallback. The dialog no longer asks the user.
          paygate_tier:
            opts.paygateTier ?? get().paygateTier ?? "PAYGATE_TIER_ONE",
          // Backend resolves [tier][quality][aspect] → Flow model key.
          video_quality: useSettingsStore.getState().videoQuality,
        };
        if (resolvedModelId) videoParams.model_id = resolvedModelId;

        // Dreamina-specific settings — only forwarded when the user has
        // selected a non-Flow model. The backend ignores them on Flow
        // path; surface them unconditionally so the UI doesn't need to
        // know the provider mapping.
        const videoSettings = (nodeForModel?.data as Record<string, unknown> | undefined) ?? {};
        if (videoSettings.duration_seconds) videoParams.duration_seconds = videoSettings.duration_seconds;
        if (videoSettings.resolution) videoParams.resolution = videoSettings.resolution;
        if (videoSettings.generate_audio !== undefined) videoParams.generate_audio = videoSettings.generate_audio;
        // Person-driven (KYC): backend turns the wired image/audio/video refs
        // into Avis KYC assets and dispatches portrait→video / lip-sync / video-ref.
        if (videoSettings.kycMode === true) videoParams.kyc_mode = true;
        // References for r2v. Phase 8.1.5d: the legacy manual multi-ref list
        // (VideoNodeSettings text input) was removed — canvas-wired ref nodes
        // (Character/VisualAsset/MasterShot) are the single source, each
        // carrying its @image label for positional ordering. Standalone
        // custom uploads from the dialog are appended below.
        const resolvedCaps = useVideoModelsStore
          .getState()
          .models.find((m) => m.model_id === resolvedModelId)?.capabilities;
        let r2vRefs: string[] = [];
        let r2vLabels: (string | null)[] = [];
        if (resolvedCaps?.supports_multi_ref) {
          const detailed = collectUpstreamR2vRefsDetailed(rfId);
          r2vRefs = detailed.map((r) => r.id);
          r2vLabels = detailed.map((r) => r.label);
        }
        // Phase 8.1.5/d: standalone custom refs (uploaded in the dialog, not
        // wired to a node). Split by kind: image → reference_images (append
        // after canvas refs); audio → audio_ref_url; video → reference_videos.
        const customVideoRefs: string[] = [];
        let customAudioRef: string | undefined;
        if (Array.isArray(opts.customRefs)) {
          for (const c of opts.customRefs) {
            if (!c.mediaId) continue;
            if (c.kind === "audio") {
              if (!customAudioRef) customAudioRef = c.mediaId; // single audio (multi defer 8.2)
            } else if (c.kind === "video") {
              if (!customVideoRefs.includes(c.mediaId)) customVideoRefs.push(c.mediaId);
            } else if (!r2vRefs.includes(c.mediaId)) {
              r2vRefs = [...r2vRefs, c.mediaId];
              r2vLabels = [...r2vLabels, c.label];
            }
          }
        }
        if (r2vRefs.length > 0) {
          videoParams.reference_images = r2vRefs;
          // Always sent (one code path); backend ignores an all-null list.
          videoParams.reference_labels = r2vLabels;
        }
        if (customVideoRefs.length > 0) {
          videoParams.reference_videos = customVideoRefs;
        }
        if (typeof videoSettings.last_frame_asset_id === "string" && videoSettings.last_frame_asset_id) {
          videoParams.last_frame_url = videoSettings.last_frame_asset_id;
        }
        // Audio reference (Seedance 2.0 r2v+audio): a freshly-uploaded custom
        // audio wins; else explicit node setting; else the first connected
        // AudioRefNode. Worker hoists the media_id → R2 public URL on submit.
        const audioRef =
          customAudioRef ||
          (typeof videoSettings.audio_ref_media_id === "string" && videoSettings.audio_ref_media_id) ||
          collectUpstreamAudioMediaId(rfId);
        if (audioRef) {
          videoParams.audio_ref_url = audioRef;
        }

        if (hasMulti) {
          videoParams.start_media_ids = opts.sourceMediaIds;
        } else {
          videoParams.start_media_id = opts.sourceMediaId;
        }
        reqDto = await createRequest({
          type: "gen_video",
          node_id: isNaN(nodeDbId) ? undefined : nodeDbId,
          params: videoParams,
        });
      } else {
        const refMediaIds = collectUpstreamRefMediaIds(rfId);
        const params: Record<string, unknown> = {
          prompt: opts.prompt,
          project_id: projectId,
          aspect_ratio: opts.aspectRatio ?? "IMAGE_ASPECT_RATIO_LANDSCAPE",
          paygate_tier:
            opts.paygateTier ?? get().paygateTier ?? "PAYGATE_TIER_ONE",
          variant_count: variantCount,
          // User's image model preference from the Settings panel.
          // Backend resolves the nickname → real Flow model identifier.
          image_model: useSettingsStore.getState().imageModel,
        };
        if (refMediaIds.length > 0) {
          params.ref_media_ids = refMediaIds;
        }
        // Per-variant prompts: when present, each variant uses its own
        // text instead of all sharing `params.prompt`. Backend falls back
        // to single prompt when missing/short.
        if (opts.prompts && opts.prompts.length > 0) {
          params.prompts = opts.prompts;
        }
        reqDto = await createRequest({
          type: "gen_image",
          node_id: isNaN(nodeDbId) ? undefined : nodeDbId,
          params,
        });
      }
    } catch (err) {
      useShotWorkflowStore.getState().updateNodeData(rfId, { status: "error", error: err instanceof Error ? err.message : "request failed" });
      set({ error: err instanceof Error ? err.message : "Generation failed" });
      return;
    }

    // Start polling
    const requestId = reqDto.id;
    // Cap consecutive network errors so a dead agent can't keep a poll alive
    // forever; bail to failed state after this many.
    const MAX_NETWORK_RETRIES = 8;
    let networkRetries = 0;

    function scheduleNextPoll() {
      // If the node was cancelled (e.g. user deleted it), stop chaining.
      if (get().active[rfId] === undefined) return;

      const timerId = setTimeout(async () => {
        // Also bail if the user cancelled (or deleted the node) while we slept.
        if (get().active[rfId] === undefined) return;
        try {
          const req = await getRequest(requestId);
          networkRetries = 0;

          if (req.status === "running") {
            // Phase 8.1.5d: client-side phase progress for video (the API
            // returns no %). Estimate generating 10→90% from elapsed time vs
            // duration×K; capped at 90 until the clip lands (status=done).
            const runPatch: Record<string, unknown> = { status: "running" };
            if (kind === "video") {
              const created = parseServerTimeMs(req.created_at);
              const dur = (req.params.duration_seconds as number) || 5;
              // K≈60s wall-clock per output-second — Seedance 2.0 at 1080p /
              // multi-ref / person-driven runs several minutes, so a smaller K
              // pegged the bar at 90% long before the clip landed.
              const estTotalMs = dur * 60_000;
              const elapsed = Number.isFinite(created) ? Date.now() - created : 0;
              runPatch.genPhase = "generating";
              runPatch.genProgress = Math.min(90, Math.max(10, Math.round(10 + 80 * (elapsed / estTotalMs))));
            }
            useShotWorkflowStore.getState().updateNodeData(rfId, runPatch);
            // Reschedule
            set((s) => ({
              active: {
                ...s.active,
                [rfId]: { requestId, timerId: null },
              },
            }));
            scheduleNextPoll();
          } else if (req.status === "done") {
            // `media_ids` may contain `null` placeholders for variants
            // the backend marked as partial-failures (e.g. Veo content
            // filter blocked one of 4 i2v clips while the other 3
            // succeeded). Keep the positional alignment so the frontend
            // can map slot i ↔ upstream variant i, but pick the first
            // non-null entry as the "primary" mediaId for legacy
            // single-tile UI consumers.
            const mediaIds = (req.result["media_ids"] as (string | null)[] | undefined) ?? [];
            const mediaId = mediaIds.find(
              (m): m is string => typeof m === "string" && m.length > 0,
            );
            // Surface the partial-error summary onto data.error while
            // keeping status="done" — the node still has renderable
            // variants, but the UI can flag that some slots got blocked.
            const partialError = (req.result["partial_error"] as string | undefined) ?? null;
            // Per-slot error codes (aligned to mediaIds) so the detail
            // viewer can render the exact filter reason on each blocked
            // tile. `null` length-matched array when nothing's blocked;
            // missing on legacy / non-video results.
            const slotErrors =
              (req.result["slot_errors"] as (string | null)[] | undefined) ?? null;
            // Stamp the model used onto the node so the detail panel can
            // show "Banana Pro" / "Quality" etc. — read from req.params
            // (what was dispatched). Tier-1 UI locks Lite + Quality so
            // we trust params directly without a backend fallback round-trip.
            const stampedImageModel =
              req.type === "gen_image"
                ? (req.params["image_model"] as string | undefined)
                : undefined;
            const stampedVideoQuality =
              req.type === "gen_video"
                ? (req.params["video_quality"] as string | undefined)
                : undefined;
            useShotWorkflowStore.getState().updateNodeData(rfId, {
              status: "done",
              mediaId,
              mediaIds,
              slotErrors: slotErrors ?? undefined,
              aiBrief: undefined,
              aspectRatio: opts.aspectRatio,
              renderedAt: new Date().toISOString(),
              error: partialError ?? undefined,
              genProgress: undefined,
              genPhase: undefined,
              ...(stampedImageModel ? { imageModel: stampedImageModel } : {}),
              ...(stampedVideoQuality ? { videoQuality: stampedVideoQuality } : {}),
            });
            // Persist to backend so the node survives page reload.
            const dbId = parseInt(rfId, 10);
            if (!isNaN(dbId) && mediaId) {
              const n = useShotWorkflowStore.getState().nodes.find((x) => x.id === rfId);
              const d = n?.data;
              // Backend merges `data`, so only deltas need to ship.
              // `aiBrief: null` is the explicit "clear" sentinel —
              // undefined would be dropped by JSON.stringify and leave
              // the stale brief sitting on the node.
              patchNode(dbId, {
                status: "done",
                data: {
                  // Persist prompt — without this, reloading the page
                  // shows "(no prompt)" in the detail panel because the
                  // dispatch flow only stamps prompt into the in-memory
                  // store, never to the backend. This used to live in
                  // the patchNode payload pre-Phase 20 and was
                  // accidentally dropped during the "only deltas" refactor.
                  prompt: opts.prompt,
                  mediaId,
                  mediaIds,
                  slotErrors: slotErrors ?? null,
                  variantCount: d?.variantCount ?? mediaIds.length,
                  aiBrief: null,
                  aspectRatio: opts.aspectRatio,
                  renderedAt: new Date().toISOString(),
                  // `null` clears stale error from a previous attempt
                  // when this run was clean; otherwise persist the
                  // partial summary so it survives reload.
                  error: partialError ?? null,
                  ...(stampedImageModel ? { imageModel: stampedImageModel } : {}),
                  ...(stampedVideoQuality ? { videoQuality: stampedVideoQuality } : {}),
                },
              }).catch(() => {
                // Non-fatal: the in-memory state is still correct for this session.
              });
            }
            // Generation results always carry a prompt (the one we just
            // dispatched with), and downstream synth treats prompt as the
            // source of truth. Vision adds nothing here — skip it.
            // Manual upload paths in NodeCard.tsx still call
            // requestAutoBrief; that helper now early-returns if the
            // target node already has a prompt, so behaviour stays sane
            // for upload-then-type flows too.
            set((s) => {
              const next = { ...s.active };
              delete next[rfId];
              return { active: next };
            });
          } else if (req.status === "failed") {
            const errMsg = req.error ?? "unknown";
            useShotWorkflowStore.getState().updateNodeData(rfId, {
              status: "error", error: errMsg, genProgress: undefined, genPhase: undefined,
            });
            set((s) => {
              const next = { ...s.active };
              delete next[rfId];
              return { active: next, error: req.error ?? "Generation failed" };
            });
          } else {
            // queued — keep polling
            if (kind === "video") {
              useShotWorkflowStore.getState().updateNodeData(rfId, {
                status: "queued", genPhase: "queued", genProgress: 5,
              });
            }
            set((s) => ({
              active: {
                ...s.active,
                [rfId]: { requestId, timerId: null },
              },
            }));
            scheduleNextPoll();
          }
        } catch (err) {
          networkRetries += 1;
          if (networkRetries >= MAX_NETWORK_RETRIES) {
            const msg = err instanceof Error ? err.message : "network error";
            useShotWorkflowStore.getState().updateNodeData(rfId, { status: "error", error: msg });
            set((s) => {
              const next = { ...s.active };
              delete next[rfId];
              return { active: next, error: `Generation poll failed: ${msg}` };
            });
            return;
          }
          scheduleNextPoll();
        }
      }, 1500);

      set((s) => ({
        active: {
          ...s.active,
          [rfId]: { requestId, timerId },
        },
      }));
    }

    // Initialize active entry before first poll
    set((s) => ({
      active: {
        ...s.active,
        [rfId]: { requestId, timerId: null },
      },
    }));
    scheduleNextPoll();
  },

  async refineImage(rfId, opts) {
    const projectId = await get().ensureProjectId();
    if (projectId === null) return;

    const node = useShotWorkflowStore.getState().nodes.find((n) => n.id === rfId);
    const sourceMediaId = node?.data.mediaId;
    if (!sourceMediaId) {
      set({ error: "no source image to refine" });
      return;
    }

    const existing = get().active[rfId];
    if (existing && existing.timerId !== null) clearTimeout(existing.timerId);

    useShotWorkflowStore.getState().updateNodeData(rfId, {
      status: "queued",
      prompt: opts.prompt,
      error: undefined,
      variantCount: 1,
      mediaIds: undefined,
    });

    const nodeDbId = parseInt(rfId, 10);
    let reqDto;
    try {
      reqDto = await createRequest({
        type: "edit_image",
        node_id: isNaN(nodeDbId) ? undefined : nodeDbId,
        params: {
          prompt: opts.prompt,
          project_id: projectId,
          source_media_id: sourceMediaId,
          ref_media_ids: opts.refMediaIds ?? [],
          aspect_ratio: opts.aspectRatio ?? "IMAGE_ASPECT_RATIO_LANDSCAPE",
          paygate_tier: get().paygateTier ?? "PAYGATE_TIER_ONE",
          image_model: useSettingsStore.getState().imageModel,
        },
      });
    } catch (err) {
      useShotWorkflowStore.getState().updateNodeData(rfId, {
        status: "error",
        error: err instanceof Error ? err.message : "refine failed",
      });
      set({ error: err instanceof Error ? err.message : "refine failed" });
      return;
    }

    // Reuse the same poll loop by manually wiring active entry; copy-paste of
    // dispatchGeneration's poller would be loud, so we do a minimal wait here.
    const requestId = reqDto.id;
    set((s) => ({
      active: { ...s.active, [rfId]: { requestId, timerId: null } },
    }));

    const poll = async () => {
      try {
        const req = await getRequest(requestId);
        if (req.status === "running" || req.status === "queued") {
          useShotWorkflowStore.getState().updateNodeData(rfId, { status: req.status });
          const t = setTimeout(poll, 1500);
          set((s) => ({
            active: { ...s.active, [rfId]: { requestId, timerId: t } },
          }));
          return;
        }
        if (req.status === "done") {
          const mediaIds = (req.result["media_ids"] as string[] | undefined) ?? [];
          const mediaId = mediaIds[0];
          // edit_image still routes through the user's image model setting.
          const stampedImageModel = req.params["image_model"] as string | undefined;
          useShotWorkflowStore.getState().updateNodeData(rfId, {
            status: "done",
            mediaId,
            mediaIds,
            aspectRatio: opts.aspectRatio,
            renderedAt: new Date().toISOString(),
            ...(stampedImageModel ? { imageModel: stampedImageModel } : {}),
          });
          const dbId = parseInt(rfId, 10);
          if (!isNaN(dbId) && mediaId) {
            // Backend merges `data` — ship the new state including
            // prompt so it survives reload (regression fix: pre-Phase 20
            // the patchNode payload included prompt; the "only deltas"
            // refactor dropped it on the assumption prompt was already
            // persisted, but the dispatch flow never wrote it to backend).
            patchNode(dbId, {
              data: {
                prompt: opts.prompt,
                mediaId,
                mediaIds,
                variantCount: 1,
                aspectRatio: opts.aspectRatio,
                renderedAt: new Date().toISOString(),
                ...(stampedImageModel ? { imageModel: stampedImageModel } : {}),
              },
            }).catch(() => {});
          }
          set((s) => {
            const next = { ...s.active };
            delete next[rfId];
            return { active: next };
          });
          return;
        }
        // failed
        const errMsg = req.error ?? "refine failed";
        useShotWorkflowStore.getState().updateNodeData(rfId, {
          status: "error",
          error: errMsg,
        });
        set((s) => {
          const next = { ...s.active };
          delete next[rfId];
          return { active: next, error: errMsg };
        });
      } catch (err) {
        const t = setTimeout(poll, 1500);
        set((s) => ({
          active: { ...s.active, [rfId]: { requestId, timerId: t } },
        }));
        console.warn("refine poll failed", err);
      }
    };
    setTimeout(poll, 800);
  },

  async dispatchStoryboard(rfId, opts) {
    const projectId = await get().ensureProjectId();
    if (projectId === null) return;

    const knownTier = opts.paygateTier ?? get().paygateTier;
    if (!knownTier) {
      set({
        error:
          "Open Flow once so the extension can detect your plan, then retry.",
      });
      useShotWorkflowStore.getState().updateNodeData(rfId, {
        status: "error",
        error: "paygate_tier_unknown",
      });
      return;
    }

    const shotCount = Math.max(1, Math.min(opts.shotCount, 8));
    const aspectRatio = opts.aspectRatio ?? "IMAGE_ASPECT_RATIO_LANDSCAPE";

    // Cancel any in-flight poll for this node.
    const existingEntry = get().active[rfId];
    if (existingEntry && existingEntry.timerId !== null) {
      clearTimeout(existingEntry.timerId);
    }

    // Optimistic shots[] — placeholders so the UI shows N tiles immediately.
    const placeholderShots = Array.from({ length: shotCount }, (_, k) => ({
      idx: k,
      prompt: "",
      parentShotIdx: null as number | null,
      status: "queued" as const,
    }));
    useShotWorkflowStore.getState().updateNodeData(rfId, {
      status: "queued",
      shots: placeholderShots,
      shotCount,
      narrativeSeed: opts.narrativeSeed,
      aspectRatio,
      mediaIds: Array.from({ length: shotCount }, () => null),
      mediaId: undefined,
      error: undefined,
    });

    const nodeDbId = parseInt(rfId, 10);
    const refMediaIds = collectUpstreamRefMediaIds(rfId);
    let reqDto;
    try {
      reqDto = await createRequest({
        type: "gen_storyboard",
        node_id: isNaN(nodeDbId) ? undefined : nodeDbId,
        params: {
          shot_count: shotCount,
          narrative_seed: opts.narrativeSeed ?? "",
          project_id: projectId,
          aspect_ratio: aspectRatio,
          paygate_tier: knownTier,
          image_model: useSettingsStore.getState().imageModel,
          global_ref_media_ids: refMediaIds,
        },
      });
    } catch (err) {
      useShotWorkflowStore.getState().updateNodeData(rfId, {
        status: "error",
        error: err instanceof Error ? err.message : "request failed",
      });
      set({ error: err instanceof Error ? err.message : "Generation failed" });
      return;
    }

    const requestId = reqDto.id;
    const MAX_NETWORK_RETRIES = 8;
    let networkRetries = 0;

    const poll = async () => {
      if (get().active[rfId] === undefined) return;
      try {
        const req = await getRequest(requestId);
        networkRetries = 0;
        if (req.status === "running" || req.status === "queued") {
          useShotWorkflowStore.getState().updateNodeData(rfId, { status: req.status });
          const t = setTimeout(poll, 1500);
          set((s) => ({
            active: { ...s.active, [rfId]: { requestId, timerId: t } },
          }));
          return;
        }
        if (req.status === "done") {
          const result = req.result as {
            shots?: Array<{
              idx: number;
              prompt: string;
              parentShotIdx: number | null;
              mediaId?: string | null;
              status: string;
              error?: string | null;
            }>;
            node_status?: string;
            media_ids?: (string | null)[];
          };
          const shots = (result.shots ?? []).map((s) => ({
            idx: s.idx,
            prompt: s.prompt,
            parentShotIdx: s.parentShotIdx ?? null,
            mediaId: s.mediaId ?? undefined,
            status: s.status as
              | "idle" | "queued" | "running" | "done" | "error" | "blocked",
            error: s.error ?? undefined,
          }));
          const mediaIds = (result.media_ids ?? []) as (string | null)[];
          const nodeStatus = (result.node_status as
            | "idle" | "queued" | "running" | "done" | "error" | "partial"
            | undefined) ?? "done";
          const firstMid = mediaIds.find(
            (m): m is string => typeof m === "string" && m.length > 0,
          );
          useShotWorkflowStore.getState().updateNodeData(rfId, {
            status: nodeStatus,
            shots,
            shotCount: shots.length,
            mediaIds,
            mediaId: firstMid,
            aspectRatio,
            renderedAt: new Date().toISOString(),
          });
          const dbId = parseInt(rfId, 10);
          if (!isNaN(dbId)) {
            patchNode(dbId, {
              status: nodeStatus,
              data: {
                shots,
                shotCount: shots.length,
                narrativeSeed: opts.narrativeSeed,
                mediaIds,
                aspectRatio,
                renderedAt: new Date().toISOString(),
                globalRefMediaIds: refMediaIds,
              },
            }).catch(() => {});
          }
          set((s) => {
            const next = { ...s.active };
            delete next[rfId];
            return { active: next };
          });
          return;
        }
        // failed
        const errMsg = req.error ?? "storyboard generation failed";
        useShotWorkflowStore.getState().updateNodeData(rfId, {
          status: "error",
          error: errMsg,
        });
        set((s) => {
          const next = { ...s.active };
          delete next[rfId];
          return { active: next, error: errMsg };
        });
      } catch (err) {
        networkRetries += 1;
        if (networkRetries >= MAX_NETWORK_RETRIES) {
          const msg = err instanceof Error ? err.message : "network error";
          useShotWorkflowStore.getState().updateNodeData(rfId, {
            status: "error",
            error: msg,
          });
          set((s) => {
            const next = { ...s.active };
            delete next[rfId];
            return { active: next, error: msg };
          });
          return;
        }
        const t = setTimeout(poll, 1500);
        set((s) => ({
          active: { ...s.active, [rfId]: { requestId, timerId: t } },
        }));
      }
    };

    set((s) => ({
      active: { ...s.active, [rfId]: { requestId, timerId: null } },
    }));
    setTimeout(poll, 800);
  },

  async retryStoryboardShot(rfId, shotIdx) {
    const node = useShotWorkflowStore.getState().nodes.find((n) => n.id === rfId);
    if (!node || !Array.isArray(node.data.shots)) {
      set({ error: "node has no shots" });
      return;
    }
    const projectId = await get().ensureProjectId();
    if (projectId === null) return;
    const knownTier = get().paygateTier;
    if (!knownTier) {
      set({ error: "tier unknown — open Flow first" });
      return;
    }
    const nodeDbId = parseInt(rfId, 10);

    // Optimistic per-shot status flip.
    const newShots = node.data.shots.map((s) =>
      s.idx === shotIdx
        ? { ...s, status: "queued" as const, error: undefined }
        : s,
    );
    useShotWorkflowStore.getState().updateNodeData(rfId, { shots: newShots });

    let reqDto;
    try {
      // Refs are live — collect at retry time, not from a snapshot. If
      // the user re-wired upstream edges since the original gen, the
      // retry uses the new pool. Without this the retry runs ref-less
      // and identity drifts (the whole reason for the storyboard).
      const refMediaIds = collectUpstreamRefMediaIds(rfId);
      reqDto = await createRequest({
        type: "retry_storyboard_shot",
        node_id: isNaN(nodeDbId) ? undefined : nodeDbId,
        params: {
          shot_idx: shotIdx,
          project_id: projectId,
          paygate_tier: knownTier,
          aspect_ratio:
            (node.data.aspectRatio as string | undefined) ??
            "IMAGE_ASPECT_RATIO_LANDSCAPE",
          image_model: useSettingsStore.getState().imageModel,
          ref_media_ids: refMediaIds,
        },
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "retry failed";
      useShotWorkflowStore.getState().updateNodeData(rfId, {
        shots: node.data.shots.map((s) =>
          s.idx === shotIdx ? { ...s, status: "error", error: msg } : s,
        ),
      });
      set({ error: msg });
      return;
    }

    const requestId = reqDto.id;
    // Register in the active poll registry so cancelGeneration(rfId)
    // (called on node deletion) actually stops the retry poll.
    set((s) => ({
      active: { ...s.active, [rfId]: { requestId, timerId: null } },
    }));

    const poll = async () => {
      // Implicit cancel: if the node was deleted, the active entry is gone.
      if (get().active[rfId] === undefined) return;
      try {
        const req = await getRequest(requestId);
        if (req.status === "running" || req.status === "queued") {
          const t = setTimeout(poll, 1500);
          set((s) => ({
            active: { ...s.active, [rfId]: { requestId, timerId: t } },
          }));
          return;
        }
        if (req.status === "done") {
          const newMid = (req.result["media_id"] as string | null | undefined) ?? null;
          const current = useShotWorkflowStore
            .getState()
            .nodes.find((n) => n.id === rfId);
          const baseShots = (current?.data.shots ?? []) as typeof newShots;
          const updated = baseShots.map((s) =>
            s.idx === shotIdx
              ? {
                  ...s,
                  status: (newMid ? "done" : "error") as
                    | "done" | "error",
                  mediaId: newMid ?? undefined,
                  error: newMid ? undefined : "missing_media",
                }
              : s,
          );
          // Aggregate node-level status from all shots. "partial" is
          // reserved for the genuine mixed-success-and-failure case;
          // when other shots are still queued/running the node is
          // still "running" overall.
          const hasInProgress = updated.some(
            (s) => s.status === "queued" || s.status === "running",
          );
          const aggregate: NodeStatus =
            hasInProgress
              ? "running"
              : updated.every((s) => s.status === "done")
                ? "done"
                : updated.some((s) => s.status === "done")
                  ? "partial"
                  : "error";
          useShotWorkflowStore.getState().updateNodeData(rfId, {
            shots: updated,
            mediaIds: updated.map((s) => s.mediaId ?? null),
            status: aggregate,
          });
          const dbId = parseInt(rfId, 10);
          if (!isNaN(dbId)) {
            patchNode(dbId, {
              status: aggregate,
              data: {
                shots: updated,
                mediaIds: updated.map((s) => s.mediaId ?? null),
              },
            }).catch(() => {});
          }
          set((s) => {
            const next = { ...s.active };
            delete next[rfId];
            return { active: next };
          });
          return;
        }
        // failed
        const errMsg = req.error ?? "retry failed";
        const current = useShotWorkflowStore
          .getState()
          .nodes.find((n) => n.id === rfId);
        useShotWorkflowStore.getState().updateNodeData(rfId, {
          shots: (current?.data.shots ?? []).map((s) =>
            s.idx === shotIdx ? { ...s, status: "error", error: errMsg } : s,
          ),
        });
        set((s) => {
          const next = { ...s.active };
          delete next[rfId];
          return { active: next, error: errMsg };
        });
      } catch {
        const t = setTimeout(poll, 1500);
        set((s) => ({
          active: { ...s.active, [rfId]: { requestId, timerId: t } },
        }));
      }
    };
    setTimeout(poll, 800);
  },

  cancelGeneration(rfId) {
    const entry = get().active[rfId];
    if (entry && entry.timerId !== null) {
      clearTimeout(entry.timerId);
    }
    set((s) => {
      const next = { ...s.active };
      delete next[rfId];
      return { active: next };
    });
  },

  clearError() {
    set({ error: null });
  },
}));

import { useEffect } from "react";

import { useVideoModelsStore } from "../store/videoModels";
import { useShotWorkflowStore } from "../store/shotWorkflow";
import { useProjectStore } from "../store/project";
import { patchNode } from "../api/client";

// USD-per-output-second by resolution — mirrors the backend budget estimate
// (agent/.../budget_service.py _RATE_USD_PER_SEC, calibrated to real Avis
// usdCost). This is a pre-gen ESTIMATE; the exact charge is settled after the
// clip completes. Keep in sync with the backend rates.
const USD_PER_SEC: Record<string, number> = {
  "480p": 0.1,
  "720p": 0.18,
  "1080p": 0.42,
  "4k": 1.7,
};

function estimateVideoUsd(duration: number, resolution: string): number {
  const rate = USD_PER_SEC[resolution] ?? 0.42;
  return Math.max(1, duration || 5) * rate;
}

/**
 * Per-node video settings rendered inside the Generation dialog when
 * the target is a VideoNode.
 *
 * Conditional surface based on the resolved model's capability matrix:
 *
 * - Model dropdown — fed by `GET /api/video/models`
 * - Reference images (multi-ref / r2v) — disabled when
 *   `capabilities.supports_multi_ref` is false. If the node already
 *   has refs persisted from a previous model selection, we render a
 *   persistent warning banner ("N refs will be ignored on submit") so
 *   the user makes an explicit choice (remove vs switch model). No
 *   silent drop — per the locked Phase 5 decision (C).
 * - Last frame keyframe — disabled when `supports_last_frame` is false
 * - Audio toggle — only shown when `supports_audio_toggle` is true
 * - Duration / aspect / resolution — `<select>` from capability tuples
 * - Skip content filter (B2B) — only when `supports_b2b_unmoderated`
 */

interface Props {
  rfId: string;
}

export function VideoNodeSettings({ rfId }: Props) {
  const models = useVideoModelsStore((s) => s.models);
  const defaultModelId = useVideoModelsStore((s) => s.defaultModelId);
  const loaded = useVideoModelsStore((s) => s.loaded);
  const loadError = useVideoModelsStore((s) => s.loadError);
  const load = useVideoModelsStore((s) => s.load);

  const node = useShotWorkflowStore((s) => s.nodes.find((n) => n.id === rfId));
  const updateNodeData = useShotWorkflowStore((s) => s.updateNodeData);
  const projectSettings = useProjectStore((s) => s.currentProject?.settings);

  useEffect(() => {
    if (!loaded) void load();
  }, [load, loaded]);

  if (!node) return null;

  const data = (node.data ?? {}) as Record<string, unknown>;
  const projectDefault =
    typeof projectSettings === "object" && projectSettings !== null
      ? ((projectSettings as Record<string, unknown>).default_video_model as
          | string
          | undefined)
      : undefined;
  const overrideId = data.videoModelId as string | undefined;
  const resolvedId = overrideId ?? projectDefault ?? defaultModelId ?? "flow-default";
  const model = models.find((m) => m.model_id === resolvedId) ?? models[0];

  if (loadError) {
    return (
      <div className="video-settings video-settings--error">
        Couldn't load video models: {loadError}
      </div>
    );
  }
  if (!loaded || !model) {
    return <div className="video-settings video-settings--loading">Loading models…</div>;
  }

  const caps = model.capabilities;
  const lastFrame = (data.last_frame_asset_id as string | undefined) ?? "";
  // Default 5s when the model allows it (both Seedance tiers do), else the
  // model's first allowed value.
  const durationDefault = caps.durations.includes(5) ? 5 : caps.durations[0];
  const duration = (data.duration_seconds as number | undefined) ?? durationDefault;
  // Phase 8.1.5c: render a slider when durations form a contiguous 1s range
  // (Seedance 2.0 = 4..15, 2.5 = 4..30); otherwise keep the discrete dropdown.
  const durSorted = [...caps.durations].sort((a, b) => a - b);
  const durIsRange =
    durSorted.length > 1 &&
    durSorted[durSorted.length - 1] - durSorted[0] + 1 === durSorted.length;
  // Default to 720p when the model offers it (matches the Seedance default),
  // else the model's first allowed value — keeps new nodes off 480p/4k.
  const resolution =
    (data.resolution as string | undefined) ??
    (caps.resolutions.includes("720p") ? "720p" : caps.resolutions[0]);
  const generateAudio =
    typeof data.generate_audio === "boolean" ? (data.generate_audio as boolean) : true;
  // Seedance 2.5 (Avis, 20 Aug 2026). Defaults match the provider's: mp4 is the
  // container everything plays, and `auto` lets Avis pick the subtask so an
  // ordinary reference generation needs no decision from the artist.
  const outputFormat =
    (data.output_format as string | undefined) ?? caps.output_formats?.[0] ?? "mp4";
  const omniTask = (data.omni_reference_task_type as string | undefined) ?? "auto";
  // Person-driven (KYC): when on, the wired image/audio/video refs are sent as
  // identity-verified KYC assets (portrait→video / lip-sync / video-reference).
  const kycMode = typeof data.kycMode === "boolean" ? (data.kycMode as boolean) : false;
  const contentFilterDisabled =
    typeof data.contentFilterDisabled === "boolean"
      ? (data.contentFilterDisabled as boolean)
      : false;

  function persist(patch: Record<string, unknown>) {
    updateNodeData(rfId, patch);
    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) {
      patchNode(dbId, { data: patch }).catch(() => {});
    }
  }

  return (
    <div className="video-settings">
      <label className="video-settings-row">
        <span className="video-settings-label">Model</span>
        <select
          value={resolvedId}
          onChange={(e) => persist({ videoModelId: e.target.value })}
          className="video-settings-select"
        >
          {models.map((m) => (
            <option key={m.model_id} value={m.model_id}>
              {m.display_name}
            </option>
          ))}
        </select>
        {!overrideId && projectDefault && projectDefault === resolvedId ? (
          <span className="video-settings-hint">project default</span>
        ) : null}
        {!overrideId && !projectDefault ? (
          <span className="video-settings-hint">system default</span>
        ) : null}
      </label>

      {/* Same reasoning as the aspect-ratio chips in GenerationDialog: an edit
          sends duration=-1 and takes the length from the source clip, so a
          live slider here would set a number that is never used. Show the
          value as derived instead of pretending it is a choice. */}
      <div className={`video-settings-row${omniTask === "edit" ? " video-settings-row--disabled" : ""}`}>
        <label className="video-settings-label" htmlFor={`vs-dur-${rfId}`}>
          {omniTask === "edit"
            ? "Duration: from source"
            : `Duration${durIsRange ? `: ${duration}s` : ""}`}
        </label>
        {durIsRange ? (
          <input
            id={`vs-dur-${rfId}`}
            type="range"
            min={durSorted[0]}
            max={durSorted[durSorted.length - 1]}
            step={1}
            value={duration}
            onChange={(e) => persist({ duration_seconds: parseInt(e.target.value, 10) })}
            disabled={omniTask === "edit"}
            className="video-settings-slider"
            aria-label={`Duration ${duration} seconds`}
          />
        ) : (
          <select
            id={`vs-dur-${rfId}`}
            value={duration}
            onChange={(e) => persist({ duration_seconds: parseInt(e.target.value, 10) })}
            disabled={omniTask === "edit"}
            className="video-settings-select"
          >
            {caps.durations.map((d) => (
              <option key={d} value={d}>
                {d}s
              </option>
            ))}
          </select>
        )}


        <label className="video-settings-label" htmlFor={`vs-res-${rfId}`}>
          Resolution
        </label>
        <select
          id={`vs-res-${rfId}`}
          value={resolution}
          onChange={(e) => persist({ resolution: e.target.value })}
          className="video-settings-select"
        >
          {caps.resolutions.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </div>

      {/* ── Seedance 2.5 only (Avis, 20 Aug 2026) ─────────────────────────
          Both are rendered ONLY when the model advertises them, because Avis
          returns 400 — 'Model "…" does not support: outputFormat' — rather than
          ignoring an unknown field. A control that is always visible would be a
          control that breaks every generation on 2.0. */}
      {caps.output_formats && caps.output_formats.length > 1 ? (
        <div className="video-settings-row">
          <label className="video-settings-label" htmlFor={`vs-fmt-${rfId}`}>
            Format
          </label>
          <select
            id={`vs-fmt-${rfId}`}
            value={outputFormat}
            onChange={(e) => persist({ output_format: e.target.value })}
            className="video-settings-select"
          >
            {caps.output_formats.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
          <span className="video-settings-hint">
            {outputFormat === "mov" ? "higher colour precision" : "plays anywhere"}
          </span>
        </div>
      ) : null}

      {caps.supports_omni_reference ? (
        <div className="video-settings-row">
          <label className="video-settings-label" htmlFor={`vs-omni-${rfId}`}>
            Task
          </label>
          <select
            id={`vs-omni-${rfId}`}
            value={omniTask}
            onChange={(e) => persist({ omni_reference_task_type: e.target.value })}
            className="video-settings-select"
          >
            <option value="auto">Auto — let Avis decide</option>
            <option value="reference">Reference → video</option>
            <option value="edit">Edit an existing clip</option>
            <option value="extend">Extend an existing clip</option>
          </select>
        </div>
      ) : null}

      {/* Say the constraint where the choice is made. Both rules come from the
          provider: edit/extend operate ON a clip, so they need one attached and
          the output follows its shape — and the whole point of naming the
          subtask is to fail early, which only helps if the person is told what
          "early" means. */}
      {caps.supports_omni_reference && omniTask === "edit" ? (
        <div className="video-settings-row video-settings-note">
          <span className="video-settings-hint">
            Editing rewrites a <strong>reference video</strong> (4–30s) wired
            into this node. The output keeps that clip's{" "}
            <strong>length and shape</strong> — Duration below is ignored, and
            the aspect ratio follows the source.
          </span>
        </div>
      ) : null}

      {caps.supports_omni_reference && omniTask === "extend" ? (
        <div className="video-settings-row video-settings-note">
          <span className="video-settings-hint">
            Extending needs a <strong>reference video</strong> (4–30s) wired
            into this node, and the aspect ratio is forced to{" "}
            <strong>adaptive</strong> so the output keeps that clip's shape.
            <strong> Duration</strong> is the length of the result.
          </span>
        </div>
      ) : null}

      {/* The other half of the same lesson, learned from a refused generation:
          a lone reference image is sent as a START FRAME, and Avis refuses the
          subtask hint on those outright. */}
      {caps.supports_omni_reference && omniTask === "reference" ? (
        <div className="video-settings-row video-settings-note">
          <span className="video-settings-hint">
            Needs <strong>two or more</strong> reference images, or a reference
            video — a single image is treated as a start frame.
          </span>
        </div>
      ) : null}

      {/* Estimated cost before generating (calibrated to real Avis usdCost;
          the exact charge is settled after the clip finishes). */}
      {/* An edit bills by the SOURCE clip's length, which this dialog has no
          way to know — the slider's value is not it. Quoting a total from the
          slider understated a 28s source at 720p as $0.90 against roughly $5,
          so quote the rate instead and say what multiplies it. A wrong number
          is worse than an honest formula. */}
      <div className="video-settings-row" style={{ alignItems: "baseline" }}>
        <span className="video-settings-label">Est. cost</span>
        {omniTask === "edit" ? (
          <>
            <span style={{ fontWeight: 600 }}>
              ≈ ${(USD_PER_SEC[resolution] ?? 0.42).toFixed(2)}/s
            </span>
            <span className="video-settings-hint">
              × the reference video's length
            </span>
          </>
        ) : (
          <>
            <span style={{ fontWeight: 600 }}>
              ≈ ${estimateVideoUsd(duration, resolution).toFixed(2)}
            </span>
            <span className="video-settings-hint">final billed after gen</span>
          </>
        )}
      </div>

      {/* Phase 8.1.5d: the legacy manual multi-ref editor (media_id / URL
          text input) was removed — references are now managed in the
          dialog's VideoRefsPanel (canvas ref nodes + "+ Add custom image"),
          the single source of truth. See generation.ts dispatch. */}

      <div className="video-settings-row">
        <label className="video-settings-label" htmlFor={`vs-lf-${rfId}`}>
          Last frame
        </label>
        <input
          id={`vs-lf-${rfId}`}
          type="text"
          placeholder={
            caps.supports_last_frame
              ? "asset id or public URL (optional)"
              : "Disabled — model has no keyframe interpolation"
          }
          value={lastFrame}
          disabled={!caps.supports_last_frame}
          onChange={(e) => persist({ last_frame_asset_id: e.target.value })}
          className="video-settings-input"
        />
      </div>

      {caps.supports_audio_toggle ? (
        <label className="video-settings-row video-settings-row--toggle">
          <input
            type="checkbox"
            checked={generateAudio}
            onChange={(e) => persist({ generate_audio: e.target.checked })}
          />
          <span>Generate audio track</span>
        </label>
      ) : null}

      {caps.supports_kyc ? (
        <label className="video-settings-row video-settings-row--toggle">
          <input
            type="checkbox"
            checked={kycMode}
            onChange={(e) => persist({ kycMode: e.target.checked })}
          />
          <span>
            Real person (KYC) — portrait→video / lip-sync
            <span className="video-settings-hint"> needs KYC + a real-person photo</span>
          </span>
        </label>
      ) : null}

      {caps.supports_b2b_unmoderated ? (
        <label className="video-settings-row video-settings-row--toggle">
          <input
            type="checkbox"
            checked={contentFilterDisabled}
            onChange={(e) => persist({ contentFilterDisabled: e.target.checked })}
          />
          <span>
            Skip content filter (B2B)
            <span className="video-settings-hint">
              {" "}
              DanceSee B2B account required
              {contentFilterDisabled
                ? " · explicit refs: also enable Real person (KYC) · output kept ~3h"
                : ""}
            </span>
          </span>
        </label>
      ) : null}
    </div>
  );
}

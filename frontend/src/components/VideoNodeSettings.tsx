import { useEffect } from "react";

import { useVideoModelsStore } from "../store/videoModels";
import { useShotWorkflowStore } from "../store/shotWorkflow";
import { useProjectStore } from "../store/project";
import { patchNode } from "../api/client";

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
  const refs = Array.isArray(data.reference_image_ids)
    ? (data.reference_image_ids as string[])
    : [];
  const lastFrame = (data.last_frame_asset_id as string | undefined) ?? "";
  const duration = (data.duration_seconds as number | undefined) ?? caps.durations[0];
  const aspect = (data.aspect_ratio as string | undefined) ?? caps.aspect_ratios[0];
  const resolution = (data.resolution as string | undefined) ?? caps.resolutions[0];
  const generateAudio =
    typeof data.generate_audio === "boolean" ? (data.generate_audio as boolean) : true;

  function persist(patch: Record<string, unknown>) {
    updateNodeData(rfId, patch);
    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) {
      patchNode(dbId, { data: patch }).catch(() => {});
    }
  }

  // Persistent warning: user previously attached refs but the current
  // model can't honor them. Don't silent-drop — surface explicitly so
  // the user can act (remove refs OR switch model).
  const refMismatchWarning =
    refs.length > 0 && !caps.supports_multi_ref
      ? `${refs.length} reference image${refs.length === 1 ? "" : "s"} will be ignored on submit — ${model.display_name} is i2v-only. ` +
        "Switch to a multi-ref model, or remove these references."
      : null;

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

      <div className="video-settings-row">
        <label className="video-settings-label" htmlFor={`vs-dur-${rfId}`}>
          Duration
        </label>
        <select
          id={`vs-dur-${rfId}`}
          value={duration}
          onChange={(e) => persist({ duration_seconds: parseInt(e.target.value, 10) })}
          className="video-settings-select"
        >
          {caps.durations.map((d) => (
            <option key={d} value={d}>
              {d}s
            </option>
          ))}
        </select>

        <label className="video-settings-label" htmlFor={`vs-ar-${rfId}`}>
          Aspect
        </label>
        <select
          id={`vs-ar-${rfId}`}
          value={aspect}
          onChange={(e) => persist({ aspect_ratio: e.target.value })}
          className="video-settings-select"
        >
          {caps.aspect_ratios.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>

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

      <div className="video-settings-row">
        <span className="video-settings-label">References (multi-ref)</span>
        {caps.supports_multi_ref ? (
          <span className="video-settings-hint">
            {refs.length}/{caps.max_refs} attached
          </span>
        ) : (
          <span className="video-settings-hint">
            Disabled — {model.display_name} is i2v-only. Switch to a model with multi-ref support.
          </span>
        )}
      </div>

      {refMismatchWarning ? (
        <div role="alert" className="video-settings-warning">
          {refMismatchWarning}
          <button
            type="button"
            className="video-settings-warning-btn"
            onClick={() => persist({ reference_image_ids: [] })}
          >
            Remove all refs
          </button>
        </div>
      ) : null}

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
    </div>
  );
}

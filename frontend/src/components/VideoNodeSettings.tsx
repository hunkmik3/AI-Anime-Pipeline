import { useEffect, useState } from "react";

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

  // Multi-ref editor local state (hooks must precede early returns).
  const [addValue, setAddValue] = useState("");
  const [dragIdx, setDragIdx] = useState<number | null>(null);

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
  const roleHints = Array.isArray(data.reference_role_hints)
    ? (data.reference_role_hints as (string | null)[])
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

  // ── multi-ref mutations — reference_image_ids and reference_role_hints
  //    move in lockstep so @imageN stays aligned with its role hint. ──
  function persistRefs(nextRefs: string[], nextHints: (string | null)[]) {
    persist({
      reference_image_ids: nextRefs,
      reference_role_hints: nextHints.slice(0, nextRefs.length),
    });
  }

  function addRef() {
    const v = addValue.trim();
    if (!v) return;
    if (refs.includes(v)) {
      setAddValue("");
      return;
    }
    if (refs.length >= caps.max_refs) return;
    persistRefs([...refs, v], [...roleHints, null]);
    setAddValue("");
  }

  function removeRef(i: number) {
    persistRefs(
      refs.filter((_, idx) => idx !== i),
      roleHints.filter((_, idx) => idx !== i),
    );
  }

  function setHint(i: number, value: string) {
    const next = [...roleHints];
    while (next.length < refs.length) next.push(null);
    next[i] = value || null;
    persist({ reference_role_hints: next });
  }

  function moveRef(from: number, to: number) {
    if (from === to || from < 0 || to < 0 || from >= refs.length || to >= refs.length) return;
    const nextRefs = [...refs];
    const nextHints = [...roleHints];
    while (nextHints.length < refs.length) nextHints.push(null);
    const [movedRef] = nextRefs.splice(from, 1);
    const [movedHint] = nextHints.splice(from, 1);
    nextRefs.splice(to, 0, movedRef);
    nextHints.splice(to, 0, movedHint ?? null);
    persistRefs(nextRefs, nextHints);
  }

  // media_id → local route; a pasted public URL is used as-is.
  const thumbSrc = (id: string) => (/^https?:\/\//.test(id) ? id : `/media/${id}`);
  const ROLE_HINTS = ["", "character", "environment", "spatial", "motion"];

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

      <div className="video-settings-row video-settings-row--refs">
        <span className="video-settings-label">References (multi-ref)</span>
        {caps.supports_multi_ref ? (
          <span className="video-settings-hint">
            {refs.length}/{caps.max_refs} attached · order = @image1…N
          </span>
        ) : (
          <span className="video-settings-hint">
            Disabled — {model.display_name} is i2v-only. Switch to a model with multi-ref support.
          </span>
        )}
      </div>

      {caps.supports_multi_ref ? (
        <>
          <ul className="video-refs-list">
            {refs.map((ref, i) => (
              <li
                key={`${ref}-${i}`}
                className={`video-refs-item${dragIdx === i ? " video-refs-item--dragging" : ""}`}
                draggable
                onDragStart={() => setDragIdx(i)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault();
                  if (dragIdx !== null) moveRef(dragIdx, i);
                  setDragIdx(null);
                }}
                onDragEnd={() => setDragIdx(null)}
              >
                <span className="video-refs-grip" aria-hidden>⋮⋮</span>
                <span className="video-refs-badge">@image{i + 1}</span>
                <img className="video-refs-thumb" src={thumbSrc(ref)} alt={`@image${i + 1}`} />
                <select
                  className="video-refs-role"
                  value={roleHints[i] ?? ""}
                  title="Role hint — UI/synthesis only, not sent to the Dreamina API"
                  onChange={(e) => setHint(i, e.target.value)}
                >
                  {ROLE_HINTS.map((h) => (
                    <option key={h || "none"} value={h}>
                      {h ? h : "role hint…"}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="video-refs-remove"
                  aria-label={`Remove @image${i + 1}`}
                  onClick={() => removeRef(i)}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>

          {refs.length < caps.max_refs ? (
            <div className="video-settings-row video-refs-add">
              <input
                type="text"
                className="video-settings-input"
                placeholder="media_id or public image URL"
                value={addValue}
                onChange={(e) => setAddValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addRef();
                  }
                }}
              />
              <button
                type="button"
                className="video-settings-warning-btn"
                disabled={!addValue.trim()}
                onClick={addRef}
              >
                Add reference
              </button>
            </div>
          ) : (
            <span className="video-settings-hint">Max references reached for this model.</span>
          )}
          <p className="video-settings-hint video-refs-note">
            Role hints are UI-only — the Dreamina API accepts only
            <code> reference_image</code>; hints feed prompt synthesis (Phase 6),
            not the submit. Drag to reorder → changes @imageN binding.
          </p>
        </>
      ) : null}

      {refMismatchWarning ? (
        <div role="alert" className="video-settings-warning">
          {refMismatchWarning}
          <button
            type="button"
            className="video-settings-warning-btn"
            onClick={() => persist({ reference_image_ids: [], reference_role_hints: [] })}
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

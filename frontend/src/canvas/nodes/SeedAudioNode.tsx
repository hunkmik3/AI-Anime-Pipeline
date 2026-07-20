import { useEffect, useRef, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import {
  generateSeedAudio,
  patchNode,
  uploadAudio,
  uploadImage,
  type SeedAudioParams,
} from "../../api/client";
import { useGenerationStore } from "../../store/generation";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";
import { RefLabelFields } from "./shared/RefLabelFields";

/**
 * SeedAudioNode — BytePlus Seed Audio 1.0 (all-in-one audio scene).
 *
 * Type a scene prompt (narration + "dialogue" + voice traits + music/SFX cues)
 * → one mixed clip. The result lands in `audioMediaId`, so the node plays/
 * downloads it and can feed a VideoNode's @audio like an AudioRefNode.
 *
 * Interactive controls carry `nodrag` (React Flow convention) so dragging a
 * slider/input adjusts the control instead of moving the whole node.
 */
const PROMPT_MAX = 2048;
const FORMATS = ["mp3", "wav", "pcm", "ogg_opus"] as const;
const SAMPLE_RATES = [8000, 16000, 24000, 32000, 44100, 48000] as const;

function shortRef(r: string): string {
  return r.startsWith("http") ? (r.length > 26 ? r.slice(0, 26) + "…" : r) : "🔊 uploaded";
}

function SeedAudioBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const audioMediaId = data.audioMediaId;
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [urlDraft, setUrlDraft] = useState("");
  const [imgDraft, setImgDraft] = useState("");
  const audioFileRef = useRef<HTMLInputElement>(null);
  const imgFileRef = useRef<HTMLInputElement>(null);

  const prompt = data.seedPrompt ?? "";
  const fmt = data.seedFormat ?? "mp3";
  const sr = data.seedSampleRate ?? 24000;
  const speed = data.seedSpeechRate ?? 0;
  const volume = data.seedLoudnessRate ?? 0;
  const pitch = data.seedPitchRate ?? 0;
  const refs = data.seedAudioRefs ?? [];
  const imageRef = data.seedImageRef ?? "";
  const imageSet = !!imageRef.trim();

  // Sliders track a LOCAL value while dragging (smooth, no re-render churn);
  // the store + network write happens once on release. Writing to the store on
  // every tick makes SceneCanvas rebuild all nodes → the thumb flickers.
  const [rates, setRates] = useState({ speech: speed, loud: volume, pitch });
  useEffect(() => {
    setRates({ speech: speed, loud: volume, pitch });
  }, [speed, volume, pitch]);

  function persist(patch: Partial<FlowboardNodeData>) {
    useShotWorkflowStore.getState().updateNodeData(rfId, patch);
    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) patchNode(dbId, { data: patch }).catch(() => {});
  }

  function addRef(value: string) {
    const v = value.trim();
    const cur = data.seedAudioRefs ?? [];
    if (!v || cur.length >= 3 || cur.includes(v)) return;
    persist({ seedAudioRefs: [...cur, v] });
    setUrlDraft("");
    setAddOpen(false);
  }

  function removeRef(i: number) {
    const cur = [...(data.seedAudioRefs ?? [])];
    cur.splice(i, 1);
    persist({ seedAudioRefs: cur });
  }

  async function upload(file: File, kind: "audio" | "image") {
    setError(null);
    setUploading(true);
    try {
      const projectId = await useGenerationStore.getState().ensureProjectId();
      if (!projectId) {
        setError("no project");
        return;
      }
      const dbId = parseInt(rfId, 10);
      const nid = isNaN(dbId) ? undefined : dbId;
      if (kind === "audio") {
        const resp = await uploadAudio(file, projectId, nid);
        addRef(resp.media_id);
      } else {
        const resp = await uploadImage(file, projectId, nid);
        persist({ seedImageRef: resp.media_id });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function generate() {
    if (!prompt.trim()) {
      setError("Enter a prompt first");
      return;
    }
    setError(null);
    setBusy(true);
    persist({ status: "running" });
    try {
      const dbId = parseInt(rfId, 10);
      const body: SeedAudioParams = {
        prompt: prompt.trim(),
        format: fmt,
        sample_rate: sr,
        speech_rate: rates.speech,
        loudness_rate: rates.loud,
        pitch_rate: rates.pitch,
        node_id: isNaN(dbId) ? undefined : dbId,
      };
      if (imageSet) body.image_ref = imageRef.trim();
      else if (refs.length) body.references = refs;

      const res = await generateSeedAudio(body);
      persist({
        audioMediaId: res.media_id,
        audioMime: res.mime,
        seedDuration: res.duration ?? undefined,
        status: "done",
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "generation failed");
      persist({ status: "error" });
    } finally {
      setBusy(false);
    }
  }

  const overLimit = prompt.length > PROMPT_MAX;

  return (
    <div className="node-body node-body--audio-ref">
      {/* Prompt */}
      <textarea
        className="audio-ref__desc-input nodrag nowheel"
        style={{ minHeight: 66, resize: "vertical", width: "100%" }}
        value={prompt}
        placeholder="Describe the scene — narration, &quot;dialogue&quot;, voice traits (in parentheses), music &amp; SFX…"
        maxLength={PROMPT_MAX + 200}
        onChange={(e) => persist({ seedPrompt: e.target.value })}
      />
      <div
        className="video-settings-hint"
        style={{ textAlign: "right", color: overLimit ? "#e06c6c" : undefined }}
      >
        {prompt.length}/{PROMPT_MAX}
      </div>

      {/* Result: player full-width, a small right-aligned download pill below */}
      {audioMediaId ? (
        <div style={{ marginBottom: 6 }}>
          <audio
            className="audio-ref__player nodrag"
            controls
            src={`/media/${audioMediaId}`}
            style={{ width: "100%", display: "block" }}
          />
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 4 }}>
            <a
              className="nodrag"
              href={`/media/${audioMediaId}`}
              download
              title="Download audio"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                padding: "2px 8px",
                fontSize: 11,
                borderRadius: 6,
                textDecoration: "none",
                color: "var(--text, #ddd)",
                background: "rgba(255,255,255,0.06)",
                border: "1px solid rgba(255,255,255,0.14)",
              }}
            >
              ⬇︎ Download
            </a>
          </div>
        </div>
      ) : null}

      {/* Format + sample rate */}
      <div className="video-settings-row">
        <label className="video-settings-label">Format</label>
        <select
          className="video-settings-select nodrag"
          value={fmt}
          onChange={(e) => persist({ seedFormat: e.target.value })}
        >
          {FORMATS.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
        <label className="video-settings-label">Rate</label>
        <select
          className="video-settings-select nodrag"
          value={sr}
          onChange={(e) => persist({ seedSampleRate: parseInt(e.target.value, 10) })}
        >
          {SAMPLE_RATES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      <button
        type="button"
        className="audio-ref__action nodrag"
        style={{ marginTop: 4 }}
        onClick={() => setShowAdvanced((v) => !v)}
      >
        {showAdvanced ? "Less ▲" : "More settings ▾"}
      </button>

      {showAdvanced ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
          {/* Speed / Volume / Pitch. nodrag = the thumb doesn't move the node.
              onChange updates ONLY local state (smooth); the store/network write
              fires on release (pointer-up / key-up) to avoid flicker. */}
          {[
            {
              label: "Speed", val: rates.speech, min: -50, max: 100,
              onLocal: (n: number) => setRates((r) => ({ ...r, speech: n })),
              onCommit: (n: number) => persist({ seedSpeechRate: n }),
            },
            {
              label: "Volume", val: rates.loud, min: -50, max: 100,
              onLocal: (n: number) => setRates((r) => ({ ...r, loud: n })),
              onCommit: (n: number) => persist({ seedLoudnessRate: n }),
            },
            {
              label: "Pitch", val: rates.pitch, min: -12, max: 12,
              onLocal: (n: number) => setRates((r) => ({ ...r, pitch: n })),
              onCommit: (n: number) => persist({ seedPitchRate: n }),
            },
          ].map((s) => (
            <div className="video-settings-row" key={s.label}>
              <label className="video-settings-label">{s.label}: {s.val}</label>
              <input
                type="range"
                className="video-settings-slider nodrag"
                min={s.min}
                max={s.max}
                step={1}
                value={s.val}
                onChange={(e) => s.onLocal(parseInt(e.target.value, 10))}
                onPointerUp={(e) => s.onCommit(parseInt((e.target as HTMLInputElement).value, 10))}
                onKeyUp={(e) => s.onCommit(parseInt((e.target as HTMLInputElement).value, 10))}
              />
            </div>
          ))}

          {/* Audio refs (voice clone → @audio1..3): one add-slot, then "Add more" */}
          <div className="video-settings-hint">Audio refs (@audio1–3) — voice clone</div>
          {refs.map((r, i) => (
            <div className="video-settings-row" key={i} style={{ gap: 6 }}>
              <span
                className="video-settings-label"
                style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                title={r}
              >
                @audio{i + 1}: {shortRef(r)}
              </span>
              <button type="button" className="audio-ref__action nodrag" onClick={() => removeRef(i)} title="Remove">✕</button>
            </div>
          ))}
          {!imageSet && refs.length < 3 ? (
            addOpen || refs.length === 0 ? (
              <div className="video-settings-row" style={{ gap: 6 }}>
                <input
                  className="video-settings-input nodrag"
                  placeholder={`@audio${refs.length + 1} URL (wav/mp3)`}
                  value={urlDraft}
                  onChange={(e) => setUrlDraft(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") addRef(urlDraft); }}
                />
                {urlDraft.trim() ? (
                  <button type="button" className="audio-ref__action nodrag" onClick={() => addRef(urlDraft)}>Add</button>
                ) : (
                  <button type="button" className="audio-ref__action nodrag" onClick={() => audioFileRef.current?.click()} disabled={uploading}>
                    {uploading ? "…" : "⬆ Upload"}
                  </button>
                )}
              </div>
            ) : (
              <button type="button" className="audio-ref__action nodrag" onClick={() => setAddOpen(true)}>
                ＋ Add more ({refs.length}/3)
              </button>
            )
          ) : null}

          {/* Image ref (mood) — mutually exclusive with audio refs */}
          <div className="video-settings-hint">or Image ref (mood)</div>
          {imageSet ? (
            <div className="video-settings-row" style={{ gap: 6 }}>
              <span className="video-settings-label" style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={imageRef}>
                {shortRef(imageRef).replace("🔊", "🖼")}
              </span>
              <button type="button" className="audio-ref__action nodrag" onClick={() => persist({ seedImageRef: "" })} title="Remove">✕</button>
            </div>
          ) : refs.length === 0 ? (
            <div className="video-settings-row" style={{ gap: 6 }}>
              <input
                className="video-settings-input nodrag"
                placeholder="Image URL"
                value={imgDraft}
                onChange={(e) => setImgDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && imgDraft.trim()) { persist({ seedImageRef: imgDraft.trim() }); setImgDraft(""); } }}
              />
              <button type="button" className="audio-ref__action nodrag" onClick={() => imgFileRef.current?.click()} disabled={uploading}>
                {uploading ? "…" : "Choose"}
              </button>
            </div>
          ) : (
            <div className="video-settings-hint" style={{ opacity: 0.6 }}>(remove audio refs to use an image)</div>
          )}
        </div>
      ) : null}

      {/* @audio label so it can bind in a downstream VideoNode prompt */}
      {audioMediaId ? (
        <RefLabelFields rfId={rfId} data={data} labelPlaceholder="@audio1" />
      ) : null}

      <button
        type="button"
        className="audio-ref__action nodrag"
        style={{ marginTop: 6, width: "100%" }}
        onClick={generate}
        disabled={busy || overLimit || !prompt.trim()}
      >
        {busy ? "Generating…" : audioMediaId ? "Regenerate" : "Generate audio"}
      </button>

      {data.seedDuration ? (
        <div className="video-settings-hint">Last take: {data.seedDuration.toFixed(1)}s</div>
      ) : null}
      {error && <p className="audio-ref__error">{error}</p>}

      {/* hidden file pickers */}
      <input
        ref={audioFileRef}
        type="file"
        accept="audio/mpeg,audio/wav,audio/x-wav,.mp3,.wav"
        style={{ display: "none" }}
        onChange={(e) => { const f = e.target.files?.[0]; if (f) void upload(f, "audio"); e.target.value = ""; }}
      />
      <input
        ref={imgFileRef}
        type="file"
        accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
        style={{ display: "none" }}
        onChange={(e) => { const f = e.target.files?.[0]; if (f) void upload(f, "image"); e.target.value = ""; }}
      />
    </div>
  );
}

export function SeedAudioNode(props: NodeProps<FlowNode>) {
  return (
    <BaseNodeShell
      data={props.data}
      selected={props.selected ?? false}
      showTargetHandle={false}
    >
      <SeedAudioBody rfId={props.id} data={props.data} />
    </BaseNodeShell>
  );
}

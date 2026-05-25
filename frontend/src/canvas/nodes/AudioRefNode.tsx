import { useEffect, useRef, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import { patchNode, uploadAudio } from "../../api/client";
import { useGenerationStore } from "../../store/generation";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";

/**
 * AudioRefNode (Phase 7) — 5th anime node type.
 *
 * Uploads a voice/audio reference (mp3/wav/…) for Seedance 2.0's
 * `reference_audio` r2v+audio mode. Pattern mirrors CharacterNode /
 * VisualAssetNode: upload → local cache + Asset(kind=audio) → media_id.
 *
 * Output: the audio media_id is passed downstream to a connected VideoNode
 * (root node, no inputs). The capability gate lives on the VideoNode side —
 * audio is only honored when the resolved model has `supports_audio_ref`
 * (Seedance 2.0); on other models the worker drops it with a warning.
 */
function AudioRefBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const audioMediaId = data.audioMediaId;
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingDesc, setEditingDesc] = useState(false);
  const [descDraft, setDescDraft] = useState(data.voiceDescription ?? "");
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDescDraft(data.voiceDescription ?? "");
  }, [data.voiceDescription]);

  function persist(patch: Partial<FlowboardNodeData>) {
    useShotWorkflowStore.getState().updateNodeData(rfId, patch);
    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) {
      patchNode(dbId, { data: patch }).catch(() => {});
    }
  }

  async function upload(file: File) {
    setError(null);
    setUploading(true);
    try {
      const projectId = await useGenerationStore.getState().ensureProjectId();
      if (!projectId) {
        setError("no project");
        return;
      }
      const dbId = parseInt(rfId, 10);
      const resp = await uploadAudio(file, projectId, isNaN(dbId) ? undefined : dbId);
      persist({ audioMediaId: resp.media_id, audioMime: resp.mime, status: "done" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "audio upload failed");
    } finally {
      setUploading(false);
    }
  }

  function saveDesc() {
    if (descDraft !== (data.voiceDescription ?? "")) {
      persist({ voiceDescription: descDraft });
    }
    setEditingDesc(false);
  }

  return (
    <div className="node-body node-body--audio-ref">
      {audioMediaId ? (
        <div className="audio-ref__loaded">
          <audio
            className="audio-ref__player"
            controls
            src={`/media/${audioMediaId}`}
          />
          <button
            type="button"
            className="audio-ref__action"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            {uploading ? "Uploading…" : "Replace"}
          </button>
        </div>
      ) : (
        <div className="audio-ref__empty">
          <button
            type="button"
            className="audio-ref__action"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            {uploading ? "Uploading…" : "Upload audio (mp3/wav)"}
          </button>
        </div>
      )}

      {editingDesc ? (
        <input
          className="audio-ref__desc-input"
          value={descDraft}
          autoFocus
          placeholder='Voice label — e.g. "Police Officer 1 — formal authority"'
          onChange={(e) => setDescDraft(e.target.value)}
          onBlur={saveDesc}
          onKeyDown={(e) => {
            if (e.key === "Enter") saveDesc();
            if (e.key === "Escape") setEditingDesc(false);
          }}
        />
      ) : (
        <div
          className="audio-ref__desc"
          onDoubleClick={() => setEditingDesc(true)}
          title="Double-click to edit voice description"
        >
          {data.voiceDescription || "Double-click to add a voice description…"}
        </div>
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept="audio/mpeg,audio/wav,audio/x-wav,audio/mp4,audio/aac,audio/ogg,.mp3,.wav,.m4a,.aac,.ogg"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) upload(f);
          e.target.value = "";
        }}
      />
      {error && <p className="audio-ref__error">{error}</p>}
    </div>
  );
}

export function AudioRefNode(props: NodeProps<FlowNode>) {
  return (
    <BaseNodeShell
      data={props.data}
      selected={props.selected ?? false}
      showTargetHandle={false}
    >
      <AudioRefBody rfId={props.id} data={props.data} />
    </BaseNodeShell>
  );
}

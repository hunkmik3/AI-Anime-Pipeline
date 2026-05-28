import { useEffect, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import {
  getProjectBible,
  patchNode,
} from "../../api/client";
import { useProjectStore } from "../../store/project";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";

function formatProjectBible(bible: Record<string, unknown>): string {
  const lines: string[] = [];
  if (typeof bible.art_style === "string" && bible.art_style.trim()) {
    lines.push(`Art style: ${bible.art_style.trim()}`);
  }
  if (Array.isArray(bible.color_palette) && bible.color_palette.length > 0) {
    lines.push(`Palette: ${bible.color_palette.join(", ")}`);
  }
  if (typeof bible.line_style === "string" && bible.line_style.trim()) {
    lines.push(`Line: ${bible.line_style.trim()}`);
  }
  if (typeof bible.lighting_conventions === "string" && bible.lighting_conventions.trim()) {
    lines.push(`Lighting: ${bible.lighting_conventions.trim()}`);
  }
  if (
    Array.isArray(bible.negative_prompts) && bible.negative_prompts.length > 0
  ) {
    lines.push(`Negative: ${bible.negative_prompts.join(", ")}`);
  }
  return lines.join("\n") || "(Project Bible empty)";
}

function BibleRefBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const bibleType = data.bibleType ?? "project";
  const [loading, setLoading] = useState(false);
  const [text, setText] = useState(data.bibleText ?? "");
  const [error, setError] = useState<string | null>(null);

  const projectId = useProjectStore((s) => s.currentProjectId);

  async function reload(type: "project" | "scene") {
    setLoading(true);
    setError(null);
    try {
      let formatted = "";
      if (type === "project") {
        if (!projectId) {
          setError("no project");
          return;
        }
        const b = await getProjectBible(projectId);
        formatted = formatProjectBible(b as Record<string, unknown>);
      } else {
        // Phase 8.3: Scene Bible was removed. Keep the node type for
        // backward compat but surface that there's no scene bible anymore.
        formatted = "(Scene Bible removed in Phase 8.3 — use Project Bible)";
      }
      setText(formatted);
      useShotWorkflowStore.getState().updateNodeData(rfId, {
        bibleType: type,
        bibleText: formatted,
      });
      const dbId = parseInt(rfId, 10);
      if (!isNaN(dbId)) {
        patchNode(dbId, {
          data: { bibleType: type, bibleText: formatted },
        }).catch(() => {});
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // First render: if data.bibleText is empty, auto-load. Otherwise show
    // the persisted snapshot (so reload doesn't churn on every mount).
    if (!data.bibleText) {
      void reload(bibleType);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onTypeChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const next = e.target.value as "project" | "scene";
    void reload(next);
  }

  return (
    <div className="node-body node-body--prompt">
      <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
        <select
          className="visual-asset__action"
          value={bibleType}
          onChange={onTypeChange}
          style={{ flex: 1 }}
          aria-label="Bible source"
        >
          <option value="project">Project Bible</option>
          <option value="scene">Scene Bible</option>
        </select>
        <button
          type="button"
          className="visual-asset__action"
          onClick={() => reload(bibleType)}
          disabled={loading}
          title="Reload from API"
        >
          {loading ? "…" : "↻"}
        </button>
      </div>
      <pre className="prompt-text" style={{ maxHeight: 160, overflow: "auto" }}>
        {text || "(no bible loaded)"}
      </pre>
      {error && <p className="visual-asset__error" role="alert">{error}</p>}
    </div>
  );
}

export function BibleRefNode(props: NodeProps<FlowNode>) {
  return (
    <BaseNodeShell
      data={props.data}
      selected={props.selected ?? false}
      showTargetHandle={false}
    >
      <BibleRefBody rfId={props.id} data={props.data} />
    </BaseNodeShell>
  );
}

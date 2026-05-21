import { Handle, Position } from "@xyflow/react";
import type { ReactNode } from "react";

import type { FlowboardNodeData } from "../../store/shotWorkflow";
import { ICON, STATUS_COLOR } from "./shared/statusColors";
import { isLLMBusy } from "./shared/llmBusy";

function StatusStrip({ status }: { status?: string }) {
  const color = STATUS_COLOR[status ?? "idle"] ?? "transparent";
  const isRunning = status === "running";
  return (
    <div
      className={isRunning ? "status-strip status-strip--running" : "status-strip"}
      style={{ background: color }}
    />
  );
}

export interface BaseNodeShellProps {
  data: FlowboardNodeData;
  selected: boolean;
  /** Render a target (left) handle. Defaults to true. */
  showTargetHandle?: boolean;
  /** Render a source (right) handle. Defaults to true. */
  showSourceHandle?: boolean;
  /** Modifier class added to .node-card (e.g. "note" → .node-card--note). */
  variant?: string;
  isGenerable?: boolean;
  isDownloadable?: boolean;
  onGenerate?: () => void;
  onDownload?: () => void;
  /** Extra header chip, rendered after the title. */
  extraHeader?: ReactNode;
  children: ReactNode;
}

export function BaseNodeShell({
  data,
  selected,
  showTargetHandle = true,
  showSourceHandle = true,
  variant,
  isGenerable,
  isDownloadable,
  onGenerate,
  onDownload,
  extraHeader,
  children,
}: BaseNodeShellProps) {
  const isRunning = data.status === "running";
  const llmBusy = isLLMBusy(data);

  return (
    <div
      className={`node-card${variant ? ` node-card--${variant}` : ""}${
        selected ? " node-card--selected" : ""
      }${llmBusy ? " node-card--llm-busy" : ""}`}
    >
      <StatusStrip status={data.status} />
      {showTargetHandle && (
        <Handle type="target" position={Position.Left} className="node-handle" />
      )}

      <div className="node-header">
        <span className="node-icon" aria-hidden="true">{ICON[data.type] ?? "□"}</span>
        <span className="node-title">{data.title}</span>
        {llmBusy && (
          <span className="node-header__llm-pill" aria-live="polite">
            <span className="node-header__llm-spinner" aria-hidden="true" />
            {data.autoPromptStatus === "pending" ? "Composing…" : "Analyzing…"}
          </span>
        )}
        {extraHeader}
        <div className="node-header__actions">
          {isDownloadable && onDownload && (
            <button
              className="node-header__btn"
              onClick={(e) => {
                e.stopPropagation();
                onDownload();
              }}
              aria-label="Download media"
              title="Download"
              tabIndex={0}
            >
              ⬇
            </button>
          )}
          {isGenerable && onGenerate && (
            <button
              className={`node-header__btn${isRunning ? " node-header__btn--running" : ""}`}
              onClick={(e) => {
                e.stopPropagation();
                if (llmBusy) return;
                onGenerate();
              }}
              aria-label="Generate from this node"
              title={llmBusy ? "Backend is still composing — try again in a moment" : "Generate"}
              tabIndex={0}
              disabled={llmBusy}
            >
              ▶
            </button>
          )}
        </div>
        <span className="node-short-id">#{data.shortId}</span>
      </div>

      {children}

      {showSourceHandle && (
        <Handle type="source" position={Position.Right} className="node-handle" />
      )}
    </div>
  );
}

export function downloadExt(type: string): string {
  if (type === "video") return "mp4";
  return "png";
}

import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ReactFlowProvider } from "@xyflow/react";

import { ShotCanvas } from "../canvas/ShotCanvas";
import { AddNodePalette } from "../canvas/AddNodePalette";
import { ReferencesPanel } from "../components/ReferencesPanel";
import { StatusBar } from "../components/StatusBar";

import { useProjectStore } from "../store/project";
import { useSceneStore } from "../store/scene";
import { useShotStore } from "../store/shot";
import { useShotWorkflowStore } from "../store/shotWorkflow";

/**
 * Shot-level editor — the React Flow canvas plus shot metadata. This is
 * the moral equivalent of pre-Phase-3 ``App.tsx`` wrapping ``<Board/>``,
 * now scoped to a single Shot. The active shot id is read from the URL,
 * and ``useShotWorkflowStore.loadShotWorkflow`` is what actually pulls
 * the canvas nodes/edges. Switching shots in the URL clears the canvas
 * immediately (no stale render of the previous shot's nodes).
 */
export function ShotEditor() {
  const { shotId } = useParams<{ shotId: string }>();
  const wfShotId = useShotWorkflowStore((s) => s.shotId);
  const loading = useShotWorkflowStore((s) => s.loading);
  const loadShotWorkflow = useShotWorkflowStore((s) => s.loadShotWorkflow);
  const clearShot = useShotWorkflowStore((s) => s.clearShot);

  const currentShot = useShotStore((s) => s.currentShot);
  const selectShot = useShotStore((s) => s.selectShot);
  const updateScriptText = useShotStore((s) => s.updateScriptText);
  const runShot = useShotStore((s) => s.runShot);
  const cancelShot = useShotStore((s) => s.cancelShot);

  const currentScene = useSceneStore((s) => s.currentScene);
  const selectScene = useSceneStore((s) => s.selectScene);

  const currentProject = useProjectStore((s) => s.currentProject);
  const selectProject = useProjectStore((s) => s.selectProject);

  const [scriptDraft, setScriptDraft] = useState("");
  const [scriptDirty, setScriptDirty] = useState(false);
  const [scriptSaving, setScriptSaving] = useState(false);
  const generationGate = useRef<string | null>(null);

  useEffect(() => {
    if (!shotId) return;
    // Critical for the "switch mid-generation" smoke test: clear the
    // workflow canvas immediately when the URL changes so the user never
    // sees the previous shot's nodes blink before the new shot loads.
    clearShot();
    void (async () => {
      generationGate.current = shotId;
      await selectShot(shotId);
      const sh = useShotStore.getState().currentShot;
      if (sh) {
        if (sh.scene_id !== useSceneStore.getState().currentSceneId) {
          await selectScene(sh.scene_id);
        }
        const sc = useSceneStore.getState().currentScene;
        if (sc && sc.project_id !== useProjectStore.getState().currentProjectId) {
          await selectProject(sc.project_id);
        }
      }
      // If the user navigated away again while these awaited, bail.
      if (generationGate.current !== shotId) return;
      await loadShotWorkflow(shotId);
    })();
  }, [shotId, clearShot, loadShotWorkflow, selectShot, selectScene, selectProject]);

  useEffect(() => {
    if (currentShot && currentShot.id === shotId) {
      setScriptDraft(currentShot.script_text);
      setScriptDirty(false);
    }
  }, [currentShot, shotId]);

  async function handleSaveScript() {
    if (!shotId || scriptSaving) return;
    setScriptSaving(true);
    try {
      await updateScriptText(shotId, scriptDraft);
      setScriptDirty(false);
    } finally {
      setScriptSaving(false);
    }
  }

  if (!shotId) {
    return <div className="page-empty">No shot id in URL.</div>;
  }

  const breadcrumb = (
    <nav className="breadcrumb" aria-label="Breadcrumb">
      <Link to="/projects">Projects</Link>
      <span aria-hidden="true">/</span>
      {currentProject ? (
        <Link to={`/projects/${currentProject.id}`}>{currentProject.name}</Link>
      ) : (
        <span>…</span>
      )}
      <span aria-hidden="true">/</span>
      {currentScene ? (
        <Link to={`/scenes/${currentScene.id}`}>{currentScene.name}</Link>
      ) : (
        <span>…</span>
      )}
      <span aria-hidden="true">/</span>
      <span>
        Shot #{currentShot ? currentShot.order_index + 1 : "…"}
      </span>
    </nav>
  );

  return (
    <div className="shot-editor">
      <header className="shot-editor__header">
        <div className="shot-editor__crumbs">
          {breadcrumb}
          <span className={`shot-row__status status-${currentShot?.status ?? "idle"}`}>
            {currentShot?.status ?? "—"}
          </span>
        </div>
        <div className="shot-editor__header-actions">
          <button
            type="button"
            className="btn"
            onClick={() => void runShot(shotId)}
            disabled={!currentShot || currentShot.status === "running"}
            title="Phase 2: flips shot status only. Phase 7 wires the DAG engine."
          >
            Run shot
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => void cancelShot(shotId)}
            disabled={!currentShot || currentShot.status !== "running"}
          >
            Cancel
          </button>
        </div>
      </header>

      <details className="shot-editor__script">
        <summary>Shot script</summary>
        <textarea
          className="form-field__textarea"
          rows={5}
          value={scriptDraft}
          onChange={(e) => {
            setScriptDraft(e.target.value);
            setScriptDirty(true);
          }}
          placeholder="Free-text script for this shot. Used as the canonical brief; LLM uses it during Phase 6 prompt synthesis."
        />
        <div className="form-actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => void handleSaveScript()}
            disabled={!scriptDirty || scriptSaving}
          >
            {scriptSaving ? "Saving…" : scriptDirty ? "Save script" : "Saved"}
          </button>
        </div>
      </details>

      <ReactFlowProvider>
        <div className="canvas-wrap">
          {loading && wfShotId !== shotId ? (
            <div className="canvas-loading">Loading shot workflow…</div>
          ) : (
            <>
              <ShotCanvas />
              <AddNodePalette />
            </>
          )}
          <StatusBar />
          <ReferencesPanel />
        </div>
      </ReactFlowProvider>
    </div>
  );
}

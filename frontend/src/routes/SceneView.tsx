import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useProjectStore } from "../store/project";
import { useSceneStore } from "../store/scene";
import { useShotStore } from "../store/shot";
import { ScriptInputDialog } from "./ScriptInputDialog";

/**
 * One scene's hub. Top: breadcrumb + scene bible. Body: ordered shots
 * list with create / delete / open. ScriptInput dialog stub surfaces the
 * intent (paste a VN script → bulk-create shots) but actual parsing
 * waits for Phase 6's ``/api/prompt/parse-script`` endpoint.
 */
export function SceneView() {
  const { sceneId } = useParams<{ sceneId: string }>();
  const navigate = useNavigate();

  const currentScene = useSceneStore((s) => s.currentScene);
  const sceneBible = useSceneStore((s) => s.sceneBible);
  const selectScene = useSceneStore((s) => s.selectScene);
  const saveBible = useSceneStore((s) => s.saveBible);
  const deleteScene = useSceneStore((s) => s.deleteScene);

  const currentProject = useProjectStore((s) => s.currentProject);
  const selectProject = useProjectStore((s) => s.selectProject);

  const shots = useShotStore((s) =>
    sceneId ? s.shotsByScene[sceneId] ?? [] : [],
  );
  const loadShots = useShotStore((s) => s.loadShots);
  const createShot = useShotStore((s) => s.createShot);
  const deleteShot = useShotStore((s) => s.deleteShot);
  const resetShots = useShotStore((s) => s.resetForScene);

  const [bibleDraft, setBibleDraft] = useState("");
  const [bibleSaving, setBibleSaving] = useState(false);
  const [bibleDirty, setBibleDirty] = useState(false);

  const [creating, setCreating] = useState(false);
  const [scriptDialogOpen, setScriptDialogOpen] = useState(false);

  useEffect(() => {
    if (!sceneId) return;
    resetShots(sceneId);
    void (async () => {
      await selectScene(sceneId);
      const sc = useSceneStore.getState().currentScene;
      if (sc) {
        if (sc.project_id !== useProjectStore.getState().currentProjectId) {
          await selectProject(sc.project_id);
        }
      }
      await loadShots(sceneId);
    })();
  }, [sceneId, selectScene, selectProject, loadShots, resetShots]);

  useEffect(() => {
    if (sceneBible) {
      setBibleDraft(sceneBible.scene_bible_text);
      setBibleDirty(false);
    }
  }, [sceneBible]);

  async function handleSaveBible() {
    if (!sceneId || bibleSaving) return;
    setBibleSaving(true);
    try {
      await saveBible({
        scene_bible_text: bibleDraft,
        master_establishing_asset_id:
          sceneBible?.master_establishing_asset_id ?? null,
      });
      setBibleDirty(false);
    } finally {
      setBibleSaving(false);
    }
  }

  async function handleAddShot() {
    if (!sceneId || creating) return;
    setCreating(true);
    try {
      const shot = await createShot(sceneId);
      if (shot) navigate(`/shots/${shot.id}`);
    } finally {
      setCreating(false);
    }
  }

  if (!sceneId) {
    return <div className="page-empty">No scene id in URL.</div>;
  }

  return (
    <div className="page page--scene-view">
      <header className="page-header">
        <div>
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <Link to="/projects">Projects</Link>
            <span aria-hidden="true">/</span>
            {currentProject ? (
              <Link to={`/projects/${currentProject.id}`}>
                {currentProject.name}
              </Link>
            ) : (
              <span>…</span>
            )}
            <span aria-hidden="true">/</span>
            <span>{currentScene?.name ?? "…"}</span>
          </nav>
          <h1 className="page-title">{currentScene?.name ?? "…"}</h1>
          <p className="page-subtitle">
            {currentScene
              ? `Scene #${currentScene.order_index + 1} · ${currentScene.shot_count} shots`
              : "Loading…"}
          </p>
        </div>
        <div className="page-header__actions">
          <button
            type="button"
            className="btn"
            onClick={() => setScriptDialogOpen(true)}
            title="Generate shots from script (available Phase 6)"
          >
            Script → shots
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => {
              if (!currentScene) return;
              if (
                window.confirm(
                  `Delete scene "${currentScene.name}"? All shots inside will also be deleted.`,
                )
              ) {
                void (async () => {
                  await deleteScene(currentScene.id);
                  navigate(`/projects/${currentScene.project_id}`);
                })();
              }
            }}
          >
            Delete scene
          </button>
        </div>
      </header>

      <div className="dashboard-grid">
        <section className="dashboard-bible" aria-labelledby="scene-bible-h">
          <header className="dashboard-section__header">
            <h2 id="scene-bible-h">Scene Bible</h2>
            <p className="dashboard-section__hint">
              Spatial / lighting / continuity anchor for this scene. Layered
              under the Project Bible in Phase 6 prompts.
            </p>
          </header>
          <textarea
            className="form-field__textarea"
            rows={12}
            value={bibleDraft}
            onChange={(e) => {
              setBibleDraft(e.target.value);
              setBibleDirty(true);
            }}
            placeholder="e.g. Night-time rooftop café in Tokyo, 2 main characters facing each other across a small table. Camera anchored at table-edge. Practical lighting from string lights + ambient cyan glow from billboards. Rain stops mid-scene."
          />
          <div className="form-actions">
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void handleSaveBible()}
              disabled={!bibleDirty || bibleSaving}
            >
              {bibleSaving ? "Saving…" : bibleDirty ? "Save bible" : "Saved"}
            </button>
          </div>
        </section>

        <section className="dashboard-scenes" aria-labelledby="shots-h">
          <header className="dashboard-section__header">
            <h2 id="shots-h">Shots</h2>
            <p className="dashboard-section__hint">
              Each shot owns its own React-Flow workflow graph (open via
              ShotEditor).
            </p>
          </header>
          <div className="form-actions">
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void handleAddShot()}
              disabled={creating}
            >
              {creating ? "Adding…" : "+ Add shot"}
            </button>
          </div>
          {shots.length === 0 ? (
            <div className="page-empty">No shots in this scene yet.</div>
          ) : (
            <ol className="shot-list">
              {shots
                .slice()
                .sort((a, b) => a.order_index - b.order_index)
                .map((shot) => (
                  <li key={shot.id} className="shot-row">
                    <Link to={`/shots/${shot.id}`} className="shot-row__body">
                      <span className="shot-row__order">
                        #{shot.order_index + 1}
                      </span>
                      <span className={`shot-row__status status-${shot.status}`}>
                        {shot.status}
                      </span>
                      <span className="shot-row__script">
                        {shot.script_text
                          ? shot.script_text.slice(0, 120)
                          : "— no script —"}
                      </span>
                    </Link>
                    <button
                      type="button"
                      className="shot-row__delete"
                      onClick={() => {
                        if (
                          window.confirm(
                            `Delete shot #${shot.order_index + 1}? Its workflow nodes will also be deleted.`,
                          )
                        ) {
                          void deleteShot(shot.id);
                        }
                      }}
                      aria-label={`Delete shot #${shot.order_index + 1}`}
                    >
                      ✕
                    </button>
                  </li>
                ))}
            </ol>
          )}
        </section>
      </div>

      {scriptDialogOpen && (
        <ScriptInputDialog
          sceneId={sceneId}
          onClose={() => setScriptDialogOpen(false)}
        />
      )}
    </div>
  );
}

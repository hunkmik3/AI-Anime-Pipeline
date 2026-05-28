import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { EMPTY_PROJECT_BIBLE, type ProjectBible, type SceneDTO } from "../api/client";
import { ReferencesPanel } from "../components/ReferencesPanel";
import { useProjectStore } from "../store/project";
import { useSceneStore } from "../store/scene";

const EMPTY_SCENES: SceneDTO[] = [];

/**
 * Phase 8.3 project hub (the new entry point at /projects/:projectId).
 *
 * Left: project-level shared refs (Character / VisualAsset reused across
 * episodes). Right: scenes grid (= episodes) + create. Clicking a scene
 * opens its multi-shot SceneCanvas. Project Bible editing lives in a
 * collapsible panel (kept for Automation; Scene Bible was removed in 8.3).
 */
export function SceneView() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();

  const currentProject = useProjectStore((s) => s.currentProject);
  const currentProjectId = useProjectStore((s) => s.currentProjectId);
  const projectBible = useProjectStore((s) => s.projectBible);
  const selectProject = useProjectStore((s) => s.selectProject);
  const refreshDetail = useProjectStore((s) => s.refreshProjectDetail);
  const saveBible = useProjectStore((s) => s.saveBible);

  const scenes = useSceneStore((s) =>
    projectId ? s.scenesByProject[projectId] ?? EMPTY_SCENES : EMPTY_SCENES,
  );
  const loadScenes = useSceneStore((s) => s.loadScenes);
  const createScene = useSceneStore((s) => s.createScene);
  const deleteScene = useSceneStore((s) => s.deleteScene);
  const resetScenes = useSceneStore((s) => s.resetForProject);

  const [bibleDraft, setBibleDraft] = useState<ProjectBible>(EMPTY_PROJECT_BIBLE);
  const [bibleSaving, setBibleSaving] = useState(false);
  const [bibleDirty, setBibleDirty] = useState(false);

  const [sceneName, setSceneName] = useState("");
  const [creatingScene, setCreatingScene] = useState(false);

  useEffect(() => {
    if (!projectId) return;
    if (projectId !== currentProjectId) {
      resetScenes(projectId);
      void selectProject(projectId);
    }
    void loadScenes(projectId);
  }, [projectId, currentProjectId, selectProject, loadScenes, resetScenes]);

  useEffect(() => {
    if (projectBible) {
      setBibleDraft({ ...EMPTY_PROJECT_BIBLE, ...projectBible });
      setBibleDirty(false);
    }
  }, [projectBible]);

  const sceneCount = scenes.length;
  const sortedScenes = useMemo(
    () => scenes.slice().sort((a, b) => a.order_index - b.order_index),
    [scenes],
  );

  async function handleSaveBible() {
    if (!projectId || bibleSaving) return;
    setBibleSaving(true);
    try {
      await saveBible(bibleDraft);
      setBibleDirty(false);
      await refreshDetail();
    } finally {
      setBibleSaving(false);
    }
  }

  async function handleCreateScene() {
    if (!projectId || creatingScene) return;
    const name = sceneName.trim() || `Episode ${sceneCount + 1}`;
    setCreatingScene(true);
    try {
      const scene = await createScene(projectId, name);
      setSceneName("");
      // Open the new scene's multi-shot canvas straight away.
      if (scene) navigate(`/projects/${projectId}/scenes/${scene.id}`);
    } finally {
      setCreatingScene(false);
    }
  }

  function updateBibleField<K extends keyof ProjectBible>(key: K, value: ProjectBible[K]) {
    setBibleDraft((prev) => ({ ...prev, [key]: value }));
    setBibleDirty(true);
  }

  const paletteText = useMemo(
    () => bibleDraft.color_palette.join(", "),
    [bibleDraft.color_palette],
  );
  const negativeText = useMemo(
    () => bibleDraft.negative_prompts.join("\n"),
    [bibleDraft.negative_prompts],
  );

  if (!projectId) {
    return <div className="page-empty">No project id in URL.</div>;
  }

  return (
    <div className="page page--scene-view">
      <header className="page-header">
        <div>
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <Link to="/projects">Projects</Link>
            <span aria-hidden="true">/</span>
            <span>{currentProject?.name ?? "…"}</span>
          </nav>
          <h1 className="page-title">{currentProject?.name ?? "…"}</h1>
          <p className="page-subtitle">
            {currentProject
              ? `${currentProject.scene_count} scenes · ${currentProject.asset_count} assets`
              : "Loading…"}
          </p>
        </div>
        <div className="page-header__actions">
          <Link to={`/projects/${projectId}/library`} className="btn">
            Asset library
          </Link>
          <Link to={`/projects/${projectId}/cost`} className="btn">
            Cost
          </Link>
        </div>
      </header>

      <div className="scene-hub-grid">
        {/* Left: project-level shared references (reused across episodes). */}
        <aside className="scene-hub-refs" aria-label="Project references">
          <ReferencesPanel />
        </aside>

        {/* Right: scenes (episodes) + create + Project Bible (collapsible). */}
        <section className="scene-hub-main">
          <header className="dashboard-section__header">
            <h2>Scenes</h2>
            <p className="dashboard-section__hint">
              Each scene is an episode with its own multi-shot canvas.
            </p>
          </header>

          <form
            className="scene-create"
            onSubmit={(e) => {
              e.preventDefault();
              void handleCreateScene();
            }}
          >
            <input
              type="text"
              value={sceneName}
              onChange={(e) => setSceneName(e.target.value)}
              placeholder={`Episode ${sceneCount + 1}`}
              disabled={creatingScene}
              maxLength={120}
            />
            <button type="submit" className="btn btn--primary" disabled={creatingScene}>
              {creatingScene ? "Adding…" : "+ New Scene"}
            </button>
          </form>

          {scenes.length === 0 ? (
            <div className="page-empty">
              No scenes yet. Add the first scene to start storyboarding.
            </div>
          ) : (
            <ol className="scene-grid">
              {sortedScenes.map((scene) => {
                const shotCount = scene.canvas_state?.shot_groups?.length ?? 0;
                return (
                  <li key={scene.id} className="scene-card">
                    <Link
                      to={`/projects/${projectId}/scenes/${scene.id}`}
                      className="scene-card__body"
                    >
                      <div className="scene-card__order">#{scene.order_index + 1}</div>
                      <div className="scene-card__meta">
                        <div className="scene-card__name">{scene.name}</div>
                        <div className="scene-card__hint">
                          {shotCount} shot{shotCount === 1 ? "" : "s"}
                        </div>
                      </div>
                    </Link>
                    <button
                      type="button"
                      className="scene-card__delete"
                      onClick={() => {
                        if (
                          window.confirm(
                            `Delete scene "${scene.name}"? All shots inside will also be deleted.`,
                          )
                        ) {
                          void deleteScene(scene.id);
                        }
                      }}
                      aria-label={`Delete ${scene.name}`}
                    >
                      ✕
                    </button>
                  </li>
                );
              })}
            </ol>
          )}

          <details className="project-bible-collapse">
            <summary>Project Bible (style anchor)</summary>
            <div className="dashboard-bible">
              <label className="form-field">
                <span className="form-field__label">Art style</span>
                <input
                  type="text"
                  value={bibleDraft.art_style}
                  onChange={(e) => updateBibleField("art_style", e.target.value)}
                  placeholder="e.g. cel-shaded anime, 90s OVA"
                />
              </label>
              <label className="form-field">
                <span className="form-field__label">Color palette</span>
                <input
                  type="text"
                  value={paletteText}
                  onChange={(e) =>
                    updateBibleField(
                      "color_palette",
                      e.target.value.split(",").map((x) => x.trim()).filter(Boolean),
                    )
                  }
                  placeholder="comma-separated, e.g. teal, amber, ink black"
                />
              </label>
              <label className="form-field">
                <span className="form-field__label">Line style</span>
                <input
                  type="text"
                  value={bibleDraft.line_style}
                  onChange={(e) => updateBibleField("line_style", e.target.value)}
                  placeholder="e.g. fine ink outline, varied weight"
                />
              </label>
              <label className="form-field">
                <span className="form-field__label">Lighting conventions</span>
                <textarea
                  rows={3}
                  value={bibleDraft.lighting_conventions}
                  onChange={(e) => updateBibleField("lighting_conventions", e.target.value)}
                  placeholder="e.g. high-contrast key light, soft rim, mood-driven"
                />
              </label>
              <label className="form-field">
                <span className="form-field__label">Negative prompts</span>
                <textarea
                  rows={3}
                  value={negativeText}
                  onChange={(e) =>
                    updateBibleField(
                      "negative_prompts",
                      e.target.value.split("\n").map((x) => x.trim()).filter(Boolean),
                    )
                  }
                  placeholder="one per line"
                />
              </label>
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
            </div>
          </details>
        </section>
      </div>
    </div>
  );
}

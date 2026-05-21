import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { EMPTY_PROJECT_BIBLE, type ProjectBible } from "../api/client";
import { useProjectStore } from "../store/project";
import { useSceneStore } from "../store/scene";

/**
 * One-project hub. Left pane: editable Project Bible (style anchor).
 * Right pane: Scenes grid + create button. Phase 3 keeps scene thumbnails
 * empty until shot videos / master shots can be referenced.
 */
export function ProjectDashboard() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();

  const currentProject = useProjectStore((s) => s.currentProject);
  const currentProjectId = useProjectStore((s) => s.currentProjectId);
  const projectBible = useProjectStore((s) => s.projectBible);
  const selectProject = useProjectStore((s) => s.selectProject);
  const refreshDetail = useProjectStore((s) => s.refreshProjectDetail);
  const saveBible = useProjectStore((s) => s.saveBible);

  const scenes = useSceneStore((s) =>
    projectId ? s.scenesByProject[projectId] ?? [] : [],
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
      // Switching projects: drop other project's scenes from memory and
      // load the new one's tree.
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
    const name = sceneName.trim() || `Scene ${sceneCount + 1}`;
    setCreatingScene(true);
    try {
      const scene = await createScene(projectId, name);
      setSceneName("");
      if (scene) navigate(`/scenes/${scene.id}`);
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
    <div className="page page--project-dashboard">
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

      <div className="dashboard-grid">
        <section className="dashboard-bible" aria-labelledby="bible-h">
          <header className="dashboard-section__header">
            <h2 id="bible-h">Project Bible</h2>
            <p className="dashboard-section__hint">
              Style anchor injected into every prompt synthesis call (Phase 6).
            </p>
          </header>
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
                  e.target.value
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean),
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
              onChange={(e) =>
                updateBibleField("lighting_conventions", e.target.value)
              }
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
                  e.target.value
                    .split("\n")
                    .map((s) => s.trim())
                    .filter(Boolean),
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
        </section>

        <section className="dashboard-scenes" aria-labelledby="scenes-h">
          <header className="dashboard-section__header">
            <h2 id="scenes-h">Scenes</h2>
            <p className="dashboard-section__hint">
              Ordered sequence of scenes. Each contains one or more shots.
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
              placeholder={`Scene ${sceneCount + 1}`}
              disabled={creatingScene}
              maxLength={120}
            />
            <button
              type="submit"
              className="btn btn--primary"
              disabled={creatingScene}
            >
              {creatingScene ? "Adding…" : "+ Add scene"}
            </button>
          </form>

          {scenes.length === 0 ? (
            <div className="page-empty">
              No scenes yet. Add the first scene to start storyboarding.
            </div>
          ) : (
            <ol className="scene-grid">
              {scenes
                .slice()
                .sort((a, b) => a.order_index - b.order_index)
                .map((scene) => (
                  <li key={scene.id} className="scene-card">
                    <Link to={`/scenes/${scene.id}`} className="scene-card__body">
                      <div className="scene-card__order">#{scene.order_index + 1}</div>
                      <div className="scene-card__meta">
                        <div className="scene-card__name">{scene.name}</div>
                        <div className="scene-card__hint">
                          {scene.scene_bible_text
                            ? scene.scene_bible_text.slice(0, 80)
                            : "No bible yet"}
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
                ))}
            </ol>
          )}
        </section>
      </div>
    </div>
  );
}

import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  EMPTY_PROJECT_BIBLE,
  thumbUrl,
  setSceneCover,
  uploadImage,
  type ProjectBible,
  type SceneDTO,
} from "../api/client";
import { ReferencesPanel } from "../components/ReferencesPanel";
import { useProjectStore } from "../store/project";
import { useSceneStore } from "../store/scene";
import { useAuthStore } from "../store/auth";
import { useReferencesStore } from "../store/references";

const EMPTY_SCENES: SceneDTO[] = [];

/** Open a native file picker and resolve with the chosen image (or null). */
function pickImageFile(): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp";
    input.onchange = () => resolve(input.files?.[0] ?? null);
    input.click();
  });
}

// Project Bible editor is hidden for now (not needed yet). Flip to true to
// restore it — all the state/handlers below stay wired so this is reversible.
const SHOW_PROJECT_BIBLE = false;

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
  // Phase 9.1: scenes are structural — only admins create/delete them.
  const isAdmin = useAuthStore((s) => s.isAdmin());

  const [bibleDraft, setBibleDraft] = useState<ProjectBible>(EMPTY_PROJECT_BIBLE);
  const [bibleSaving, setBibleSaving] = useState(false);
  const [bibleDirty, setBibleDirty] = useState(false);

  const [sceneName, setSceneName] = useState("");
  const [creatingScene, setCreatingScene] = useState(false);
  const [newSceneOpen, setNewSceneOpen] = useState(false);
  // scene cover upload (per-card)
  const [coverBusy, setCoverBusy] = useState<string | null>(null);

  async function handleSceneCover(sceneId: string, file: File) {
    if (!projectId) return;
    setCoverBusy(sceneId);
    try {
      const { media_id } = await uploadImage(file, projectId);
      await setSceneCover(sceneId, media_id);
      await loadScenes(projectId);
    } catch (e) {
      // eslint-disable-next-line no-alert
      alert(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setCoverBusy(null);
    }
  }

  const loadReferences = useReferencesStore((s) => s.load);
  useEffect(() => {
    if (!projectId) return;
    if (projectId !== currentProjectId) {
      resetScenes(projectId);
      void selectProject(projectId);
    }
    void loadScenes(projectId);
    void loadReferences(projectId); // library is scoped to this project
  }, [projectId, currentProjectId, selectProject, loadScenes, resetScenes, loadReferences]);

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
      await createScene(projectId, name);
      // Stay on the episodes list (the new card appears in the grid); the user
      // opens the canvas by clicking the episode when they're ready.
      setSceneName("");
      setNewSceneOpen(false);
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
              ? `${currentProject.scene_count} episode${currentProject.scene_count === 1 ? "" : "s"} · ${currentProject.asset_count} asset${currentProject.asset_count === 1 ? "" : "s"}`
              : "Loading…"}
          </p>
        </div>
        <div className="page-header__actions">
          <Link to={`/projects/${projectId}/library`} className="btn">
            Asset library
          </Link>
        </div>
      </header>

      {/* Project-level shared references — a floating drawer (its own toggle
          tab), so it no longer reserves an empty left column. */}
      <ReferencesPanel />

      <div className="scene-hub">
        {/* Scenes (episodes) + create + Project Bible (collapsible). */}
        <section className="scene-hub-main">
          <header className="dashboard-section__header">
            <h2>Episodes</h2>
            <p className="dashboard-section__hint">
              Each episode has its own multi-sequence canvas.
            </p>
          </header>

          {isAdmin && (
            <div className="scene-create">
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => {
                  setSceneName("");
                  setNewSceneOpen(true);
                }}
              >
                + New Episode
              </button>
            </div>
          )}

          {scenes.length === 0 ? (
            <div className="page-empty">
              {isAdmin
                ? "No episodes yet. Add the first episode to start storyboarding."
                : "No episodes yet. An admin will create episodes for this project."}
            </div>
          ) : (
            <ol className="scene-grid">
              {sortedScenes.map((scene) => {
                const shotCount = scene.canvas_state?.shot_groups?.length ?? 0;
                // Deterministic thumbnail gradient per scene (until a real
                // establishing frame is wired) — stable across renders.
                const hue = (scene.order_index * 47 + 200) % 360;
                return (
                  <li key={scene.id} className="scene-card">
                    <Link
                      to={`/projects/${projectId}/scenes/${scene.id}`}
                      className="scene-card__body"
                    >
                      <div
                        className="scene-card__thumb"
                        style={{
                          background: `linear-gradient(135deg, hsl(${hue} 42% 26%), hsl(${(hue + 40) % 360} 46% 16%))`,
                        }}
                      >
                        {scene.thumb_media_id ? (
                          <img
                            className="scene-card__img"
                            src={thumbUrl(scene.thumb_media_id, 400)}
                            alt=""
                            loading="lazy"
                            onError={(e) => {
                              (e.currentTarget as HTMLImageElement).style.display = "none";
                            }}
                          />
                        ) : (
                          <svg
                            className="scene-card__glyph"
                            viewBox="0 0 24 24"
                            width="40"
                            height="40"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="1.4"
                            aria-hidden="true"
                          >
                            <rect x="2" y="7" width="20" height="14" rx="2" />
                            <path d="M2 7l3-4h4l-3 4M9 7l3-4h4l-3 4M16 7l3-4h4l-3 4" />
                          </svg>
                        )}
                        <span className="scene-card__badge">EP {scene.order_index + 1}</span>

                        {/* hover-to-upload cover — a button (not a nav link) that
                            opens a file picker in JS, so it never navigates. */}
                        <button
                          type="button"
                          className={`scene-card__upload${coverBusy === scene.id ? " is-busy" : ""}`}
                          title="Upload a cover thumbnail"
                          disabled={coverBusy === scene.id}
                          onClick={async (e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            const f = await pickImageFile();
                            if (f) void handleSceneCover(scene.id, f);
                          }}
                        >
                          {coverBusy === scene.id ? (
                            "Uploading…"
                          ) : (
                            <>
                              <svg
                                viewBox="0 0 24 24"
                                width="14"
                                height="14"
                                fill="none"
                                stroke="currentColor"
                                strokeWidth="1.8"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                aria-hidden="true"
                              >
                                <path d="M12 16V4M6 10l6-6 6 6M4 20h16" />
                              </svg>
                              {scene.thumb_media_id ? "Change" : "Thumbnail"}
                            </>
                          )}
                        </button>
                      </div>
                      <div className="scene-card__meta">
                        <div className="scene-card__name" title={scene.name}>
                          {scene.name}
                        </div>
                        <div className="scene-card__hint">
                          {shotCount} sequence{shotCount === 1 ? "" : "s"}
                        </div>
                      </div>
                    </Link>
                    {isAdmin && (
                      <button
                        type="button"
                        className="scene-card__delete"
                        onClick={() => {
                          if (
                            window.confirm(
                              `Delete episode "${scene.name}"? All sequences inside will also be deleted.`,
                            )
                          ) {
                            void deleteScene(scene.id);
                          }
                        }}
                        aria-label={`Delete ${scene.name}`}
                      >
                        ✕
                      </button>
                    )}
                  </li>
                );
              })}
            </ol>
          )}

          {SHOW_PROJECT_BIBLE && (
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
          )}
        </section>
      </div>

      {newSceneOpen && (
        <div
          className="project-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget && !creatingScene) setNewSceneOpen(false);
          }}
        >
          <div className="project-modal" role="dialog" aria-modal="true">
            <h2 className="project-modal__title">New episode</h2>
            <p className="project-modal__hint">
              Name the episode. Once created it shows up in the list — click it to open the canvas.
            </p>
            <input
              type="text"
              className="project-modal__input"
              autoFocus
              maxLength={120}
              value={sceneName}
              placeholder={`Episode ${sceneCount + 1}`}
              onChange={(e) => setSceneName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void handleCreateScene();
                if (e.key === "Escape" && !creatingScene) setNewSceneOpen(false);
              }}
            />
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={() => setNewSceneOpen(false)}
                disabled={creatingScene}
              >
                Cancel
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--primary"
                onClick={() => void handleCreateScene()}
                disabled={creatingScene}
              >
                {creatingScene ? "Creating…" : "Create episode"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

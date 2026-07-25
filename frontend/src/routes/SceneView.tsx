import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import {
  thumbUrl,
  setSceneCover,
  uploadImage,
  type ProjectCapability,
  type SceneDTO,
  type SeriesDTO,
} from "../api/client";
import { ReferencesPanel } from "../components/ReferencesPanel";
import { ProjectVideoGallery } from "../components/ProjectVideoGallery";
import { ProjectMembersDialog } from "../components/ProjectMembersDialog";
import { useProjectStore } from "../store/project";
import { useSceneStore } from "../store/scene";
import { useSeriesStore } from "../store/series";
import { useReferencesStore } from "../store/references";

const EMPTY_SCENES: SceneDTO[] = [];
const EMPTY_SERIES: SeriesDTO[] = [];

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

/**
 * Phase 10 project home (entry point at /projects/:projectId).
 *
 * This is where the production structure is BUILT — no longer in the admin
 * console. The hierarchy is Project → Series → Episode|Chapter → Sequence:
 *   • Admin creates the Project and assigns a producer.
 *   • Producer/Lead build Series and their Episodes/Chapters right here.
 *   • Opening an Episode/Chapter goes to its multi-Sequence canvas.
 *
 * What each control does is gated by the caller's project role via the
 * `can` capability map the backend returns on the project — the UI hides what
 * the role can't do rather than letting the user click into a 403.
 */
export function SceneView() {
  const { projectId } = useParams<{ projectId: string }>();
  const location = useLocation();

  const currentProject = useProjectStore((s) => s.currentProject);
  const currentProjectId = useProjectStore((s) => s.currentProjectId);
  const selectProject = useProjectStore((s) => s.selectProject);

  const scenes = useSceneStore((s) =>
    projectId ? s.scenesByProject[projectId] ?? EMPTY_SCENES : EMPTY_SCENES,
  );
  const loadScenes = useSceneStore((s) => s.loadScenes);
  const createScene = useSceneStore((s) => s.createScene);
  const resetScenes = useSceneStore((s) => s.resetForProject);

  const series = useSeriesStore((s) =>
    projectId ? s.byProject[projectId] ?? EMPTY_SERIES : EMPTY_SERIES,
  );
  const loadSeries = useSeriesStore((s) => s.loadSeries);
  const createSeries = useSeriesStore((s) => s.createSeries);
  const renameSeries = useSeriesStore((s) => s.renameSeries);
  const deleteSeries = useSeriesStore((s) => s.deleteSeries);

  // Capability gates — the owner is a producer implicitly; admins get all.
  const can = (cap: ProjectCapability): boolean =>
    currentProject?.can?.[cap] ?? false;
  const myRole = currentProject?.my_role ?? null;

  const [coverBusy, setCoverBusy] = useState<string | null>(null);
  const [membersOpen, setMembersOpen] = useState(false);

  // New-series modal
  const [seriesModalOpen, setSeriesModalOpen] = useState(false);
  const [seriesName, setSeriesName] = useState("");
  const [seriesCode, setSeriesCode] = useState("");
  const [savingSeries, setSavingSeries] = useState(false);

  // New-episode modal (scoped to one series)
  const [epModalFor, setEpModalFor] = useState<SeriesDTO | null>(null);
  const [epName, setEpName] = useState("");
  const [epCode, setEpCode] = useState("");
  const [savingEp, setSavingEp] = useState(false);

  const loadReferences = useReferencesStore((s) => s.load);
  useEffect(() => {
    if (!projectId) return;
    if (projectId !== currentProjectId) {
      resetScenes(projectId);
      void selectProject(projectId);
    }
    void loadScenes(projectId);
    void loadSeries(projectId);
    void loadReferences(projectId);
  }, [
    projectId,
    currentProjectId,
    selectProject,
    loadScenes,
    loadSeries,
    resetScenes,
    loadReferences,
  ]);

  // Deep-link from the sidebar: clicking a Series routes here with
  // #series-<id>. Scroll it into view + flash a highlight once the series
  // sections have rendered.
  useEffect(() => {
    const hash = location.hash;
    if (!hash.startsWith("#series-")) return;
    const el = document.getElementById(hash.slice(1));
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.classList.add("series-block--flash");
    const t = setTimeout(() => el.classList.remove("series-block--flash"), 1600);
    return () => clearTimeout(t);
  }, [location.hash, series.length, scenes.length]);

  // Group episodes under their series; anything with no series_id (legacy /
  // in-flight) falls into a synthetic "Unfiled" bucket so it's never lost.
  const scenesBySeries = useMemo(() => {
    const map = new Map<string, SceneDTO[]>();
    for (const sc of scenes) {
      const key = sc.series_id ?? "__unfiled__";
      const list = map.get(key) ?? [];
      list.push(sc);
      map.set(key, list);
    }
    for (const list of map.values()) {
      list.sort((a, b) => a.order_index - b.order_index);
    }
    return map;
  }, [scenes]);

  // The Series selected from the sidebar (#series-<id>). When set, the main
  // screen shows ONLY that series' episodes; with none selected it lists all.
  const selectedSeriesId = location.hash.startsWith("#series-")
    ? location.hash.slice("#series-".length)
    : null;

  const sortedSeries = useMemo(() => {
    const all = series.slice().sort((a, b) => a.order_index - b.order_index);
    return selectedSeriesId ? all.filter((s) => s.id === selectedSeriesId) : all;
  }, [series, selectedSeriesId]);
  // Unfiled episodes only belong on the full (unfiltered) view.
  const unfiled = selectedSeriesId ? [] : scenesBySeries.get("__unfiled__") ?? [];

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

  async function handleCreateSeries() {
    if (!projectId || savingSeries) return;
    setSavingSeries(true);
    try {
      await createSeries(projectId, {
        name: seriesName.trim() || `Series ${series.length + 1}`,
        code: seriesCode.trim(),
      });
      setSeriesName("");
      setSeriesCode("");
      setSeriesModalOpen(false);
    } finally {
      setSavingSeries(false);
    }
  }

  async function handleCreateEpisode() {
    if (!projectId || !epModalFor || savingEp) return;
    const label = epModalFor.unit_label || "Episode";
    const count = (scenesBySeries.get(epModalFor.id) ?? []).length;
    setSavingEp(true);
    try {
      await createScene(projectId, epName.trim() || `${label} ${count + 1}`, {
        series_id: epModalFor.id,
        code: epCode.trim(),
      });
      setEpName("");
      setEpCode("");
      setEpModalFor(null);
    } finally {
      setSavingEp(false);
    }
  }

  async function handleDeleteSeries(s: SeriesDTO) {
    if (!projectId) return;
    const count = (scenesBySeries.get(s.id) ?? []).length;
    if (count > 0) {
      // eslint-disable-next-line no-alert
      alert(
        `"${s.name}" still has ${count} ${s.unit_label.toLowerCase()}${count === 1 ? "" : "s"}. ` +
          `Delete or move them first.`,
      );
      return;
    }
    if (!window.confirm(`Delete series "${s.name}"?`)) return;
    await deleteSeries(projectId, s.id);
  }

  function handleRenameSeries(s: SeriesDTO) {
    if (!projectId) return;
    // eslint-disable-next-line no-alert
    const name = window.prompt("Series name", s.name);
    if (name == null) return;
    void renameSeries(projectId, s.id, { name: name.trim() || s.name });
  }

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
              ? `${series.length} series · ${currentProject.scene_count} total · ${currentProject.asset_count} asset${currentProject.asset_count === 1 ? "" : "s"}`
              : "Loading…"}
            {myRole ? <span className="role-chip">{myRole}</span> : null}
          </p>
        </div>
        <div className="page-header__actions">
          {can("member.manage") && (
            <button
              type="button"
              className="btn"
              onClick={() => setMembersOpen(true)}
            >
              Members
            </button>
          )}
          <Link to={`/projects/${projectId}/library`} className="btn">
            Asset library
          </Link>
        </div>
      </header>

      {/* Project-level shared references — a floating drawer. */}
      <ReferencesPanel />

      <div className="scene-hub">
        <section className="scene-hub-main">
          <header className="dashboard-section__header dashboard-section__header--row">
            <div>
              <h2>
                Series
                {selectedSeriesId ? (
                  <Link to={`/projects/${projectId}`} className="series-show-all">
                    ← All series
                  </Link>
                ) : null}
              </h2>
              <p className="dashboard-section__hint">
                {selectedSeriesId
                  ? "Showing one series. Click “All series” to see the rest."
                  : "A series holds its Episodes. Open one to storyboard its sequences."}
              </p>
            </div>
          </header>

          {sortedSeries.length === 0 && unfiled.length === 0 ? (
            <div className="page-empty">
              {can("series.create")
                ? "No series yet. Create the first series to start building episodes."
                : "No series yet. A producer will set up this project's structure."}
            </div>
          ) : null}

          {sortedSeries.map((s) => {
            const eps = scenesBySeries.get(s.id) ?? [];
            const unit = s.unit_label || "Episode";
            return (
              <section key={s.id} id={`series-${s.id}`} className="series-block">
                <header className="series-block__header">
                  <div className="series-block__title">
                    <h3>
                      {s.code ? <span className="series-block__code">{s.code}</span> : null}
                      {s.name}
                    </h3>
                    <span className="series-block__count">
                      {eps.length} {unit.toLowerCase()}
                      {eps.length === 1 ? "" : "s"}
                    </span>
                  </div>
                  <div className="series-block__actions">
                    {can("episode.create") && (
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() => {
                          setEpName("");
                          setEpCode("");
                          setEpModalFor(s);
                        }}
                      >
                        + New {unit}
                      </button>
                    )}
                    {can("series.update") && (
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={() => handleRenameSeries(s)}
                        title="Rename series"
                      >
                        Rename
                      </button>
                    )}
                    {can("series.delete") && (
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={() => void handleDeleteSeries(s)}
                        title="Delete series"
                      >
                        ✕
                      </button>
                    )}
                  </div>
                </header>

                {eps.length === 0 ? (
                  <div className="series-block__empty">
                    No {unit.toLowerCase()}s yet.
                  </div>
                ) : (
                  <ol className="scene-grid">
                    {eps.map((scene) => (
                      <EpisodeCard
                        key={scene.id}
                        scene={scene}
                        projectId={projectId}
                        coverBusy={coverBusy === scene.id}
                        canDecorate={can("project.decorate")}
                        onCover={(f) => void handleSceneCover(scene.id, f)}
                      />
                    ))}
                  </ol>
                )}
              </section>
            );
          })}

          {/* Legacy / in-flight episodes with no series — surfaced so nothing
              is ever hidden while a producer files them. */}
          {unfiled.length > 0 && (
            <section className="series-block">
              <header className="series-block__header">
                <div className="series-block__title">
                  <h3>Unfiled</h3>
                  <span className="series-block__count">
                    {unfiled.length} episode{unfiled.length === 1 ? "" : "s"}
                  </span>
                </div>
              </header>
              <ol className="scene-grid">
                {unfiled.map((scene) => (
                  <EpisodeCard
                    key={scene.id}
                    scene={scene}
                    projectId={projectId}
                    coverBusy={coverBusy === scene.id}
                    canDecorate={can("project.decorate")}
                    onCover={(f) => void handleSceneCover(scene.id, f)}
                  />
                ))}
              </ol>
            </section>
          )}

          {projectId ? <ProjectVideoGallery projectId={projectId} /> : null}
        </section>
      </div>

      {membersOpen && (
        <ProjectMembersDialog
          projectId={projectId}
          onClose={() => setMembersOpen(false)}
        />
      )}

      {seriesModalOpen && (
        <div
          className="project-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget && !savingSeries) setSeriesModalOpen(false);
          }}
        >
          <div className="project-modal" role="dialog" aria-modal="true">
            <h2 className="project-modal__title">New series</h2>
            <p className="project-modal__hint">
              A series holds its Episodes. Open one to storyboard its sequences.
            </p>
            <input
              type="text"
              className="project-modal__input"
              autoFocus
              maxLength={120}
              value={seriesName}
              placeholder={`Series ${series.length + 1}`}
              onChange={(e) => setSeriesName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void handleCreateSeries();
                if (e.key === "Escape" && !savingSeries) setSeriesModalOpen(false);
              }}
            />
            <div className="project-modal__row">
              <input
                type="text"
                className="project-modal__input"
                maxLength={32}
                value={seriesCode}
                placeholder="Code (e.g. S1)"
                onChange={(e) => setSeriesCode(e.target.value)}
              />
            </div>
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={() => setSeriesModalOpen(false)}
                disabled={savingSeries}
              >
                Cancel
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--primary"
                onClick={() => void handleCreateSeries()}
                disabled={savingSeries}
              >
                {savingSeries ? "Creating…" : "Create series"}
              </button>
            </div>
          </div>
        </div>
      )}

      {epModalFor && (
        <div
          className="project-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget && !savingEp) setEpModalFor(null);
          }}
        >
          <div className="project-modal" role="dialog" aria-modal="true">
            <h2 className="project-modal__title">
              New {epModalFor.unit_label.toLowerCase()} in {epModalFor.name}
            </h2>
            <p className="project-modal__hint">
              Once created it appears in the grid — click it to open the
              sequence canvas.
            </p>
            <input
              type="text"
              className="project-modal__input"
              autoFocus
              maxLength={120}
              value={epName}
              placeholder={`${epModalFor.unit_label} ${(scenesBySeries.get(epModalFor.id) ?? []).length + 1}`}
              onChange={(e) => setEpName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void handleCreateEpisode();
                if (e.key === "Escape" && !savingEp) setEpModalFor(null);
              }}
            />
            <div className="project-modal__row">
              <input
                type="text"
                className="project-modal__input"
                maxLength={32}
                value={epCode}
                placeholder={
                  epModalFor.unit_label === "Chapter" ? "Code (e.g. CH012)" : "Code (e.g. EP007)"
                }
                onChange={(e) => setEpCode(e.target.value)}
              />
            </div>
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={() => setEpModalFor(null)}
                disabled={savingEp}
              >
                Cancel
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--primary"
                onClick={() => void handleCreateEpisode()}
                disabled={savingEp}
              >
                {savingEp ? "Creating…" : `Create ${epModalFor.unit_label.toLowerCase()}`}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** One Episode/Chapter card in a series grid. Extracted so the same markup
 *  serves both real series and the Unfiled bucket. */
function EpisodeCard({
  scene,
  projectId,
  coverBusy,
  canDecorate,
  onCover,
}: {
  scene: SceneDTO;
  projectId: string;
  coverBusy: boolean;
  canDecorate: boolean;
  onCover: (file: File) => void;
}) {
  const shotCount = scene.canvas_state?.shot_groups?.length ?? 0;
  const hue = (scene.order_index * 47 + 200) % 360;
  return (
    <li className="scene-card">
      <Link to={`/projects/${projectId}/scenes/${scene.id}`} className="scene-card__body">
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

          <div
            className="scene-card__thumb-actions"
            style={{ position: "absolute", bottom: 8, right: 8, zIndex: 3, display: "flex", gap: 6 }}
          >
            {canDecorate && (
              <button
                type="button"
                className={`scene-card__upload${coverBusy ? " is-busy" : ""}`}
                style={{ position: "static", bottom: "auto", right: "auto" }}
                title="Upload a cover thumbnail"
                disabled={coverBusy}
                onClick={async (e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  const f = await pickImageFile();
                  if (f) onCover(f);
                }}
              >
                {coverBusy ? (
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
            )}
          </div>
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
    </li>
  );
}

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  createScene,
  createSeries,
  createShot,
  deleteScene,
  deleteSeries,
  deleteShot,
  getProject,
  listScenes,
  listSeries,
  listShots,
  patchSeries,
  type ProjectCapability,
  type ProjectDetailDTO,
  type SceneDTO,
  type SeriesDTO,
  type ShotDTO,
} from "../api/client";

/**
 * Phase 10: build a project's whole structure — Series → Episode/Chapter →
 * Sequence — in one modal, without leaving the current page. Opened from the
 * admin console (and the producer/lead console) so structure work no longer
 * bounces the user out to a separate project home.
 *
 * Every mutating control is shown only when the caller's project role allows it
 * (the `can` map the backend returns on the project). Opening a Sequence's
 * canvas is real navigation — that's the actual work surface — so it closes the
 * modal and routes to the episode canvas.
 */
export function ProjectStructureModal({
  projectId,
  projectName,
  onClose,
}: {
  projectId: string;
  projectName: string;
  onClose: () => void;
}) {
  const navigate = useNavigate();

  const [project, setProject] = useState<ProjectDetailDTO | null>(null);
  const [series, setSeries] = useState<SeriesDTO[]>([]);
  const [scenes, setScenes] = useState<SceneDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const can = (cap: ProjectCapability): boolean => project?.can?.[cap] ?? false;

  const reload = useCallback(async () => {
    const [proj, ser, scn] = await Promise.all([
      getProject(projectId),
      listSeries(projectId),
      listScenes(projectId),
    ]);
    setProject(proj);
    setSeries(ser.slice().sort((a, b) => a.order_index - b.order_index));
    setScenes(scn);
  }, [projectId]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        await reload();
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [reload]);

  function episodesOf(seriesId: string): SceneDTO[] {
    return scenes
      .filter((s) => s.series_id === seriesId)
      .sort((a, b) => a.order_index - b.order_index);
  }
  const unfiled = scenes.filter((s) => !s.series_id);

  async function guard(fn: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await fn();
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function addSeries() {
    // eslint-disable-next-line no-alert
    const name = window.prompt("New series name", `Series ${series.length + 1}`);
    if (name == null) return;
    await guard(() =>
      createSeries(projectId, {
        name: name.trim() || `Series ${series.length + 1}`,
      }).then(() => undefined),
    );
  }

  async function renameSeries(s: SeriesDTO) {
    // eslint-disable-next-line no-alert
    const name = window.prompt("Series name", s.name);
    if (name == null) return;
    await guard(() => patchSeries(s.id, { name: name.trim() || s.name }).then(() => undefined));
  }

  async function removeSeries(s: SeriesDTO) {
    const n = episodesOf(s.id).length;
    if (n > 0) {
      // eslint-disable-next-line no-alert
      alert(`"${s.name}" still has ${n} ${s.unit_label.toLowerCase()}(s). Delete them first.`);
      return;
    }
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Delete series "${s.name}"?`)) return;
    await guard(() => deleteSeries(s.id).then(() => undefined));
  }

  async function addEpisode(s: SeriesDTO) {
    const count = episodesOf(s.id).length;
    const unit = s.unit_label || "Episode";
    // eslint-disable-next-line no-alert
    const name = window.prompt(`New ${unit.toLowerCase()} name`, `${unit} ${count + 1}`);
    if (name == null) return;
    // eslint-disable-next-line no-alert
    const code = window.prompt(`Code (optional)`, "") ?? "";
    await guard(() =>
      createScene(projectId, {
        name: name.trim() || `${unit} ${count + 1}`,
        series_id: s.id,
        code: code.trim(),
      }).then(() => undefined),
    );
  }

  async function removeEpisode(sc: SceneDTO, unit: string) {
    // eslint-disable-next-line no-alert
    if (
      !window.confirm(
        `Delete ${unit.toLowerCase()} "${sc.name}"? All sequences inside are deleted too.`,
      )
    )
      return;
    await guard(() => deleteScene(sc.id).then(() => undefined));
  }

  function openCanvas(sceneId: string) {
    onClose();
    navigate(`/projects/${projectId}/scenes/${sceneId}`);
  }

  return (
    <div
      className="cover-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        className="cover-modal struct-modal"
        role="dialog"
        aria-label={`Structure — ${projectName}`}
      >
        <div className="cover-modal__head">
          <div>
            <h3 className="cover-modal__title">Structure — {projectName}</h3>
            <p className="cover-modal__sub">
              Series → Episode → Sequence.
              {project?.my_role ? (
                <span className="role-chip">{project.my_role}</span>
              ) : null}
            </p>
          </div>
          <button className="cover-modal__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {error ? <div className="admin-error" style={{ margin: "0 0 10px" }}>{error}</div> : null}

        {loading ? (
          <div className="admin2__empty">Loading…</div>
        ) : (
          <div className="struct-body">
            {can("series.create") && (
              <div className="struct-toolbar">
                <button className="btn2 btn2--primary" onClick={() => void addSeries()} disabled={busy}>
                  + New Series
                </button>
              </div>
            )}

            {series.length === 0 && unfiled.length === 0 ? (
              <div className="admin2__empty">
                {can("series.create")
                  ? "No series yet. Create the first one above."
                  : "No series yet."}
              </div>
            ) : null}

            {series.map((s) => {
              const unit = s.unit_label || "Episode";
              const eps = episodesOf(s.id);
              return (
                <section key={s.id} className="struct-series">
                  <header className="struct-series__head">
                    <div className="struct-series__title">
                      {s.code ? <span className="series-block__code">{s.code}</span> : null}
                      <b>{s.name}</b>
                      <span className="struct-muted">
                        {eps.length} {unit.toLowerCase()}{eps.length === 1 ? "" : "s"}
                      </span>
                    </div>
                    <div className="struct-series__actions">
                      {can("episode.create") && (
                        <button className="btn2 btn2--ghost" onClick={() => void addEpisode(s)} disabled={busy}>
                          + {unit}
                        </button>
                      )}
                      {can("series.update") && (
                        <button className="btn2 btn2--ghost" onClick={() => void renameSeries(s)} disabled={busy}>
                          Rename
                        </button>
                      )}
                      {can("series.delete") && (
                        <button className="btn2 btn2--ghost" onClick={() => void removeSeries(s)} disabled={busy}>
                          ✕
                        </button>
                      )}
                    </div>
                  </header>

                  {eps.length === 0 ? (
                    <div className="struct-empty">No {unit.toLowerCase()}s yet.</div>
                  ) : (
                    <ul className="struct-eplist">
                      {eps.map((sc) => (
                        <EpisodeRow
                          key={sc.id}
                          scene={sc}
                          canCreateSeq={can("sequence.create")}
                          canDeleteSeq={can("sequence.delete")}
                          canDeleteEp={can("episode.delete")}
                          busy={busy}
                          onOpen={() => openCanvas(sc.id)}
                          onDelete={() => void removeEpisode(sc, unit)}
                          onMutateSeq={(fn) => guard(fn)}
                        />
                      ))}
                    </ul>
                  )}
                </section>
              );
            })}

            {unfiled.length > 0 && (
              <section className="struct-series">
                <header className="struct-series__head">
                  <div className="struct-series__title">
                    <b>Unfiled</b>
                    <span className="struct-muted">{unfiled.length} episodes</span>
                  </div>
                </header>
                <ul className="struct-eplist">
                  {unfiled.map((sc) => (
                    <EpisodeRow
                      key={sc.id}
                      scene={sc}
                      canCreateSeq={can("sequence.create")}
                      canDeleteSeq={can("sequence.delete")}
                      canDeleteEp={can("episode.delete")}
                      busy={busy}
                      onOpen={() => openCanvas(sc.id)}
                      onDelete={() => void removeEpisode(sc, "Episode")}
                      onMutateSeq={(fn) => guard(fn)}
                    />
                  ))}
                </ul>
              </section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** One Episode/Chapter row: shows its sequences (lazy-loaded on expand), with
 *  add/delete sequence + open-canvas. */
function EpisodeRow({
  scene,
  canCreateSeq,
  canDeleteSeq,
  canDeleteEp,
  busy,
  onOpen,
  onDelete,
  onMutateSeq,
}: {
  scene: SceneDTO;
  canCreateSeq: boolean;
  canDeleteSeq: boolean;
  canDeleteEp: boolean;
  busy: boolean;
  onOpen: () => void;
  onDelete: () => void;
  onMutateSeq: (fn: () => Promise<void>) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [seqs, setSeqs] = useState<ShotDTO[] | null>(null);

  const loadSeqs = useCallback(async () => {
    setSeqs(await listShots(scene.id));
  }, [scene.id]);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && seqs === null) {
      try {
        await loadSeqs();
      } catch {
        setSeqs([]);
      }
    }
  }

  return (
    <li className="struct-ep">
      <div className="struct-ep__row">
        <button className="struct-ep__expand" onClick={() => void toggle()}>
          <span className="admin-proj__chev">{open ? "▾" : "▸"}</span>
          {scene.code ? <span className="series-block__code">{scene.code}</span> : null}
          <span className="struct-ep__name">{scene.name}</span>
        </button>
        <div className="struct-ep__actions">
          <button className="btn2 btn2--primary" onClick={onOpen} disabled={busy}>
            Open canvas →
          </button>
          {canDeleteEp && (
            <button className="btn2 btn2--ghost" onClick={onDelete} disabled={busy}>
              Delete
            </button>
          )}
        </div>
      </div>

      {open && (
        <div className="struct-seqs">
          {seqs === null ? (
            <span className="struct-muted">Loading…</span>
          ) : (
            <>
              {seqs.length === 0 ? (
                <span className="struct-muted">No sequences yet.</span>
              ) : (
                <ul className="struct-seqlist">
                  {seqs.map((q, i) => (
                    <li key={q.id} className="struct-seq">
                      <span className="struct-seq__label">
                        {q.code || `SQ${String(i + 1).padStart(2, "0")}`}
                      </span>
                      <button
                        className="struct-seq__open"
                        onClick={onOpen}
                        title="Open in canvas"
                      >
                        canvas →
                      </button>
                      {canDeleteSeq && (
                        <button
                          className="struct-seq__del"
                          disabled={busy}
                          onClick={() =>
                            void onMutateSeq(async () => {
                              await deleteShot(q.id);
                              await loadSeqs();
                            })
                          }
                          aria-label="Delete sequence"
                        >
                          ✕
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {canCreateSeq && (
                <button
                  className="btn2 btn2--ghost struct-seq__add"
                  disabled={busy}
                  onClick={() =>
                    void onMutateSeq(async () => {
                      // eslint-disable-next-line no-alert
                      const code = window.prompt("Sequence code (optional)", "") ?? "";
                      await createShot(scene.id, { code: code.trim() });
                      await loadSeqs();
                    })
                  }
                >
                  + Sequence
                </button>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
}

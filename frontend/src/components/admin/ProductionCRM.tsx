import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import {
  createSeries,
  getCrewNames,
  listProjects,
  listSeries,
  listSeriesEpisodes,
  patchScene,
  patchSeries,
  type SceneDTO,
  type SeriesDTO,
} from "../../api/client";
import { toast } from "../../store/toast";

/**
 * Phase 10 Production CRM — the Series_Master / Episode_Tracker spreadsheet,
 * in-app. A roomy table of every series across all projects (with a live
 * episode rollup); expand a row to edit its episodes' pipeline status + the
 * four role assignees inline. Restored from the pre-server-baseline build.
 */

const SERIES_STATUS = ["Planning", "On-going", "Completed", "Cancelled"] as const;
const PRIORITY = ["High", "Medium", "Low"] as const;
const EP_STATUS = ["NotStarted", "Script", "Production", "Completed", "Dropped"] as const;

/** A series row enriched with its owning project's name. */
type Row = SeriesDTO & { project_name: string };

function statusClass(s: string): string {
  const k = s.toLowerCase();
  if (k === "completed") return "crm-chip--done";
  if (k === "on-going" || k === "production" || k === "script") return "crm-chip--active";
  if (k === "cancelled" || k === "dropped") return "crm-chip--dead";
  return "crm-chip--idle";
}
function priClass(p: string): string {
  const k = p.toLowerCase();
  return k === "high" ? "crm-chip--high" : k === "low" ? "crm-chip--low" : "crm-chip--med";
}

// ── Episode table (inline-editable) ──────────────────────────────────────────

const CREW_ROLES = ["scriptwriter", "concept_creator", "ai_creator", "editor"] as const;

function EpisodeTable({
  seriesId,
  crewNames,
  onCrewAdded,
}: {
  seriesId: string;
  crewNames: string[];
  onCrewAdded: (name: string) => void;
}) {
  const [rows, setRows] = useState<SceneDTO[] | null>(null);

  useEffect(() => {
    let alive = true;
    void listSeriesEpisodes(seriesId)
      .then((r) => alive && setRows(r))
      .catch(() => alive && setRows([]));
    return () => {
      alive = false;
    };
  }, [seriesId]);

  function patch(ep: SceneDTO, key: string, value: string) {
    setRows((cur) =>
      cur
        ? cur.map((r) =>
            r.id === ep.id
              ? { ...r, production: { ...(r.production ?? {}), [key]: value } }
              : r,
          )
        : cur,
    );
    void patchScene(ep.id, { production: { [key]: value } }).catch(() => {
      toast("Save failed", "error");
    });
  }

  /** Crew cell = a dropdown of known names + the current value + "＋ New…". */
  function CrewSelect({ ep, role }: { ep: SceneDTO; role: string }) {
    const cur = ep.production?.[role] == null ? "" : String(ep.production[role]);
    // Union of the global pool and the current value (so a name not yet in the
    // pool stays selectable) — sorted, case-insensitive.
    const opts = Array.from(new Set([...crewNames, ...(cur ? [cur] : [])])).sort(
      (a, b) => a.toLowerCase().localeCompare(b.toLowerCase()),
    );
    return (
      <select
        className="crm-input crm-input--crew"
        value={cur}
        onChange={(e) => {
          if (e.target.value === "__new__") {
            // eslint-disable-next-line no-alert
            const name = (window.prompt("New name") ?? "").trim();
            if (name) {
              onCrewAdded(name);
              patch(ep, role, name);
            }
            return;
          }
          patch(ep, role, e.target.value);
        }}
      >
        <option value="">—</option>
        {opts.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
        <option value="__new__">＋ New…</option>
      </select>
    );
  }

  if (rows === null) return <div className="crm-sub__loading">Loading episodes…</div>;
  if (rows.length === 0)
    return <div className="crm-sub__empty">No episodes yet in this series.</div>;

  return (
    <div className="crm-sub">
      <table className="crm-eptable">
        <thead>
          <tr>
            <th>Episode</th>
            <th>Status</th>
            <th>Scriptwriter</th>
            <th>Concept</th>
            <th>AI creator</th>
            <th>Editor</th>
            <th>Deadline</th>
            <th>Done</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((ep) => {
            const p = ep.production ?? {};
            const str = (k: string) => (p[k] == null ? "" : String(p[k]));
            return (
              <tr key={ep.id}>
                <td className="crm-eptable__ep">
                  <b>{ep.code || ep.name}</b>
                </td>
                <td>
                  <select
                    className={`crm-input crm-input--status ${statusClass(str("status") || "NotStarted")}`}
                    value={str("status") || "NotStarted"}
                    onChange={(e) => patch(ep, "status", e.target.value)}
                  >
                    {EP_STATUS.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </td>
                {CREW_ROLES.map((role) => (
                  <td key={role}>
                    <CrewSelect ep={ep} role={role} />
                  </td>
                ))}
                <td>
                  <input
                    className="crm-input crm-input--date"
                    type="date"
                    defaultValue={str("deadline")}
                    onBlur={(e) => {
                      if (e.target.value !== str("deadline")) patch(ep, "deadline", e.target.value);
                    }}
                  />
                </td>
                <td>
                  <input
                    className="crm-input crm-input--date"
                    type="date"
                    defaultValue={str("complete_date")}
                    onBlur={(e) => {
                      if (e.target.value !== str("complete_date"))
                        patch(ep, "complete_date", e.target.value);
                    }}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── New / Edit series form ───────────────────────────────────────────────────

interface ProjectLite {
  id: string;
  name: string;
}

function SeriesForm({
  projects,
  editing,
  onClose,
  onSaved,
}: {
  projects: ProjectLite[];
  editing: Row | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const p0 = editing?.production ?? {};
  const [projectId, setProjectId] = useState(editing?.project_id ?? projects[0]?.id ?? "");
  const [name, setName] = useState(editing?.name ?? "");
  const [code, setCode] = useState(editing?.code ?? "");
  const [f, setF] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const k of Object.keys(p0)) init[k] = p0[k] == null ? "" : String(p0[k]);
    return init;
  });
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: string) => setF((s) => ({ ...s, [k]: v }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !name.trim() || !projectId) return;
    setBusy(true);
    try {
      const production = { ...f };
      if (editing) {
        await patchSeries(editing.id, { name: name.trim(), code: code.trim(), production });
        toast(`Saved "${name.trim()}"`);
      } else {
        await createSeries(projectId, { name: name.trim(), code: code.trim(), production });
        toast(`Created "${name.trim()}"`);
      }
      onSaved();
      onClose();
    } catch (err) {
      toast(err instanceof Error ? err.message : "save failed", "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="project-modal-backdrop"
      role="dialog"
      aria-modal="true"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <form className="crm-form" onSubmit={submit}>
        <div className="crm-form__head">
          <h2>{editing ? "Edit series" : "New series"}</h2>
          <button type="button" className="crm-form__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <div className="crm-form__body">
          <section className="crm-form__section">
            <h3>Identity</h3>
            <div className="crm-form__grid">
              <label className="crm-field">
                <span>Project</span>
                <select
                  className="crm-field__input"
                  value={projectId}
                  disabled={!!editing}
                  onChange={(e) => setProjectId(e.target.value)}
                >
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="crm-field crm-field--wide">
                <span>Series name *</span>
                <input
                  className="crm-field__input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="She Wears Special Outfit For Boss"
                  autoFocus
                />
              </label>
              <label className="crm-field">
                <span>Code</span>
                <input
                  className="crm-field__input"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="OUTF"
                />
              </label>
              <label className="crm-field">
                <span>Full code</span>
                <input
                  className="crm-field__input"
                  value={f.full_code ?? ""}
                  onChange={(e) => set("full_code", e.target.value)}
                  placeholder="MOGU_26001_OUTF_…"
                />
              </label>
            </div>
          </section>

          <section className="crm-form__section">
            <h3>Classification</h3>
            <div className="crm-form__grid">
              <label className="crm-field">
                <span>Tier</span>
                <input
                  className="crm-field__input"
                  list="crm-tiers"
                  value={f.tier ?? ""}
                  onChange={(e) => set("tier", e.target.value)}
                  placeholder="A / B / C / D / S"
                />
                <datalist id="crm-tiers">
                  <option>S</option>
                  <option>A</option>
                  <option>B</option>
                  <option>C</option>
                  <option>D</option>
                </datalist>
              </label>
              <label className="crm-field">
                <span>Status</span>
                <select
                  className="crm-field__input"
                  value={f.status ?? "Planning"}
                  onChange={(e) => set("status", e.target.value)}
                >
                  {SERIES_STATUS.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
              <label className="crm-field">
                <span>Priority</span>
                <select
                  className="crm-field__input"
                  value={f.priority ?? "Medium"}
                  onChange={(e) => set("priority", e.target.value)}
                >
                  {PRIORITY.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </section>

          <section className="crm-form__section">
            <h3>Schedule</h3>
            <div className="crm-form__grid">
              <label className="crm-field">
                <span>Start date</span>
                <input className="crm-field__input" type="date" value={f.start_date ?? ""} onChange={(e) => set("start_date", e.target.value)} />
              </label>
              <label className="crm-field">
                <span>End date</span>
                <input className="crm-field__input" type="date" value={f.end_date ?? ""} onChange={(e) => set("end_date", e.target.value)} />
              </label>
              <label className="crm-field">
                <span>Planned episodes</span>
                <input className="crm-field__input" type="number" min={0} value={f.total_episodes_planned ?? ""} onChange={(e) => set("total_episodes_planned", e.target.value)} />
              </label>
              <label className="crm-field">
                <span>Duration / episode (s)</span>
                <input className="crm-field__input" type="number" min={0} value={f.episode_duration_sec ?? ""} onChange={(e) => set("episode_duration_sec", e.target.value)} />
              </label>
              <label className="crm-field">
                <span>Folder link</span>
                <input className="crm-field__input" value={f.folder_link ?? ""} onChange={(e) => set("folder_link", e.target.value)} placeholder="Drive folder…" />
              </label>
            </div>
          </section>

          <section className="crm-form__section">
            <h3>Content</h3>
            <div className="crm-form__grid">
              <label className="crm-field"><span>Genres</span><input className="crm-field__input" value={f.genres ?? ""} onChange={(e) => set("genres", e.target.value)} placeholder="Drama, Romance" /></label>
              <label className="crm-field"><span>Tropes</span><input className="crm-field__input" value={f.tropes ?? ""} onChange={(e) => set("tropes", e.target.value)} placeholder="Climax, Payoff" /></label>
              <label className="crm-field"><span>Target market</span><input className="crm-field__input" value={f.target_market ?? ""} onChange={(e) => set("target_market", e.target.value)} /></label>
              <label className="crm-field"><span>Secondary markets</span><input className="crm-field__input" value={f.secondary_markets ?? ""} onChange={(e) => set("secondary_markets", e.target.value)} /></label>
              <label className="crm-field"><span>Target audience</span><input className="crm-field__input" value={f.target_audience ?? ""} onChange={(e) => set("target_audience", e.target.value)} /></label>
              <label className="crm-field"><span>Original language</span><input className="crm-field__input" value={f.language_original ?? ""} onChange={(e) => set("language_original", e.target.value)} placeholder="Vietnamese" /></label>
              <label className="crm-field crm-field--full">
                <span>Logline</span>
                <textarea className="crm-field__input" rows={2} value={f.logline ?? ""} onChange={(e) => set("logline", e.target.value)} placeholder="One-sentence summary…" />
              </label>
            </div>
          </section>
        </div>

        <div className="crm-form__foot">
          <button type="button" className="btn2 btn2--ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="btn2 btn2--primary" disabled={busy || !name.trim() || !projectId}>
            {busy ? "Saving…" : editing ? "Save" : "Create series"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ── Main CRM ─────────────────────────────────────────────────────────────────

export function ProductionCRM() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [projects, setProjects] = useState<ProjectLite[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [statusF, setStatusF] = useState("");
  const [priF, setPriF] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [formFor, setFormFor] = useState<Row | null | "new">(null);
  const [crewNames, setCrewNames] = useState<string[]>([]);

  useEffect(() => {
    void getCrewNames()
      .then((r) => setCrewNames(r.names))
      .catch(() => {});
  }, []);

  const load = useCallback(async () => {
    try {
      const projs = await listProjects();
      setProjects(projs.map((p) => ({ id: p.id, name: p.name })));
      // Fan out per project (the series list is project-scoped) and flatten.
      const perProject = await Promise.all(
        projs.map((p) =>
          listSeries(p.id)
            .then((ss) => ss.map((s) => ({ ...s, project_name: p.name }) as Row))
            .catch(() => [] as Row[]),
        ),
      );
      setRows(perProject.flat());
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (rows ?? []).filter((r) => {
      const st = String(r.production?.status ?? "");
      const pr = String(r.production?.priority ?? "");
      if (statusF && st !== statusF) return false;
      if (priF && pr !== priF) return false;
      if (!needle) return true;
      return (
        r.name.toLowerCase().includes(needle) ||
        (r.code ?? "").toLowerCase().includes(needle) ||
        r.project_name.toLowerCase().includes(needle)
      );
    });
  }, [rows, q, statusF, priF]);

  if (err) return <div className="admin-error">{err}</div>;

  return (
    <div className="crm">
      <div className="crm__toolbar">
        <input className="crm__search" placeholder="Search series, code, project…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="crm__filter" value={statusF} onChange={(e) => setStatusF(e.target.value)}>
          <option value="">All status</option>
          {SERIES_STATUS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select className="crm__filter" value={priF} onChange={(e) => setPriF(e.target.value)}>
          <option value="">All priority</option>
          {PRIORITY.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <span className="crm__count">{shown.length} series</span>
        <button className="btn2 btn2--primary crm__new" onClick={() => setFormFor("new")}>
          + New series
        </button>
      </div>

      <div className="crm__card">
        {rows === null ? (
          <div className="crm-sub__loading">Loading…</div>
        ) : shown.length === 0 ? (
          <div className="admin2__empty">No series match.</div>
        ) : (
          <table className="crm-table">
            <thead>
              <tr>
                <th className="crm-table__ex" />
                <th>Code</th>
                <th>Series</th>
                <th>Project</th>
                <th>Tier</th>
                <th>Status</th>
                <th>Priority</th>
                <th>Genres</th>
                <th>Start</th>
                <th>End</th>
                <th>Planned</th>
                <th>Progress</th>
                <th className="admin2__th-actions" />
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => {
                const isOpen = open === r.id;
                const p = r.production ?? {};
                const done = r.stats?.by_status?.Completed ?? 0;
                const total = r.stats?.episodes ?? r.episode_count ?? 0;
                const planned = Number(p.total_episodes_planned ?? 0) || total;
                const pct = r.stats?.completion_pct ?? 0;
                return (
                  <Fragment key={r.id}>
                    <tr className="crm-table__row" onClick={() => setOpen(isOpen ? null : r.id)}>
                      <td className="admin2__muted">{isOpen ? "▾" : "▸"}</td>
                      <td className="crm-table__code">{r.code || "—"}</td>
                      <td className="crm-table__name">{r.name}</td>
                      <td className="admin2__muted">{r.project_name}</td>
                      <td>{p.tier ? <span className="crm-chip crm-chip--tier">{p.tier}</span> : "—"}</td>
                      <td>{p.status ? <span className={`crm-chip ${statusClass(String(p.status))}`}>{p.status}</span> : "—"}</td>
                      <td>{p.priority ? <span className={`crm-chip ${priClass(String(p.priority))}`}>{p.priority}</span> : "—"}</td>
                      <td className="crm-table__clip" title={p.genres ? String(p.genres) : ""}>
                        {p.genres ? String(p.genres) : "—"}
                      </td>
                      <td className="admin2__muted crm-table__nowrap">{p.start_date ? String(p.start_date) : "—"}</td>
                      <td className="admin2__muted crm-table__nowrap">{p.end_date ? String(p.end_date) : "—"}</td>
                      <td className="admin2__muted">{p.total_episodes_planned != null && p.total_episodes_planned !== "" ? String(p.total_episodes_planned) : "—"}</td>
                      <td>
                        <div className="crm-prog" title={`${done}/${planned} episodes`}>
                          <div className="crm-prog__bar">
                            <span style={{ width: `${Math.min(100, pct)}%` }} />
                          </div>
                          <span className="crm-prog__txt">
                            {done}/{planned || "?"}
                          </span>
                        </div>
                      </td>
                      <td className="crm-table__actions">
                        <button
                          className="btn2 btn2--ghost"
                          onClick={(e) => {
                            e.stopPropagation();
                            setFormFor(r);
                          }}
                        >
                          Edit
                        </button>
                      </td>
                    </tr>
                    {isOpen ? (
                      <tr className="crm-table__drill">
                        <td colSpan={13}>
                          <EpisodeTable
                            seriesId={r.id}
                            crewNames={crewNames}
                            onCrewAdded={(n) =>
                              setCrewNames((cur) =>
                                cur.includes(n) ? cur : [...cur, n],
                              )
                            }
                          />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {formFor ? (
        <SeriesForm
          projects={projects}
          editing={formFor === "new" ? null : formFor}
          onClose={() => setFormFor(null)}
          onSaved={() => void load()}
        />
      ) : null}
    </div>
  );
}

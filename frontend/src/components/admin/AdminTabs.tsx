import { Link } from "react-router-dom";
import { Fragment, useCallback, useEffect, useRef, useState } from "react";

import { useRevalidate } from "../../hooks/useRevalidate";

import {
  thumbUrl,
  listProjectImages,
  listSeries,
  uploadImage,
  type BudgetSummaryDTO,
  type ProjectImage,
  type SeriesDTO,
} from "../../api/client";
import { toast } from "../../store/toast";
import { BudgetCell, BudgetPanel } from "../BudgetPanel";
import { TierChip } from "../TierChip";
import { HBars } from "./Charts";
import { ShotGens } from "./ProjectShots";

/* emerald bar colour (validated dark-mode mark) */
const KEPT = "#1f9e57";

/** Shared fetch that surfaces the API's `detail` on failure. */
async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* keep status */
    }
    throw new Error(String(detail));
  }
  return res.json() as Promise<T>;
}

const usd = (v?: number | null) => (v != null ? `$${v.toFixed(2)}` : "—");

function useFetch<T>(url: string) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      setData(await getJson<T>(url));
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "load failed");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [url]);
  useEffect(() => {
    void load();
  }, [load]);
  // Every useFetch-backed admin view refreshes the same way: quietly (no
  // Skeleton flash) on focus + a light interval, so stats/audit/cost stay live.
  useRevalidate(() => void load(true), { intervalMs: 20000 });
  return { data, err, loading, reload: load };
}

function Skeleton() {
  return (
    <div className="admin2__skeleton">
      <div className="admin2__sk-row" />
      <div className="admin2__sk-row" />
      <div className="admin2__sk-row" />
    </div>
  );
}

// ── Overview ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

interface Overview {
  spent_usd: number;
  kept_usd: number;
  wasted_usd: number;
  waste_pct: number;
  kept_clips: number;
  takes: number;
  downloaded_clips: number;
  cost_per_clip: number;
}
interface ModelRow {
  model: string;
  usd: number;
  takes: number;
  pct: number;
}

export function OverviewTab() {
  const ov = useFetch<Overview>("/api/admin/stats/overview");
  const models = useFetch<ModelRow[]>("/api/admin/stats/models");
  const shots = useFetch<AllShotRow[]>("/api/admin/stats/shots");

  if (ov.loading) return <Skeleton />;
  if (ov.err) return <div className="admin-error">{ov.err}</div>;
  const o = ov.data!;

  const modelRows = (models.data ?? []).map((m) => ({
    label: m.model,
    value: m.usd,
    meta: `${m.takes} gens · ${m.pct}%`,
  }));
  const topShots = (shots.data ?? []).slice(0, 8).map((sh) => ({
    label: `${sh.shot_label} · ${sh.project_name}`,
    value: sh.total_usd,
    meta: `${sh.gens} gens`,
  }));

  return (
    <>
      {/* KPI tiles — total spend only */}
      <section className="admin2__stats">
        <div className="stat">
          <span className="stat__label">Total spent</span>
          <span className="stat__value">{usd(o.spent_usd)}</span>
        </div>
        <div className="stat">
          <span className="stat__label">Generations</span>
          <span className="stat__value">{o.takes}</span>
        </div>
        <div className="stat">
          <span className="stat__label">Clips</span>
          <span className="stat__value">{o.kept_clips}</span>
        </div>
        <div className="stat">
          <span className="stat__label">Cost / clip</span>
          <span className="stat__value">{usd(o.cost_per_clip)}</span>
        </div>
      </section>

      <div className="chart-grid">
        <section className="admin2__card admin2__pad chart-card">
          <div className="chart-card__head">
            <h3 className="chart-card__title">Top sequences by spend</h3>
            <span className="chart-card__hint">where the money goes</span>
          </div>
          {shots.loading ? (
            <Skeleton />
          ) : topShots.length === 0 ? (
            <div className="chart-empty">No spend yet.</div>
          ) : (
            <HBars data={topShots} color={KEPT} unit="usd" />
          )}
        </section>

        <section className="admin2__card admin2__pad chart-card">
          <div className="chart-card__head">
            <h3 className="chart-card__title">Spend by model / resolution</h3>
            <span className="chart-card__hint">hover for details</span>
          </div>
          {models.loading ? <Skeleton /> : <HBars data={modelRows} color={KEPT} unit="usd" />}
        </section>
      </div>
    </>
  );
}

// ── Cost per shot ────────────────────────────────────────────────────────────

interface AllShotRow {
  shot_id: string;
  shot_label: string;
  scene_name: string | null;
  project_id: string | null;
  project_name: string;
  total_usd: number;
  gens: number;
  clips: number;
}

// Nested spend: project → series → episode → sequence
// (GET /api/admin/stats/cost-tree).
//
// The series tier was missing, so a project's episodes were listed flat — with two
// series in one project that meant two rows both called "Episode 1" and no way to
// tell which show they belonged to.
interface CostSeq {
  shot_id: string;
  shot_label: string;
  total_usd: number;
  gens: number;
  clips: number;
}
interface CostEpisode {
  scene_id: string;
  code: string;
  name: string;
  total_usd: number;
  gens: number;
  clips: number;
  sequences: CostSeq[];
}
interface CostSeries {
  series_id: string;
  code: string;
  name: string;
  total_usd: number;
  gens: number;
  clips: number;
  episodes: CostEpisode[];
}
interface CostProject {
  project_id: string;
  name: string;
  total_usd: number;
  gens: number;
  clips: number;
  series: CostSeries[];
}

/** Immutable toggle of an id inside a Set (for the expand/collapse state). */
function toggleId(set: Set<string>, id: string): Set<string> {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

export function CostTab() {
  const { data, err, loading } = useFetch<CostProject[]>("/api/admin/stats/cost-tree");
  const [openP, setOpenP] = useState<Set<string>>(new Set());
  const [openSe, setOpenSe] = useState<Set<string>>(new Set());
  const [openE, setOpenE] = useState<Set<string>>(new Set());
  const [openS, setOpenS] = useState<Set<string>>(new Set());

  // Open the projects on arrival, so the series are visible without a click —
  // a fully collapsed tree makes you open a row just to learn what is in it.
  // Seeded once per load, or collapsing a project would spring back open.
  const seeded = useRef<CostProject[] | null>(null);
  useEffect(() => {
    if (!data || seeded.current === data) return;
    seeded.current = data;
    setOpenP(new Set(data.map((p) => p.project_id)));
  }, [data]);

  if (loading) return <Skeleton />;
  if (err) return <div className="admin-error">{err}</div>;
  const projects = data ?? [];

  return (
    <div className="admin2__card">
      {projects.length === 0 ? (
        <div className="admin2__empty">No project has spent anything yet.</div>
      ) : (
        <div className="ctree">
          {projects.map((p) => {
            const pOpen = openP.has(p.project_id);
            return (
              <div key={p.project_id} className="ctree__group">
                <button
                  className="ctree__row ctree__row--project"
                  onClick={() => setOpenP((s) => toggleId(s, p.project_id))}
                >
                  <span className="ctree__main">
                    <span className="ctree__chev">{pOpen ? "▾" : "▸"}</span>
                    <span className="ctree__name">{p.name || "Untitled"}</span>
                    <span className="ctree__meta">
                      {p.series.length} series · {p.gens} gens
                    </span>
                  </span>
                  <span className="ctree__val">{usd(p.total_usd)}</span>
                </button>

                {pOpen ? (
                  <div className="ctree__kids ctree__kids--series">
                    {p.series.map((se) => {
                      const seOpen = openSe.has(se.series_id);
                      return (
                        <div key={se.series_id} className="ctree__group">
                          <button
                            className="ctree__row ctree__row--series"
                            onClick={() => setOpenSe((s) => toggleId(s, se.series_id))}
                          >
                            <span className="ctree__main">
                              <span className="ctree__chev">{seOpen ? "▾" : "▸"}</span>
                              {se.code ? (
                                <span className="ctree__code">{se.code}</span>
                              ) : null}
                              <span className="ctree__name">{se.name || "Untitled series"}</span>
                              <span className="ctree__meta">
                                {se.episodes.length} episode
                                {se.episodes.length === 1 ? "" : "s"}
                              </span>
                            </span>
                            <span className="ctree__val">{usd(se.total_usd)}</span>
                          </button>

                          {seOpen ? (
                            <div className="ctree__kids ctree__kids--episode">
                              {se.episodes.map((e) => {
                                const eOpen = openE.has(e.scene_id);
                                return (
                                  <div key={e.scene_id} className="ctree__group">
                                    <button
                                      className="ctree__row ctree__row--episode"
                                      onClick={() => setOpenE((s) => toggleId(s, e.scene_id))}
                                    >
                                      <span className="ctree__main">
                                        <span className="ctree__chev">{eOpen ? "▾" : "▸"}</span>
                                        {e.code ? (
                                          <span className="ctree__code">{e.code}</span>
                                        ) : null}
                                        <span className="ctree__name">
                                          {e.name || "Untitled episode"}
                                        </span>
                                        <span className="ctree__meta">
                                          {e.sequences.length} sequence
                                          {e.sequences.length === 1 ? "" : "s"}
                                        </span>
                                      </span>
                                      <span className="ctree__val">{usd(e.total_usd)}</span>
                                    </button>

                                    {eOpen ? (
                                      <div className="ctree__kids ctree__kids--sequence">
                                        {e.sequences.length === 0 ? (
                                          <div className="ctree__none">
                                            Nothing generated in this episode yet.
                                          </div>
                                        ) : null}
                                        {e.sequences.map((sq) => {
                                          const sOpen = openS.has(sq.shot_id);
                                          return (
                                            <div key={sq.shot_id} className="ctree__group">
                                              <button
                                                className="ctree__row ctree__row--sequence"
                                                onClick={() =>
                                                  setOpenS((s) => toggleId(s, sq.shot_id))
                                                }
                                              >
                                                <span className="ctree__main">
                                                  <span className="ctree__chev">
                                                    {sOpen ? "▾" : "▸"}
                                                  </span>
                                                  <span className="ctree__name">
                                                    {sq.shot_label}
                                                  </span>
                                                  <span className="ctree__meta">
                                                    {sq.gens} gens · {sq.clips} clip
                                                    {sq.clips === 1 ? "" : "s"}
                                                  </span>
                                                </span>
                                                <span className="ctree__val">
                                                  {usd(sq.total_usd)}
                                                </span>
                                              </button>
                                              {sOpen ? (
                                                <div className="ctree__gens">
                                                  <ShotGens shotId={sq.shot_id} />
                                                </div>
                                              ) : null}
                                            </div>
                                          );
                                        })}
                                      </div>
                                    ) : null}
                                  </div>
                                );
                              })}
                            </div>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Projects ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

interface ProjectCost {
  project_id: string;
  name: string;
  total_usd: number;
  kept_usd: number;
  wasted_usd: number;
  clips: number;
  takes: number;
  downloaded_clips: number;
  waste_pct: number;
}

interface AdminProject {
  id: string;
  name: string;
  owner_user_id: string | null;
  owner_name: string | null;
  // Full assigned set (owner first, then members) — a project can be shared.
  assignee_ids?: string[];
  created_at: string | null;
  thumb_media_id?: string | null;
  settings?: Record<string, unknown>;
  /** Phase 11.1: credit-budget rollup the list endpoint returns. */
  budget?: BudgetSummaryDTO;
}

interface AdminUserLite {
  id: string;
  username: string;
  display_name?: string | null;
  role: string;
  status: string;
}

async function sendJson(url: string, method: string, body?: unknown) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* keep status */
    }
    throw new Error(String(detail));
  }
  return res.status === 204 ? null : res.json();
}

/** Multi-select of users a project is assigned to — a button that opens a
 *  checkbox list. Used both in the create form and per-row in the table. */
function AssigneePicker({
  users,
  selected,
  onChange,
  placeholder,
  disabled,
}: {
  users: AdminUserLite[];
  selected: string[];
  onChange: (ids: string[]) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const nameOf = (id: string) => {
    const u = users.find((x) => x.id === id);
    return u ? u.display_name || u.username : "unknown";
  };
  const label =
    selected.length === 0
      ? placeholder ?? "— Assign to… —"
      : selected.length <= 2
        ? selected.map(nameOf).join(", ")
        : `${nameOf(selected[0])} +${selected.length - 1} more`;

  function toggle(id: string) {
    onChange(
      selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id],
    );
  }

  return (
    <div className="assignee-picker" ref={ref}>
      <button
        type="button"
        className="assignee-picker__btn"
        onClick={() => !disabled && setOpen((o) => !o)}
        disabled={disabled}
        title={selected.length ? selected.map(nameOf).join(", ") : label}
      >
        <span className="assignee-picker__label">{label}</span>
        <span className="assignee-picker__caret" aria-hidden>▾</span>
      </button>
      {open ? (
        <div className="assignee-picker__menu">
          {users.length === 0 ? (
            <div className="assignee-picker__empty">No users</div>
          ) : (
            users.map((u) => (
              <label key={u.id} className="assignee-picker__opt">
                <input
                  type="checkbox"
                  checked={selected.includes(u.id)}
                  onChange={() => toggle(u.id)}
                />
                <span>
                  {u.display_name || u.username}
                  {u.role === "admin" ? " (admin)" : ""}
                </span>
              </label>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

/** Read-only table of a project's Series (same shape as the Production board,
 *  but info-only — customizing anything is done back in the Production tab).
 *  Inline-styled so it always renders regardless of stylesheet caching. */
function ProjectSeriesPreview({ projectId }: { projectId: string }) {
  const [rows, setRows] = useState<SeriesDTO[] | null>(null);
  useEffect(() => {
    let alive = true;
    void listSeries(projectId)
      .then((r) => alive && setRows(r))
      .catch(() => alive && setRows([]));
    return () => {
      alive = false;
    };
  }, [projectId]);

  if (rows === null) return <div className="pshots__empty">Loading series…</div>;
  if (rows.length === 0)
    return <div className="pshots__empty">No series yet — use “Structure” to add one.</div>;

  const th: React.CSSProperties = {
    textAlign: "left",
    padding: "11px 16px",
    fontSize: "0.7rem",
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.5px",
    color: "#8a97a3",
    background: "#1a222b",
    borderBottom: "1px solid #2b3640",
    whiteSpace: "nowrap",
  };
  const td: React.CSSProperties = {
    padding: "11px 16px",
    fontSize: "0.86rem",
    color: "#e7ecf0",
    borderTop: "1px solid #232e39",
    verticalAlign: "middle",
  };
  const val = (s: SeriesDTO, k: string) => {
    const v = (s.production ?? {})[k];
    return v === undefined || v === null || String(v).trim() === "" ? "—" : String(v);
  };

  return (
    <div style={{ padding: "10px 6px 6px", overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", background: "#141b22", borderRadius: 10, overflow: "hidden" }}>
        <thead>
          <tr>
            <th style={th}>Code</th>
            <th style={th}>Series</th>
            <th style={th}>Tier</th>
            <th style={th}>Status</th>
            <th style={th}>Priority</th>
            <th style={th}>Episodes</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id}>
              <td style={{ ...td, fontFamily: "ui-monospace, monospace", color: "#3ddc97", fontWeight: 700 }}>
                {s.code || "—"}
              </td>
              <td style={{ ...td, fontWeight: 600 }}>{s.name}</td>
              <td style={td}>
                <TierChip tier={(s.production ?? {}).tier} />
              </td>
              <td style={td}>{val(s, "status")}</td>
              <td style={td}>{val(s, "priority")}</td>
              <td style={{ ...td, color: "#8a97a3" }}>{s.episode_count ?? 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ProjectsTab() {
  const projects = useFetch<AdminProject[]>("/api/projects");
  const users = useFetch<AdminUserLite[]>("/api/admin/users");
  const costs = useFetch<ProjectCost[]>("/api/admin/stats/projects");

  const [name, setName] = useState("");
  const [assigneeIds, setAssigneeIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // cover picker
  const [coverFor, setCoverFor] = useState<AdminProject | null>(null);
  const [coverImgs, setCoverImgs] = useState<ProjectImage[] | null>(null);
  const [uploading, setUploading] = useState(false);
  // structure modal (Series → Episode/Chapter → Sequence) — no page nav
  // per-shot cost drill-down (which project row is expanded)
  const [openShots, setOpenShots] = useState<string | null>(null);

  const members = (users.data ?? []).filter((u) => u.status !== "suspended");
  // cost lookup by project id, so the management table can show spend inline
  const costById = new Map((costs.data ?? []).map((c) => [c.project_id, c]));

  async function create() {
    if (busy) return;
    const nm = name.trim();
    if (!nm) {
      setErr("Enter a project name.");
      return;
    }
    if (assigneeIds.length === 0) {
      setErr("Assign the project to at least one person.");
      return;
    }
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      await sendJson("/api/projects", "POST", { name: nm, member_user_ids: assigneeIds });
      setName("");
      setAssigneeIds([]);
      setMsg(
        `Created "${nm}" for ${assigneeIds.length} ${
          assigneeIds.length === 1 ? "person" : "people"
        }.`,
      );
      await Promise.all([projects.reload(), costs.reload()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to create project");
    } finally {
      setBusy(false);
    }
  }

  async function setAssignees(p: AdminProject, ids: string[]) {
    const current = p.assignee_ids ?? (p.owner_user_id ? [p.owner_user_id] : []);
    // No-op if the set is unchanged (ignoring order).
    if (
      ids.length === current.length &&
      ids.every((x) => current.includes(x))
    ) {
      return;
    }
    setErr(null);
    setMsg(null);
    try {
      await sendJson(`/api/projects/${p.id}`, "PATCH", { member_user_ids: ids });
      setMsg(
        ids.length === 0
          ? `"${p.name}" is now unassigned.`
          : `"${p.name}" is now shared with ${ids.length} ${
              ids.length === 1 ? "person" : "people"
            }.`,
      );
      await projects.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to update assignees");
    }
  }

  async function remove(p: AdminProject) {
    if (
      !window.confirm(
        `Delete project "${p.name}"?\nAll episodes, sequences, nodes and assets inside will be permanently deleted.`,
      )
    )
      return;
    setErr(null);
    setMsg(null);
    try {
      await sendJson(`/api/projects/${p.id}`, "DELETE");
      setMsg(`Deleted "${p.name}".`);
      await Promise.all([projects.reload(), costs.reload()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to delete");
    }
  }

  async function openCover(p: AdminProject) {
    setCoverFor(p);
    setCoverImgs(null);
    try {
      const { images } = await listProjectImages(p.id);
      setCoverImgs(images);
    } catch {
      setCoverImgs([]);
    }
  }

  async function uploadCover(p: AdminProject, file: File) {
    setUploading(true);
    setErr(null);
    try {
      const { media_id } = await uploadImage(file, p.id);
      await setCover(p, media_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function setCover(p: AdminProject, mediaId: string | null) {
    // Merge into existing settings (update_project replaces the whole object).
    const next = { ...(p.settings ?? {}) };
    if (mediaId) next.cover_media_id = mediaId;
    else delete next.cover_media_id;
    setErr(null);
    try {
      await sendJson(`/api/projects/${p.id}`, "PATCH", { settings: next });
      setMsg(mediaId ? `Cover updated for "${p.name}".` : `"${p.name}" back to auto cover.`);
      setCoverFor(null);
      setCoverImgs(null);
      await projects.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to set cover");
    }
  }

  const rows = projects.data ?? [];

  return (
    <>
      {/* Provision a project on a user's behalf. */}
      <section className="admin2__card admin-proj-new">
        <div className="admin-proj-new__title">Create a project for a member</div>
        <p className="admin2__email" style={{ marginTop: 0 }}>
          Admins create the project and hand it to an owner (its <b>producer</b>). Only assigned
          people — and admins — can see it. From there the producer opens the project and builds
          its <b>Series → Episodes/Chapters → Sequences</b> themselves, and can staff the rest of
          the team from the project's <b>Members</b> panel.
        </p>
        <div className="admin-proj-new__row">
          <input
            className="admin2__search"
            placeholder="Project name…"
            value={name}
            maxLength={120}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void create()}
          />
          <AssigneePicker
            users={members}
            selected={assigneeIds}
            onChange={setAssigneeIds}
            placeholder="— Assign to whom? —"
          />
          <button className="btn2 btn2--primary" onClick={() => void create()} disabled={busy}>
            {busy ? "Creating…" : "+ Create project"}
          </button>
        </div>
        {msg ? <div className="admin-proj-new__ok">{msg}</div> : null}
        {err ? <div className="admin-error" style={{ marginTop: 10 }}>{err}</div> : null}
      </section>

      {/* All projects (admin sees every user's) with owner + spend + actions. */}
      <div className="admin2__card">
        {projects.loading ? (
          <Skeleton />
        ) : rows.length === 0 ? (
          <div className="admin2__empty">
            No projects yet. Create the first one for a member above.
          </div>
        ) : (
          <table className="admin2__table admin2__table--cards admin2__cards-proj">
            <thead>
              <tr>
                <th className="admin-proj__thumb-col" aria-label="Cover" />
                <th>Project</th>
                <th>Assigned to</th>
                <th>Total spent</th>
                <th>Budget</th>
                <th>Clips</th>
                <th className="admin2__th-actions" aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const c = costById.get(p.id);
                const shotsOpen = openShots === p.id;
                return (
                  <Fragment key={p.id}>
                  <tr className={shotsOpen ? "is-open" : undefined}>
                    <td>
                      <button
                        className="admin-proj__thumb"
                        onClick={() => void openCover(p)}
                        title="Set cover image"
                        aria-label="Set cover image"
                      >
                        {p.thumb_media_id ? (
                          <img src={thumbUrl(p.thumb_media_id, 120)} alt="" loading="lazy" decoding="async" />
                        ) : (
                          <span className="admin-proj__thumb-empty">🖼</span>
                        )}
                      </button>
                    </td>
                    <td>
                      <button
                        className="admin-proj__expand"
                        onClick={() => setOpenShots(shotsOpen ? null : p.id)}
                        title="View series in this project"
                      >
                        <span className="admin-proj__chev">{shotsOpen ? "▾" : "▸"}</span>
                        <b>{p.name || "Untitled"}</b>
                      </button>
                    </td>
                    <td data-label="Assigned to">
                      <AssigneePicker
                        users={members}
                        selected={p.assignee_ids ?? (p.owner_user_id ? [p.owner_user_id] : [])}
                        onChange={(ids) => void setAssignees(p, ids)}
                        placeholder="— unassigned —"
                      />
                    </td>
                    <td>{c ? <b>{usd(c.total_usd)}</b> : <span className="admin2__muted">—</span>}</td>
                    <td><BudgetCell budget={p.budget} /></td>
                    <td className="admin2__muted">{c ? c.clips : 0}</td>
                    <td className="admin2__row-actions admin-proj__actions">
                      <Link
                        className="btn2 btn2--primary admin-proj__open"
                        to={`/projects/${p.id}`}
                        state={{ from: "admin" }}
                        title="Open this project"
                      >
                        Open
                      </Link>
                      <button
                        className="btn2 btn2--ghost admin-proj__del"
                        onClick={() => void remove(p)}
                        title="Delete project"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                  {shotsOpen ? (
                    <tr className="admin-proj__shots-row">
                      <td colSpan={7}>
                        {/* BOD sets the ceiling here; a PM tops it up. */}
                        <BudgetPanel scope="project" scopeId={p.id} canSetBase canGrant canDecide />
                        <ProjectSeriesPreview projectId={p.id} />
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

      {/* structure modal — build the hierarchy without leaving the console */}

      {/* cover picker */}
      {coverFor ? (
        <div
          className="cover-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) {
              setCoverFor(null);
              setCoverImgs(null);
            }
          }}
        >
          <div className="cover-modal" role="dialog" aria-label="Set cover image">
            <div className="cover-modal__head">
              <div>
                <h3 className="cover-modal__title">Cover — {coverFor.name}</h3>
                <p className="cover-modal__sub">
                  Pick a generated image as the cover, or use the latest automatically.
                </p>
              </div>
              <button
                className="cover-modal__close"
                onClick={() => {
                  setCoverFor(null);
                  setCoverImgs(null);
                }}
                aria-label="Close"
              >
                ×
              </button>
            </div>

            <div className="cover-modal__auto">
              <label className={`btn2 btn2--primary cover-upload${uploading ? " is-busy" : ""}`}>
                {uploading ? "Uploading…" : "⬆ Upload image"}
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  disabled={uploading}
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) void uploadCover(coverFor!, f);
                    e.currentTarget.value = "";
                  }}
                />
              </label>
              <button className="btn2 btn2--ghost" onClick={() => void setCover(coverFor!, null)}>
                ✨ Use latest generated (auto)
              </button>
            </div>

            {coverImgs === null ? (
              <Skeleton />
            ) : coverImgs.length === 0 ? (
              <div className="admin2__empty">
                No generated images in this project yet. Generate an image first, or it will use
                the monogram.
              </div>
            ) : (
              <div className="cover-grid">
                {coverImgs.map((im) => {
                  const active = coverFor!.settings?.cover_media_id === im.media_id;
                  return (
                    <button
                      key={im.media_id}
                      className={`cover-tile${active ? " is-active" : ""}`}
                      onClick={() => void setCover(coverFor!, im.media_id)}
                      title="Use as cover"
                    >
                      <img src={thumbUrl(im.media_id, 240)} alt="" loading="lazy" decoding="async" />
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      ) : null}
    </>
  );
}

// ── Audit log ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

interface AuditRow {
  id: number;
  created_at?: string | null;
  action: string;
  actor?: string | null;
  target?: string | null;
  ip?: string | null;
  detail?: string | null;
}

export function AuditTab({ fmtTime }: { fmtTime: (iso?: string | null) => string }) {
  const { data, err, loading } = useFetch<AuditRow[]>("/api/admin/audit?limit=300");
  if (loading) return <Skeleton />;
  if (err) return <div className="admin-error">{err}</div>;
  const rows = data ?? [];
  return (
    <div className="admin2__card">
      {rows.length === 0 ? (
        <div className="admin2__empty">No events yet.</div>
      ) : (
        <table className="admin2__table admin2__table--cards admin2__cards-audit">
          <thead>
            <tr>
              <th>Time</th>
              <th>Action</th>
              <th>Actor</th>
              <th>Target</th>
              <th>IP</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id}>
                <td className="admin2__muted">{fmtTime(a.created_at)}</td>
                <td>
                  <span className="chip chip--role-user">{a.action}</span>
                </td>
                <td>{a.actor ?? "—"}</td>
                <td>{a.target ?? "—"}</td>
                <td className="admin2__muted">{a.ip ?? "—"}</td>
                <td className="admin2__muted">{a.detail ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── Registrations: self-service signups awaiting approval ────────────────────

interface RegRow {
  id: string;
  email: string;
  display_name: string | null;
  note: string | null;
  status: string;                 // pending | approved | rejected
  created_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  created_username: string | null;
}

interface ApproveResult {
  username: string;
  email: string;
  temp_password: string;
  email_sent: boolean;
}

function fmtWhen(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export function RegistrationsTab({ onChanged }: { onChanged?: () => void }) {
  const [show, setShow] = useState<"pending" | "all">("pending");
  const { data, err, loading, reload } = useFetch<RegRow[]>(
    show === "pending" ? "/api/admin/registrations?status=pending" : "/api/admin/registrations",
  );
  const [busy, setBusy] = useState<string | null>(null);
  // Approval returns the temp password; keep it on screen so the admin can
  // relay it by hand when the email didn't go out.
  const [relay, setRelay] = useState<ApproveResult | null>(null);

  async function decide(r: RegRow, action: "approve" | "reject") {
    if (busy) return;
    setBusy(r.id);
    try {
      const res = await fetch(`/api/admin/registrations/${r.id}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        let detail = `${res.status}`;
        try {
          detail = (await res.json()).detail ?? detail;
        } catch {
          /* keep status */
        }
        throw new Error(String(detail));
      }
      const out = await res.json();
      if (action === "approve") {
        if (out.email_sent) {
          toast(`Approved ${out.username} — credentials emailed to ${out.email}`);
        } else {
          // Mail failed: do NOT let the password vanish with a toast.
          toast("Approved, but the email failed to send — copy the password below", "error");
          setRelay(out as ApproveResult);
        }
      } else {
        toast(`Rejected ${r.email}`);
      }
      await reload();
      onChanged?.();
    } catch (e) {
      toast(e instanceof Error ? e.message : "action failed", "error");
    } finally {
      setBusy(null);
    }
  }

  if (loading) return <Skeleton />;
  if (err) return <div className="admin-error">{err}</div>;
  const rows = data ?? [];

  return (
    <>
      {relay ? (
        <section className="admin2__card admin2__pad relay">
          <div className="relay__head">
            <b>Send these credentials to {relay.email} manually</b>
            <button className="btn2 btn2--ghost" onClick={() => setRelay(null)}>
              Dismiss
            </button>
          </div>
          <p className="relay__hint">
            The account was created, but the email didn't go out (check the SMTP
            settings). This password is shown once — it won't be recoverable after
            you dismiss this.
          </p>
          <div className="relay__creds">
            <span>Username: <b>{relay.username}</b></span>
            <span>Password: <b>{relay.temp_password}</b></span>
          </div>
          <button
            className="btn2 btn2--primary"
            onClick={() => {
              void navigator.clipboard.writeText(
                `Username: ${relay.username}\nPassword: ${relay.temp_password}`,
              );
              toast("Copied to clipboard");
            }}
          >
            Copy
          </button>
        </section>
      ) : null}

      <div className="admin2__toolbar">
        <span className="admin2__count">
          {rows.length} {show === "pending" ? "pending" : "total"}
        </span>
        <button
          className="btn2 btn2--ghost"
          onClick={() => setShow(show === "pending" ? "all" : "pending")}
        >
          {show === "pending" ? "Show all" : "Show pending only"}
        </button>
      </div>

      <div className="admin2__card">
        {rows.length === 0 ? (
          <div className="admin2__empty">
            {show === "pending"
              ? "No one is waiting for approval."
              : "No signup requests yet."}
          </div>
        ) : (
          <table className="admin2__table">
            <thead>
              <tr>
                <th>Applicant</th>
                <th>Requested</th>
                <th>Reason</th>
                <th>Status</th>
                <th className="admin2__th-actions" aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>
                    <div className="admin2__user-txt">
                      <span className="admin2__name">{r.display_name || r.email}</span>
                      <span className="admin2__email">{r.email}</span>
                    </div>
                  </td>
                  <td className="admin2__muted">{fmtWhen(r.created_at)}</td>
                  <td className="admin2__muted reg__note" title={r.note ?? ""}>
                    {r.note || "—"}
                  </td>
                  <td>
                    {r.status === "pending" ? (
                      <span className="chip chip--suspended">pending</span>
                    ) : r.status === "approved" ? (
                      // username == the email in the Applicant column, so don't repeat it
                      <span className="chip chip--active">approved</span>
                    ) : (
                      <span className="chip chip--role-user">rejected</span>
                    )}
                  </td>
                  <td className="admin2__row-actions admin-proj__actions">
                    {r.status === "pending" ? (
                      <>
                        <button
                          className="btn2 btn2--primary"
                          disabled={busy === r.id}
                          onClick={() => void decide(r, "approve")}
                        >
                          {busy === r.id ? "…" : "Approve"}
                        </button>
                        <button
                          className="btn2 btn2--ghost admin-proj__del"
                          disabled={busy === r.id}
                          onClick={() => void decide(r, "reject")}
                        >
                          Reject
                        </button>
                      </>
                    ) : (
                      <span className="admin2__muted">
                        {r.decided_by ? `by ${r.decided_by}` : "—"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

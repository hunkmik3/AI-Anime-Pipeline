import { Fragment, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { thumbUrl, listProjectImages, uploadImage, type ProjectImage } from "../../api/client";
import { HBars } from "./Charts";
import { ProjectShots, ShotGens } from "./ProjectShots";

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
  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await getJson<T>(url));
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "load failed");
    } finally {
      setLoading(false);
    }
  }, [url]);
  useEffect(() => {
    void load();
  }, [load]);
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

export function CostTab() {
  const { data, err, loading } = useFetch<AllShotRow[]>("/api/admin/stats/shots");
  const [open, setOpen] = useState<string | null>(null);

  if (loading) return <Skeleton />;
  if (err) return <div className="admin-error">{err}</div>;
  const rows = data ?? [];
  const total = rows.reduce((s, r) => s + r.total_usd, 0);

  return (
    <div className="admin2__card">
      <p className="tab-note">
        Money spent generating on each <b>sequence</b>, priciest first — straight from the Avis
        bill. Click a sequence to see every generation (who, model, cost). Total: <b>{usd(total)}</b>.
      </p>
      {rows.length === 0 ? (
        <div className="admin2__empty">No sequence has spent anything yet.</div>
      ) : (
        <table className="admin2__table">
          <thead>
            <tr>
              <th className="pshots__th-ex" />
              <th>Sequence</th>
              <th>Project · Episode</th>
              <th>Total spent</th>
              <th>Gens</th>
              <th>Clips</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((sh) => {
              const isOpen = open === sh.shot_id;
              return (
                <Fragment key={sh.shot_id}>
                  <tr
                    className="row-click"
                    onClick={() => setOpen(isOpen ? null : sh.shot_id)}
                  >
                    <td className="admin2__muted">{isOpen ? "▾" : "▸"}</td>
                    <td>
                      <b>{sh.shot_label}</b>
                    </td>
                    <td className="admin2__muted">
                      {sh.project_name}
                      {sh.scene_name ? ` · ${sh.scene_name}` : ""}
                    </td>
                    <td>
                      <b>{usd(sh.total_usd)}</b>
                    </td>
                    <td className="admin2__muted">{sh.gens}</td>
                    <td className="admin2__muted">{sh.clips}</td>
                  </tr>
                  {isOpen ? (
                    <tr>
                      <td colSpan={6} className="drill">
                        <ShotGens shotId={sh.shot_id} />
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
  created_at: string | null;
  thumb_media_id?: string | null;
  settings?: Record<string, unknown>;
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

export function ProjectsTab() {
  const projects = useFetch<AdminProject[]>("/api/projects");
  const users = useFetch<AdminUserLite[]>("/api/admin/users");
  const costs = useFetch<ProjectCost[]>("/api/admin/stats/projects");

  const [name, setName] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // cover picker
  const [coverFor, setCoverFor] = useState<AdminProject | null>(null);
  const [coverImgs, setCoverImgs] = useState<ProjectImage[] | null>(null);
  const [uploading, setUploading] = useState(false);
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
    if (!ownerId) {
      setErr("Choose an owner for the project.");
      return;
    }
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      await sendJson("/api/projects", "POST", { name: nm, owner_user_id: ownerId });
      setName("");
      setOwnerId("");
      const who = members.find((u) => u.id === ownerId);
      setMsg(`Created "${nm}" for ${who?.display_name || who?.username || "user"}.`);
      await Promise.all([projects.reload(), costs.reload()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to create project");
    } finally {
      setBusy(false);
    }
  }

  async function reassign(p: AdminProject, newOwner: string) {
    if (!newOwner || newOwner === (p.owner_user_id ?? "")) return;
    setErr(null);
    setMsg(null);
    try {
      await sendJson(`/api/projects/${p.id}`, "PATCH", { owner_user_id: newOwner });
      const who = members.find((u) => u.id === newOwner);
      setMsg(`"${p.name}" reassigned to ${who?.display_name || who?.username}.`);
      await projects.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to reassign");
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
          Only admins can create projects · episodes · sequences. Pick an owner — only they (and
          admins) can see &amp; work inside that project. After creating it, hit <b>Open</b> to add
          episodes and sequences.
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
          <select
            className="admin2__search"
            value={ownerId}
            onChange={(e) => setOwnerId(e.target.value)}
          >
            <option value="">— Assign to whom? —</option>
            {members.map((u) => (
              <option key={u.id} value={u.id}>
                {u.display_name || u.username}
                {u.role === "admin" ? " (admin)" : ""}
              </option>
            ))}
          </select>
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
          <table className="admin2__table">
            <thead>
              <tr>
                <th className="admin-proj__thumb-col" aria-label="Cover" />
                <th>Project</th>
                <th>Owner</th>
                <th>Total spent</th>
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
                        title="View cost per sequence"
                      >
                        <span className="admin-proj__chev">{shotsOpen ? "▾" : "▸"}</span>
                        <b>{p.name || "Untitled"}</b>
                      </button>
                    </td>
                    <td>
                      <select
                        className="admin-proj__owner"
                        value={p.owner_user_id ?? ""}
                        onChange={(e) => void reassign(p, e.target.value)}
                      >
                        {!p.owner_user_id ? <option value="">— unassigned —</option> : null}
                        {members.map((u) => (
                          <option key={u.id} value={u.id}>
                            {u.display_name || u.username}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>{c ? <b>{usd(c.total_usd)}</b> : <span className="admin2__muted">—</span>}</td>
                    <td className="admin2__muted">{c ? c.clips : 0}</td>
                    <td className="admin2__row-actions admin-proj__actions">
                      <button
                        className="btn2 btn2--ghost"
                        onClick={() => void openCover(p)}
                        title="Set cover image"
                      >
                        Cover
                      </button>
                      <Link
                        className="btn2 btn2--primary admin-proj__open"
                        to={`/projects/${p.id}`}
                        title="Open the project to add episodes & sequences"
                      >
                        Open →
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
                      <td colSpan={6}>
                        <ProjectShots projectId={p.id} />
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
        <table className="admin2__table">
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

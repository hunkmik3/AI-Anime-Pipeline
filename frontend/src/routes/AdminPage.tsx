import { Fragment, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useAuthStore } from "../store/auth";
import { parseServerTimeMs } from "../utils/serverTime";
import { ConfirmDialog, PromptDialog } from "../components/Modals";
import { KebabMenu } from "../components/KebabMenu";
import { CreateUserDialog, type NewUser } from "../components/CreateUserDialog";
import { toast } from "../store/toast";
import {
  OverviewTab,
  CostTab,
  ProjectsTab,
  AuditTab,
  RegistrationsTab,
} from "../components/admin/AdminTabs";

interface AdminUser {
  id: string;
  username: string;
  role: string;
  status: string;
  display_name?: string | null;
  email?: string | null;
  last_login?: string | null;
  must_change_password?: boolean;
  has_password?: boolean;   // false = Google-SSO account
  created_at?: string | null;
  budget_usd?: number;
  spent_usd?: number;
  available_usd?: number;
}

interface ActivityItem {
  request_id: number | null;
  created_at?: string | null;
  finished_at?: string | null;
  kind?: string | null;
  model?: string | null;
  ledger_status?: string | null; // reserved | settled | released | null (not metered)
  estimated_usd?: number | null;
  actual_usd?: number | null;
  cost_usd?: number | null; // null = not metered (free / pre-budget)
  request_type?: string | null;
  request_status?: string | null;
  error?: string | null;
  duration_seconds?: number | null;
  resolution?: string | null;
  prompt?: string | null;
  inputs?: { id: string; label: string }[];
  params?: Record<string, unknown>;
  video_url?: string | null;
  media_ids: string[];
}

interface PoolSummary {
  pool_usd: number;
  spent_usd: number;
  reserved_usd: number;
  available_usd: number;
  granted_usd: number;
  user_remaining_usd: number;
  configured: boolean;
  over_allocated: boolean;
  exhausted: boolean;
}

const usd = (v?: number | null): string => (v != null ? `$${v.toFixed(2)}` : "—");

function fmtParamValue(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

interface ActivityData {
  user: AdminUser;
  summary: {
    budget_usd: number;
    spent_usd: number;
    reserved_usd: number;
    available_usd: number;
    gen_count: number;
    shown: number;
  };
  items: ActivityItem[];
}

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const ms = parseServerTimeMs(iso);
  return ms ? new Date(ms).toLocaleString() : "—";
}

/** "just now" / "3h ago" / "2d ago" — friendlier than a raw stamp. */
function relTime(iso?: string | null): string {
  if (!iso) return "never logged in";
  const ms = parseServerTimeMs(iso);
  if (!ms) return "—";
  const diff = Math.max(0, Date.now() - ms);
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(ms).toLocaleDateString();
}

function initials(u: { display_name?: string | null; username: string }): string {
  const src = (u.display_name || u.username).trim();
  const parts = src.split(/\s+/).filter(Boolean);
  const s =
    parts.length >= 2
      ? parts[0][0] + parts[parts.length - 1][0]
      : src.slice(0, 2);
  return s.toUpperCase();
}

async function jsonOrThrow(res: Response) {
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* keep status */
    }
    throw new Error(String(detail));
  }
  return res.json();
}

/** Minimal inline icon set for the sidebar nav (keeps the console self-
 *  contained — no icon-font dependency). Stroke inherits currentColor. */
function NavIcon({ name }: { name: string }) {
  const p: Record<string, string> = {
    overview: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6V11h-6v9Zm0-16v5h6V4h-6Z",
    members:
      "M16 11a4 4 0 1 0-4-4 4 4 0 0 0 4 4Zm-8 0a4 4 0 1 0-4-4 4 4 0 0 0 4 4Zm0 2c-2.7 0-8 1.3-8 4v3h9v-3c0-1 .4-1.9 1.1-2.7C6.9 13.1 8 13 8 13Zm8 0c-.3 0-.7 0-1.2.1 1.3 1 2.2 2.3 2.2 3.9v3h7v-3c0-2.7-5.3-4-8-4Z",
    cost: "M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6",
    signups:
      "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM19 8v6M22 11h-6",
    projects:
      "M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2Z",
    audit:
      "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6ZM14 2v6h6M8 13h8M8 17h5",
  };
  const fill = name === "overview" || name === "projects";
  return (
    <svg
      className="dash__ico"
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill={fill ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={p[name] ?? p.overview} />
    </svg>
  );
}

export function AdminPage() {
  const me = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // create-member modal + search
  const [createOpen, setCreateOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);

  // global Avis pool (Avis has no balance API — admin enters the top-up)
  const [pool, setPool] = useState<PoolSummary | null>(null);
  const [poolOpen, setPoolOpen] = useState(false);

  // activity modal
  const [activityUser, setActivityUser] = useState<AdminUser | null>(null);
  const [activity, setActivity] = useState<ActivityData | null>(null);
  const [activityLoading, setActivityLoading] = useState(false);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  // which tab is showing
  const [tab, setTab] = useState<
    "overview" | "members" | "signups" | "cost" | "projects" | "audit"
  >("overview");

  // pending signup count → sidebar badge (so a request isn't missed)
  const [pendingSignups, setPendingSignups] = useState(0);
  const refreshPending = useCallback(async () => {
    try {
      const r = await jsonOrThrow(await fetch("/api/admin/registrations/pending-count"));
      setPendingSignups(r.count ?? 0);
    } catch {
      /* badge is best-effort */
    }
  }, []);

  // Which action modal is open (replaces window.prompt / confirm).
  const [modal, setModal] = useState<
    { kind: "password" | "budget" | "delete"; user: AdminUser } | null
  >(null);


  function toggleExpand(key: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function openActivity(u: AdminUser) {
    setActivityUser(u);
    setActivity(null);
    setActivityError(null);
    setExpanded(new Set());
    setActivityLoading(true);
    try {
      setActivity(await jsonOrThrow(await fetch(`/api/admin/users/${u.id}/activity?limit=200`)));
    } catch (e) {
      setActivityError(e instanceof Error ? e.message : "load failed");
    } finally {
      setActivityLoading(false);
    }
  }

  function closeActivity() {
    setActivityUser(null);
    setActivity(null);
    setActivityError(null);
    setExpanded(new Set());
  }

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [us, pl] = await Promise.all([
        jsonOrThrow(await fetch("/api/admin/users")),
        jsonOrThrow(await fetch("/api/admin/pool")),
      ]);
      setUsers(us);
      setPool(pl);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    } finally {
      setLoading(false);
    }
  }, []);

  async function savePool(v: number) {
    try {
      setPool(
        await jsonOrThrow(
          await fetch("/api/admin/pool", {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pool_usd: v }),
          }),
        ),
      );
      setPoolOpen(false);
      toast(`Updated Avis pool: $${v.toFixed(2)}`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "pool update failed";
      setError(msg);
      toast(msg, "error");
    }
  }

  useEffect(() => {
    void refresh();
    void refreshPending();
  }, [refresh, refreshPending]);

  async function createUser(nu: NewUser) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await jsonOrThrow(
        await fetch("/api/admin/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(nu),
        }),
      );
      setCreateOpen(false);
      await refresh();
      toast(`Created account "${nu.username}"`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "create failed";
      setError(msg);
      toast(msg, "error");
    } finally {
      setBusy(false);
    }
  }

  async function patchUser(id: string, body: Record<string, unknown>, successMsg?: string) {
    setError(null);
    try {
      await jsonOrThrow(
        await fetch(`/api/admin/users/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
      );
      await refresh();
      if (successMsg) toast(successMsg);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "update failed";
      setError(msg);
      toast(msg, "error");
    }
  }

  async function doDelete(u: AdminUser) {
    setError(null);
    try {
      await jsonOrThrow(await fetch(`/api/admin/users/${u.id}`, { method: "DELETE" }));
      await refresh();
      toast(`Deleted account "${u.username}"`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "delete failed";
      setError(msg);
      toast(msg, "error");
    }
  }

  // Derived: search filter + summary stats.
  const q = search.trim().toLowerCase();
  const shown = users.filter(
    (u) =>
      !q ||
      u.username.toLowerCase().includes(q) ||
      (u.display_name ?? "").toLowerCase().includes(q) ||
      (u.email ?? "").toLowerCase().includes(q),
  );
  const totalAvailable = users.reduce((s, u) => s + (u.available_usd ?? 0), 0);
  const activeCount = users.filter((u) => u.status === "active").length;
  const suspendedCount = users.length - activeCount;

  const TABS = [
    ["overview", "Overview", "overview"],
    ["members", "Members", "members"],
    ["signups", "Sign-ups", "signups"],
    ["cost", "Cost", "cost"],
    ["projects", "Projects", "projects"],
    ["audit", "Audit log", "audit"],
  ] as const;
  const SUBTITLES: Record<string, string> = {
    overview: "Pool and real spend at a glance — straight from the Avis bill.",
    members: "Provision accounts, budgets and roles for your team.",
    signups:
      "People who requested an account. Approving one emails them a temporary password.",
    cost: "Total spend per project, broken down into episodes and sequences.",
    projects: "Create and assign projects to members — only admins can build the structure.",
    audit: "Security log: logins, SSO, and every admin action.",
  };
  const curLabel = TABS.find(([k]) => k === tab)?.[1] ?? "Dashboard";

  return (
    <div className="dash">
      {/* ── left sidebar nav (Dasher) ── */}
      <aside className="dash__side">
        <Link to="/projects" className="dash__brand">
          <img src="/favicon.png" alt="" width={30} height={30} />
          <span className="dash__brand-txt">
            Giant Studio
            <small>Admin console</small>
          </span>
        </Link>

        <nav className="dash__nav" role="tablist" aria-label="Admin sections">
          {TABS.map(([k, label, icon]) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              className={`dash__nav-item${tab === k ? " is-active" : ""}`}
              onClick={() => setTab(k)}
            >
              <NavIcon name={icon} />
              <span>{label}</span>
              {k === "signups" && pendingSignups > 0 ? (
                <span className="dash__nav-badge" aria-label={`${pendingSignups} waiting`}>
                  {pendingSignups}
                </span>
              ) : null}
            </button>
          ))}
        </nav>

        <div className="dash__side-foot">
          <div className="dash__me">
            <span className="dash__me-avatar" aria-hidden="true">
              {initials({ display_name: me?.display_name, username: me?.username ?? "?" })}
            </span>
            <span className="dash__me-txt">
              <b>{me?.display_name || me?.username}</b>
              <small>Admin</small>
            </span>
          </div>
          <Link to="/projects" className="dash__side-link">
            ← Back to app
          </Link>
          <button className="dash__side-link" onClick={() => logout()}>
            Sign out
          </button>
        </div>
      </aside>

      {/* ── main column ── */}
      <div className="dash__main">
        <header className="dash__topbar">
          <div>
            <h1 className="dash__title">{curLabel}</h1>
            <p className="dash__sub">{SUBTITLES[tab]}</p>
          </div>
          <div className="dash__topbar-actions">
            {tab === "members" ? (
              <button className="btn2 btn2--primary" onClick={() => setCreateOpen(true)}>
                + Add member
              </button>
            ) : null}
          </div>
        </header>

        <main className="dash__content">
          {error ? <div className="admin-error">{error}</div> : null}

      {/* ───────────────── TỔNG QUAN ───────────────── */}
      {tab === "overview" ? (
        <>
          {/* Avis exposes no balance API — the admin enters the top-up and we
              draw it down against the real per-generation usdCost. */}
          <section
            className={`pool${pool?.exhausted ? " pool--danger" : pool?.over_allocated ? " pool--warn" : ""}`}
          >
            <div className="pool__head">
              <span className="pool__title">Avis pool (real API-key balance)</span>
              <button className="btn2 btn2--ghost pool__edit" onClick={() => setPoolOpen(true)}>
                {pool?.configured ? "Update balance" : "Enter balance"}
              </button>
            </div>

            {!pool?.configured ? (
              <p className="pool__hint">
                Avis has no balance API — open the <b>Avis dashboard</b>, copy the current
                balance and enter it here. The system draws it down by the <b>real cost</b> of
                each generation.
              </p>
            ) : (
              <>
                <div className="pool__nums">
                  <span>
                    <b>${pool.pool_usd.toFixed(2)}</b> topped up
                  </span>
                  <span className="pool__sep">·</span>
                  <span>${pool.spent_usd.toFixed(2)} spent</span>
                  <span className="pool__sep">·</span>
                  <span>${pool.reserved_usd.toFixed(2)} on hold</span>
                  <span className="pool__sep">·</span>
                  <span className="pool__avail">
                    <b>${pool.available_usd.toFixed(2)}</b> left
                  </span>
                </div>
                <div className="pool__bar">
                  <span
                    style={{
                      width: `${pool.pool_usd > 0 ? Math.min(100, ((pool.spent_usd + pool.reserved_usd) / pool.pool_usd) * 100) : 0}%`,
                    }}
                  />
                </div>
                {pool.exhausted ? (
                  <p className="pool__alert">
                    🚫 <b>Pool exhausted</b> — every new generation request will be rejected.
                    Top up on Avis, then update the balance here.
                  </p>
                ) : pool.over_allocated ? (
                  <p className="pool__alert">
                    ⚠️ <b>Over-allocated</b>: users can still spend a total of{" "}
                    <b>${pool.user_remaining_usd.toFixed(2)}</b> but the pool only has{" "}
                    <b>${pool.available_usd.toFixed(2)}</b> left. Top up or lower the budgets.
                  </p>
                ) : (
                  <p className="pool__ok">
                    ✓ Healthy — users can still spend ${pool.user_remaining_usd.toFixed(2)} in
                    total, within the remaining pool.
                  </p>
                )}
              </>
            )}
          </section>

          <OverviewTab />
        </>
      ) : null}

      {/* ───────────────── THÀNH VIÊN ───────────────── */}
      {tab === "members" ? (
        <>
          <section className="admin2__stats">
            <div className="stat">
              <span className="stat__label">Members</span>
              <span className="stat__value">{users.length}</span>
            </div>
            <div className="stat">
              <span className="stat__label">Active</span>
              <span className="stat__value stat__value--good">{activeCount}</span>
            </div>
            <div className="stat">
              <span className="stat__label">Suspended</span>
              <span className={`stat__value${suspendedCount ? " stat__value--warn" : ""}`}>
                {suspendedCount}
              </span>
            </div>
            <div className="stat">
              <span className="stat__label">Budget remaining</span>
              <span className="stat__value">${totalAvailable.toFixed(2)}</span>
            </div>
          </section>

          <div className="admin2__toolbar">
            <input
              className="admin2__search"
              placeholder="Search by name, username or email…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <span className="admin2__count">
              {shown.length}/{users.length} members
            </span>
          </div>

          <div className="admin2__card">
            {loading ? (
              <div className="admin2__skeleton">
                <div className="admin2__sk-row" />
                <div className="admin2__sk-row" />
                <div className="admin2__sk-row" />
              </div>
            ) : shown.length === 0 ? (
              <div className="admin2__empty">
                {users.length === 0
                  ? "No members yet — click “+ Add member” to get started."
                  : "No matching members found."}
              </div>
            ) : (
              <table className="admin2__table">
                <thead>
                  <tr>
                    <th>Member</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Budget</th>
                    <th>Last login</th>
                    <th className="admin2__th-actions" aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {shown.map((u) => {
                    const budget = u.budget_usd ?? 0;
                    const spent = u.spent_usd ?? 0;
                    const pct = budget > 0 ? Math.min(100, (spent / budget) * 100) : 0;
                    const isMe = u.id === me?.id;
                    const isSso = u.has_password === false;
                    return (
                      <tr
                        key={u.id}
                        className={u.status === "suspended" ? "is-suspended" : undefined}
                      >
                        <td>
                          <div className="admin2__user">
                            <span className="admin2__avatar" aria-hidden="true">
                              {initials(u)}
                            </span>
                            <span className="admin2__user-txt">
                              <span className="admin2__name">
                                {u.display_name || u.username}
                                {isMe ? <span className="admin2__you">you</span> : null}
                              </span>
                              <span className="admin2__email">{u.email || u.username}</span>
                            </span>
                          </div>
                        </td>
                        <td>
                          <span className={`chip chip--role-${u.role}`}>{u.role}</span>
                          {isSso ? <span className="chip chip--google">Google</span> : null}
                        </td>
                        <td>
                          <span className={`chip chip--${u.status}`}>
                            {u.status === "active" ? "Active" : "Suspended"}
                          </span>
                        </td>
                        <td>
                          <div className="admin2__budget">
                            <span className="admin2__budget-nums">
                              <b>{usd(u.available_usd)}</b> left / {usd(budget)}
                            </span>
                            <span className="admin2__bar">
                              <span style={{ width: `${pct}%` }} />
                            </span>
                          </div>
                        </td>
                        <td className="admin2__muted">{relTime(u.last_login)}</td>
                        <td className="admin2__row-actions">
                          <KebabMenu
                            items={[
                              { label: "View activity", onSelect: () => void openActivity(u) },
                              {
                                label: "Set budget",
                                onSelect: () => setModal({ kind: "budget", user: u }),
                              },
                              ...(isMe
                                ? []
                                : [
                                    {
                                      label:
                                        u.status === "active" ? "Suspend account" : "Reactivate",
                                      onSelect: () =>
                                        void patchUser(
                                          u.id,
                                          {
                                            status:
                                              u.status === "active" ? "suspended" : "active",
                                          },
                                          u.status === "active"
                                            ? `Suspended "${u.username}"`
                                            : `Reactivated "${u.username}"`,
                                        ),
                                    },
                                    {
                                      label:
                                        u.role === "admin" ? "Demote to user" : "Promote to admin",
                                      onSelect: () =>
                                        void patchUser(
                                          u.id,
                                          { role: u.role === "admin" ? "user" : "admin" },
                                          `Changed role for "${u.username}"`,
                                        ),
                                    },
                                    {
                                      label: isSso ? "Set password" : "Reset password",
                                      onSelect: () => setModal({ kind: "password", user: u }),
                                    },
                                    {
                                      label: "Delete account",
                                      danger: true,
                                      onSelect: () => setModal({ kind: "delete", user: u }),
                                    },
                                  ]),
                            ]}
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </>
      ) : null}

      {/* ───────────────── CHI PHÍ / DỰ ÁN / NHẬT KÝ ───────────────── */}
      {tab === "signups" ? (
        <RegistrationsTab
          onChanged={() => {
            void refreshPending();
            void refresh();
          }}
        />
      ) : null}
      {tab === "cost" ? <CostTab /> : null}
      {tab === "projects" ? <ProjectsTab /> : null}
      {tab === "audit" ? <AuditTab fmtTime={fmtTime} /> : null}
        </main>

      {createOpen ? (
        <CreateUserDialog
          busy={busy}
          onSubmit={(nu) => void createUser(nu)}
          onClose={() => setCreateOpen(false)}
        />
      ) : null}

      {poolOpen ? (
        <PromptDialog
          title="Avis pool balance"
          label="Current balance on the Avis dashboard ($)"
          type="number"
          initial={String(pool?.pool_usd ?? 0)}
          placeholder="e.g. 444.32"
          submitLabel="Save"
          validate={(v) => {
            const n = Number(v);
            return !Number.isFinite(n) || n < 0 ? "Invalid amount" : null;
          }}
          onSubmit={(v) => void savePool(Number(v))}
          onClose={() => setPoolOpen(false)}
        />
      ) : null}


      {modal?.kind === "password" && (
        <PromptDialog
          title={`Change password — ${modal.user.username}`}
          label="New password (≥ 8 characters)"
          type="password"
          submitLabel="Set password"
          validate={(v) => (v.length < 8 ? "Password must be at least 8 characters" : null)}
          onSubmit={(v) => {
            void patchUser(
              modal.user.id,
              { password: v },
              `Set a temporary password for "${modal.user.username}" — they must change it on next login`,
            );
            setModal(null);
          }}
          onClose={() => setModal(null)}
        />
      )}
      {modal?.kind === "budget" && (
        <PromptDialog
          title={`Budget — ${modal.user.username}`}
          label="Budget $ (total)"
          type="number"
          initial={String(modal.user.budget_usd ?? 0)}
          submitLabel="Save"
          validate={(v) => {
            const n = Number(v);
            return !Number.isFinite(n) || n < 0 ? "Invalid amount" : null;
          }}
          onSubmit={(v) => {
            void patchUser(
              modal.user.id,
              { budget_usd: Number(v) },
              `Set budget $${Number(v).toFixed(2)} for "${modal.user.username}"`,
            );
            setModal(null);
          }}
          onClose={() => setModal(null)}
        />
      )}
      {modal?.kind === "delete" && (
        <ConfirmDialog
          title={`Delete account "${modal.user.username}"?`}
          danger
          confirmLabel="Delete"
          message={
            <>
              Their projects will be <b>un-owned</b> (generated data is NOT deleted). This
              action cannot be undone.
            </>
          }
          onConfirm={() => {
            void doDelete(modal.user);
            setModal(null);
          }}
          onClose={() => setModal(null)}
        />
      )}

      {activityUser && (
        <div
          className="admin-activity-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeActivity();
          }}
        >
          <div className="admin-activity" role="dialog" aria-label="User activity">
            <div className="admin-activity__head">
              <h2>
                Activity — {activityUser.display_name || activityUser.username}
              </h2>
              <button
                className="admin-activity__close"
                onClick={closeActivity}
                aria-label="Close"
              >
                ×
              </button>
            </div>

            {activityLoading ? (
              <div className="admin-loading">Loading…</div>
            ) : activityError ? (
              <div className="admin-error">{activityError}</div>
            ) : activity ? (
              <>
                <div className="admin-activity__summary">
                  <div>
                    <span>Budget</span>
                    <b>${activity.summary.budget_usd.toFixed(2)}</b>
                  </div>
                  <div>
                    <span>Spent</span>
                    <b className="admin-activity__spent">
                      ${activity.summary.spent_usd.toFixed(2)}
                    </b>
                  </div>
                  <div>
                    <span>On hold</span>
                    <b>${activity.summary.reserved_usd.toFixed(2)}</b>
                  </div>
                  <div>
                    <span>Available</span>
                    <b>${activity.summary.available_usd.toFixed(2)}</b>
                  </div>
                  <div>
                    <span>Generations</span>
                    <b>{activity.summary.gen_count}</b>
                  </div>
                </div>

                {activity.items.length === 0 ? (
                  <div className="admin-activity__empty">No generations yet.</div>
                ) : (
                  <div className="admin-activity__scroll">
                    <table className="admin-activity__table">
                      <thead>
                        <tr>
                          <th>Time</th>
                          <th>Type / Model</th>
                          <th>Params</th>
                          <th>Cost</th>
                          <th>Status</th>
                          <th>Details</th>
                        </tr>
                      </thead>
                      <tbody>
                        {activity.items.map((it, i) => {
                          const isOpen = expanded.has(i);
                          const isVideo =
                            it.kind === "video" || it.request_type === "gen_video";
                          return (
                            <Fragment key={it.request_id ?? i}>
                              <tr className={isOpen ? "admin-activity__row--open" : undefined}>
                                <td>{fmtTime(it.created_at)}</td>
                                <td>
                                  {it.request_type ?? it.kind ?? "—"}
                                  <div className="admin-uname">{it.model ?? "—"}</div>
                                </td>
                                <td>
                                  {it.duration_seconds ? `${it.duration_seconds}s` : "—"}
                                  {it.resolution ? ` · ${it.resolution}` : ""}
                                  {it.prompt ? (
                                    <div className="admin-uname" title={it.prompt}>
                                      {it.prompt.length > 48
                                        ? `${it.prompt.slice(0, 48)}…`
                                        : it.prompt}
                                    </div>
                                  ) : null}
                                </td>
                                <td>
                                  {usd(it.cost_usd)}
                                  <div className="admin-uname">{it.ledger_status ?? "—"}</div>
                                </td>
                                <td>
                                  <span
                                    className={`admin-activity__badge${
                                      it.request_status === "done"
                                        ? " admin-activity__badge--ok"
                                        : it.request_status === "failed"
                                          ? " admin-activity__badge--err"
                                          : ""
                                    }`}
                                  >
                                    {it.request_status ?? "—"}
                                  </span>
                                  {it.error ? (
                                    <div className="admin-activity__err" title={it.error}>
                                      {it.error}
                                    </div>
                                  ) : null}
                                </td>
                                <td>
                                  <button
                                    className="admin-activity__view"
                                    onClick={() => toggleExpand(i)}
                                    aria-expanded={isOpen}
                                  >
                                    {isOpen ? "▾ Hide" : "▶ View"}
                                    {it.media_ids.length ? ` (${it.media_ids.length})` : ""}
                                  </button>
                                </td>
                              </tr>
                              {isOpen && (
                                <tr className="admin-activity__detail">
                                  <td colSpan={6}>
                                    <div className="admin-activity__detail-grid">
                                      <div className="admin-activity__kv">
                                        <span>Request ID</span>
                                        <b>{it.request_id ?? "—"}</b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Type / Kind</span>
                                        <b>
                                          {it.request_type ?? "—"} · {it.kind ?? "—"}
                                        </b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Model</span>
                                        <b>{it.model ?? "—"}</b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Params</span>
                                        <b>
                                          {it.duration_seconds
                                            ? `${it.duration_seconds}s`
                                            : "—"}
                                          {it.resolution ? ` · ${it.resolution}` : ""}
                                        </b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Estimated</span>
                                        <b>{usd(it.estimated_usd)}</b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Actual</span>
                                        <b>{usd(it.actual_usd)}</b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Wallet</span>
                                        <b>{it.ledger_status ?? "not charged"}</b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Finished</span>
                                        <b>{fmtTime(it.finished_at)}</b>
                                      </div>
                                    </div>

                                    {it.inputs && it.inputs.length ? (
                                      <div className="admin-activity__block">
                                        <span>Input / Reference images ({it.inputs.length})</span>
                                        <div className="admin-activity__media">
                                          {it.inputs.map((inp) => (
                                            <a
                                              key={inp.id}
                                              className="admin-activity__thumb"
                                              href={`/media/${inp.id}`}
                                              target="_blank"
                                              rel="noopener noreferrer"
                                              title={`${inp.label} — click to view full size`}
                                            >
                                              <img
                                                src={`/media/${inp.id}`}
                                                alt={inp.label}
                                                loading="lazy"
                                                className="admin-activity__img"
                                              />
                                              <span className="admin-activity__thumb-label">
                                                {inp.label}
                                              </span>
                                            </a>
                                          ))}
                                        </div>
                                      </div>
                                    ) : null}

                                    {it.prompt ? (
                                      <div className="admin-activity__block">
                                        <span>Prompt</span>
                                        <p>{it.prompt}</p>
                                      </div>
                                    ) : null}

                                    {it.params && Object.keys(it.params).length ? (
                                      <div className="admin-activity__block">
                                        <span>Full parameters</span>
                                        <div className="admin-activity__params">
                                          {Object.entries(it.params).map(([k, v]) => (
                                            <div key={k} className="admin-activity__kv">
                                              <span>{k}</span>
                                              <b>{fmtParamValue(v)}</b>
                                            </div>
                                          ))}
                                        </div>
                                      </div>
                                    ) : null}

                                    {it.error ? (
                                      <div className="admin-activity__block admin-activity__block--err">
                                        <span>Error</span>
                                        <p>{it.error}</p>
                                      </div>
                                    ) : null}

                                    {it.media_ids.length ? (
                                      <div className="admin-activity__block">
                                        <span>Output ({it.media_ids.length})</span>
                                        <div className="admin-activity__media">
                                          {it.media_ids.map((m) =>
                                            isVideo ? (
                                              <video
                                                key={m}
                                                src={`/media/${m}`}
                                                controls
                                                preload="metadata"
                                                className="admin-activity__video admin-activity__video--lg"
                                              />
                                            ) : (
                                              <a
                                                key={m}
                                                className="admin-activity__thumb"
                                                href={`/media/${m}`}
                                                target="_blank"
                                                rel="noopener noreferrer"
                                                title="Click to view full size"
                                              >
                                                <img
                                                  src={`/media/${m}`}
                                                  alt=""
                                                  loading="lazy"
                                                  className="admin-activity__img admin-activity__img--lg"
                                                />
                                              </a>
                                            ),
                                          )}
                                        </div>
                                        {it.video_url ? (
                                          <a
                                            className="admin-activity__out"
                                            href={it.video_url}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                          >
                                            ↗ Original link (Avis)
                                          </a>
                                        ) : null}
                                      </div>
                                    ) : null}
                                  </td>
                                </tr>
                              )}
                            </Fragment>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            ) : null}
          </div>
        </div>
      )}
      </div>
    </div>
  );
}

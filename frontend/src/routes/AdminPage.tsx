import { Fragment, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useAuthStore } from "../store/auth";
import { parseServerTimeMs } from "../utils/serverTime";
import { ConfirmDialog, PromptDialog } from "../components/Modals";
import { KebabMenu } from "../components/KebabMenu";
import { CreateUserDialog, type NewUser } from "../components/CreateUserDialog";
import { toast } from "../store/toast";

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

interface AuditEntry {
  id: number;
  created_at?: string | null;
  action: string;
  actor?: string | null;
  target?: string | null;
  ip?: string | null;
  detail?: string | null;
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

/** "vừa xong" / "3 giờ trước" / "2 ngày trước" — friendlier than a raw stamp. */
function relTime(iso?: string | null): string {
  if (!iso) return "chưa đăng nhập";
  const ms = parseServerTimeMs(iso);
  if (!ms) return "—";
  const diff = Math.max(0, Date.now() - ms);
  const m = Math.floor(diff / 60000);
  if (m < 1) return "vừa xong";
  if (m < 60) return `${m} phút trước`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} giờ trước`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d} ngày trước`;
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

export function AdminPage() {
  const me = useAuthStore((s) => s.user);
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

  // audit log
  const [auditOpen, setAuditOpen] = useState(false);
  const [auditRows, setAuditRows] = useState<AuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);

  // Which action modal is open (replaces window.prompt / confirm).
  const [modal, setModal] = useState<
    { kind: "password" | "budget" | "delete"; user: AdminUser } | null
  >(null);

  async function openAudit() {
    setAuditOpen(true);
    setAuditLoading(true);
    setAuditRows([]);
    try {
      setAuditRows(await jsonOrThrow(await fetch("/api/admin/audit?limit=300")));
    } catch (e) {
      setError(e instanceof Error ? e.message : "audit load failed");
    } finally {
      setAuditLoading(false);
    }
  }

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
      toast(`Đã cập nhật quỹ Avis: $${v.toFixed(2)}`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "pool update failed";
      setError(msg);
      toast(msg, "error");
    }
  }

  useEffect(() => {
    void refresh();
  }, [refresh]);

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
      toast(`Đã tạo tài khoản "${nu.username}"`);
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
      toast(`Đã xoá tài khoản "${u.username}"`);
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

  return (
    <div className="admin2">
      <header className="admin2__head">
        <div>
          <h1 className="admin2__title">Quản lý tài khoản</h1>
          <p className="admin2__sub">Cấp tài khoản, phân quyền và ngân sách cho đội ngũ.</p>
        </div>
        <div className="admin2__head-actions">
          <Link className="btn2 btn2--ghost" to="/projects">
            ← Projects
          </Link>
          <button className="btn2 btn2--ghost" onClick={openAudit}>
            Nhật ký audit
          </button>
          <button className="btn2 btn2--primary" onClick={() => setCreateOpen(true)}>
            + Thêm thành viên
          </button>
        </div>
      </header>

      {/* Global Avis pool — Avis exposes no balance API, so the admin enters the
          top-up and we draw it down against the real per-gen usdCost. */}
      <section className={`pool${pool?.exhausted ? " pool--danger" : pool?.over_allocated ? " pool--warn" : ""}`}>
        <div className="pool__head">
          <span className="pool__title">Quỹ Avis (số dư thật của API key)</span>
          <button className="btn2 btn2--ghost pool__edit" onClick={() => setPoolOpen(true)}>
            {pool?.configured ? "Cập nhật số dư" : "Nhập số dư"}
          </button>
        </div>

        {!pool?.configured ? (
          <p className="pool__hint">
            Avis không có API xem số dư — mở <b>dashboard Avis</b>, copy số dư hiện tại và
            nhập vào đây. Hệ thống sẽ tự trừ dần theo <b>chi phí thật</b> của mỗi lần gen.
          </p>
        ) : (
          <>
            <div className="pool__nums">
              <span><b>${pool.pool_usd.toFixed(2)}</b> đã nạp</span>
              <span className="pool__sep">·</span>
              <span>${pool.spent_usd.toFixed(2)} đã tiêu</span>
              <span className="pool__sep">·</span>
              <span>${pool.reserved_usd.toFixed(2)} đang giữ chỗ</span>
              <span className="pool__sep">·</span>
              <span className="pool__avail">
                còn lại <b>${pool.available_usd.toFixed(2)}</b>
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
                🚫 <b>Quỹ đã cạn</b> — mọi yêu cầu gen mới sẽ bị từ chối. Nạp thêm trên Avis
                rồi cập nhật số dư ở đây.
              </p>
            ) : pool.over_allocated ? (
              <p className="pool__alert">
                ⚠️ <b>Cấp vượt quỹ</b>: user còn có thể tiêu tổng cộng{" "}
                <b>${pool.user_remaining_usd.toFixed(2)}</b> nhưng quỹ chỉ còn{" "}
                <b>${pool.available_usd.toFixed(2)}</b>. Hãy nạp thêm hoặc giảm budget của user.
              </p>
            ) : (
              <p className="pool__ok">
                ✓ An toàn — user còn có thể tiêu tổng ${pool.user_remaining_usd.toFixed(2)},
                nằm trong quỹ còn lại.
              </p>
            )}
          </>
        )}
      </section>

      <section className="admin2__stats">
        <div className="stat">
          <span className="stat__label">Thành viên</span>
          <span className="stat__value">{users.length}</span>
        </div>
        <div className="stat">
          <span className="stat__label">Đang hoạt động</span>
          <span className="stat__value stat__value--good">{activeCount}</span>
        </div>
        <div className="stat">
          <span className="stat__label">Đã khoá</span>
          <span className={`stat__value${suspendedCount ? " stat__value--warn" : ""}`}>
            {suspendedCount}
          </span>
        </div>
        <div className="stat">
          <span className="stat__label">Ngân sách còn lại</span>
          <span className="stat__value">${totalAvailable.toFixed(2)}</span>
        </div>
      </section>

      <div className="admin2__toolbar">
        <input
          className="admin2__search"
          placeholder="Tìm theo tên, tài khoản hoặc email…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <span className="admin2__count">
          {shown.length}/{users.length} thành viên
        </span>
      </div>

      {error ? <div className="admin-error">{error}</div> : null}

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
              ? "Chưa có thành viên nào — bấm “+ Thêm thành viên” để bắt đầu."
              : "Không tìm thấy thành viên phù hợp."}
          </div>
        ) : (
          <table className="admin2__table">
            <thead>
              <tr>
                <th>Thành viên</th>
                <th>Vai trò</th>
                <th>Trạng thái</th>
                <th>Ngân sách</th>
                <th>Đăng nhập gần nhất</th>
                <th className="admin2__th-actions" aria-label="Thao tác" />
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
                  <tr key={u.id} className={u.status === "suspended" ? "is-suspended" : undefined}>
                    <td>
                      <div className="admin2__user">
                        <span className="admin2__avatar" aria-hidden="true">
                          {initials(u)}
                        </span>
                        <span className="admin2__user-txt">
                          <span className="admin2__name">
                            {u.display_name || u.username}
                            {isMe ? <span className="admin2__you">bạn</span> : null}
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
                        {u.status === "active" ? "Hoạt động" : "Đã khoá"}
                      </span>
                    </td>
                    <td>
                      <div className="admin2__budget">
                        <span className="admin2__budget-nums">
                          <b>{usd(u.available_usd)}</b> còn / {usd(budget)}
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
                          { label: "Xem hoạt động", onSelect: () => void openActivity(u) },
                          {
                            label: "Đặt ngân sách",
                            onSelect: () => setModal({ kind: "budget", user: u }),
                          },
                          ...(isMe
                            ? []
                            : [
                                {
                                  label: u.status === "active" ? "Khoá tài khoản" : "Mở khoá",
                                  onSelect: () =>
                                    void patchUser(
                                      u.id,
                                      { status: u.status === "active" ? "suspended" : "active" },
                                      u.status === "active"
                                        ? `Đã khoá "${u.username}"`
                                        : `Đã mở khoá "${u.username}"`,
                                    ),
                                },
                                {
                                  label: u.role === "admin" ? "Hạ xuống user" : "Nâng lên admin",
                                  onSelect: () =>
                                    void patchUser(
                                      u.id,
                                      { role: u.role === "admin" ? "user" : "admin" },
                                      `Đã đổi vai trò "${u.username}"`,
                                    ),
                                },
                                {
                                  label: isSso ? "Đặt mật khẩu" : "Đặt lại mật khẩu",
                                  onSelect: () => setModal({ kind: "password", user: u }),
                                },
                                {
                                  label: "Xoá tài khoản",
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

      {createOpen ? (
        <CreateUserDialog
          busy={busy}
          onSubmit={(nu) => void createUser(nu)}
          onClose={() => setCreateOpen(false)}
        />
      ) : null}

      {poolOpen ? (
        <PromptDialog
          title="Số dư quỹ Avis"
          label="Số dư hiện tại trên dashboard Avis ($)"
          type="number"
          initial={String(pool?.pool_usd ?? 0)}
          placeholder="vd 444.32"
          submitLabel="Lưu"
          validate={(v) => {
            const n = Number(v);
            return !Number.isFinite(n) || n < 0 ? "Số tiền không hợp lệ" : null;
          }}
          onSubmit={(v) => void savePool(Number(v))}
          onClose={() => setPoolOpen(false)}
        />
      ) : null}

      {auditOpen && (
        <div
          className="admin-activity-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) setAuditOpen(false);
          }}
        >
          <div className="admin-activity" role="dialog" aria-label="Audit log">
            <div className="admin-activity__head">
              <h2>Nhật ký audit</h2>
              <button
                className="admin-activity__close"
                onClick={() => setAuditOpen(false)}
                aria-label="Đóng"
              >
                ×
              </button>
            </div>
            {auditLoading ? (
              <div className="admin-loading">Đang tải…</div>
            ) : (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Thời gian</th>
                    <th>Hành động</th>
                    <th>Người thực hiện</th>
                    <th>Đối tượng</th>
                    <th>IP</th>
                    <th>Chi tiết</th>
                  </tr>
                </thead>
                <tbody>
                  {auditRows.map((a) => (
                    <tr key={a.id}>
                      <td>{fmtTime(a.created_at)}</td>
                      <td>{a.action}</td>
                      <td>{a.actor ?? "—"}</td>
                      <td>{a.target ?? "—"}</td>
                      <td>{a.ip ?? "—"}</td>
                      <td>{a.detail ?? "—"}</td>
                    </tr>
                  ))}
                  {auditRows.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="admin-uname">
                        (chưa có sự kiện)
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {modal?.kind === "password" && (
        <PromptDialog
          title={`Đổi mật khẩu — ${modal.user.username}`}
          label="Mật khẩu mới (≥ 8 ký tự)"
          type="password"
          submitLabel="Đặt mật khẩu"
          validate={(v) => (v.length < 8 ? "Mật khẩu tối thiểu 8 ký tự" : null)}
          onSubmit={(v) => {
            void patchUser(
              modal.user.id,
              { password: v },
              `Đã đặt mật khẩu tạm cho "${modal.user.username}" — họ phải đổi khi đăng nhập`,
            );
            setModal(null);
          }}
          onClose={() => setModal(null)}
        />
      )}
      {modal?.kind === "budget" && (
        <PromptDialog
          title={`Ngân sách — ${modal.user.username}`}
          label="Ngân sách $ (tổng)"
          type="number"
          initial={String(modal.user.budget_usd ?? 0)}
          submitLabel="Lưu"
          validate={(v) => {
            const n = Number(v);
            return !Number.isFinite(n) || n < 0 ? "Số tiền không hợp lệ" : null;
          }}
          onSubmit={(v) => {
            void patchUser(
              modal.user.id,
              { budget_usd: Number(v) },
              `Đã đặt ngân sách $${Number(v).toFixed(2)} cho "${modal.user.username}"`,
            );
            setModal(null);
          }}
          onClose={() => setModal(null)}
        />
      )}
      {modal?.kind === "delete" && (
        <ConfirmDialog
          title={`Xoá tài khoản "${modal.user.username}"?`}
          danger
          confirmLabel="Xoá"
          message={
            <>
              Project của họ sẽ được <b>gỡ chủ sở hữu</b> (KHÔNG xoá dữ liệu đã gen). Hành
              động này không hoàn tác.
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
                Hoạt động — {activityUser.display_name || activityUser.username}
              </h2>
              <button
                className="admin-activity__close"
                onClick={closeActivity}
                aria-label="Đóng"
              >
                ×
              </button>
            </div>

            {activityLoading ? (
              <div className="admin-loading">Đang tải…</div>
            ) : activityError ? (
              <div className="admin-error">{activityError}</div>
            ) : activity ? (
              <>
                <div className="admin-activity__summary">
                  <div>
                    <span>Ngân sách</span>
                    <b>${activity.summary.budget_usd.toFixed(2)}</b>
                  </div>
                  <div>
                    <span>Đã tiêu</span>
                    <b className="admin-activity__spent">
                      ${activity.summary.spent_usd.toFixed(2)}
                    </b>
                  </div>
                  <div>
                    <span>Đang giữ</span>
                    <b>${activity.summary.reserved_usd.toFixed(2)}</b>
                  </div>
                  <div>
                    <span>Còn lại</span>
                    <b>${activity.summary.available_usd.toFixed(2)}</b>
                  </div>
                  <div>
                    <span>Số lần gen</span>
                    <b>{activity.summary.gen_count}</b>
                  </div>
                </div>

                {activity.items.length === 0 ? (
                  <div className="admin-activity__empty">Chưa có lần gen nào.</div>
                ) : (
                  <div className="admin-activity__scroll">
                    <table className="admin-activity__table">
                      <thead>
                        <tr>
                          <th>Thời gian</th>
                          <th>Loại / Model</th>
                          <th>Thông số</th>
                          <th>Chi phí</th>
                          <th>Trạng thái</th>
                          <th>Chi tiết</th>
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
                                    {isOpen ? "▾ Ẩn" : "▶ Xem"}
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
                                        <span>Loại / Kind</span>
                                        <b>
                                          {it.request_type ?? "—"} · {it.kind ?? "—"}
                                        </b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Model</span>
                                        <b>{it.model ?? "—"}</b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Thông số</span>
                                        <b>
                                          {it.duration_seconds
                                            ? `${it.duration_seconds}s`
                                            : "—"}
                                          {it.resolution ? ` · ${it.resolution}` : ""}
                                        </b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Ước lượng</span>
                                        <b>{usd(it.estimated_usd)}</b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Thực trả</span>
                                        <b>{usd(it.actual_usd)}</b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Ví</span>
                                        <b>{it.ledger_status ?? "không tính phí"}</b>
                                      </div>
                                      <div className="admin-activity__kv">
                                        <span>Kết thúc</span>
                                        <b>{fmtTime(it.finished_at)}</b>
                                      </div>
                                    </div>

                                    {it.inputs && it.inputs.length ? (
                                      <div className="admin-activity__block">
                                        <span>Ảnh input / Reference ({it.inputs.length})</span>
                                        <div className="admin-activity__media">
                                          {it.inputs.map((inp) => (
                                            <a
                                              key={inp.id}
                                              className="admin-activity__thumb"
                                              href={`/media/${inp.id}`}
                                              target="_blank"
                                              rel="noopener noreferrer"
                                              title={`${inp.label} — bấm để xem full`}
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
                                        <span>Tham số đầy đủ</span>
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
                                        <span>Lỗi</span>
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
                                                title="Bấm để xem full"
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
                                            ↗ Link gốc (Avis)
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
  );
}

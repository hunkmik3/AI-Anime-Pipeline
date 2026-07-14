import { Fragment, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useAuthStore } from "../store/auth";
import { parseServerTimeMs } from "../utils/serverTime";
import { ConfirmDialog, PromptDialog } from "../components/Modals";
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

  // create form
  const [nu, setNu] = useState("");
  const [np, setNp] = useState("");
  const [nrole, setNrole] = useState("user");
  const [nemail, setNemail] = useState("");
  const [busy, setBusy] = useState(false);

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
      setUsers(await jsonOrThrow(await fetch("/api/admin/users")));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function createUser(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await jsonOrThrow(
        await fetch("/api/admin/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: nu.trim(),
            password: np,
            role: nrole,
            email: nemail.trim() || undefined,
          }),
        }),
      );
      setNu("");
      setNp("");
      setNrole("user");
      setNemail("");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "create failed");
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

  return (
    <div className="admin-page">
      <div className="admin-head">
        <h1>Quản lý tài khoản</h1>
        <button className="admin-back" onClick={openAudit}>
          Nhật ký audit
        </button>
        <Link className="admin-back" to="/projects">
          ← Về Projects
        </Link>
      </div>

      <form className="admin-create" onSubmit={createUser}>
        <input placeholder="Tài khoản" value={nu} onChange={(e) => setNu(e.target.value)} disabled={busy} />
        <input
          placeholder="Mật khẩu"
          type="text"
          value={np}
          onChange={(e) => setNp(e.target.value)}
          disabled={busy}
        />
        <input
          placeholder="Email (tuỳ chọn)"
          type="email"
          value={nemail}
          onChange={(e) => setNemail(e.target.value)}
          disabled={busy}
        />
        <select value={nrole} onChange={(e) => setNrole(e.target.value)} disabled={busy}>
          <option value="user">user</option>
          <option value="admin">admin</option>
        </select>
        <button type="submit" disabled={busy || !nu || !np}>
          + Tạo tài khoản
        </button>
      </form>

      {error ? <div className="admin-error">{error}</div> : null}

      {loading ? (
        <div className="admin-loading">Đang tải…</div>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Tài khoản</th>
              <th>Vai trò</th>
              <th>Trạng thái</th>
              <th>Ngân sách $</th>
              <th>Còn lại $</th>
              <th>Thao tác</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className={u.status === "suspended" ? "admin-row--suspended" : undefined}>
                <td>
                  {u.display_name || u.username}
                  {u.username !== (u.display_name || u.username) ? <span className="admin-uname"> ({u.username})</span> : null}
                  {u.has_password === false ? (
                    <span className="admin-badge admin-badge--google" title="Đăng nhập bằng Google">
                      google
                    </span>
                  ) : null}
                  {u.email ? <span className="admin-uname"> · {u.email}</span> : null}
                  <span className="admin-uname"> · đăng nhập: {fmtTime(u.last_login)}</span>
                </td>
                <td>
                  <span className={`admin-badge admin-badge--role-${u.role}`}>{u.role}</span>
                </td>
                <td>
                  <span className={`admin-badge admin-badge--${u.status}`}>
                    {u.status === "active" ? "hoạt động" : "đã khoá"}
                  </span>
                </td>
                <td>
                  {typeof u.budget_usd === "number" ? `$${u.budget_usd.toFixed(2)}` : "—"}
                  {typeof u.spent_usd === "number" ? (
                    <span className="admin-uname"> (tiêu ${u.spent_usd.toFixed(2)})</span>
                  ) : null}
                </td>
                <td>{typeof u.available_usd === "number" ? `$${u.available_usd.toFixed(2)}` : "—"}</td>
                <td className="admin-actions">
                  <button onClick={() => openActivity(u)}>Hoạt động</button>
                  <button onClick={() => setModal({ kind: "budget", user: u })}>Ngân sách</button>
                  {u.id === me?.id ? (
                    <span className="admin-self">(bạn)</span>
                  ) : (
                    <>
                      {u.status === "active" ? (
                        <button
                          onClick={() =>
                            patchUser(u.id, { status: "suspended" }, `Đã khoá "${u.username}"`)
                          }
                        >
                          Khoá
                        </button>
                      ) : (
                        <button
                          onClick={() =>
                            patchUser(u.id, { status: "active" }, `Đã mở khoá "${u.username}"`)
                          }
                        >
                          Mở
                        </button>
                      )}
                      <button
                        onClick={() =>
                          patchUser(
                            u.id,
                            { role: u.role === "admin" ? "user" : "admin" },
                            `Đã đổi vai trò "${u.username}"`,
                          )
                        }
                        title="Đổi vai trò"
                      >
                        {u.role === "admin" ? "→ user" : "→ admin"}
                      </button>
                      <button onClick={() => setModal({ kind: "password", user: u })}>
                        Đổi mật khẩu
                      </button>
                      <button className="admin-del" onClick={() => setModal({ kind: "delete", user: u })}>
                        Xoá
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

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

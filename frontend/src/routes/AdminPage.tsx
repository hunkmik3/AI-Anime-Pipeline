import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useAuthStore } from "../store/auth";

interface AdminUser {
  id: string;
  username: string;
  role: string;
  status: string;
  display_name?: string | null;
  created_at?: string | null;
  budget_usd?: number;
  spent_usd?: number;
  available_usd?: number;
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
  const [busy, setBusy] = useState(false);

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
          body: JSON.stringify({ username: nu.trim(), password: np, role: nrole }),
        }),
      );
      setNu("");
      setNp("");
      setNrole("user");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "create failed");
    } finally {
      setBusy(false);
    }
  }

  async function patchUser(id: string, body: Record<string, unknown>) {
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
    } catch (e) {
      setError(e instanceof Error ? e.message : "update failed");
    }
  }

  function resetPassword(u: AdminUser) {
    const pw = window.prompt(`Mật khẩu mới cho "${u.username}":`);
    if (pw) void patchUser(u.id, { password: pw });
  }

  function setBudget(u: AdminUser) {
    const cur = typeof u.budget_usd === "number" ? u.budget_usd : 0;
    const raw = window.prompt(`Ngân sách $ cho "${u.username}" (tổng):`, String(cur));
    if (raw === null) return;
    const v = Number(raw);
    if (!Number.isFinite(v) || v < 0) return;
    void patchUser(u.id, { budget_usd: v });
  }

  return (
    <div className="admin-page">
      <div className="admin-head">
        <h1>Quản lý tài khoản</h1>
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
                </td>
                <td>{u.role}</td>
                <td>{u.status}</td>
                <td>
                  {typeof u.budget_usd === "number" ? `$${u.budget_usd.toFixed(2)}` : "—"}
                  {typeof u.spent_usd === "number" ? (
                    <span className="admin-uname"> (tiêu ${u.spent_usd.toFixed(2)})</span>
                  ) : null}
                </td>
                <td>{typeof u.available_usd === "number" ? `$${u.available_usd.toFixed(2)}` : "—"}</td>
                <td className="admin-actions">
                  <button onClick={() => setBudget(u)}>Ngân sách</button>
                  {u.id === me?.id ? (
                    <span className="admin-self">(bạn)</span>
                  ) : (
                    <>
                      {u.status === "active" ? (
                        <button onClick={() => patchUser(u.id, { status: "suspended" })}>Khoá</button>
                      ) : (
                        <button onClick={() => patchUser(u.id, { status: "active" })}>Mở</button>
                      )}
                      <button onClick={() => resetPassword(u)}>Đổi mật khẩu</button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

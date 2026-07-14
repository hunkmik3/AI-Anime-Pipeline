import { useCallback, useEffect, useState } from "react";

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

// ── Tổng quan ───────────────────────────────────────────────────────────────

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

  if (ov.loading) return <Skeleton />;
  if (ov.err) return <div className="admin-error">{ov.err}</div>;
  const o = ov.data!;

  return (
    <>
      <section className="admin2__stats">
        <div className="stat">
          <span className="stat__label">Tổng đã chi</span>
          <span className="stat__value">{usd(o.spent_usd)}</span>
        </div>
        <div className="stat">
          <span className="stat__label">✅ Ra clip dùng được</span>
          <span className="stat__value stat__value--good">{usd(o.kept_usd)}</span>
        </div>
        <div className="stat">
          <span className="stat__label">💸 Đốt vào gen lại</span>
          <span className={`stat__value${o.waste_pct > 30 ? " stat__value--warn" : ""}`}>
            {usd(o.wasted_usd)}
            <small className="stat__pct"> ({o.waste_pct}%)</small>
          </span>
        </div>
        <div className="stat">
          <span className="stat__label">Chi phí / 1 clip</span>
          <span className="stat__value">{usd(o.cost_per_clip)}</span>
        </div>
      </section>

      <div className="admin2__card admin2__pad">
        <div className="tab-h">
          <b>{o.kept_clips}</b> clip hoàn thành từ <b>{o.takes}</b> lần gen · đã tải xuống{" "}
          <b>{o.downloaded_clips}</b>
        </div>

        <h3 className="tab-h3">Tiền đi đâu? (theo model / độ phân giải)</h3>
        {models.loading ? (
          <Skeleton />
        ) : (
          <div className="mbars">
            {(models.data ?? []).map((m) => (
              <div className="mbar" key={m.model}>
                <span className="mbar__label">{m.model}</span>
                <span className="mbar__track">
                  <span className="mbar__fill" style={{ width: `${m.pct}%` }} />
                </span>
                <span className="mbar__val">
                  {usd(m.usd)} <small>· {m.takes} lần · {m.pct}%</small>
                </span>
              </div>
            ))}
            {(models.data ?? []).length === 0 ? (
              <div className="admin2__empty">Chưa có generation nào.</div>
            ) : null}
          </div>
        )}
      </div>
    </>
  );
}

// ── Chi phí & Lãng phí ──────────────────────────────────────────────────────

interface UserCost {
  user_id: string;
  username: string;
  display_name?: string | null;
  budget_usd: number;
  spent_usd: number;
  kept_usd: number;
  wasted_usd: number;
  kept_clips: number;
  wasted_takes: number;
  takes: number;
  downloaded_clips: number;
  waste_pct: number;
}
interface ClipRow {
  node_id: number;
  title: string;
  project?: string | null;
  scene?: string | null;
  takes: number;
  wasted_usd: number;
  kept_usd: number;
  total_usd: number;
  downloaded: boolean;
  model?: string | null;
}

export function CostTab() {
  const { data, err, loading } = useFetch<UserCost[]>("/api/admin/stats/users");
  const [open, setOpen] = useState<string | null>(null);
  const [clips, setClips] = useState<Record<string, ClipRow[]>>({});

  async function toggle(uid: string) {
    if (open === uid) {
      setOpen(null);
      return;
    }
    setOpen(uid);
    if (!clips[uid]) {
      try {
        const rows = await getJson<ClipRow[]>(`/api/admin/stats/users/${uid}/clips`);
        setClips((c) => ({ ...c, [uid]: rows }));
      } catch {
        setClips((c) => ({ ...c, [uid]: [] }));
      }
    }
  }

  if (loading) return <Skeleton />;
  if (err) return <div className="admin-error">{err}</div>;
  const rows = data ?? [];

  return (
    <div className="admin2__card">
      <p className="tab-note">
        💸 <b>Đốt</b> = gen ra clip nhưng gen lại → tiền mất trắng. ✅ <b>Dùng</b> = bản cuối
        được giữ. Gen <b>lỗi kỹ thuật không tính tiền</b> (hệ thống tự hoàn). Số liệu lấy thẳng
        từ hoá đơn Avis — <b>không ai sửa được</b>.
      </p>
      {rows.length === 0 ? (
        <div className="admin2__empty">Chưa có chi tiêu nào.</div>
      ) : (
        <table className="admin2__table">
          <thead>
            <tr>
              <th>Người dùng</th>
              <th>Được cấp</th>
              <th>Đã xài</th>
              <th>💸 Đốt vào clip bỏ</th>
              <th>✅ Tiền ra clip dùng</th>
              <th>Đã tải</th>
              <th className="admin2__th-actions" />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <>
                <tr
                  key={r.user_id}
                  className="row-click"
                  onClick={() => void toggle(r.user_id)}
                >
                  <td>
                    <b>{r.display_name || r.username}</b>
                    <span className="admin2__email"> · {r.takes} lần gen</span>
                  </td>
                  <td className="admin2__muted">{usd(r.budget_usd)}</td>
                  <td>
                    <b>{usd(r.spent_usd)}</b>
                  </td>
                  <td>
                    <span className={r.waste_pct > 30 ? "burn burn--bad" : "burn"}>
                      {usd(r.wasted_usd)}
                    </span>
                    <span className="admin2__email">
                      {" "}
                      ({r.wasted_takes} clip · {r.waste_pct}%)
                    </span>
                  </td>
                  <td>
                    <span className="keep">{usd(r.kept_usd)}</span>
                    <span className="admin2__email"> ({r.kept_clips} clip)</span>
                  </td>
                  <td className="admin2__muted">
                    {r.downloaded_clips}/{r.kept_clips}
                  </td>
                  <td className="admin2__muted">{open === r.user_id ? "▾" : "▸"}</td>
                </tr>
                {open === r.user_id ? (
                  <tr key={`${r.user_id}-d`}>
                    <td colSpan={7} className="drill">
                      {!clips[r.user_id] ? (
                        <Skeleton />
                      ) : clips[r.user_id].length === 0 ? (
                        <div className="admin2__empty">Chưa có clip nào.</div>
                      ) : (
                        <table className="admin2__table drill__table">
                          <thead>
                            <tr>
                              <th>Clip</th>
                              <th>Số lần gen</th>
                              <th>💸 Bỏ đi</th>
                              <th>✅ Bản dùng</th>
                              <th>Tổng tốn</th>
                              <th>Đã tải</th>
                            </tr>
                          </thead>
                          <tbody>
                            {clips[r.user_id].map((c) => (
                              <tr key={c.node_id}>
                                <td>
                                  {c.title}
                                  <span className="admin2__email">
                                    {" "}
                                    · {c.project ?? "—"} / {c.scene ?? "—"}
                                  </span>
                                </td>
                                <td>
                                  <b>{c.takes}</b>
                                </td>
                                <td className={c.wasted_usd > 0 ? "burn" : "admin2__muted"}>
                                  {usd(c.wasted_usd)}
                                </td>
                                <td className="keep">{usd(c.kept_usd)}</td>
                                <td>
                                  <b>{usd(c.total_usd)}</b>
                                </td>
                                <td>{c.downloaded ? "✓" : "—"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </td>
                  </tr>
                ) : null}
              </>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── Dự án ───────────────────────────────────────────────────────────────────

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

export function ProjectsTab() {
  const { data, err, loading } = useFetch<ProjectCost[]>("/api/admin/stats/projects");
  if (loading) return <Skeleton />;
  if (err) return <div className="admin-error">{err}</div>;
  const rows = data ?? [];
  return (
    <div className="admin2__card">
      {rows.length === 0 ? (
        <div className="admin2__empty">Chưa có project nào tốn chi phí.</div>
      ) : (
        <table className="admin2__table">
          <thead>
            <tr>
              <th>Project</th>
              <th>Tổng chi phí</th>
              <th>💸 Đốt</th>
              <th>✅ Ra clip</th>
              <th>Clip</th>
              <th>Lần gen</th>
              <th>Đã tải</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.project_id}>
                <td>
                  <b>{p.name}</b>
                </td>
                <td>
                  <b>{usd(p.total_usd)}</b>
                </td>
                <td>
                  <span className={p.waste_pct > 30 ? "burn burn--bad" : "burn"}>
                    {usd(p.wasted_usd)}
                  </span>
                  <span className="admin2__email"> ({p.waste_pct}%)</span>
                </td>
                <td className="keep">{usd(p.kept_usd)}</td>
                <td>{p.clips}</td>
                <td className="admin2__muted">{p.takes}</td>
                <td className="admin2__muted">
                  {p.downloaded_clips}/{p.clips}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── Nhật ký audit ───────────────────────────────────────────────────────────

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
        <div className="admin2__empty">Chưa có sự kiện nào.</div>
      ) : (
        <table className="admin2__table">
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

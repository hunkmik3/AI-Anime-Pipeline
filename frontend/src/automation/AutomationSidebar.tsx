/**
 * The left rail on /automation.
 *
 * Not ProjectSidebar: that one walks Project → Series → Episode, a hierarchy
 * this surface does not have and would only be borrowing the look of. A board
 * here is flat — a premise and its graph — so the rail is a flat list.
 */
import { useEffect, useState } from "react";

import { useAutomation } from "../store/automation";
import { projectTime } from "./projectTime";

const COLLAPSED_KEY = "flowboard:automation-sidebar-collapsed";

export function AutomationSidebar() {
  const projects = useAutomation((s) => s.projects);
  const currentId = useAutomation((s) => s.currentProjectId);
  const saveState = useAutomation((s) => s.saveState);
  const saveError = useAutomation((s) => s.saveError);
  const loadProjects = useAutomation((s) => s.loadProjects);
  const createProject = useAutomation((s) => s.createProject);
  const openProject = useAutomation((s) => s.openProject);
  const renameProject = useAutomation((s) => s.renameProject);
  const deleteProject = useAutomation((s) => s.deleteProject);

  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now);
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(COLLAPSED_KEY) === "true"; }
    catch { return false; }
  });

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    // Refresh elapsed labels even when there are no board changes.
    const refresh = () => setNow(Date.now());
    const timer = window.setInterval(refresh, 60_000);
    window.addEventListener("focus", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
    };
  }, []);

  useEffect(() => {
    try { localStorage.setItem(COLLAPSED_KEY, String(collapsed)); }
    catch { /* Still allow collapsing when browser storage is unavailable. */ }
  }, [collapsed]);

  async function guard(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const saveLabel =
    saveState === "saving"
      ? "đang lưu…"
      : saveState === "saved"
        ? "đã lưu"
        : saveState === "error"
          ? "lưu lỗi"
          : "";

  return (
    <aside className={`auto-rail${collapsed ? " auto-rail--collapsed" : ""}`} aria-label="Thanh danh sách board">
      <div className="auto-rail__head">
        {!collapsed && <span className="auto-rail__label">Automation</span>}
        {!collapsed && currentId && saveLabel && (
          <span
            className={`auto-rail__save auto-rail__save--${saveState}`}
            title={saveError ?? undefined}
          >
            {saveLabel}
          </span>
        )}
        <button
          type="button"
          className="auto-rail__toggle"
          title={collapsed ? "Mở rộng thanh bên" : "Thu gọn thanh bên"}
          aria-label={collapsed ? "Mở rộng thanh bên" : "Thu gọn thanh bên"}
          aria-expanded={!collapsed}
          aria-controls="automation-sidebar-content"
          onClick={() => setCollapsed((value) => !value)}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <path d="M9 4v16" />
            <path d={collapsed ? "m13 9 3 3-3 3" : "m17 9-3 3 3 3"} />
          </svg>
        </button>
      </div>

      <div className="auto-rail__body" id="automation-sidebar-content" hidden={collapsed}>
        {creating ? (
          <form
            className="auto-rail__new"
            onSubmit={(e) => {
              e.preventDefault();
              const name = draft.trim();
              if (!name) return;
              void guard(async () => {
                await createProject(name);
                setDraft("");
                setCreating(false);
              });
            }}
          >
            <input
              autoFocus
              value={draft}
              placeholder="Tên board"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && setCreating(false)}
            />
            <button type="submit" className="auto-btn auto-btn--primary" disabled={busy}>
              Tạo
            </button>
          </form>
        ) : (
          <button
            type="button"
            className="auto-rail__add"
            onClick={() => setCreating(true)}
            disabled={busy}
          >
            + Board mới
          </button>
        )}

        {error && <p className="auto-error auto-rail__error">{error}</p>}

        <nav className="auto-rail__list">
          {projects.length === 0 && (
            <p className="auto-rail__empty">
              Chưa có board nào. Tạo một cái để công việc được lưu lại — không có board
              thì mọi thứ chỉ nằm trong trình duyệt.
            </p>
          )}

          {projects.map((p) => {
            const active = p.id === currentId;
            const time = projectTime(p.updated_at, now);
            if (renamingId === p.id) {
              return (
                <form
                  key={p.id}
                  className="auto-rail__new"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const name = renameDraft.trim();
                    if (!name) return setRenamingId(null);
                    void guard(async () => {
                      await renameProject(p.id, name);
                      setRenamingId(null);
                    });
                  }}
                >
                  <input
                    autoFocus
                    value={renameDraft}
                    onChange={(e) => setRenameDraft(e.target.value)}
                    onKeyDown={(e) => e.key === "Escape" && setRenamingId(null)}
                    onBlur={() => setRenamingId(null)}
                  />
                </form>
              );
            }
            return (
              <div key={p.id} className={`auto-rail__item${active ? " auto-rail__item--on" : ""}`}>
                <button
                  type="button"
                  className="auto-rail__open"
                  onClick={() => void guard(() => openProject(p.id))}
                  disabled={busy}
                >
                  <span className="auto-rail__name">{p.name}</span>
                  <span className="auto-rail__meta">
                    {p.title && p.title !== p.name ? `${p.title} · ` : ""}
                    <time dateTime={time.dateTime} title={time.title} aria-label={time.title}>{time.label}</time>
                  </span>
                </button>
                <div className="auto-rail__actions">
                  <button
                    type="button"
                    title="Đổi tên"
                    onClick={() => {
                      setRenameDraft(p.name);
                      setRenamingId(p.id);
                    }}
                  >
                    ✎
                  </button>
                  <button
                    type="button"
                    title="Xoá"
                    onClick={() => {
                      if (confirm(`Xoá board "${p.name}"? Không khôi phục được.`)) {
                        void guard(() => deleteProject(p.id));
                      }
                    }}
                  >
                    ✕
                  </button>
                </div>
              </div>
            );
          })}
        </nav>
      </div>
    </aside>
  );
}

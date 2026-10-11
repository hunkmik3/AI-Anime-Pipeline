import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useAutomation } from "../store/automation";
import { parseServerTimeMs } from "../utils/serverTime";

type Event = { id: string; project_id: string; project_name: string; status: string;
  title: string; message: string; updated_at: string };
type Inbox = { events: Event[]; active_projects: number };
function read<T>(key: string, fallback: T): T {
  try { return JSON.parse(localStorage.getItem(key) || "null") ?? fallback; } catch { return fallback; }
}
function save(key: string, value: unknown) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Private browsing can disallow storage. */ } }

export function ProductionInbox() {
  const [inbox, setInbox] = useState<Inbox>({ events: [], active_projects: 0 });
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [seen, setSeen] = useState<string[]>(() => read("production-seen-v1", []));
  const [permission, setPermission] = useState(() => typeof Notification === "undefined" ? "unsupported" : Notification.permission);
  const root = useRef<HTMLDivElement>(null);
  const since = useRef(read<number>("production-watch-since-v1", Date.now()));
  useEffect(() => {
    save("production-watch-since-v1", since.current);
    let alive = true, loading = false;
    const controller = new AbortController();
    const refresh = async () => {
      if (loading) return;
      loading = true;
      try {
        const next = await api<Inbox>("/api/automation/production-inbox", { signal: controller.signal });
        if (!alive) return;
        setInbox(next); setError("");
        const sent = new Set(read<string[]>("production-notified-v1", []));
        for (const event of next.events) {
          if (sent.has(event.id) || parseServerTimeMs(event.updated_at) < since.current) continue;
          if (typeof Notification !== "undefined" && Notification.permission === "granted") {
            try {
              const notice = new Notification(`${event.title} · ${event.project_name}`, { body: event.message, tag: event.id });
              notice.onclick = () => { window.focus(); void useAutomation.getState().openProject(event.project_id); notice.close(); };
            } catch { /* The in-app inbox remains available when OS notifications aren't supported. */ }
          }
          sent.add(event.id);
        }
        save("production-notified-v1", [...sent].slice(-200));
      } catch (e) { if (alive) setError((e as Error).message); }
      finally { loading = false; }
    };
    void refresh(); const timer = setInterval(() => void refresh(), 15000);
    return () => { alive = false; controller.abort(); clearInterval(timer); };
  }, []);
  useEffect(() => {
    if (!open) return;
    const close = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const escape = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [open]);
  const unread = inbox.events.filter(e => !seen.includes(e.id) && parseServerTimeMs(e.updated_at) >= since.current).length;
  function toggle() {
    setOpen(!open);
    if (!open) { const ids = inbox.events.map(e => e.id); setSeen(ids); save("production-seen-v1", ids); }
  }
  return <div ref={root} className="production-inbox">
    <button className="auto-btn" type="button" aria-expanded={open} aria-label={`Thông báo sản xuất${unread ? `, ${unread} mới` : ""}`} onClick={toggle}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></svg>
      Thông báo{unread ? ` (${unread})` : ""}
    </button>
    {open && <section className="production-inbox__panel" aria-label="Thông báo sản xuất">
      <header><strong>Thông báo sản xuất</strong><button className="auto-btn" onClick={() => setOpen(false)} aria-label="Đóng thông báo">×</button></header>
      <p>{inbox.active_projects ? `${inbox.active_projects} dự án đang tự chạy trên server.` : "Không có dự án đang chạy."} Kết quả được lưu khi bạn rời tab.</p>
      {permission === "default" && <button className="auto-btn" onClick={async () => setPermission(await Notification.requestPermission())}>Bật thông báo ngoài tab</button>}
      <small>{permission === "granted" ? "Sẽ báo khi xong hoặc cần bạn quyết định, kể cả khi xem project khác. Giữ tab app mở để nhận thông báo hệ thống." : "Các kết quả vẫn hiện ở đây. Thông báo hệ thống cần trình duyệt cho phép và tab app còn mở."}</small>
      {error && <p role="status">Chưa lấy được thông báo mới. App sẽ tự kết nối lại.</p>}
      {!inbox.events.length && <p>Chưa có kết quả mới.</p>}
      <div className="production-inbox__events">{inbox.events.map(event => <button type="button" key={event.id} onClick={() => { void useAutomation.getState().openProject(event.project_id); setOpen(false); }}>
        <strong>{event.title} · {event.project_name}</strong><span>{event.message}</span>
        <time dateTime={event.updated_at}>{new Date(parseServerTimeMs(event.updated_at)).toLocaleString("vi-VN")}</time>
      </button>)}</div>
    </section>}
  </div>;
}

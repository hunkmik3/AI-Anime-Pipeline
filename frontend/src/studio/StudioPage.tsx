import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { STYLE_PRESETS } from "../store/videoAnalysis";
import { studioApi, type FilmDetail, type FilmSummary, type StationKey } from "./api";
import { NewFilm } from "./NewFilm";
import { SourceStation } from "./SourceStation";
import { ShotlistStation } from "./ShotlistStation";
import { MaterialsStation } from "./MaterialsStation";
import { ClipsStation } from "./ClipsStation";
import { ReviewStation } from "./ReviewStation";
import "./studio.css";

const styleLabel = (key?: string) => STYLE_PRESETS.find(p => p.key === key)?.label ?? key ?? "";
const ago = (iso: string | null) => {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 1 ? "vừa xong" : m < 60 ? `${m} phút trước` : m < 1440 ? `${Math.round(m / 60)} giờ trước` : new Date(iso).toLocaleDateString("vi-VN");
};

function FilmList({ films }: { films: FilmSummary[] }) {
  if (!films.length) return <p className="st-empty">Chưa có phim nào. Tạo phim đầu tiên ở bên cạnh.</p>;
  return <ul className="st-films">
    {films.map(f => <li key={f.id}>
      <Link to={`/studio/${f.id}`}>
        <span className="st-films__name">{f.name}</span>
        <span className="st-films__meta">{styleLabel(f.settings.style)} · {f.settings.aspect_ratio} · {f.settings.resolution}</span>
        <span className={`st-pill st-pill--${f.status === "failed" ? "attention" : f.stage === "awaiting_review" ? "attention" : f.stage === "production" ? "done" : "running"}`}>
          {f.stage_label || f.status}</span>
        <span className="st-films__time">{ago(f.updated_at)}</span>
      </Link>
    </li>)}
  </ul>;
}

function Home() {
  const [films, setFilms] = useState<FilmSummary[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    studioApi.list().then(setFilms).catch(e => setError(String(e.message ?? e)));
  }, []);
  return <div className="st-home">
    <div className="st-home__list">
      <header className="st-home__head">
        <h1>Reskin Studio</h1>
        <p>Video gốc vào, phim theo phong cách của bạn ra — và bạn quyết ở mọi bước.</p>
      </header>
      {error && <p className="st-error">{error}</p>}
      {films ? <FilmList films={films} /> : <p className="st-empty">Đang tải…</p>}
    </div>
    <NewFilm />
  </div>;
}

const STATION_ORDER: StationKey[] = ["source", "shotlist", "materials", "clips", "review"];

function Workspace({ id }: { id: string }) {
  const [params, setParams] = useSearchParams();
  const station = (params.get("s") as StationKey) || "shotlist";
  const [film, setFilm] = useState<FilmDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [inboxOpen, setInboxOpen] = useState(false);
  const timer = useRef<number>();

  const load = useCallback(async () => {
    try {
      const d = await studioApi.detail(id);
      setFilm(d); setError("");
      return d;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }, [id]);

  useEffect(() => {
    let stopped = false;
    async function tick() {
      const d = document.hidden ? film : await load();
      if (stopped) return;
      const active = d && (d.stations.some(s => s.status === "running") || d.clips.some(c => ["rendering", "preparing", "reviewing"].includes(c.status)));
      timer.current = window.setTimeout(tick, active ? 5000 : 20000);
    }
    void tick();
    return () => { stopped = true; window.clearTimeout(timer.current); };
  }, [load]);

  /** Run one owner action, then refresh; errors stay on screen. */
  const act = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label); setError("");
    try { await fn(); await load(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(""); }
  }, [load]);

  const go = (s: StationKey, extra: Record<string, string> = {}) => setParams({ s, ...extra });

  if (!film) return <div className="st-loading">{error ? <p className="st-error">{error}</p> : "Đang mở phim…"}</div>;
  const ctx = { film, act, busy, go, reload: load, params };
  const failing = film.providers.filter(p => p.status === "failing");
  return <div className="st-ws">
    <header className="st-ws__head">
      <Link to="/studio" className="st-back" aria-label="Về danh sách phim">←</Link>
      <div className="st-ws__title">
        <h1>{film.name}</h1>
        <p>{styleLabel(film.settings.style)} · {film.settings.aspect_ratio} · {film.settings.resolution} · {film.stage_label}</p>
      </div>
      {film.cost && <div className="st-cost" title={film.cost.note}>
        <strong>${film.cost.seedance_usd.toFixed(2)}{film.cost.budget_usd ? <small> / ${film.cost.budget_usd.toFixed(0)}</small> : null}</strong>
        <small>{film.cost.seconds.toFixed(0)} s Seedance · {film.cost.images} ảnh{film.cost.left_usd != null ? ` · còn $${film.cost.left_usd.toFixed(2)}` : ""}</small>
      </div>}
      <div className="st-providers" aria-label="Tình trạng nhà cung cấp">
        {film.providers.map(p => <span key={p.name} className={`st-dot st-dot--${p.status}`}
          title={`${p.name}: ${p.status === "failing" ? p.message : p.status === "ok" ? "bình thường" : "chưa dùng gần đây"}`}>{p.name.split(" (")[0]}</span>)}
      </div>
      <button type="button" className={`st-inbox-btn${film.inbox.length ? " has-items" : ""}`} onClick={() => setInboxOpen(o => !o)} aria-expanded={inboxOpen}>
        Cần bạn <b>{film.inbox.length}</b>
      </button>
    </header>
    {failing.length > 0 && <div className="st-banner" role="status">
      {failing.map(p => <p key={p.name}><strong>{p.name}</strong> đang lỗi{p.hint ? ` — ${p.hint}` : ""} <small>{p.message.slice(0, 160)}</small></p>)}
    </div>}
    {error && <p className="st-error st-error--bar" role="alert">{error}</p>}
    <nav className="st-stations" aria-label="Các trạm">
      {STATION_ORDER.map((key, i) => {
        const s = film.stations.find(x => x.key === key) ?? { key, label: key, status: "pending", summary: "" };
        return <button type="button" key={key} className={`st-station st-station--${s.status}${station === key ? " is-active" : ""}`}
          onClick={() => go(key)} aria-current={station === key ? "page" : undefined}>
          <span className="st-station__step">{i + 1}</span>
          <span className="st-station__text"><strong>{s.label}</strong><small>{s.summary}</small></span>
        </button>;
      })}
    </nav>
    <main className="st-main">
      {station === "source" && <SourceStation {...ctx} />}
      {station === "shotlist" && <ShotlistStation {...ctx} />}
      {station === "materials" && <MaterialsStation {...ctx} />}
      {station === "clips" && <ClipsStation {...ctx} />}
      {station === "review" && <ReviewStation {...ctx} />}
    </main>
    {inboxOpen && <aside className="st-inbox" aria-label="Việc cần bạn">
      <header><h2>Cần bạn quyết</h2><button type="button" onClick={() => setInboxOpen(false)} aria-label="Đóng">×</button></header>
      {film.inbox.length === 0 ? <p className="st-empty">Không có gì chờ bạn.</p> :
        <ul>{film.inbox.map((item, i) => <li key={i} className={`st-inbox__item st-inbox__item--${item.kind}`}>
          <button type="button" onClick={() => { go(item.station, item.clip ? { clip: item.clip } : {}); setInboxOpen(false); }}>
            <span>{item.message}</span>{item.hint && <small>{item.hint}</small>}
          </button>
        </li>)}</ul>}
    </aside>}
  </div>;
}

export type StationProps = {
  film: FilmDetail;
  act(label: string, fn: () => Promise<unknown>): Promise<void>;
  busy: string;
  go(s: StationKey, extra?: Record<string, string>): void;
  reload(): Promise<FilmDetail | null>;
  params: URLSearchParams;
};

export function StudioPage() {
  const { videoId } = useParams();
  return <div className="st">{videoId ? <Workspace id={videoId} key={videoId} /> : <Home />}</div>;
}

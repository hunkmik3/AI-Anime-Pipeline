import { useState } from "react";
import { cameraLabel, fmtTime, studioApi, type Clip, type FilmDetail } from "./api";
import type { StationProps } from "./StudioPage";

const QUALITY: Record<string, string> = { checks_passed: "Đạt kiểm tra tự động", needs_review: "Cần bạn nghe/xem", failed: "Kiểm tra lỗi" };

function ClipReview({ film, clip, act, busy }: { film: FilmDetail; clip: Clip; act: StationProps["act"]; busy: string }) {
  const q = clip.quality;
  const lines = clip.shots.flatMap(s => s.lines.map(l => l.line)).join(" ");
  return <article className={`st-review${clip.approval ? " is-approved" : ""}`}>
    <header>
      <h3>{clip.label}</h3>
      <small>{fmtTime(clip.source_start)}–{fmtTime(clip.source_end)}</small>
      {clip.approval ? <span className="st-pill st-pill--done">Đã duyệt</span>
        : q?.status ? <span className={`st-pill st-pill--${q.status === "checks_passed" ? "done" : "attention"}`}>{QUALITY[q.status] ?? q.status}</span> : null}
    </header>
    <div className="st-review__players">
      <figure>
        <video key={clip.url ?? ""} controls preload="metadata" src={clip.url ?? undefined} />
        <figcaption>Bản gen{clip.cleaned_url ? " (đã lọc nhạc)" : ""}</figcaption>
      </figure>
      {clip.motion_url && <figure>
        <video key={clip.motion_url} controls preload="none" src={clip.motion_url} />
        <figcaption>Clay{clip.motion_method?.includes("pose") ? " + khung xương" : ""}</figcaption>
      </figure>}
      <figure>
        <video key={clip.key} controls preload="none" src={`${studioApi.sourceUrl(film.id)}#t=${clip.source_start},${clip.source_end}`} />
        <figcaption>Video gốc</figcaption>
      </figure>
    </div>
    {clip.camera.length > 0 && <p className="st-review__lines"><span>Máy quay đo từ nền (gần đúng):</span>{" "}
      {clip.camera.map(c => `shot ${c.shot + 1} ${cameraLabel(c.label)}`).join(" · ")}</p>}
    {lines && <p className="st-review__lines"><span>Thoại cần có:</span> {lines}</p>}
    {q?.heard && <p className="st-review__lines"><span>Nghe được trong bản gen:</span> {q.heard}</p>}
    {q && q.findings.length > 0 && <ul className="st-review__findings">{q.findings.map((f, i) => <li key={i}>{f}</li>)}</ul>}
    {q?.error && <p className="st-reason">{q.error.slice(0, 240)}</p>}
    <div className="st-actions">
      {clip.approval
        ? <button type="button" className="st-link" disabled={!!busy} onClick={() => void act(`unapprove-${clip.key}`, () => studioApi.approve(film.id, clip.key, clip.url!, false))}>Bỏ duyệt</button>
        : <button type="button" className="st-primary" disabled={!!busy || !clip.url} onClick={() => void act(`approve-${clip.key}`, () => studioApi.approve(film.id, clip.key, clip.url!, true))}>Duyệt clip này</button>}
    </div>
  </article>;
}

export function ReviewStation({ film, act, busy }: StationProps) {
  const [all, setAll] = useState(false);
  const made = film.clips.filter(c => c.url);
  const needs = made.filter(c => !c.approval && c.quality?.status !== "checks_passed");
  const shown = all ? made : needs;
  const missing = film.clips.filter(c => !c.url);
  return <div className="st-reviewst">
    <section className="st-card st-export">
      <div>
        <h2>Ghép phim</h2>
        <p>{made.length}/{film.clips.length} clip đã có bản gen{missing.length ? ` — thiếu ${missing.map(c => c.label).join(", ")}` : ""}.
          Ghép theo thứ tự phim, dùng bản đã lọc nhạc nếu có.</p>
      </div>
      <button type="button" className="st-primary" disabled={!!busy || !made.length}
        onClick={() => void act("assemble", () => studioApi.assemble(film.id))}>{busy === "assemble" ? "Đang ghép…" : "Ghép phim"}</button>
      {film.films.length > 0 && <ul className="st-export__list">
        {film.films.map(f => <li key={f.filename}>
          <a href={studioApi.filmUrl(film.id, f.filename)} target="_blank" rel="noreferrer">{new Date(f.at).toLocaleString("vi-VN")}</a>
          <span>{fmtTime(f.duration_s)} · {f.clips.length} clip{f.missing.length ? ` · thiếu ${f.missing.length}` : ""}</span>
        </li>)}
      </ul>}
    </section>
    <div className="st-tabs" role="tablist">
      <button type="button" role="tab" aria-selected={!all} className={!all ? "is-on" : ""} onClick={() => setAll(false)}>Cần xem <small>{needs.length}</small></button>
      <button type="button" role="tab" aria-selected={all} className={all ? "is-on" : ""} onClick={() => setAll(true)}>Tất cả clip đã gen <small>{made.length}</small></button>
    </div>
    {shown.length === 0 ? <p className="st-empty">{all ? "Chưa có clip nào." : "Không còn clip nào chờ bạn xem."}</p>
      : <div className="st-review-grid">{shown.map(c => <ClipReview key={c.key} film={film} clip={c} act={act} busy={busy} />)}</div>}
  </div>;
}

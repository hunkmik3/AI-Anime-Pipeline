import { useState } from "react";
import { cameraLabel, fmtTime, studioApi, type AudioChoice, type Clip, type FilmDetail, type Take } from "./api";
import type { StationProps } from "./StudioPage";

const QUALITY: Record<string, string> = { checks_passed: "Đạt kiểm tra tự động", needs_review: "Cần bạn nghe/xem", failed: "Kiểm tra lỗi" };
const AUDIO: Record<AudioChoice, string> = { native: "Tiếng gốc Seedance", cleaned: "Lọc nhạc (chỉ giữ giọng nói)", silent: "Im lặng" };

function Takes({ film, clip, act, busy }: { film: FilmDetail; clip: Clip; act: StationProps["act"]; busy: string }) {
  const [list, setList] = useState<Take[] | null>(null);
  const [open, setOpen] = useState(false);
  async function toggle() {
    if (!open && !list) setList((await studioApi.clip(film.id, clip.key)).takes);
    setOpen(o => !o);
  }
  if (clip.take_count < 2) return null;
  return <div className="st-takes">
    <button type="button" className="st-link" onClick={() => void toggle()} aria-expanded={open}>Các bản gen ({clip.take_count})</button>
    {open && list && <ul>{list.map(t => <li key={t.job_id} className={t.job_id === clip.take_id ? "is-current" : ""}>
      <a href={t.cleaned_url ?? t.render_url} target="_blank" rel="noreferrer">{t.at ? new Date(t.at).toLocaleString("vi-VN") : t.job_id.slice(0, 8)}</a>
      {t.quality?.status && <span className="st-muted">{QUALITY[t.quality.status] ?? t.quality.status}</span>}
      {t.job_id === clip.take_id ? <span className="st-pill st-pill--done">Đang dùng</span>
        : <button type="button" className="st-link" disabled={!!busy || film.revision == null}
          onClick={() => void act(`take-${clip.key}`, () => studioApi.editClip(film.id, clip.key, film.revision!, { selected_take: t.job_id }))}>Dùng bản này</button>}
    </li>)}</ul>}
  </div>;
}

function ClipReview({ film, clip, act, busy }: { film: FilmDetail; clip: Clip; act: StationProps["act"]; busy: string }) {
  const q = clip.quality;
  return <article className={`st-review${clip.approval ? " is-approved" : ""}`}>
    <header>
      <h3>{clip.label}</h3>
      <small>{fmtTime(clip.source_start)}–{fmtTime(clip.source_end)}</small>
      {clip.approval ? <span className="st-pill st-pill--done">Đã duyệt</span>
        : q?.status ? <span className={`st-pill st-pill--${q.status === "checks_passed" ? "done" : "attention"}`}>{QUALITY[q.status] ?? q.status}</span> : null}
    </header>
    <div className="st-review__players">
      <figure>
        <video key={clip.url ?? ""} controls preload="metadata" src={clip.url ?? undefined} muted={clip.audio_choice === "silent"} />
        <figcaption>Bản gen · {clip.cleaning ? "đang lọc nhạc…" : AUDIO[clip.audio_choice]}</figcaption>
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
    {clip.dialogue_text && <p className="st-review__lines"><span>Thoại cần có:</span> {clip.dialogue_text}</p>}
    {q?.heard && <p className="st-review__lines"><span>Nghe được trong bản gen:</span> {q.heard}</p>}
    {q && q.findings.length > 0 && <ul className="st-review__findings">{q.findings.map((f, i) => <li key={i}>{f}</li>)}</ul>}
    {q?.error && <p className="st-reason">{q.error.slice(0, 240)}</p>}
    {clip.cleanup_error && <p className="st-reason">Lọc nhạc lỗi: {clip.cleanup_error.slice(0, 240)} — clip đang dùng tiếng gốc.</p>}
    <Takes film={film} clip={clip} act={act} busy={busy} />
    <div className="st-actions">
      {clip.approval
        ? <button type="button" className="st-link" disabled={!!busy} onClick={() => void act(`unapprove-${clip.key}`, () => studioApi.approve(film.id, clip.key, clip.url!, false))}>Bỏ duyệt</button>
        : <button type="button" className="st-primary" disabled={!!busy || !clip.url} onClick={() => void act(`approve-${clip.key}`, () => studioApi.approve(film.id, clip.key, clip.url!, true))}>Duyệt clip này</button>}
      <label className="st-inline">
        <span>Âm thanh</span>
        <select value={clip.audio_choice} disabled={!!busy || film.revision == null}
          onChange={e => void act(`audio-${clip.key}`, () => studioApi.editClip(film.id, clip.key, film.revision!, { audio_choice: e.target.value as AudioChoice }))}>
          {(Object.keys(AUDIO) as AudioChoice[]).map(a => <option key={a} value={a}>{AUDIO[a]}</option>)}
        </select>
      </label>
      <button type="button" className="st-link" disabled={!!busy || !clip.url} onClick={() => void act(`recheck-${clip.key}`, () => studioApi.recheck(film.id, clip.key))}>Kiểm tra lại</button>
      <button type="button" className="st-link" disabled={!!busy || film.revision == null}
        onClick={() => {
          if (!window.confirm(`Gen lại ${clip.label} thành bản mới (khoảng $${clip.estimate_usd.toFixed(2)})? Bản hiện tại vẫn giữ để chọn.`)) return;
          void act(`retake-${clip.key}`, () => studioApi.retake(film.id, film.revision!, [clip.key]));
        }}>Gen lại</button>
    </div>
  </article>;
}

const megabytes = (n: number) => `${(n / 1e6).toFixed(1)} MB`;

export function ReviewStation({ film, act, busy }: StationProps) {
  const [all, setAll] = useState(false);
  const made = film.clips.filter(c => c.url);
  const needs = made.filter(c => !c.approval && c.quality?.status !== "checks_passed");
  const shown = all ? made : needs;
  const missing = film.clips.filter(c => !c.url);
  const latest = film.films[0];
  return <div className="st-reviewst">
    <section className="st-card st-export">
      <div>
        <h2>Ghép &amp; xuất</h2>
        <p>{made.length}/{film.clips.length} clip đã có bản gen{missing.length ? ` — thiếu ${missing.map(c => c.label).join(", ")}` : ""}.
          Ghép theo thứ tự phim, dùng bản gen và âm thanh bạn chọn cho từng clip (mặc định tiếng gốc Seedance;
          muốn bỏ nhạc ở clip nào thì chọn "Lọc nhạc" cho clip đó).</p>
      </div>
      <div className="st-actions st-actions--tight">
        <button type="button" disabled={!!busy || !made.length} onClick={() => void act("export", () => studioApi.exportClips(film.id))}>
          {busy === "export" ? "Đang đóng gói…" : "Xuất clip + EDL"}</button>
        <button type="button" className="st-primary" disabled={!!busy || !made.length}
          onClick={() => void act("assemble", () => studioApi.assemble(film.id))}>{busy === "assemble" ? "Đang ghép…" : "Ghép phim"}</button>
      </div>
      {latest && (latest.kept_full?.length || latest.unchecked?.length) ? <p className="st-note st-export__wide">
        {latest.kept_full?.length ? `Giữ nguyên độ dài để không cắt mất thoại: ${latest.kept_full.join(", ")}. ` : ""}
        {latest.unchecked?.length ? `Chưa có kiểm tra thoại nên cắt đúng gốc mà không chắc: ${latest.unchecked.join(", ")}.` : ""}</p> : null}
      {film.films.length > 0 && <ul className="st-export__list">
        {film.films.map(f => <li key={f.filename}>
          <a href={studioApi.filmUrl(film.id, f.filename)} target="_blank" rel="noreferrer">Phim · {new Date(f.at).toLocaleString("vi-VN")}</a>
          <span>{fmtTime(f.duration_s)} · {f.clips.length} clip{f.missing.length ? ` · thiếu ${f.missing.length}` : ""}</span>
        </li>)}
      </ul>}
      {film.exports.length > 0 && <ul className="st-export__list">
        {film.exports.map(x => <li key={x.filename}>
          <a href={studioApi.exportUrl(film.id, x.filename)}>Gói clip · {new Date(x.at).toLocaleString("vi-VN")}</a>
          <span>{x.clips.length} clip · {megabytes(x.bytes)} · có film.edl và clips.csv</span>
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

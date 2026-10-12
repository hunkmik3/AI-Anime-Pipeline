import { useEffect, useState } from "react";
import { CLIP_STATUS_LABEL, IMAGE_MODEL_LABELS, MOTION_LABEL, cameraLabel, fmtTime, studioApi, type Clip, type FilmDetail, type MotionProfile } from "./api";
import type { StationProps } from "./StudioPage";

const TONE: Record<string, string> = {
  done: "done", rendering: "running", preparing: "running", reviewing: "running", prepared: "pending",
  waiting: "pending", failed: "attention", held: "attention",
};

function PromptEditor({ film, clip, act, busy, onClose }: { film: FilmDetail; clip: Clip; act: StationProps["act"]; busy: string; onClose(): void }) {
  const [loaded, setLoaded] = useState<{ prompt: string; end_state: string; manual: boolean; verified?: boolean; writer: string } | null>(null);
  const [text, setText] = useState("");
  const [endState, setEndState] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    studioApi.prompt(film.id, clip.key)
      .then(p => { setLoaded(p); setText(p.prompt ?? ""); setEndState(p.end_state ?? ""); })
      .catch(e => setError(String(e.message ?? e)));
  }, [film.id, clip.key]);
  const changed = loaded && (text !== loaded.prompt || endState !== (loaded.end_state ?? ""));
  return <div className="st-modal" role="dialog" aria-label={`Prompt ${clip.label}`}>
    <div className="st-modal__box st-modal__box--editor">
      <header>
        <h2>Prompt — {clip.label}</h2>
        {loaded && <span className={`st-pill st-pill--${loaded.manual ? "attention" : "done"}`}>
          {loaded.manual ? (loaded.verified ? "Tự viết · đã kiểm tra" : "Tự viết · chưa kiểm tra") : `Tự động · ${loaded.writer}`}</span>}
        <button type="button" onClick={onClose} aria-label="Đóng">×</button>
      </header>
      {error ? <p className="st-error">{error}</p> : loaded === null ? <p className="st-muted">Đang tải…</p> : <div className="st-editor">
        <p className="st-muted">Sửa trực tiếp rồi bấm Dùng prompt này. Trước khi render, một model độc lập đối chiếu nguyên văn prompt với shotlist
          (thoại, người nói, khoá nguồn); prompt không bị viết lại, sai thì clip dừng và báo lý do.</p>
        <textarea className="st-prompt st-prompt--edit" value={text} onChange={e => setText(e.target.value)} spellCheck={false} aria-label="Nội dung prompt" />
        <label className="st-field st-field--compact">
          <span>Trạng thái cuối clip (cho clip sau nối tiếp)</span>
          <textarea rows={2} value={endState} onChange={e => setEndState(e.target.value)} />
        </label>
        <div className="st-actions">
          <button type="button" className="st-primary" disabled={!changed || !!busy || film.revision == null}
            onClick={() => void act(`prompt-${clip.key}`, async () => {
              await studioApi.editClip(film.id, clip.key, film.revision!, { manual_prompt: text, end_state: endState });
              onClose();
            })}>Dùng prompt này cho clip</button>
          {loaded.manual && <button type="button" disabled={!!busy || film.revision == null}
            onClick={() => void act(`auto-${clip.key}`, async () => {
              await studioApi.editClip(film.id, clip.key, film.revision!, { auto_prompt: true });
              onClose();
            })}>Về prompt tự động</button>}
          {changed && <button type="button" className="st-link" onClick={() => { setText(loaded.prompt); setEndState(loaded.end_state ?? ""); }}>Bỏ thay đổi</button>}
        </div>
      </div>}
    </div>
  </div>;
}

function Preview({ clip, onClose, onPrompt }: { clip: Clip; onClose(): void; onPrompt(): void }) {
  return <div className="st-modal" role="dialog" aria-label={`Xem trước ${clip.label}`}>
    <div className="st-modal__box st-modal__box--editor">
      <header><h2>Xem trước — {clip.label}</h2><button type="button" onClick={onClose} aria-label="Đóng">×</button></header>
      <div className="st-preview">
        <figure>
          {clip.motion_url ? <video controls preload="metadata" src={clip.motion_url} /> : <div className="st-preview__none">Chưa dựng clay</div>}
          <figcaption>Clay{clip.motion_method?.includes("pose") ? " + khung xương" : ""}</figcaption>
        </figure>
        <figure>
          {clip.staging_url ? <img src={clip.staging_url} alt={`Ảnh dàn cảnh ${clip.label}`} /> : <div className="st-preview__none">Chưa có ảnh dàn cảnh</div>}
          <figcaption>Ảnh dàn cảnh{clip.staging_model_used ? ` · vẽ bằng ${IMAGE_MODEL_LABELS[clip.staging_model_used] ?? clip.staging_model_used} (dự phòng)` : ""}</figcaption>
        </figure>
      </div>
      <div className="st-editor">
        {clip.camera.length > 0 && <p className="st-muted">Máy quay đo từ nền: {clip.camera.map(c => `shot ${c.shot + 1} ${cameraLabel(c.label)}`).join(" · ")}</p>}
        <div className="st-actions">
          {(clip.has_prompt || clip.prompt_by === "manual") && <button type="button" onClick={onPrompt}>Mở prompt</button>}
          <span className="st-muted">{clip.status === "prepared" ? "Đã chuẩn bị xong; render khi bạn thấy ổn." : CLIP_STATUS_LABEL[clip.status]}</span>
        </div>
      </div>
    </div>
  </div>;
}

function AbsentButton({ film, job, act, busy }: { film: FilmDetail; job: Clip["unknown_jobs"][number]; act: StationProps["act"]; busy: string }) {
  return <button type="button" className="st-link" disabled={!!busy}
    title="Nhà cung cấp đã từ chối nên không có gì được tạo; đóng job để chạy lại."
    onClick={() => void act(`absent-${job.id}`, () => studioApi.markAbsent(film.id, job.id,
      `Studio: provider refused the request (${job.error.slice(0, 200) || 'no submission'}); nothing was created.`))}>
    Đóng job {job.kind} chưa rõ</button>;
}

export function ClipsStation({ film, act, busy, go }: StationProps) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [prompt, setPrompt] = useState<Clip | null>(null);
  const [preview, setPreview] = useState<Clip | null>(null);
  const clips = film.clips;
  const chosen = clips.filter(c => picked.has(c.key));
  const usd = chosen.reduce((s, c) => s + c.estimate_usd, 0);
  const toggle = (key: string) => setPicked(p => { const n = new Set(p); n.has(key) ? n.delete(key) : n.add(key); return n; });
  const pick = (f: (c: Clip) => boolean) => setPicked(new Set(clips.filter(f).map(c => c.key)));
  const running = film.runs.some(r => r.status === "running");
  if (!clips.length) return <p className="st-empty">Chưa có clip — trạm Nguồn đang dựng board.</p>;
  const doneChosen = chosen.filter(c => c.take_count > 0);
  async function start(mode: "prepare" | "render") {
    await act(`run-${mode}`, () => studioApi.render(film.id, film.revision!, chosen.map(c => c.key), mode));
    setPicked(new Set());
  }
  async function retake() {
    const usd = doneChosen.reduce((s, c) => s + c.estimate_usd, 0);
    if (!window.confirm(`Gen lại ${doneChosen.length} clip thành bản mới (khoảng $${usd.toFixed(2)})? Bản cũ vẫn giữ để chọn ở trạm Duyệt.`)) return;
    await act("retake", () => studioApi.retake(film.id, film.revision!, doneChosen.map(c => c.key)));
    setPicked(new Set());
  }
  return <div className="st-clips">
    <div className="st-toolbar">
      <span>Chọn nhanh:</span>
      <button type="button" className="st-link" onClick={() => pick(c => c.status !== "done")}>Chưa xong</button>
      <button type="button" className="st-link" onClick={() => pick(c => c.status === "failed")}>Lỗi</button>
      <button type="button" className="st-link" onClick={() => pick(() => true)}>Tất cả</button>
      <button type="button" className="st-link" onClick={() => setPicked(new Set())}>Bỏ chọn</button>
      <span className="st-toolbar__grow" />
      <span className="st-muted">{chosen.length} clip · ~${usd.toFixed(2)}</span>
      <button type="button" disabled={!chosen.length || !!busy || running || film.revision == null} onClick={() => void start("prepare")}
        title="Vẽ thẻ, dựng clay, ảnh dàn cảnh và viết prompt — chưa render">Chuẩn bị</button>
      <button type="button" disabled={!doneChosen.length || !!busy || running || film.revision == null} onClick={() => void retake()}
        title="Clip đã gen: làm bản mới, giữ bản cũ">Gen lại {doneChosen.length || ""}</button>
      <button type="button" className="st-primary" disabled={!chosen.length || !!busy || running || film.revision == null} onClick={() => void start("render")}>
        Render {chosen.length || ""}</button>
    </div>
    {running && <p className="st-note">Đang có một lượt chạy; chờ nó xong để bắt đầu lượt khác.</p>}
    <table className="st-table">
      <thead><tr><th scope="col"><span className="st-sr">Chọn</span></th><th scope="col">Clip</th><th scope="col">Thời lượng</th>
        <th scope="col">Trạng thái</th><th scope="col">Chuyển động</th><th scope="col">Ảnh dàn cảnh</th><th scope="col">Ước tính</th><th scope="col"></th></tr></thead>
      <tbody>
        {clips.map(c => <tr key={c.key} className={picked.has(c.key) ? "is-picked" : ""}>
          <td><input type="checkbox" aria-label={`Chọn ${c.label}`} checked={picked.has(c.key)} onChange={() => toggle(c.key)} /></td>
          <td><strong>{c.label}</strong><small>{fmtTime(c.source_start)}–{fmtTime(c.source_end)} · {c.shot_count} shot{c.take_count > 1 ? ` · ${c.take_count} bản` : ""}</small></td>
          <td className="st-num">{c.seconds.toFixed(1)} s</td>
          <td>
            <span className={`st-pill st-pill--${TONE[c.status] ?? "pending"}`}>{CLIP_STATUS_LABEL[c.status] ?? c.status}</span>
            {c.reason && <small className="st-reason" title={c.reason}>{c.hint || c.reason.slice(0, 160)}</small>}
            {c.status === "held" && <button type="button" className="st-link" onClick={() => go("shotlist", { clip: c.key })}>Xử lý ở Shotlist</button>}
            {c.unknown_jobs.map(j => <AbsentButton key={j.id} film={film} job={j} act={act} busy={busy} />)}
          </td>
          <td>
            <select aria-label={`Kiểu chuyển động ${c.label}`} value={c.motion_profile ?? "standard"} disabled={!!busy || film.revision == null}
              onChange={e => void act(`motion-${c.key}`, () => studioApi.editClip(film.id, c.key, film.revision!, { motion_profile: e.target.value }))}>
              {(Object.keys(MOTION_LABEL) as MotionProfile[]).map(k => <option key={k} value={k}>{MOTION_LABEL[k]}</option>)}
            </select>
          </td>
          <td>
            <select aria-label={`Model ảnh dàn cảnh ${c.label}`} value={c.staging_image_model} disabled={!!busy || film.revision == null}
              onChange={e => void act(`staging-${c.key}`, () => studioApi.editClip(film.id, c.key, film.revision!, { staging_image_model: e.target.value }))}>
              {Object.entries(IMAGE_MODEL_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </td>
          <td className="st-num">${c.estimate_usd.toFixed(2)}</td>
          <td className="st-row-actions">
            <button type="button" className="st-link" onClick={() => setPreview(c)}>Xem trước</button>
            {(c.has_prompt || c.prompt_by === "manual") && <button type="button" className="st-link" onClick={() => setPrompt(c)}>
              {c.prompt_by === "manual" ? "Prompt tự viết" : "Prompt"}</button>}
            {c.url && <a className="st-link" href={c.url} target="_blank" rel="noreferrer">Xem clip</a>}
          </td>
        </tr>)}
      </tbody>
    </table>
    {film.runs.length > 0 && <section className="st-card st-runs">
      <h2>Các lượt chạy gần đây</h2>
      <ul>{film.runs.map(r => <li key={r.id}>
        <span className={`st-pill st-pill--${r.status === "succeeded" ? "done" : r.status === "running" ? "running" : "attention"}`}>{r.status}</span>
        <span>{r.mode === "prepare" ? "Chuẩn bị" : "Render"} · {r.keys.length ? `${r.keys.length} clip` : "cả phim"} · {new Date(r.created_at).toLocaleString("vi-VN")}</span>
        {r.status === "running" && <button type="button" className="st-link" disabled={!!busy}
          onClick={() => void act(`pause-${r.id}`, () => studioApi.controlRun(film.id, r.id, "pause"))}>Tạm dừng</button>}
        {(r.status === "paused" || r.status === "blocked") && <button type="button" className="st-link" disabled={!!busy || running}
          onClick={() => void act(`resume-${r.id}`, () => studioApi.controlRun(film.id, r.id, "resume"))}>Chạy tiếp</button>}
        {r.errors.length > 0 && <small className="st-reason">{r.errors[0]}</small>}
      </li>)}</ul>
    </section>}
    {prompt && <PromptEditor film={film} clip={prompt} act={act} busy={busy} onClose={() => setPrompt(null)} />}
    {preview && <Preview clip={preview} onClose={() => setPreview(null)} onPrompt={() => { setPrompt(preview); setPreview(null); }} />}
  </div>;
}

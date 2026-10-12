import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { STYLE_PRESETS } from "../store/videoAnalysis";
import { IMAGE_MODEL_LABELS, MOTION_LABEL, studioApi, uploadMaterialImage, type FilmDetail, type MotionProfile } from "./api";
import type { StationProps } from "./StudioPage";

const yes = (v?: boolean) => (v ? "Có" : "Không");
const KIND: Record<string, string> = {
  shot: "Shot", cut: "Điểm cắt", material: "Material", image: "Ảnh sheet", clip: "Clip", split: "Tách clip", merge: "Gộp clip",
  settings: "Thiết lập", undo: "Hoàn tác",
};

function Settings({ film, act, busy }: { film: FilmDetail; act: StationProps["act"]; busy: string }) {
  const s = film.settings;
  const b = film.board_settings;
  const initial = {
    resolution: String(s.resolution ?? "480p"), timing_policy: String(s.timing_policy ?? "full_take"),
    motion_profile: (b?.motionProfile ?? s.motion_profile ?? "standard") as MotionProfile,
    pause_for_review: !!s.pause_for_review, prepare_before_render: !!s.prepare_before_render, review_masters: !!s.review_masters,
    image_fallback_model: b?.imageFallbackModel ?? "", budget: b?.budgetUsd ? String(b.budgetUsd) : "",
    style_note: b?.styleNote ?? "", style_image_url: b?.styleImageUrl ?? "",
  };
  const [form, setForm] = useState(initial);
  const [notes, setNotes] = useState<string[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const version = JSON.stringify(initial);
  useEffect(() => setForm(initial), [version]);
  const set = (change: Partial<typeof form>) => setForm(f => ({ ...f, ...change }));
  const changed = Object.fromEntries(Object.entries(form).filter(([k, v]) => v !== (initial as Record<string, unknown>)[k]));
  async function save() {
    const body: Record<string, unknown> = { expected_revision: film.revision ?? undefined };
    for (const [k, v] of Object.entries(changed)) {
      if (k === "budget") { if (v === "") body.clear_budget = true; else body.budget_usd = Number(String(v).replace(",", ".")); }
      else body[k] = v;
    }
    await act("settings", async () => {
      const preview = await studioApi.editSettings(film.id, { ...body, dry_run: true });
      const many = preview.impact.clips.length;
      if (many && !window.confirm(`${preview.impact.notes?.join("\n") ?? ""}\n${many} clip sẽ phải làm lại (khoảng $${preview.impact.estimate_usd.toFixed(2)}). Lưu?`)) return;
      const out = await studioApi.editSettings(film.id, body);
      setNotes(out.impact.notes ?? []);
    });
  }
  async function styleImage(file: File) {
    await act("style-image", async () => {
      const { url } = await uploadMaterialImage(film.project_id!, file);
      set({ style_image_url: url });
    });
  }
  return <section className="st-card">
    <h2>Thiết lập sản xuất</h2>
    <div className="st-settings">
      <label className="st-field"><span>Độ phân giải</span>
        <select value={form.resolution} onChange={e => set({ resolution: e.target.value })}><option>480p</option><option>720p</option></select></label>
      <label className="st-field"><span>Khi ghép phim</span>
        <select value={form.timing_policy} onChange={e => set({ timing_policy: e.target.value })}>
          <option value="full_take">Giữ trọn bản gen</option><option value="source_duration">Cắt đúng thời lượng gốc</option></select></label>
      <label className="st-field"><span>Chuyển động (mặc định cả phim)</span>
        <select value={form.motion_profile} onChange={e => set({ motion_profile: e.target.value as MotionProfile })}>
          {(Object.keys(MOTION_LABEL) as MotionProfile[]).map(k => <option key={k} value={k}>{MOTION_LABEL[k]}</option>)}</select></label>
      <label className="st-field"><span>Khi model ảnh bị nhà cung cấp chặn, vẽ bằng</span>
        <select value={form.image_fallback_model} onChange={e => set({ image_fallback_model: e.target.value })}>
          <option value="">Không tự chuyển</option>
          {Object.entries(IMAGE_MODEL_LABELS).filter(([k]) => k).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      <label className="st-field"><span>Trần chi phí Seedance (USD)</span>
        <input inputMode="decimal" value={form.budget} onChange={e => set({ budget: e.target.value })} placeholder="Để trống = không giới hạn" /></label>
    </div>
    <label className="st-field"><span>Ghi chú phong cách (thêm vào mọi sheet và prompt)</span>
      <textarea rows={2} value={form.style_note} onChange={e => set({ style_note: e.target.value })} placeholder="Ví dụ: tông màu ấm, hạt phim nhẹ, ánh hoàng hôn" /></label>
    <div className="st-style-image">
      {form.style_image_url ? <img src={form.style_image_url} alt="Ảnh mẫu phong cách" /> : <span className="st-muted">Chưa có ảnh mẫu phong cách</span>}
      <button type="button" className="st-link" disabled={!!busy || !film.project_id} onClick={() => input.current?.click()}>Chọn ảnh mẫu</button>
      {form.style_image_url && <button type="button" className="st-link" onClick={() => set({ style_image_url: "" })}>Bỏ ảnh mẫu</button>}
      <input ref={input} type="file" accept="image/*" hidden onChange={e => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void styleImage(f); }} />
    </div>
    <fieldset className="st-gates">
      <legend>Dừng để tôi duyệt</legend>
      {([["pause_for_review", "Sau shotlist & danh mục"], ["review_masters", "Sau khi vẽ thẻ chính"], ["prepare_before_render", "Trước khi render"]] as const)
        .map(([k, label]) => <label key={k} className="st-gate"><input type="checkbox" checked={form[k]} onChange={e => set({ [k]: e.target.checked })} />
          <span><strong>{label}</strong></span></label>)}
    </fieldset>
    <div className="st-actions">
      <button type="button" className="st-primary" disabled={!Object.keys(changed).length || !!busy} onClick={() => void save()}>Lưu thiết lập</button>
      {Object.keys(changed).length > 0 && <button type="button" className="st-link" onClick={() => setForm(initial)}>Bỏ thay đổi</button>}
    </div>
    {notes.length > 0 && <p className="st-note">{notes.join(" ")}</p>}
  </section>;
}

function History({ film, act, busy }: { film: FilmDetail; act: StationProps["act"]; busy: string }) {
  if (!film.history.length) return <section className="st-card"><h2>Lịch sử thay đổi</h2><p className="st-muted">Chưa có thay đổi nào trong studio.</p></section>;
  return <section className="st-card st-history">
    <h2>Lịch sử thay đổi</h2>
    <ul>{film.history.map(h => <li key={h.id}>
      <span className="st-pill st-pill--pending">{KIND[h.kind] ?? h.kind}</span>
      <span className="st-history__what">{h.summary}</span>
      <small>{new Date(h.at).toLocaleString("vi-VN")} · {h.by}{h.clips.length ? ` · ${h.clips.join(", ")}` : ""}</small>
      {h.kind !== "undo" && <button type="button" className="st-link" disabled={!!busy || film.revision == null}
        onClick={() => { if (window.confirm(`Hoàn tác: ${h.summary}?`)) void act(`undo-${h.id}`, () => studioApi.undo(film.id, h.id, film.revision!)); }}>Hoàn tác</button>}
    </li>)}</ul>
  </section>;
}

export function SourceStation({ film, act, busy }: StationProps) {
  const navigate = useNavigate();
  const s = film.settings;
  const rows: [string, string][] = [
    ["Phong cách", STYLE_PRESETS.find(p => p.key === s.style)?.label ?? String(s.style ?? "")],
    ["Khung hình", String(s.aspect_ratio ?? "")],
    ["Clip dài tối đa", `${s.clip_seconds ?? 15} s`],
    ["Phạm vi", s.generate_seconds ? `${Math.round(s.generate_seconds / 60)} phút đầu` : "Cả phim"],
    ["Đổi vai", s.casting_request || "—"],
    ["Đổi bối cảnh", s.environment_request || "—"],
    ["Duyệt thẻ chính", yes(s.review_masters)],
  ];
  const stopped = film.status === "failed" || film.status === "interrupted" || film.stage === "blocked";
  const waiting = film.stage === "awaiting_review";
  const p = film.progress;
  async function remove() {
    const typed = window.prompt(`Xoá phim "${film.name}" cùng board, mọi bản gen và phân tích? Gõ lại tên phim để xác nhận.`);
    if (typed !== film.name) return;
    await act("delete", async () => { await studioApi.deleteFilm(film.id); navigate("/studio"); });
  }
  return <div className="st-source">
    <section className="st-card">
      <h2>Tiến độ</h2>
      <p className="st-source__stage"><strong>{film.stage_label || film.status}</strong>
        {p.total ? <span> · {p.done ?? 0}/{p.total} {p.stage ?? ""}</span> : null}</p>
      {p.total ? <progress max={p.total} value={p.done ?? 0} /> : null}
      {film.error && <p className="st-error">{film.error}</p>}
      {waiting && <p className="st-note">Shotlist, danh mục và material đã sẵn sàng. Xem trạm 2–3, sửa nếu cần, rồi bấm Tiếp tục để vẽ thẻ và render.</p>}
      <div className="st-actions">
        {(waiting || stopped) && <button type="button" className="st-primary" disabled={!!busy}
          onClick={() => void act("continue", () => studioApi.continueFilm(film.id))}>
          {waiting ? "Tiếp tục sản xuất" : "Chạy tiếp từ chỗ dừng"}</button>}
      </div>
      <dl className="st-kv">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
    </section>
    <Settings film={film} act={act} busy={busy} />
    <section className="st-card st-source__video">
      <h2>Video gốc</h2>
      <video controls preload="metadata" src={studioApi.sourceUrl(film.id)} />
    </section>
    <History film={film} act={act} busy={busy} />
    <section className="st-card st-danger">
      <h2>Xoá phim</h2>
      <p className="st-muted">Xoá board, mọi bản gen, lịch sử và phân tích của phim này. Không hoàn tác được.</p>
      <button type="button" className="st-danger__btn" disabled={!!busy} onClick={() => void remove()}>Xoá phim…</button>
    </section>
  </div>;
}

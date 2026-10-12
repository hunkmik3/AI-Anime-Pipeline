import { useState, type ChangeEvent, type DragEvent } from "react";
import { useNavigate } from "react-router-dom";
import { DEFAULT_RULES, STYLE_PRESETS } from "../store/videoAnalysis";
import type { FilmStyle } from "../automation/filmStyles";
import { IMAGE_MODEL_LABELS, MOTION_LABEL, uploadFilm, type FilmSettings, type MotionProfile } from "./api";

const PRESETS_KEY = "flowboard.studio.presets";
type Preset = { name: string; values: Record<string, unknown> };
function readPresets(): Preset[] {
  try { return JSON.parse(localStorage.getItem(PRESETS_KEY) || "[]"); } catch { return []; }
}
function writePresets(list: Preset[]) {
  try { localStorage.setItem(PRESETS_KEY, JSON.stringify(list)); } catch { /* storage unavailable: presets stay for this visit */ }
}

const LANGUAGES: [string, string][] = [["", "Tự nhận"], ["en", "Tiếng Anh"], ["vi", "Tiếng Việt"], ["zh", "Tiếng Trung"], ["ja", "Tiếng Nhật"], ["ko", "Tiếng Hàn"], ["es", "Tiếng Tây Ban Nha"]];

function Choice<T extends string | number>({ label, value, options, onChange, hint }: {
  label: string; value: T; options: [T, string][]; onChange(v: T): void; hint?: string;
}) {
  return <fieldset className="st-choice">
    <legend>{label}</legend>
    <div role="radiogroup" aria-label={label}>
      {options.map(([v, text]) => <button type="button" key={String(v)} role="radio" aria-checked={v === value}
        className={v === value ? "is-on" : ""} onClick={() => onChange(v)}>{text}</button>)}
    </div>
    {hint && <small>{hint}</small>}
  </fieldset>;
}

function Gate({ label, hint, checked, onChange }: { label: string; hint: string; checked: boolean; onChange(v: boolean): void }) {
  return <label className="st-gate">
    <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} />
    <span><strong>{label}</strong><small>{hint}</small></span>
  </label>;
}

export function NewFilm() {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [style, setStyle] = useState<FilmStyle>("anime_jp_modern" as FilmStyle);
  const [resolution, setResolution] = useState("480p");
  const [ratio, setRatio] = useState("1:1");
  const [clipSeconds, setClipSeconds] = useState(15);
  const [language, setLanguage] = useState("");
  const [minutes, setMinutes] = useState("");
  const [motion, setMotion] = useState<MotionProfile>("standard");
  const [timing, setTiming] = useState<"full_take" | "source_duration">("full_take");
  const [casting, setCasting] = useState("");
  const [environment, setEnvironment] = useState("");
  const [pauseForReview, setPauseForReview] = useState(true);
  const [reviewMasters, setReviewMasters] = useState(true);
  const [prepareFirst, setPrepareFirst] = useState(false);
  const [longFilm, setLongFilm] = useState(false);
  const [budget, setBudget] = useState("");
  const [fallback, setFallback] = useState("gemini-3-pro-image");
  const [styleNote, setStyleNote] = useState("");
  const [presets, setPresets] = useState<Preset[]>(readPresets);
  const values = { style, resolution, ratio, clipSeconds, language, motion, timing, pauseForReview, reviewMasters, prepareFirst, longFilm, budget, fallback, styleNote };
  function applyPreset(p: Preset) {
    const v = p.values as Partial<typeof values>;
    if (v.style) setStyle(v.style); if (v.resolution) setResolution(v.resolution); if (v.ratio) setRatio(v.ratio);
    if (v.clipSeconds) setClipSeconds(v.clipSeconds); if (v.language !== undefined) setLanguage(v.language);
    if (v.motion) setMotion(v.motion); if (v.timing) setTiming(v.timing);
    if (v.pauseForReview !== undefined) setPauseForReview(v.pauseForReview); if (v.reviewMasters !== undefined) setReviewMasters(v.reviewMasters);
    if (v.prepareFirst !== undefined) setPrepareFirst(v.prepareFirst); if (v.longFilm !== undefined) setLongFilm(v.longFilm);
    if (v.budget !== undefined) setBudget(v.budget); if (v.fallback !== undefined) setFallback(v.fallback); if (v.styleNote !== undefined) setStyleNote(v.styleNote);
  }
  function savePreset() {
    const name = window.prompt("Tên preset (ví dụ: Anime 1:1 phim dài)")?.trim();
    if (!name) return;
    const next = [...presets.filter(p => p.name !== name), { name, values }];
    setPresets(next); writePresets(next);
  }
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);

  function pick(f: File | undefined) {
    if (!f) return;
    setFile(f);
    if (!name) setName(f.name.replace(/\.[^.]+$/, ""));
  }
  function drop(e: DragEvent) {
    e.preventDefault(); setDragging(false);
    pick(e.dataTransfer.files?.[0]);
  }
  async function start() {
    if (!file) return;
    const opening = Number(minutes.replace(",", "."));
    const settings: FilmSettings = {
      pipeline: "reskin_v1", style, aspect_ratio: ratio, resolution, clip_seconds: clipSeconds, timing_policy: timing,
      motion_profile: motion, kyc: true, unmoderated: true, review_masters: reviewMasters,
      pause_for_review: pauseForReview, prepare_before_render: prepareFirst,
      casting_request: casting.trim(), environment_request: environment.trim(),
      ...(clipSeconds > 15 ? { clip_max_shots: 12 } : {}),
      ...(minutes.trim() && opening > 0 ? { generate_seconds: Math.max(10, Math.round(opening * 60)) } : {}),
      ...(longFilm ? { catalog_parallel: 4 } : {}),
      ...(budget.trim() && Number(budget.replace(",", ".")) > 0 ? { budget_usd: Number(budget.replace(",", ".")) } : {}),
      ...(fallback ? { image_fallback_model: fallback } : {}),
      ...(styleNote.trim() ? { style_note: styleNote.trim() } : {}),
    };
    const text = STYLE_PRESETS.find(p => p.key === style)?.text ?? DEFAULT_RULES.visual_style;
    setError(""); setProgress(0);
    try {
      const created = await uploadFilm(file, name.trim() || file.name, settings, { ...DEFAULT_RULES, visual_style: text }, language, setProgress);
      navigate(`/studio/${created.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setProgress(null);
    }
  }

  return <section className="st-new" aria-labelledby="st-new-title">
    <header>
      <h2 id="st-new-title">Phim mới</h2>
      <p>Đưa video gốc vào, chọn cách reskin. Mọi bước sau đó đều sửa được trong studio.</p>
    </header>
    <label className={`st-drop${dragging ? " is-drag" : ""}${file ? " has-file" : ""}`}
      onDragOver={e => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={drop}>
      <input type="file" accept="video/*" onChange={(e: ChangeEvent<HTMLInputElement>) => { pick(e.target.files?.[0]); e.target.value = ""; }} />
      {file ? <span><strong>{file.name}</strong><small>{(file.size / 1e6).toFixed(1)} MB · bấm để đổi file</small></span>
        : <span><strong>Kéo video vào đây</strong><small>hoặc bấm để chọn file (.mp4, .mov)</small></span>}
    </label>
    <div className="st-presets">
      <select aria-label="Preset" value="" onChange={e => { const p = presets.find(x => x.name === e.target.value); if (p) applyPreset(p); }}>
        <option value="">{presets.length ? "Dùng preset…" : "Chưa có preset"}</option>
        {presets.map(p => <option key={p.name} value={p.name}>{p.name}</option>)}
      </select>
      <button type="button" className="st-link" onClick={savePreset}>Lưu thiết lập này thành preset</button>
    </div>
    <label className="st-field">
      <span>Tên phim</span>
      <input value={name} onChange={e => setName(e.target.value)} placeholder="Ví dụ: Unexpected Pregnancy — tập 1" />
    </label>
    <label className="st-field">
      <span>Phong cách</span>
      <select value={style} onChange={e => setStyle(e.target.value as FilmStyle)}>
        {STYLE_PRESETS.map(p => <option key={p.key} value={p.key}>{p.label}</option>)}
      </select>
    </label>
    <div className="st-row">
      <Choice<string> label="Độ phân giải" value={resolution} onChange={setResolution} options={[["480p", "480p"], ["720p", "720p"]]}
        hint="Reskin hiện hỗ trợ tới 720p." />
      <Choice<string> label="Khung hình" value={ratio} onChange={setRatio} options={[["1:1", "1:1"], ["9:16", "9:16"], ["16:9", "16:9"]]} />
    </div>
    <div className="st-row">
      <Choice<number> label="Clip dài tối đa" value={clipSeconds} onChange={setClipSeconds} options={[[15, "15 s"], [20, "20 s"], [30, "30 s"]]}
        hint={clipSeconds > 15 ? "Clip dài từng làm rơi thoại ở bản thử; nên giữ 15 s." : "Đã kiểm chứng trên phim 1011."} />
      <label className="st-field">
        <span>Chuyển động (clay)</span>
        <select value={motion} onChange={e => setMotion(e.target.value as MotionProfile)}>
          {(Object.keys(MOTION_LABEL) as MotionProfile[]).map(k => <option key={k} value={k}>{MOTION_LABEL[k]}</option>)}
        </select>
        <small>{motion.includes("pose") ? "Thêm tay chân dạng ma-nơ-canh và đo máy quay từng shot (4D blocking) — chậm hơn ~2/3, chạy trên máy."
          : motion === "fast" ? "Độ sâu thô hơn, 24 fps — cho phim dài." : "Bản đã kiểm chứng trên phim 1011."}</small>
      </label>
    </div>
    <div className="st-row">
      <label className="st-field">
        <span>Ngôn ngữ thoại</span>
        <select value={language} onChange={e => setLanguage(e.target.value)}>
          {LANGUAGES.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
        </select>
      </label>
      <label className="st-field">
        <span>Chỉ làm N phút đầu</span>
        <input inputMode="decimal" value={minutes} onChange={e => setMinutes(e.target.value)} placeholder="Để trống = cả phim" />
      </label>
    </div>
    <label className="st-field">
      <span>Đổi vai <small>(tuỳ chọn)</small></span>
      <textarea rows={2} value={casting} onChange={e => setCasting(e.target.value)} placeholder="Ví dụ: nữ chính tóc vàng mắt xanh; nam chính người Âu" />
    </label>
    <label className="st-field">
      <span>Đổi bối cảnh <small>(tuỳ chọn)</small></span>
      <textarea rows={2} value={environment} onChange={e => setEnvironment(e.target.value)} placeholder="Ví dụ: chuyển văn phòng sang toà nhà cổ ở Kyoto" />
    </label>
    <label className="st-field">
      <span>Ghi chú phong cách <small>(tuỳ chọn, thêm vào mọi sheet và prompt)</small></span>
      <textarea rows={2} value={styleNote} onChange={e => setStyleNote(e.target.value)} placeholder="Ví dụ: tông màu ấm, hạt phim nhẹ" />
    </label>
    <div className="st-row">
      <label className="st-field">
        <span>Trần chi phí Seedance (USD)</span>
        <input inputMode="decimal" value={budget} onChange={e => setBudget(e.target.value)} placeholder="Để trống = không giới hạn" />
      </label>
      <label className="st-field">
        <span>Khi model ảnh bị chặn, vẽ bằng</span>
        <select value={fallback} onChange={e => setFallback(e.target.value)}>
          <option value="">Không tự chuyển</option>
          {Object.entries(IMAGE_MODEL_LABELS).filter(([k]) => k).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
    </div>
    <Choice<"full_take" | "source_duration"> label="Khi ghép phim" value={timing} onChange={setTiming}
      options={[["full_take", "Giữ trọn bản gen"], ["source_duration", "Cắt đúng thời lượng gốc"]]} />
    <fieldset className="st-gates">
      <legend>Dừng để tôi duyệt</legend>
      <Gate label="Sau shotlist & danh mục" hint="Sửa thoại, người nói, nhân vật trước khi tốn tiền vẽ và render." checked={pauseForReview} onChange={setPauseForReview} />
      <Gate label="Sau khi vẽ thẻ chính" hint="Chốt gương mặt và bối cảnh trước khi vẽ trang phục và clip." checked={reviewMasters} onChange={setReviewMasters} />
      <Gate label="Trước khi render" hint="Vẽ thẻ và viết prompt xong thì chờ; bạn chọn clip để render." checked={prepareFirst} onChange={setPrepareFirst} />
      <Gate label="Phim dài (từ 20 phút)" hint="Lập danh mục theo từng đoạn song song — nhanh hơn nhiều." checked={longFilm} onChange={setLongFilm} />
    </fieldset>
    {error && <p className="st-error" role="alert">{error}</p>}
    <button type="button" className="st-primary" disabled={!file || progress !== null} onClick={() => void start()}>
      {progress === null ? "Bắt đầu" : progress < 1 ? `Đang tải lên ${Math.round(progress * 100)}%` : "Đang tạo phim…"}
    </button>
  </section>;
}

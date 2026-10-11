import { useState, type ChangeEvent, type DragEvent } from "react";
import { useNavigate } from "react-router-dom";
import { DEFAULT_RULES, STYLE_PRESETS } from "../store/videoAnalysis";
import type { FilmStyle } from "../automation/filmStyles";
import { uploadFilm, type FilmSettings } from "./api";

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
  const [motion, setMotion] = useState<"standard" | "fast">("standard");
  const [timing, setTiming] = useState<"full_take" | "source_duration">("full_take");
  const [casting, setCasting] = useState("");
  const [environment, setEnvironment] = useState("");
  const [pauseForReview, setPauseForReview] = useState(true);
  const [reviewMasters, setReviewMasters] = useState(true);
  const [prepareFirst, setPrepareFirst] = useState(false);
  const [longFilm, setLongFilm] = useState(false);
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
      <Choice<"standard" | "fast"> label="Chuyển động (clay)" value={motion} onChange={setMotion} options={[["standard", "Chuẩn"], ["fast", "Nhanh"]]}
        hint={motion === "fast" ? "Độ sâu thô hơn, 24 fps — cho phim dài." : undefined} />
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

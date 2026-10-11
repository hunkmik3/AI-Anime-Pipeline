import { useEffect, useState, type ChangeEvent } from "react";
import { api } from "../api/client";
import { useAutomation } from "../store/automation";
import { autoFilmLabel, DEFAULT_RULES, RUNNING, STAGE_LABELS, STYLE_PRESETS, useVideoAnalysis } from "../store/videoAnalysis";

function SourceToggle({ label, hint, checked, onChange }: {
  label: string; hint?: string; checked: boolean; onChange(value: boolean): void;
}) {
  return <label className="source-input__toggle">
    <span><strong>{label}</strong>{hint && <small>{hint}</small>}</span>
    <input type="checkbox" checked={checked} onChange={event => onChange(event.target.checked)} aria-label={label} />
  </label>;
}

/** Whisper language codes. "Tự nhận diện" probes the audio and finds language switches. */
const SPEECH_LANGUAGES = [
  ["", "Tự nhận diện · chậm hơn"], ["en", "English"], ["vi", "Tiếng Việt"], ["zh", "中文"],
  ["ja", "日本語"], ["ko", "한국어"], ["es", "Español"], ["th", "ไทย"], ["id", "Bahasa Indonesia"],
] as const;

export function VideoSourceInput() {
  const projectId = useAutomation(s => s.currentProjectId);
  const videos = useVideoAnalysis(s => s.videos);
  const uploading = useVideoAnalysis(s => s.uploading);
  const error = useVideoAnalysis(s => s.error);
  const upload = useVideoAnalysis(s => s.upload);
  const openVideo = useVideoAnalysis(s => s.open);
  const [preset, setPreset] = useState<(typeof STYLE_PRESETS)[number]["key"]>("donghua_premium");
  const [autoFilm, setAutoFilm] = useState(true);
  const [reviewMasters, setReviewMasters] = useState(true);
  const [filmRatio, setFilmRatio] = useState("1:1");
  const [filmResolution, setFilmResolution] = useState("480p");
  const [filmKyc, setFilmKyc] = useState(true);
  const [filmPeople, setFilmPeople] = useState(true);
  const [castingRequest, setCastingRequest] = useState("");
  const [environmentRequest, setEnvironmentRequest] = useState("");
  const [reskin, setReskin] = useState(true);
  const [fullTakes, setFullTakes] = useState(true);
  const [fastMotion, setFastMotion] = useState(false);
  const [parallelCatalog, setParallelCatalog] = useState(false);
  const [longerClips, setLongerClips] = useState(false);
  const [reskinSeconds, setReskinSeconds] = useState(15);
  const [openingMinutes, setOpeningMinutes] = useState("");
  const opening = Number(openingMinutes.replace(",", "."));
  const generateSeconds = openingMinutes.trim() && Number.isFinite(opening) && opening > 0 ? Math.max(10, Math.round(opening * 60)) : undefined;
  const [deep, setDeep] = useState(false);
  const [speechLanguage, setSpeechLanguage] = useState("");
  const [fastAnalysis, setFastAnalysis] = useState(false);
  const [fastAvailable, setFastAvailable] = useState(false);
  const [onePass, setOnePass] = useState(true);
  const [onePassAvailable, setOnePassAvailable] = useState(false);
  const [onePassProduction, setOnePassProduction] = useState(false);
  const [capabilitiesReady, setCapabilitiesReady] = useState(false);
  useEffect(() => {
    let mounted = true;
    void api<{ analysis_modes?: string[]; one_pass_auto_production?: boolean }>("/api/automation/videos/capabilities")
      .then(result => { if (mounted) {
        setFastAvailable(result.analysis_modes?.includes("fast") === true);
        setOnePassAvailable(result.analysis_modes?.includes("one_pass") === true);
        setOnePassProduction(result.one_pass_auto_production === true);
      } })
      .catch(() => { if (mounted) setFastAvailable(false); })
      .finally(() => { if (mounted) setCapabilitiesReady(true); });
    return () => { mounted = false; };
  }, []);

  const mode = onePass && onePassAvailable ? "one_pass" : fastAnalysis && fastAvailable ? "fast" : "standard";
  const waitingForServer = !capabilitiesReady || (autoFilm && mode === "one_pass" && !onePassProduction);
  const uploadDisabled = uploading !== null || waitingForServer;
  const activeCount = videos.filter(v => RUNNING.includes(v.status) || v.auto_production?.run_status === "running").length;

  function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const text = STYLE_PRESETS.find(p => p.key === preset)?.text ?? DEFAULT_RULES.visual_style;
    void upload(file, projectId, { ...DEFAULT_RULES, visual_style: text }, deep ? "deep" : "standard",
      autoFilm ? { pipeline: reskin ? 'reskin_v1' : 'cinematic', style: preset, aspect_ratio: filmRatio, resolution: filmResolution,
        clip_seconds: reskin ? reskinSeconds : useAutomation.getState().clipSeconds, kyc: reskin || filmKyc, unmoderated: filmPeople,
        environment_request: environmentRequest.trim(),
        review_masters: reviewMasters, casting_request: castingRequest.trim(), timing_policy: fullTakes ? "full_take" : "source_duration",
        ...(reskin ? { motion_profile: fastMotion ? "fast" as const : "standard" as const } : {}),
        ...(generateSeconds ? { generate_seconds: generateSeconds } : {}),
        ...(parallelCatalog ? { catalog_parallel: 4 } : {}),
        ...(reskin && (longerClips || reskinSeconds > 15) ? { clip_max_shots: reskinSeconds > 15 ? 12 : 8 } : {}) } : undefined,
      mode, speechLanguage).catch(() => undefined);
  }

  return <div className="source-input nodrag nopan">
    <p className="source-input__intro">Đưa video gốc vào, tạo bộ phim theo style của bạn.</p>

    <label className="source-input__field">
      <span>Style phim</span>
      <select value={preset} onChange={event => setPreset(event.target.value as typeof preset)}>
        {STYLE_PRESETS.map(p => <option key={p.key} value={p.key}>{p.label}</option>)}
      </select>
    </label>

    <fieldset className="source-input__mode">
      <legend>Kết quả bạn muốn</legend>
      <div>
        <label className={autoFilm ? "is-selected" : ""}>
          <input type="radio" checked={autoFilm} onChange={() => setAutoFilm(true)} name="source-output" />
          <span><strong>Phim hoàn chỉnh</strong><small>Tạo hình → clip → ghép phim</small></span>
        </label>
        <label className={!autoFilm ? "is-selected" : ""}>
          <input type="radio" checked={!autoFilm} onChange={() => setAutoFilm(false)} name="source-output" />
          <span><strong>Chỉ shotlist</strong><small>Phân tích, chưa gen ảnh/video</small></span>
        </label>
      </div>
    </fieldset>

    {autoFilm && <div className="source-input__grid">
      <label className="source-input__field"><span>Khung phim</span>
        <select value={filmRatio} onChange={event => setFilmRatio(event.target.value)}>
          <option value="1:1">1:1 · Vuông</option><option value="16:9">16:9 · Ngang</option><option value="9:16">9:16 · Dọc</option>
        </select>
      </label>
      <label className="source-input__field"><span>Độ phân giải video</span>
        <select value={filmResolution} onChange={event => setFilmResolution(event.target.value)}>
          <option>480p</option><option>720p</option>{!reskin && <option>1080p</option>}
        </select>
      </label>
    </div>}

    {autoFilm && <label className="source-input__field"><span>Chỉ làm đoạn đầu (phút)</span>
      <input type="number" min={0.5} step={0.5} inputMode="decimal" value={openingMinutes} placeholder="Cả phim"
        onChange={event => setOpeningMinutes(event.target.value)} aria-label="Chỉ làm đoạn đầu, tính bằng phút" />
    </label>}
    {autoFilm && generateSeconds && <p className="source-input__hint">Chỉ phân tích và gen {Math.round(generateSeconds / 6) / 10} phút đầu (đọc thêm 30 giây để khép cảnh cuối). Phần còn lại không tốn lượt đọc.</p>}
    {autoFilm && <SourceToggle label="Chốt tạo hình chính trước" checked={reviewMasters} onChange={setReviewMasters}
      hint="Dừng sau shotlist để bạn upload hoặc gen sheet chính; chỉ tạo diện mạo phụ khi bạn bấm tiếp tục." />}
    {autoFilm && <SourceToggle label="Reskin bám chuyển động gốc" checked={reskin} onChange={v=>{setReskin(v);if(v && filmResolution==='1080p')setFilmResolution('720p')}}
      hint="Phương án đã chốt: motion gốc + sheet tùy chọn + một ảnh dàn cảnh rộng 21:9. Mặc định video vuông, tối đa 15 giây mỗi lượt." />}
    <div className="source-input__options">
      {autoFilm && <details className="source-input__section">
        <summary><span>Tạo hình tùy chỉnh</span><small>{castingRequest.trim() ? "Đã thiết lập" : "Tùy chọn"}</small></summary>
        <div className="source-input__section-body">
          <label className="source-input__field"><span>Mô tả thay đổi nhân vật</span>
            <textarea className="auto-textarea nowheel" value={castingRequest} maxLength={2000} rows={3}
              onChange={event => setCastingRequest(event.target.value)}
              placeholder="Ví dụ: nam chính da đen; nữ chính da trắng, tóc vàng, mắt xanh." />
          </label>
          <p className="source-input__hint">Để trống để giữ tạo hình gốc. Thay đổi màu da, tóc, mắt hoặc sắc tộc; giữ tuổi, vóc dáng, kiểu tóc và trang phục.</p>
          <label className="source-input__field"><span>Bối cảnh đích</span>
            <textarea className="auto-textarea nowheel" value={environmentRequest} maxLength={2000} rows={3}
              onChange={event => setEnvironmentRequest(event.target.value)} placeholder="Ví dụ: nội thất cổ trang bằng gỗ sẫm; giữ vị trí cửa và cầu thang." />
          </label>
          <p className="source-input__hint">Sau khi có danh mục, mở Tạo hình chính để upload hoặc sửa prompt riêng cho từng đạo cụ và nhóm quần chúng.</p>
        </div>
      </details>}

      <details className="source-input__section">
        <summary><span>Cài đặt nâng cao</span><small>{mode === "one_pass" ? "Đọc một lượt" : mode === "fast" ? "Phân tích nhanh" : "Tiêu chuẩn"}</small></summary>
        <div className="source-input__section-body">
          <label className="source-input__field"><span>Cách đọc video</span>
            <select value={mode} onChange={event => { setOnePass(event.target.value === "one_pass"); setFastAnalysis(event.target.value === "fast"); }}>
              {onePassAvailable && <option value="one_pass">Đọc một lượt · đề xuất</option>}
              <option value="standard">Phân tích tiêu chuẩn</option>
              {fastAvailable && <option value="fast">Phân tích nhanh · thử nghiệm</option>}
            </select>
          </label>
          <p className="source-input__hint">{mode === "one_pass" ? "Đọc hình và nghe thoại cùng lượt; chỉ xử lý lại nhóm shot có mâu thuẫn." : mode === "fast" ? "Đọc shot và danh mục cùng lượt, sau đó đối chiếu video độc lập." : "Tách các bước phân tích và đối chiếu video nguồn."}</p>
          <label className="source-input__field"><span>Ngôn ngữ thoại</span>
            <select value={speechLanguage} onChange={event => setSpeechLanguage(event.target.value)}>
              {SPEECH_LANGUAGES.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
            </select>
          </label>
          <p className="source-input__hint">Chọn đúng ngôn ngữ để bỏ bước nhận diện ngôn ngữ. Phim nói nhiều thứ tiếng thì để tự nhận diện.</p>
          <SourceToggle label="Phân tích shot kỹ" hint="Nhiều keyframe hơn; chậm và tốn hơn." checked={deep} onChange={setDeep} />
          {autoFilm && <>
            {!reskin && <SourceToggle label="KYC" checked={filmKyc} onChange={setFilmKyc} />}
            <SourceToggle label="Người thật / B2B" checked={filmPeople} onChange={setFilmPeople} />
            <SourceToggle label="Giữ trọn thoại khi ghép" hint="Ghép nguyên đoạn đã gen; phim có thể dài hơn nguồn." checked={fullTakes} onChange={setFullTakes} />
            <SourceToggle label="Lập hồ sơ nhân vật song song · phim dài" checked={parallelCatalog} onChange={setParallelCatalog}
              hint="Các đoạn truyện độc lập đọc 4 đoạn cùng lúc; nhân vật chỉ gộp khi khuôn mặt hoặc giọng khớp, trường hợp mơ hồ được ghi chú để duyệt." />
            {reskin && <label className="source-input__field"><span>Độ dài clip tối đa</span>
              <select value={reskinSeconds} onChange={event => setReskinSeconds(Number(event.target.value))}>
                <option value={15}>15 giây · đã duyệt</option><option value={30}>30 giây · thử nghiệm</option>
              </select>
            </label>}
            {reskin && reskinSeconds > 15 && <p className="source-input__hint">Mỗi clip kết thúc ở một điểm cắt của nguồn, tối đa 30 giây và tối đa 12 shot; không cắt giữa câu hoặc khi đổi trang phục.</p>}
            {reskin && <SourceToggle label="Clip dài hơn · ít lượt gen hơn" checked={longerClips} onChange={setLongerClips}
              hint="Tối đa 8 shot mỗi clip (vẫn ≤15 giây, không cắt giữa câu): khoảng một nửa số lượt gen. Chưa thử nhiều; xem kỹ các shot ngắn." />}
            {reskin && <SourceToggle label="Video chuyển động nhanh · phim dài" checked={fastMotion} onChange={setFastMotion}
              hint="Độ sâu thô hơn, 24 hình/giây: nhanh hơn khoảng 3 lần mỗi clip. Bố cục và chuyển động giữ nguyên; bề mặt video xám mịn hơn." />}
          </>}
        </div>
      </details>

      {preset === "donghua_premium" && <details className="source-input__section">
        <summary><span>Xem mẫu style</span><small>Nhân vật & bối cảnh</small></summary>
        <div className="source-input__samples">
          <figure><img loading="lazy" src="/api/automation/styles/donghua_premium/character" alt="Style nhân vật Donghua" /><figcaption>Nhân vật</figcaption></figure>
          <figure><img loading="lazy" src="/api/automation/styles/donghua_premium/environment" alt="Style bối cảnh điện ảnh" /><figcaption>Bối cảnh</figcaption></figure>
        </div>
      </details>}
    </div>

    <div className="source-input__upload">
      <label className={`source-input__upload-button${uploadDisabled ? " is-disabled" : ""}`}>
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4" strokeLinecap="round" strokeLinejoin="round" /></svg>
        <span>{uploading !== null ? `Đang tải lên · ${Math.round(uploading * 100)}%` : "Chọn video & bắt đầu"}</span>
        <input type="file" aria-label={autoFilm ? "Tải video và sản xuất phim" : "Tải video để phân tích"}
          accept="video/mp4,video/quicktime,video/webm,.mkv,.m4v" disabled={uploadDisabled} onChange={selectFile} />
      </label>
      {uploading !== null && <progress aria-label="Tiến độ tải video" value={uploading} max={1} />}
      <p>{autoFilm ? (reviewMasters ? "Đối chiếu shotlist với nguồn, rồi chờ bạn ở tab Tạo hình chính." : "Đối chiếu nguồn, gen ảnh/video và ghép phim. Mục chưa rõ sẽ dừng để xử lý. Có dùng lượt gen.") : "Tải lên và phân tích video. Dừng ở shotlist."}</p>
      {waitingForServer && <p role="status">{capabilitiesReady ? "Pipeline một lượt chưa sẵn sàng. Chọn cách đọc khác trong Cài đặt nâng cao hoặc khởi động lại máy chủ." : "Đang kiểm tra kết nối máy chủ…"}</p>}
      {error && <p className="auto-error" role="alert">{error}</p>}
    </div>
    <p className="source-input__promise">Giữ góc quay & thoại gốc <span>·</span> Không nhạc nền</p>

    {videos.length > 0 && <details className="source-input__section source-input__history">
      <summary><span>Video đã tải <b>{videos.length}</b></span><small>{activeCount ? `${activeCount} đang xử lý` : "Mở lại"}</small></summary>
      <ul className="va-list nowheel">
        {videos.map(v => {
          const busy = RUNNING.includes(v.status) || v.auto_production?.run_status === "running";
          const pct = v.progress.total ? ((v.progress.done ?? 0) / v.progress.total) * 100 : 0;
          return <li key={v.id}><button type="button" className="va-list__item" onClick={() => void openVideo(v.id)}>
            <span className="va-list__name">{v.name}</span>
            <span className="va-list__meta">{v.auto_production ? autoFilmLabel(v) : busy
              ? `${STAGE_LABELS[v.progress.stage ?? ""] ?? "đang chờ"}${v.progress.total ? ` ${v.progress.done}/${v.progress.total}` : ""}`
              : v.status === "failed" ? "Lỗi — mở để xem" : v.status === "interrupted" ? "Bị ngắt — mở để chạy tiếp"
              : `${v.shot_count} shot${v.duration ? ` · ${v.duration.toFixed(0)}s` : ""}${v.detail === "deep" ? " · kỹ" : ""}${v.status === "adapted" ? " · đã chuyển thể" : ""}`}</span>
            {busy && <span className="va-progress" aria-hidden><span style={{ width: `${pct}%` }} /></span>}
          </button></li>;
        })}
      </ul>
    </details>}
  </div>;
}

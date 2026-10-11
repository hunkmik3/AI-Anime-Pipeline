import { STYLE_PRESETS } from "../store/videoAnalysis";
import { studioApi } from "./api";
import type { StationProps } from "./StudioPage";

const yes = (v?: boolean) => (v ? "Có" : "Không");

export function SourceStation({ film, act, busy }: StationProps) {
  const s = film.settings;
  const rows: [string, string][] = [
    ["Phong cách", STYLE_PRESETS.find(p => p.key === s.style)?.label ?? String(s.style ?? "")],
    ["Độ phân giải · khung", `${s.resolution} · ${s.aspect_ratio}`],
    ["Clip dài tối đa", `${s.clip_seconds ?? 15} s`],
    ["Chuyển động", s.motion_profile === "fast" ? "Nhanh" : "Chuẩn"],
    ["Phạm vi", s.generate_seconds ? `${Math.round(s.generate_seconds / 60)} phút đầu` : "Cả phim"],
    ["Khi ghép", s.timing_policy === "source_duration" ? "Cắt đúng thời lượng gốc" : "Giữ trọn bản gen"],
    ["Đổi vai", s.casting_request || "—"],
    ["Đổi bối cảnh", s.environment_request || "—"],
    ["Dừng sau shotlist", yes(s.pause_for_review)],
    ["Duyệt thẻ chính", yes(s.review_masters)],
    ["Chuẩn bị rồi mới render", yes(s.prepare_before_render)],
  ];
  const stopped = film.status === "failed" || film.status === "interrupted" || film.stage === "blocked";
  const waiting = film.stage === "awaiting_review";
  const p = film.progress;
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
    </section>
    <section className="st-card">
      <h2>Thiết lập</h2>
      <dl className="st-kv">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
    </section>
    <section className="st-card st-source__video">
      <h2>Video gốc</h2>
      <video controls preload="metadata" src={studioApi.sourceUrl(film.id)} />
    </section>
  </div>;
}

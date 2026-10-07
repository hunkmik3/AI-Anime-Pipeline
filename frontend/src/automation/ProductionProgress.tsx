import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api/client";
import { useAutomation } from "../store/automation";
import { useVideoAnalysis } from "../store/videoAnalysis";
import { parseServerTimeMs } from "../utils/serverTime";

type Stage = { key: string; label: string; done: number; total: number | null; percent: number | null;
  status: string; unit: string; failed?: number; error?: string; note?: string };
type Report = { project_id: string; run_id: string | null; status: string; mode: string | null;
  stages: Stage[]; completed_stages: number; total_stages: number; active_jobs: number; error: string; updated_at: string;
  jobs: {id:string;kind:string;name:string;slot:string;status:string;error:string;created_at:string}[] };
const labels: Record<string,string> = {waiting:"Chưa chạy",queued:"Đang xếp hàng",running:"Đang chạy",preparing:"Đang chuẩn bị",submitting:"Đang gửi",
  succeeded:"Hoàn tất",failed:"Có lỗi",blocked:"Cần xử lý",unknown:"Cần đối soát",paused:"Đã tạm dừng",waiting_user:"Chờ bạn chốt",skipped:"Không cần chạy",cancelled:"Đã hủy"};
const kinds: Record<string,string> = {plate:"Tạo ảnh",clip:"Gen clip",write:"Viết prompt",raccord:"Liên tục cảnh",source:"Đọc nguồn",assemble:"Ghép phim",atlas:"Ghép reference",ingest:"Chuẩn bị reference",extract_frame:"Trích ảnh nối cảnh"};

export function ProductionProgress({ jobError = "" }: { jobError?: string }) {
  const pid = useAutomation(s => s.currentProjectId);
  const uploading = useVideoAnalysis(s => s.uploading);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);
  const root = useRef<HTMLElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const popupId = useId();
  function close() { setExpanded(false); toggle.current?.focus(); }
  useEffect(() => {
    if (!expanded) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setExpanded(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); close(); } };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [expanded]);
  useEffect(() => {
    setReport(null); setError(""); setExpanded(false);
    if (!pid) return;
    let alive = true, loading = false;
    const controller = new AbortController();
    const refresh = async () => {
      if (loading) return;
      loading = true;
      try {
        const result = await api<Report>(`/api/automation/projects/${pid}/progress`, { signal: controller.signal });
        if (alive) { setReport(result); setError(""); }
      } catch (e) { if (alive) setError((e as Error).message); }
      finally { loading = false; }
    };
    void refresh(); const timer = setInterval(() => void refresh(), 4000);
    return () => { alive = false; controller.abort(); clearInterval(timer); };
  }, [pid]);
  const stages = report?.stages ?? [];
  const problem = stages.find(s => ["failed","blocked","unknown"].includes(s.status));
  const current = problem || stages.find(s => ["running","queued"].includes(s.status)) || stages.find(s => s.status === "waiting_user");
  const hasJobProblem = report?.jobs.some(j => ["failed","unknown"].includes(j.status));
  const busy = !!report?.active_jobs || report?.status === "running" || uploading !== null;
  const warning = error || jobError || report?.error || "";
  const attention = !!(problem || hasJobProblem || warning);
  const percent = uploading !== null ? Math.floor(uploading * 100) : current?.percent;
  const headline = error || jobError ? "Chưa cập nhật được tiến độ" : uploading !== null ? "Đang tải video" :
    current ? current.label : warning || hasJobProblem ? "Có tác vụ cần xử lý" :
      report?.status === "succeeded" ? (report.mode === "prepare" ? "Material & prompt đã xong" : "Lượt sản xuất đã xong") :
        report?.active_jobs ? `${report.active_jobs} tác vụ đang chạy` : stages.length ? `${report?.completed_stages}/${report?.total_stages} khâu đã xong` :
          pid && !report ? "Đang đọc tiến độ…" : "Chưa có lượt sản xuất";
  return <section ref={root} className="production-progress" aria-label="Tiến độ sản xuất">
    <button ref={toggle} type="button" className="production-progress__toggle" aria-expanded={expanded}
      aria-label={`Tiến độ: ${headline}${percent != null ? ` · ${percent}%` : ""}. Xem chi tiết`}
      aria-haspopup="dialog" aria-controls={expanded ? popupId : undefined} onClick={() => setExpanded(!expanded)}>
      <span className={`production-progress__dot ${attention ? "is-error" : busy ? "is-busy" : current?.status === "waiting_user" ? "is-waiting" : ""}`} />
      <strong>Tiến độ</strong><span className="production-progress__headline" title={headline}>{headline}</span>
      {(current || uploading !== null) && <span className="production-progress__meter">
        <progress max={100} value={percent ?? (busy ? undefined : 0)} aria-label={headline}
          aria-valuetext={percent == null ? "Chưa có phần trăm chi tiết" : `${percent}%`} />
        <b>{percent != null ? `${percent}%` : labels[current?.status ?? "running"] || "Đang xử lý"}</b>
      </span>}
      {report && stages.length > 0 && <small>{report.completed_stages}/{report.total_stages} khâu</small>}
      <span className="production-progress__detail-label">Chi tiết {expanded ? "⌃" : "⌄"}</span>
    </button>
    {expanded && <div id={popupId} role="dialog" aria-label="Chi tiết tiến độ" className="production-progress__popover">
      <header className="production-progress__popup-head"><div><strong>Tiến độ sản xuất</strong>
        <small>{report?.active_jobs ? `${report.active_jobs} tác vụ đang xử lý` : current?.status === "waiting_user" ? "Chờ bạn chốt tạo hình chính" : "Tự cập nhật mỗi 4 giây"}</small></div>
        <button type="button" onClick={close} aria-label="Đóng chi tiết tiến độ" autoFocus>×</button>
      </header>
      <div className="production-progress__body">
      {(error || jobError) && <p className="production-progress__error" role="alert">{error || jobError}. {report ? "Đang giữ số liệu của lần đọc trước." : ""}</p>}
      {uploading !== null && <div className="production-step is-running"><header><b>Tải video lên server</b><strong>{Math.floor(uploading*100)}%</strong></header><progress max={100} value={uploading*100} aria-label="Tải video lên server" /><p>Tính theo dung lượng đã tải lên.</p></div>}
      {report?.error && <p role="alert" className="production-progress__error">{report.error}</p>}
      {!stages.length && uploading === null && !report?.jobs.length && <p className="production-progress__empty">Chọn video nguồn hoặc bắt đầu một lượt sản xuất. Tiến độ từng khâu sẽ xuất hiện tại đây.</p>}
      <div className="production-progress__grid">
        {stages.map((s, index) => <article key={s.key} className={`production-step is-${s.status}`}>
          <header><span className="production-step__number">{String(index+1).padStart(2,"0")}</span><b>{s.label}</b><strong>{s.percent !== null && s.status !== "skipped" ? `${s.percent}%` : "—"}</strong></header>
          <progress max={100} value={s.percent ?? (["running","queued"].includes(s.status) ? undefined : 0)} aria-label={s.label} aria-valuetext={s.percent === null ? (labels[s.status] || s.status) : `${s.percent}%`} />
          <p><span>{labels[s.status] || s.status}</span><span>{s.total !== null ? `${s.done}/${s.total} ${s.unit}` : "Chưa có tổng số"}{s.failed ? ` · ${s.failed} lỗi / cần đối soát` : ""}</span></p>
          {s.error && <p className="production-progress__error">{s.error}</p>}
          {s.note && <small>{s.note}</small>}
        </article>)}
      </div>
      {!!report?.jobs.length && <details className="production-progress__jobs">
        <summary>Tác vụ chi tiết ({report.jobs.length})</summary>
        {report.jobs.map(j => <div key={j.id} className={`production-job is-${j.status}`}>
          <div><b>{j.kind === "source" ? "Phân tích video nguồn" : `${kinds[j.kind] || "Tác vụ"} · ${j.name}`}</b><span>{labels[j.status] || j.status}</span></div>
          {j.error ? <p role="alert">{j.error}</p> : ["queued","preparing","submitting","running"].includes(j.status) ? <progress aria-label={`${kinds[j.kind] || "Tác vụ"} ${j.name}`} /> : null}
        </div>)}
      </details>}
      <footer><span>% tính theo số mục hoàn tất, gồm mục dùng lại. Đây không phải % thời gian. Tác vụ chưa có số liệu chi tiết sẽ hiện thanh đang xử lý.</span>
        {report && <time dateTime={report.updated_at}>Cập nhật {new Date(parseServerTimeMs(report.updated_at)).toLocaleTimeString("vi-VN")}</time>}</footer>
      </div>
    </div>}
  </section>;
}

/**
 * Review surface for one reference video (guide §28, §82-83).
 *
 * Player + cut timeline on the left, the shotlist on the right with source and
 * adaptation side by side — the two are stored apart and shown apart, so a
 * reviewer can always see what the reference did next to what it became.
 * Names are edited in one table and locked before re-adapting; nothing here
 * re-watches the video.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { useAutomation } from "../store/automation";
import { SOURCE_ISSUE_LABELS, SourceIssueSummary } from "./SourceIssueSummary";
import {
  DEFAULT_RULES,
  IMAGE_MODEL_LABELS,
  RUNNING,
  STAGE_LABELS,
  STYLE_PRESETS,
  frameUrl,
  useVideoAnalysis,
  type AnalysedShot,
  type CastCharacter,
  type CastEnvironment,
  type Glossary,
  type Rules,
} from "../store/videoAnalysis";

const tc = (s: number) => {
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${(s - m * 60).toFixed(2).padStart(5, "0")}`;
};

const GLOSSARY_KINDS: { key: keyof Glossary; label: string }[] = [
  { key: "characters", label: "Nhân vật" },
  { key: "sects", label: "Môn phái" },
  { key: "locations", label: "Địa danh" },
  { key: "techniques", label: "Chiêu thức" },
];

type Tab = "shots" | "cast" | "places" | "assets" | "source" | "names" | "rules" | "qa";

export function VideoAnalysisPanel() {
  const detail = useVideoAnalysis((s) => s.detail);
  const openId = useVideoAnalysis((s) => s.openId);
  const open = useVideoAnalysis((s) => s.open);
  const adapt = useVideoAnalysis((s) => s.adapt);
  const resume = useVideoAnalysis((s) => s.resume);
  const verify = useVideoAnalysis((s) => s.verify);
  const refine = useVideoAnalysis((s) => s.refine);
  const refiningId = useVideoAnalysis((s) => s.refiningId);
  const acceptVerification = useVideoAnalysis((s) => s.acceptVerification);
  const saveGlossary = useVideoAnalysis((s) => s.saveGlossary);
  const readCast = useVideoAnalysis((s) => s.readCast);
  const generatePlate = useVideoAnalysis((s) => s.generatePlate);
  const generating = useVideoAnalysis((s) => s.generating);
  const capabilities = useAutomation((s) => s.capabilities);
  const imageModel = useAutomation((s) => s.imageModel);
  const setImageModel = useAutomation((s) => s.setImageModel);
  const imageSize = useAutomation((s) => s.imageSize);
  const setImageSize = useAutomation((s) => s.setImageSize);
  const storeError = useVideoAnalysis((s) => s.error);
  const importReferenceBoard = useAutomation((s) => s.importReferenceBoard);
  const adoptSourceVerification = useAutomation((s) => s.adoptSourceVerification);
  const createProject = useAutomation((s) => s.createProject);
  const currentProjectId = useAutomation((s) => s.currentProjectId);
  const boardBusy = useAutomation((s) => s.breakdownStatus === "running");

  const [tab, setTab] = useState<Tab>("shots");
  const [current, setCurrent] = useState(1);
  const [follow, setFollow] = useState(true);
  const [rules, setRules] = useState<Rules>(DEFAULT_RULES);
  const [glossary, setGlossary] = useState<Glossary | null>(null);
  const [boardError, setBoardError] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const rowRefs = useRef(new Map<number, HTMLElement>());

  const analysis = detail?.analysis ?? {};
  const adaptation = detail?.adaptation ?? {};
  const cast = detail?.cast ?? {};
  const characters = cast.characters ?? [];
  const environments = cast.environments ?? [];
  const assets = cast.assets ?? [...(cast.props ?? []), ...(cast.background_groups ?? [])];
  const sourceVerification = analysis.source_verification;
  const shots = analysis.shots ?? [];
  const sequences = analysis.sequences ?? [];
  const duration = analysis.video?.duration ?? 0;
  const jobRunning = detail ? RUNNING.includes(detail.status) : false;
  const refining = !!openId && (refiningId === openId || jobRunning &&
    ["source_layers", "source_identity", "source_refine", "source_protocol_review", "source_context"].includes(detail?.progress.stage ?? ""));
  const running = refining || jobRunning;
  const qa = adaptation.validation ?? analysis.validation;

  // Rules and glossary are drafts until sent; reseed them when the server's copy changes.
  useEffect(() => {
    if (adaptation.rules) setRules({ ...DEFAULT_RULES, ...adaptation.rules });
  }, [detail?.id, detail?.adaptation_version]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setGlossary(adaptation.glossary ? structuredClone(adaptation.glossary) : null);
  }, [detail?.id, detail?.adaptation_version, JSON.stringify(adaptation.glossary)]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && void open(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const seqOf = useMemo(() => {
    const map = new Map<number, number>();
    sequences.forEach((q, i) => {
      for (let n = q.first_shot; n <= q.last_shot; n++) map.set(n, i);
    });
    return map;
  }, [sequences]);

  if (!openId) return null;

  const seek = (shot: AnalysedShot) => {
    setCurrent(shot.shot);
    const v = videoRef.current;
    if (v) {
      v.currentTime = shot.start + 0.01;
      void v.play().catch(() => undefined);
    }
  };

  const onTime = () => {
    const t = videoRef.current?.currentTime ?? 0;
    const hit = shots.find((s) => t >= s.start && t < s.end);
    if (hit && hit.shot !== current) {
      setCurrent(hit.shot);
      if (follow && tab === "shots") {
        rowRefs.current.get(hit.shot)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    }
  };

  const progress = detail?.progress ?? {};
  const pct = progress.total ? Math.round(((progress.done ?? 0) / progress.total) * 100) : 0;
  const glossaryDirty = JSON.stringify(glossary) !== JSON.stringify(adaptation.glossary ?? null);
  const adaptedCount = Object.keys(adaptation.shots ?? {}).length;

  return (
    <div className="va-overlay" role="dialog" aria-modal="true" aria-label="Phân tích video mẫu">
      <div className="va-panel">
        <header className="va-head">
          <div className="va-head__id">
            <span className="auto-bar__eyebrow">Video mẫu</span>
            <h2 className="va-head__title">{detail?.name ?? "…"}</h2>
            {analysis.video && (
              <p className="va-head__meta">
                {shots.length} shot · {tc(duration)} · {analysis.video.aspect_ratio} · {analysis.video.fps.toFixed(0)} fps
                {adaptedCount > 0 && ` · đã chuyển thể ${adaptedCount}/${shots.length}`}
              </p>
            )}
          </div>

          {detail && (
            <div className={`va-status va-status--${detail.status}`}>
              <span className="va-status__label">
                {running
                  ? `${STAGE_LABELS[progress.stage ?? ""] ?? "đang chờ"}${progress.total ? ` ${progress.done}/${progress.total}` : ""}`
                  : STATUS_LABELS[detail.status]}
              </span>
              {running && (
                <span className="va-progress" aria-hidden>
                  <span style={{ width: `${pct}%` }} />
                </span>
              )}
            </div>
          )}

          <div className="va-head__actions">
            {(detail?.status === "interrupted" || (detail?.status === "failed" && !shots.length)) && (
              <button type="button" className="auto-btn" onClick={() => void resume()}>
                Chạy tiếp phân tích
              </button>
            )}
            <a
              className={`auto-btn${shots.length ? "" : " va-disabled"}`}
              href={`/api/automation/videos/${openId}/export?format=md`}
              aria-disabled={!shots.length}
            >
              Tải .md
            </a>
            <a
              className={`auto-btn${shots.length ? "" : " va-disabled"}`}
              href={`/api/automation/videos/${openId}/export?format=json`}
              aria-disabled={!shots.length}
            >
              Tải .json
            </a>
            <button
              type="button"
              className="auto-btn auto-btn--primary"
              disabled={running || !adaptedCount || boardBusy}
              title="Tạo board mới từ shotlist, nhân vật, đám đông, đạo cụ và bối cảnh đã phân tích."
              onClick={async () => {
                setBoardError(null);
                try {
                  // A new project, not the open one: a reference video is a
                  // production of its own, and overwriting whatever board the
                  // user had open is a loss they cannot undo.
                  await createProject(detail?.name?.trim() || "Video mẫu");
                  await importReferenceBoard(openId);
                  void open(null);
                } catch (err) {
                  setBoardError((err as Error).message);
                }
              }}
            >
              {boardBusy ? "đang dựng board…" : "Tạo project mới từ video"}
            </button>
            <button
              type="button"
              className="auto-btn"
              disabled={running || !adaptedCount || boardBusy || !currentProjectId}
              title="Ghi đè board đang mở bằng board dựng từ video này."
              onClick={async () => {
                if (!confirm("Thay board đang mở bằng board dựng từ video này?")) return;
                setBoardError(null);
                try {
                  await importReferenceBoard(openId);
                  void open(null);
                } catch (err) {
                  setBoardError((err as Error).message);
                }
              }}
            >
              Thay board đang mở
            </button>
            <button type="button" className="auto-btn" onClick={() => void open(null)} aria-label="Đóng">
              Đóng
            </button>
          </div>
        </header>

        {(detail?.error || storeError || boardError) && (
          <p className="auto-banner auto-banner--stop">{boardError || detail?.error || storeError}</p>
        )}

        <div className="va-body">
          <aside className="va-side">
            <video
              ref={videoRef}
              className={`va-player${analysis.video && analysis.video.height > analysis.video.width ? " va-player--tall" : ""}`}
              src={`/api/automation/videos/${openId}/source`}
              controls
              preload="metadata"
              onTimeUpdate={onTime}
            />
            {duration > 0 && (
              <div className="va-timeline" aria-label="Điểm cắt">
                {shots.map((s) => (
                  <button
                    key={s.shot}
                    type="button"
                    className={`va-timeline__shot va-seq-${(seqOf.get(s.shot) ?? 0) % 6}${s.shot === current ? " va-timeline__shot--on" : ""}`}
                    style={{ left: `${(s.start / duration) * 100}%`, width: `${((s.end - s.start) / duration) * 100}%` }}
                    title={`#${String(s.shot).padStart(3, "0")} ${tc(s.start)}–${tc(s.end)}`}
                    onClick={() => seek(s)}
                  />
                ))}
              </div>
            )}
            <label className="auto-check va-follow">
              <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
              <span>Bảng chạy theo video</span>
            </label>
            {analysis.speech_error && <p className="auto-hint auto-hint--warn">Không nghe được thoại: {analysis.speech_error}</p>}
            {analysis.cuts && (
              <p className="auto-hint">
                {analysis.cuts.kept.length} điểm cắt từ {analysis.cuts.candidates} ứng viên — bỏ{" "}
                {analysis.cuts.rejected.length} cú lia/chớp sáng không phải cắt.
              </p>
            )}
          </aside>

          <section className="va-main">
            <nav className="auto-tabs va-tabs" role="tablist">
              {(
                [
                  ["shots", `Shotlist (${shots.length})`],
                  ["cast", `Nhân vật${characters.length ? ` (${characters.length})` : ""}`],
                  ["places", `Bối cảnh${environments.length ? ` (${environments.length})` : ""}`],
                  ["assets", `Đạo cụ & đám đông (${assets.length})`],
                  ["source", `Agent 1 · ${sourceVerification?.status === "verified" ? "đã đối chiếu hình ảnh" : "cần đối chiếu hình ảnh"}`],
                  ["names", "Tên riêng"],
                  ["rules", "Quy tắc chuyển thể"],
                  ["qa", `Kiểm tra${qa ? ` (${qa.errors + qa.warnings})` : ""}`],
                ] as [Tab, string][]
              ).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={tab === key}
                  className={`auto-tab${tab === key ? " auto-tab--on" : ""}`}
                  onClick={() => setTab(key)}
                >
                  {label}
                </button>
              ))}
            </nav>

            {tab === "shots" && (
              <div className="va-scroll">
                {!shots.length && (
                  <p className="auto-hint va-empty">
                    {running ? "Đang phân tích — shotlist hiện ra khi xem xong từng shot." : "Chưa có shot nào."}
                  </p>
                )}
                {sequences.map((q, qi) => (
                  <section key={qi} className="va-seq">
                    <h3 className={`va-seq__title va-seq-${qi % 6}`}>
                      <span>SEQ {String(qi + 1).padStart(2, "0")}</span> {q.title}
                      <small>
                        shot {String(q.first_shot).padStart(3, "0")}–{String(q.last_shot).padStart(3, "0")}
                      </small>
                    </h3>
                    {q.goal && <p className="auto-hint">{q.goal}</p>}
                    {shots
                      .filter((s) => s.shot >= q.first_shot && s.shot <= q.last_shot)
                      .map((s) => (
                        <ShotRow
                          key={s.shot}
                          videoId={openId}
                          shot={s}
                          fps={analysis.video?.fps ?? 0}
                          adapted={adaptation.shots?.[String(s.shot)]}
                          on={s.shot === current}
                          onSeek={() => seek(s)}
                          rowRef={(el) => {
                            if (el) rowRefs.current.set(s.shot, el);
                            else rowRefs.current.delete(s.shot);
                          }}
                        />
                      ))}
                  </section>
                ))}
              </div>
            )}

            {tab === "source" && (
              <div className="va-scroll">
                <h3>Đối chiếu hình ảnh video gốc</h3>
                <p className="auto-hint">Kiểm tra shotlist, nhân vật chính/phụ, đám đông và đạo cụ bằng bằng chứng từ video mẫu.</p>
                <p className="auto-hint">Kết quả chỉ áp dụng cho hình ảnh trong các khung hình đã lấy mẫu và xem lại chi tiết.</p>
                <button type="button" className="auto-btn" disabled={running || !shots.length} onClick={() => void verify()}>
                  {running ? "đang xử lý…" : "Đối chiếu lại video gốc"}
                </button>
                {" "}<button type="button" className="auto-btn auto-btn--primary" disabled={running || !shots.length}
                  title="Sửa mô tả và danh sách xuất hiện theo video gốc, rồi kiểm tra lại từng shot."
                  onClick={() => void refine()}>
                  {refining ? "đang sửa & đối chiếu…" : "Sửa & đối chiếu"}
                </button>
                {refining && <p className="auto-hint">Đang giữ kết quả hiện tại để bạn xem. Bản sửa sẽ hiện khi xử lý xong.</p>}
                <p className="va-qa-summary">{sourceVerification?.status === "verified" ? "Hình ảnh: đã đối chiếu" : sourceVerification?.status === "needs_review" ? "Hình ảnh: còn mục cần xem lại" : "Hình ảnh: chưa xác minh"}
                  {` · ${sourceVerification?.reviewed_shots?.length ?? 0}/${shots.length} shot đã xem`}</p>
                {sourceVerification?.issue_summary && <SourceIssueSummary report={sourceVerification} />}
                <p className="auto-hint">Âm thanh và lời thoại: chưa được Agent 1 kiểm chứng độc lập. Chuyển động liên tục giữa các khung hình lấy mẫu cũng chưa được xác minh đầy đủ.</p>
                {!!sourceVerification?.unresolved_shots?.length && <p>Shot chưa giải quyết: {sourceVerification.unresolved_shots.join(", ")}</p>}
                {sourceVerification?.review && <p className="auto-hint">
                  Đã duyệt bởi {sourceVerification.review.accepted_by} lúc {new Date(sourceVerification.review.accepted_at).toLocaleString()}
                  {sourceVerification.review.accepted_shots?.length ? ` · chấp nhận thủ công shot ${sourceVerification.review.accepted_shots.join(", ")}` : ""}
                  {sourceVerification.review.note ? ` · “${sourceVerification.review.note}”` : ""}</p>}
                {sourceVerification && sourceVerification.status !== "verified" && (
                  <button type="button" className="auto-btn" disabled={running} title={
                    "Agent 1 không tự khẳng định được các mục bên dưới. Nếu bạn đã xem và thấy chấp nhận được, "
                    + "bấm để chấp nhận các mục hình ảnh còn cần xem lại. Ghi chú và giới hạn kiểm chứng vẫn được giữ lại; âm thanh không được xác nhận qua thao tác này."}
                    onClick={() => {
                      const note = window.prompt("Đã xem các mục cần xem lại? Ghi chú (không bắt buộc) rồi bấm OK để chấp nhận:", "");
                      if (note === null) return;
                      // A board already cut from this inventory takes the accepted
                      // report too; otherwise Agent 2 refuses it until a re-import.
                      void acceptVerification(note).then(() =>
                        adoptSourceVerification(useVideoAnalysis.getState().detail?.analysis.source_verification));
                    }}>
                    Tôi đã xem — chấp nhận kết quả đối chiếu
                  </button>
                )}
                {!!sourceVerification?.findings?.length && <h4>Các mục cần xem lại ({sourceVerification.findings.length}){sourceVerification.review ? " (đã chấp nhận thủ công)" : ""}</h4>}
                <ul className="va-findings">
                  {(sourceVerification?.findings ?? []).map((finding, i) => (
                    <li key={i} className="va-finding"><code>{finding.code}</code>
                      {finding.category && <span className="auto-hint">{SOURCE_ISSUE_LABELS[finding.category]}</span>}
                      {finding.shot != null && <button type="button" className="va-link" onClick={() => {
                        const shot = shots.find((s) => s.shot === finding.shot); if (shot) seek(shot);
                      }}>shot {finding.shot}</button>}
                      <span>{finding.message}</span>
                    </li>
                  ))}
                </ul>
                {!!sourceVerification?.scope_notes?.length && <details>
                  <summary>Giới hạn kiểm chứng — thông tin, không chặn ({sourceVerification.scope_notes.length})</summary>
                  <ul className="va-findings">
                    {sourceVerification.scope_notes.map((note, i) => <li key={i} className="va-finding">
                      <code>{note.code}</code>
                      {note.shot != null && <button type="button" className="va-link" onClick={() => {
                        const shot = shots.find((s) => s.shot === note.shot); if (shot) seek(shot);
                      }}>shot {note.shot}</button>}
                      <span>{note.message}</span>
                    </li>)}
                  </ul>
                </details>}
                {sourceVerification?.evidence?.length ? <details><summary>Bằng chứng ({sourceVerification.evidence.length})</summary>
                  {sourceVerification.evidence.map((evidence) => <p key={evidence.id}>
                    <button type="button" className="va-link" onClick={() => { const shot = shots.find((s) => s.shot === evidence.shot); if (shot) seek(shot); }}>
                      Shot {evidence.shot} · {tc(evidence.timestamp_s)}</button>{" "}
                    <a href={frameUrl(openId, evidence.frame)} target="_blank" rel="noreferrer">{evidence.id}</a>
                  </p>)}
                </details> : null}
                <p className="auto-hint">Sau khi đối chiếu lại, tạo hoặc nhập lại board để dùng bản dữ liệu mới.</p>
              </div>
            )}

            {tab === "assets" && (
              <div className="va-scroll">
                <p className="auto-hint">Dùng model ảnh và độ phân giải đang chọn trên thanh công cụ.</p>
                {!assets.length && <p>Chưa có hồ sơ đạo cụ hoặc đám đông. Đối chiếu video gốc rồi lập lại hồ sơ.</p>}
                {assets.map((asset) => <article key={asset.key} className="va-card">
                  <div className="va-card__body"><h4 className="va-card__title">{asset.name}
                    <span className="auto-tag">{asset.kind === "prop" ? "đạo cụ" : "đám đông"}</span></h4>
                    <p>{asset.description || asset.summary}</p>
                    <ShotChips shots={asset.shots ?? []} onSeek={(n) => { const shot = shots.find((s) => s.shot === n); if (shot) seek(shot); }} />
                  </div>
                  <PlateBox plate={asset.plate} busy={generating.includes(`${asset.kind}:${asset.key}`)} label={asset.name}
                    onGenerate={() => void generatePlate(asset.kind, asset.key, { model: imageModel, size: imageSize })} />
                </article>)}
              </div>
            )}

            {(tab === "cast" || tab === "places") && (
              <div className="va-scroll">
                <div className="va-castbar">
                  <label className="auto-select">
                    <span>Model ảnh</span>
                    <select value={imageModel} onChange={(e) => setImageModel(e.target.value)}>
                      {(capabilities?.image_models ?? [imageModel]).map((m) => (
                        <option key={m} value={m}>
                          {IMAGE_MODEL_LABELS[m] ?? m} · tối đa {capabilities?.image_model_max?.[m] ?? "2K"}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="auto-select">
                    <span>Độ phân giải</span>
                    <select value={imageSize} onChange={(e) => setImageSize(e.target.value)}>
                      {(capabilities?.image_sizes ?? ["1K", "2K", "4K"]).map((sz) => (
                        <option key={sz} value={sz}>
                          {sz}
                          {sz > (capabilities?.image_model_max?.[imageModel] ?? "2K") ? " (model chỉ tới 2K)" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="auto-btn"
                    disabled={running || !shots.length}
                    title="Đọc lại hồ sơ từ phân tích. Ảnh đã gen được giữ nguyên."
                    onClick={() => void readCast()}
                  >
                    {characters.length || environments.length ? "Đọc lại hồ sơ" : "Lập hồ sơ nhân vật & bối cảnh"}
                  </button>
                  <button
                    type="button"
                    className="auto-btn auto-btn--primary"
                    disabled={generating.length > 0 || !(tab === "cast" ? characters.length : environments.length)}
                    title="Gen lần lượt những mục chưa có ảnh. Mục đã có ảnh được bỏ qua — bấm 'gen lại' trên từng thẻ nếu muốn làm lại."
                    onClick={async () => {
                      const kind = tab === "cast" ? "character" : "environment";
                      const list = tab === "cast" ? characters : environments;
                      // One at a time: the two engines both rate-limit, and a
                      // failed tenth request is harder to notice than a slow one.
                      for (const entry of list) {
                        if (entry.plate) continue;
                        await generatePlate(kind, entry.key, { model: imageModel, size: imageSize });
                      }
                    }}
                  >
                    {generating.length ? `đang gen ${generating.length}…` : "Gen hết mục còn thiếu"}
                  </button>
                </div>

                {!characters.length && !environments.length && (
                  <p className="auto-hint va-empty">
                    {running
                      ? "Đang đọc hồ sơ…"
                      : "Chưa có hồ sơ. Bấm \"Lập hồ sơ nhân vật & bối cảnh\" — một lượt gọi model, đọc từ thẻ tên, mô tả hình ảnh và bối cảnh của từng shot."}
                  </p>
                )}

                {tab === "cast" &&
                  characters.map((c) => (
                    <CastCard
                      key={c.key}
                      videoId={openId}
                      character={c}
                      busy={generating.includes(`character:${c.key}`)}
                      onSeek={(n) => {
                        const s = shots.find((x) => x.shot === n);
                        if (s) seek(s);
                      }}
                      onGenerate={(stateKey) =>
                        void generatePlate("character", c.key, { model: imageModel, size: imageSize, stateKey })
                      }
                    />
                  ))}

                {tab === "places" &&
                  environments.map((e) => (
                    <PlaceCard
                      key={e.key}
                      videoId={openId}
                      place={e}
                      shotsBySetting={shots}
                      busy={generating.includes(`environment:${e.key}`)}
                      onSeek={(n) => {
                        const s = shots.find((x) => x.shot === n);
                        if (s) seek(s);
                      }}
                      onGenerate={() =>
                        void generatePlate("environment", e.key, { model: imageModel, size: imageSize })
                      }
                    />
                  ))}
              </div>
            )}

            {tab === "names" && (
              <div className="va-scroll">
                {!glossary ? (
                  <p className="auto-hint va-empty">
                    Bảng tên được lập ở bước chuyển thể. Bấm "Chuyển thể" ở tab Quy tắc để tạo.
                  </p>
                ) : (
                  <>
                    <p className="auto-hint">
                      Một tên nguồn — một tên đích, dùng y hệt ở mọi shot. Sửa ở đây rồi khoá lại: lần chuyển thể sau
                      viết lại mọi shot theo đúng bảng này, không xem lại video.
                    </p>
                    {GLOSSARY_KINDS.map(({ key, label }) => {
                      const table = glossary[key] ?? {};
                      const rows = Object.entries(table);
                      if (!rows.length) return null;
                      return (
                        <table key={key} className="va-glossary">
                          <caption>{label}</caption>
                          <thead>
                            <tr>
                              <th>Nguồn</th>
                              <th>Đích</th>
                            </tr>
                          </thead>
                          <tbody>
                            {rows.map(([src, dst]) => (
                              <tr key={src}>
                                <td>{src}</td>
                                <td>
                                  <input
                                    value={dst}
                                    onChange={(e) =>
                                      setGlossary({ ...glossary, [key]: { ...table, [src]: e.target.value } })
                                    }
                                  />
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      );
                    })}
                    <div className="auto-row va-actions">
                      <button
                        type="button"
                        className="auto-btn"
                        disabled={!glossaryDirty || running}
                        onClick={() => glossary && void saveGlossary(glossary)}
                      >
                        Lưu bảng tên
                      </button>
                      <button
                        type="button"
                        className="auto-btn auto-btn--primary"
                        disabled={running || !glossary}
                        title="Viết lại mọi shot theo bảng tên này. Chỉ tốn gọi text, không phân tích lại video."
                        onClick={() => glossary && void adapt(rules, glossary)}
                      >
                        Khoá tên & chuyển thể lại
                      </button>
                    </div>
                  </>
                )}
              </div>
            )}

            {tab === "rules" && (
              <div className="va-scroll va-rules">
                <fieldset className="va-fieldset">
                  <legend>Phong cách đích</legend>
                  <div className="auto-tabs">
                    {STYLE_PRESETS.map((p) => (
                      <button
                        key={p.key}
                        type="button"
                        className={`auto-tab${rules.visual_style === p.text ? " auto-tab--on" : ""}`}
                        onClick={() => setRules({ ...rules, visual_style: p.text })}
                      >
                        {p.label}
                      </button>
                    ))}
                  </div>
                  <textarea
                    className="auto-textarea"
                    rows={3}
                    value={rules.visual_style}
                    onChange={(e) => setRules({ ...rules, visual_style: e.target.value })}
                  />
                </fieldset>
                <fieldset className="va-fieldset">
                  <legend>Quy tắc đặt tên</legend>
                  {(
                    [
                      ["character_names", "Nhân vật"],
                      ["sect_names", "Môn phái"],
                      ["location_names", "Địa danh"],
                      ["technique_names", "Chiêu thức"],
                    ] as [keyof Rules, string][]
                  ).map(([k, label]) => (
                    <label key={k} className="va-line">
                      <span>{label}</span>
                      <input value={String(rules[k])} onChange={(e) => setRules({ ...rules, [k]: e.target.value })} />
                    </label>
                  ))}
                </fieldset>
                <fieldset className="va-fieldset">
                  <legend>Thoại</legend>
                  <label className="va-line">
                    <span>Ngôn ngữ</span>
                    <input
                      value={rules.dialogue_language}
                      onChange={(e) => setRules({ ...rules, dialogue_language: e.target.value })}
                    />
                  </label>
                  <label className="va-line">
                    <span>Cách dịch</span>
                    <select
                      value={rules.dialogue_mode}
                      onChange={(e) => setRules({ ...rules, dialogue_mode: e.target.value as Rules["dialogue_mode"] })}
                    >
                      <option value="literal">Sát nghĩa từng câu</option>
                      <option value="cinematic">Gọn cho điện ảnh (không thêm câu)</option>
                    </select>
                  </label>
                </fieldset>
                <p className="auto-hint">
                  Luôn khoá: thứ tự shot, cỡ cảnh, góc máy, chuyển động máy, blocking, hướng nhìn, động tác, nhịp phản ứng.
                </p>
                <div className="auto-row va-actions">
                  <button
                    type="button"
                    className="auto-btn auto-btn--primary"
                    disabled={running || !shots.length}
                    title="Quy tắc đổi thì lập lại bảng tên và viết lại mọi shot."
                    onClick={() => void adapt(rules, undefined, true)}
                  >
                    {adaptedCount ? "Chuyển thể lại toàn bộ" : "Chuyển thể"}
                  </button>
                </div>
              </div>
            )}

            {tab === "qa" && (
              <div className="va-scroll">
                {!qa ? (
                  <p className="auto-hint va-empty">Chưa có kết quả kiểm tra.</p>
                ) : (
                  <>
                    <p className={`va-qa-summary${qa.ok ? "" : " va-qa-summary--bad"}`}>
                      {qa.ok ? "Timeline liền, đủ shot." : "Có lỗi cấu trúc — xem bên dưới."} {qa.errors} lỗi ·{" "}
                      {qa.warnings} cảnh báo
                    </p>
                    <ul className="va-findings">
                      {qa.findings.map((f, i) => (
                        <li key={i} className={`va-finding va-finding--${f.level}`}>
                          <code>{f.code}</code>
                          {f.shot ? (
                            <button
                              type="button"
                              className="va-link"
                              onClick={() => {
                                const s = shots.find((x) => x.shot === f.shot);
                                if (s) {
                                  setTab("shots");
                                  seek(s);
                                  setTimeout(
                                    () => rowRefs.current.get(s.shot)?.scrollIntoView({ block: "center" }),
                                    50,
                                  );
                                }
                              }}
                            >
                              shot {String(f.shot).padStart(3, "0")}
                            </button>
                          ) : null}
                          <span>{f.message}</span>
                        </li>
                      ))}
                    </ul>
                    {analysis.timings_s && (
                      <p className="auto-hint">
                        Thời gian:{" "}
                        {Object.entries(analysis.timings_s)
                          .filter(([, v]) => v > 0)
                          .map(([k, v]) => `${k} ${v}s`)
                          .join(" · ")}
                      </p>
                    )}
                  </>
                )}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

const STATUS_LABELS: Record<string, string> = {
  queued: "đang chờ",
  analysing: "đang phân tích",
  casting: "đang lập hồ sơ",
  analysed: "đã phân tích — chưa chuyển thể",
  adapting: "đang chuyển thể",
  designing: "đang thiết kế tạo hình",
  conforming: "đang gỡ trang phục bản gốc khỏi shot",
  adapted: "đã chuyển thể",
  failed: "lỗi",
  interrupted: "bị ngắt — chạy tiếp được",
};

function ShotRow({
  videoId,
  shot,
  fps,
  adapted,
  on,
  onSeek,
  rowRef,
}: {
  videoId: string;
  shot: AnalysedShot;
  /** Frames, not just seconds: a 0.23s insert is 7 frames, and that is the
   *  number an editor conforms against. */
  fps: number;
  adapted?: import("../store/videoAnalysis").AdaptedShot;
  on: boolean;
  onSeek(): void;
  rowRef(el: HTMLElement | null): void;
}) {
  const src = shot.source;
  const dur = shot.end - shot.start;
  const conf = src?.confidence ?? 0;
  return (
    <article ref={rowRef} className={`va-shot${on ? " va-shot--on" : ""}`}>
      <button type="button" className="va-shot__tc" onClick={onSeek} title="Phát từ shot này">
        <b>{String(shot.shot).padStart(3, "0")}</b>
        <span>{tc(shot.start)}</span>
        <span className={dur < 0.25 ? "va-short" : ""}>{dur.toFixed(2)}s</span>
        {fps > 0 && (
          <span className="va-frames" title="khung hình đầu–cuối của shot">
            f{Math.round(shot.start * fps)}–{Math.round(shot.end * fps) - 1}
          </span>
        )}
      </button>
      <div className="va-shot__frames">
        {shot.frames.slice(0, 3).map((f) => (
          <img key={f} src={frameUrl(videoId, f)} alt="" loading="lazy" />
        ))}
      </div>
      <div className="va-shot__src">
        {src ? (
          <>
            <p className="va-shot__cam">
              {src.shot_size} · {src.camera_angle}
              {src.camera_movement && src.camera_movement !== "static" ? ` · ${src.camera_movement}` : ""}
              {conf < 0.6 && <span className="auto-tag auto-tag--warn">tin cậy {conf.toFixed(2)}</span>}
            </p>
            <p>{src.action}</p>
            {src.title_card && <p className="va-card">▣ {src.title_card}</p>}
            {shot.dialogue && <p className="va-dialogue">“{shot.dialogue}”</p>}
            {!shot.dialogue && shot.dialogue_continues != null && (
              <p className="va-continues">↳ còn đang nói câu của shot {String(shot.dialogue_continues).padStart(3, "0")}</p>
            )}
          </>
        ) : (
          <p className="auto-hint">chưa xem</p>
        )}
      </div>
      <div className="va-shot__adapt">
        {adapted ? (
          <>
            {adapted.title && <p className="va-shot__title">{adapted.title}</p>}
            {(adapted.action ?? []).map((a, i) => (
              <p key={i}>{a}</p>
            ))}
            {(adapted.dialogue ?? []).map((d, i) => (
              <p key={i} className="va-dialogue">
                <b>{d.who}</b> {d.line}
              </p>
            ))}
          </>
        ) : (
          <p className="auto-hint">—</p>
        )}
      </div>
    </article>
  );
}


/** One person: who they are, where they appear, and the sheet made from them. */
function CastCard({
  videoId,
  character,
  busy,
  onSeek,
  onGenerate,
}: {
  videoId: string;
  character: CastCharacter;
  busy: boolean;
  onSeek(shot: number): void;
  onGenerate(stateKey: string): void;
}) {
  const [state, setState] = useState(character.states?.[0]?.key ?? "");
  const look = character.states?.find((st) => st.key === state) ?? character.states?.[0];

  return (
    <article className="va-card">
      <div className="va-card__frames">
        {character.frames.slice(0, 4).map((f) => (
          <img key={f} src={frameUrl(videoId, f)} alt="" loading="lazy" />
        ))}
      </div>
      <div className="va-card__body">
        <h4 className="va-card__title">
          {character.name}
          {character.role && <span className="auto-tag">{character.role}</span>}
          {character.source_name && <small>từ “{character.source_name}”</small>}
        </h4>
        {character.summary && <p>{character.summary}</p>}
        {character.identity_anchor && (
          <p className="auto-anchor">
            <span>neo nhận dạng</span>
            {character.identity_anchor}
          </p>
        )}
        {character.states.length > 1 && (
          <div className="auto-tabs">
            {character.states.map((st) => (
              <button
                key={st.key}
                type="button"
                className={`auto-tab${st.key === state ? " auto-tab--on" : ""}`}
                onClick={() => setState(st.key)}
              >
                {st.label || st.key}
              </button>
            ))}
          </div>
        )}
        {look && (
          <dl className="va-spec">
            {look.look && (
              <>
                <dt>ngoại hình</dt>
                <dd>{look.look}</dd>
              </>
            )}
            {look.wardrobe && (
              <>
                <dt>trang phục</dt>
                <dd>{look.wardrobe}</dd>
              </>
            )}
            {look.posture && (
              <>
                <dt>dáng</dt>
                <dd>{look.posture}</dd>
              </>
            )}
          </dl>
        )}
        <ShotChips shots={character.shots} onSeek={onSeek} />
      </div>
      <PlateBox
        plate={character.plate}
        busy={busy}
        label={`${character.name} — sheet`}
        onGenerate={() => onGenerate(state)}
      />
    </article>
  );
}

/** One place: what the reference shows of it, and the plate made from it. */
function PlaceCard({
  videoId,
  place,
  shotsBySetting,
  busy,
  onSeek,
  onGenerate,
}: {
  videoId: string;
  place: CastEnvironment;
  shotsBySetting: AnalysedShot[];
  busy: boolean;
  onSeek(shot: number): void;
  onGenerate(): void;
}) {
  const [open, setOpen] = useState(false);
  // What the vision pass actually saw in each shot of this place — the raw
  // reading, kept visible because a plate is only as good as its description.
  const readings = shotsBySetting
    .filter((s) => place.shots.includes(s.shot) && s.source?.setting)
    .map((s) => ({ shot: s.shot, setting: s.source!.setting as string }));

  return (
    <article className="va-card">
      <div className="va-card__frames">
        {place.frames.slice(0, 4).map((f) => (
          <img key={f} src={frameUrl(videoId, f)} alt="" loading="lazy" />
        ))}
      </div>
      <div className="va-card__body">
        <h4 className="va-card__title">
          {place.name}
          <span className="auto-tag">{place.shots.length} shot</span>
        </h4>
        {place.summary && <p>{place.summary}</p>}
        <dl className="va-spec">
          {place.lighting && (
            <>
              <dt>ánh sáng</dt>
              <dd>{place.lighting}</dd>
            </>
          )}
          {place.mood && (
            <>
              <dt>không khí</dt>
              <dd>{place.mood}</dd>
            </>
          )}
          {place.lock && (
            <>
              <dt>khoá lại</dt>
              <dd>{place.lock}</dd>
            </>
          )}
        </dl>
        <ShotChips shots={place.shots} onSeek={onSeek} />
        {readings.length > 0 && (
          <>
            <button type="button" className="auto-plate__toggle" onClick={() => setOpen((v) => !v)}>
              {open ? "▾" : "▸"} mô tả bối cảnh theo từng shot ({readings.length})
            </button>
            {open && (
              <ul className="va-readings">
                {readings.map((r) => (
                  <li key={r.shot}>
                    <button type="button" className="va-link" onClick={() => onSeek(r.shot)}>
                      {String(r.shot).padStart(3, "0")}
                    </button>
                    <span>{r.setting}</span>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
      <PlateBox plate={place.plate} busy={busy} label={`${place.name} — plate`} onGenerate={onGenerate} />
    </article>
  );
}

function ShotChips({ shots, onSeek }: { shots: number[]; onSeek(shot: number): void }) {
  if (!shots.length) return <p className="auto-hint">không gắn được vào shot nào</p>;
  const shown = shots.slice(0, 24);
  return (
    <p className="va-chips">
      <span className="va-chips__label">shot</span>
      {shown.map((n) => (
        <button key={n} type="button" className="va-chip" onClick={() => onSeek(n)}>
          {String(n).padStart(3, "0")}
        </button>
      ))}
      {shots.length > shown.length && <span className="auto-hint">+{shots.length - shown.length}</span>}
    </p>
  );
}

function PlateBox({
  plate,
  busy,
  label,
  onGenerate,
}: {
  plate?: import("../store/videoAnalysis").Plate;
  busy: boolean;
  label: string;
  onGenerate(): void;
}) {
  return (
    <div className="va-plate">
      {plate?.url ? (
        <a href={plate.url} target="_blank" rel="noreferrer" className="va-plate__img">
          <img src={plate.url} alt={label} loading="lazy" />
        </a>
      ) : (
        <div className="va-plate__empty">chưa gen</div>
      )}
      <button type="button" className="auto-btn" disabled={busy} onClick={onGenerate}>
        {busy ? "đang gen…" : plate ? "gen lại" : "gen ảnh"}
      </button>
      {plate && (
        <p className="auto-hint">
          {IMAGE_MODEL_LABELS[plate.model] ?? plate.model} · {plate.size}
          {plate.persisted ? "" : " · chưa lưu được"}
        </p>
      )}
    </div>
  );
}

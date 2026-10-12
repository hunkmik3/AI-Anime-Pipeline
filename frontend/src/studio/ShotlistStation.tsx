import { useEffect, useMemo, useState } from "react";
import { DELIVERY_LABEL, fmtTime, studioApi, type Delivery, type FilmDetail, type Finding, type Line, type Shot } from "./api";
import type { StationProps } from "./StudioPage";

/** Accept one blocking finding with the owner's reason. */
function AcceptFinding({ film, finding, act, busy }: { film: FilmDetail; finding: Finding; act: StationProps["act"]; busy: string }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  if (finding.accepted) return <span className="st-pill st-pill--done">Đã chấp nhận</span>;
  if (finding.severity !== "blocking" || !finding.key || finding.criterion === "review_incomplete") return null;
  if (!open) return <button type="button" className="st-link" onClick={() => setOpen(true)}>Chấp nhận như hiện tại</button>;
  return <span className="st-accept">
    <input value={note} onChange={e => setNote(e.target.value)} placeholder="Lý do (ví dụ: mốc thời gian của Gemini lệch)" aria-label="Lý do chấp nhận" />
    <button type="button" disabled={!!busy || film.revision == null}
      onClick={() => void act("accept", () => studioApi.accept(film.id, film.revision!, [finding.key!], note.trim()))}>Chấp nhận</button>
    <button type="button" className="st-link" onClick={() => setOpen(false)}>Huỷ</button>
  </span>;
}

function FindingList({ film, findings, act, busy }: { film: FilmDetail; findings: Finding[]; act: StationProps["act"]; busy: string }) {
  if (!findings.length) return null;
  return <ul className="st-findings">
    {findings.map((f, i) => <li key={f.key ?? i} className={`st-finding st-finding--${f.accepted ? "accepted" : f.severity}`}>
      <span className="st-finding__what">{f.message || f.criterion || f.origin}</span>
      {(f.expected || f.heard) && <span className="st-finding__diff">
        {f.expected && <span>Shotlist: <q>{f.expected}</q></span>}{f.heard && <span>Nguồn: <q>{f.heard}</q></span>}</span>}
      <AcceptFinding film={film} finding={f} act={act} busy={busy} />
    </li>)}
  </ul>;
}

const sameLines = (a: Line[], b: Line[]) => JSON.stringify(a) === JSON.stringify(b);

function ShotEditor({ film, clipKey, clipLabel, shot, act, busy }: {
  film: FilmDetail; clipKey: string; clipLabel: string; shot: Shot; act: StationProps["act"]; busy: string;
}) {
  const [lines, setLines] = useState<Line[]>(shot.lines);
  const [action, setAction] = useState(shot.action.join("\n"));
  const [saved, setSaved] = useState("");
  // The page refreshes every few seconds; only a real change on the server resets the form.
  const serverVersion = JSON.stringify([shot.lines, shot.action]);
  useEffect(() => { setLines(shot.lines); setAction(shot.action.join("\n")); }, [serverVersion]);
  const characters = film.characters ?? [];
  const dirty = !sameLines(lines, shot.lines) || action.trim() !== shot.action.join("\n").trim();
  const setLine = (i: number, change: Partial<Line>) => setLines(ls => ls.map((l, j) => j === i ? { ...l, ...change } : l));
  async function save() {
    const body: { lines?: Line[]; action?: string[] } = {};
    if (!sameLines(lines, shot.lines)) body.lines = lines;
    if (action.trim() !== shot.action.join("\n").trim()) body.action = action.split("\n").map(x => x.trim()).filter(Boolean);
    await act(`shot-${shot.source_shot}`, async () => {
      const out = await studioApi.editShot(film.id, shot.source_shot, film.revision!, body);
      setSaved(`Đã lưu. ${out.impact.clips.join(", ")} cần đối chiếu lại nguồn trước khi gen.`);
    });
  }
  return <article className="st-shot">
    <div className="st-shot__media">
      {shot.frame ? <img src={studioApi.frameUrl(film.id, shot.frame)} alt={`Khung hình gốc shot ${shot.index}`} loading="lazy" />
        : <div className="st-shot__noframe">Không có ảnh</div>}
      <span className="st-shot__time">#{shot.index} · {shot.start.toFixed(1)}–{shot.end.toFixed(1)} s</span>
    </div>
    <div className="st-shot__body">
      <p className="st-shot__camera">{[shot.framing, shot.camera].filter(Boolean).join(" · ")}</p>
      <label className="st-field st-field--compact">
        <span>Hành động (mỗi dòng một nhịp)</span>
        <textarea rows={Math.max(2, action.split("\n").length)} value={action} onChange={e => setAction(e.target.value)} />
      </label>
      <div className="st-lines">
        <span className="st-lines__head">Thoại</span>
        {lines.length === 0 && <p className="st-muted">Shot không có thoại.</p>}
        {lines.map((l, i) => <div className="st-line" key={i}>
          <select aria-label="Người nói" value={l.speaker_id ?? ""} onChange={e => {
            const c = characters.find(x => x.id === e.target.value);
            setLine(i, { speaker_id: e.target.value || null, who: c ? c.name : l.who });
          }}>
            <option value="">Giọng không tên</option>
            {characters.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <input className="st-line__text" aria-label="Câu thoại" value={l.line} onChange={e => setLine(i, { line: e.target.value })} />
          <select aria-label="Cách nói" value={l.delivery} onChange={e => setLine(i, { delivery: e.target.value as Delivery })}>
            {(Object.keys(DELIVERY_LABEL) as Delivery[]).map(d => <option key={d} value={d}>{DELIVERY_LABEL[d]}</option>)}
          </select>
          <span className="st-line__timing" title="Mốc trong clip (giây)">
            <input aria-label="Bắt đầu" type="number" step="0.01" value={l.timing?.[0] ?? shot.start}
              onChange={e => setLine(i, { timing: [Number(e.target.value), l.timing?.[1] ?? shot.end] })} />
            –<input aria-label="Kết thúc" type="number" step="0.01" value={l.timing?.[1] ?? shot.end}
              onChange={e => setLine(i, { timing: [l.timing?.[0] ?? shot.start, Number(e.target.value)] })} />
          </span>
          <label className="st-line__cont" title="Câu nối tiếp từ shot trước">
            <input type="checkbox" checked={l.cont} onChange={e => setLine(i, { cont: e.target.checked })} />nối
          </label>
          <button type="button" className="st-link" aria-label="Xoá câu" onClick={() => setLines(ls => ls.filter((_, j) => j !== i))}>Xoá</button>
        </div>)}
        <button type="button" className="st-link" onClick={() => setLines(ls => [...ls,
          { who: "", speaker_id: null, line: "", delivery: "on_camera", cont: false, timing: [shot.start, shot.end] }])}>+ Thêm câu</button>
      </div>
      {shot.heard && <p className="st-heard">
        <span>Nguồn nghe được: <q>{shot.heard}</q></span>
        {lines.length === 1 && <button type="button" className="st-link" onClick={() => setLine(0, { line: shot.heard })}>Dùng câu này</button>}
      </p>}
      <FindingList film={film} findings={shot.findings} act={act} busy={busy} />
      <div className="st-actions">
        <button type="button" className="st-primary" disabled={!dirty || !!busy || film.revision == null} onClick={() => void save()}>Lưu shot</button>
        {dirty && <button type="button" className="st-link" onClick={() => { setLines(shot.lines); setAction(shot.action.join("\n")); }}>Bỏ thay đổi</button>}
        {saved && !dirty && <span className="st-muted">{saved}</span>}
        {shot.index > 1 && <button type="button" className="st-link st-push" disabled={!!busy || film.revision == null}
          onClick={() => {
            if (!window.confirm(`Tách ${clipLabel} thành 2 clip, clip mới bắt đầu từ shot ${shot.index}? Cả hai clip sẽ cần đối chiếu lại nguồn và render lại.`)) return;
            void act(`split-${clipKey}`, () => studioApi.split(film.id, clipKey, film.revision!, shot.index));
          }}>Tách clip tại shot này</button>}
      </div>
    </div>
  </article>;
}

export function ShotlistStation({ film, act, busy, go, params }: StationProps) {
  const clips = film.clips;
  const selected = useMemo(() => clips.find(c => c.key === params.get("clip")) ?? clips.find(c => c.blocking.length) ?? clips[0], [clips, params]);
  if (!clips.length) return <p className="st-empty">Shotlist chưa có — trạm Nguồn đang phân tích video.</p>;
  const waiting = film.stage === "awaiting_review";
  return <div className="st-split">
    <nav className="st-cliplist" aria-label="Clip">
      {clips.map(c => {
        const lines = c.shots.reduce((n, s) => n + s.lines.length, 0);
        const heard = c.shots.some(s => s.heard);
        return <button type="button" key={c.key} className={`st-cliplist__item${c.key === selected?.key ? " is-active" : ""}`}
          onClick={() => go("shotlist", { clip: c.key })}>
          <strong>{c.label}</strong>
          <small>{fmtTime(c.source_start)}–{fmtTime(c.source_end)} · {c.shots.length} shot · {lines} câu</small>
          {c.blocking.length > 0 ? <span className="st-pill st-pill--attention">{c.blocking.length} lỗi</span>
            : heard ? <span className="st-pill st-pill--warning">khác nguồn</span> : null}
        </button>;
      })}
    </nav>
    {selected && <section className="st-clip">
      {waiting && <div className="st-gatebar">
        <span>Phim đang dừng để bạn duyệt shotlist và material.</span>
        <button type="button" className="st-primary" disabled={!!busy} onClick={() => void act("continue", () => studioApi.continueFilm(film.id))}>Tiếp tục sản xuất</button>
      </div>}
      <header className="st-clip__head">
        <div>
          <h2>{selected.label}</h2>
          <p>{fmtTime(selected.source_start)}–{fmtTime(selected.source_end)} · {selected.seconds.toFixed(1)} s · {selected.shots.length} shot</p>
        </div>
        <div className="st-actions st-actions--tight">
          {clips[clips.length - 1]?.key !== selected.key && <button type="button" disabled={!!busy || film.revision == null}
            onClick={() => {
              const next = clips[clips.findIndex(c => c.key === selected.key) + 1];
              if (!window.confirm(`Gộp ${selected.label} với ${next.label} thành một clip? Clip gộp sẽ cần đối chiếu lại nguồn và render lại.`)) return;
              void act(`merge-${selected.key}`, () => studioApi.mergeNext(film.id, selected.key, film.revision!));
            }}>Gộp với clip sau</button>}
          <button type="button" disabled={!!busy || film.revision == null || film.review?.in_progress}
            onClick={() => void act("review", () => studioApi.review(film.id, film.revision!))}>
            {film.review?.in_progress ? "Đang đối chiếu…" : "Đối chiếu lại với nguồn"}</button>
        </div>
      </header>
      <video key={selected.key} className="st-clip__video" controls preload="metadata"
        src={`${studioApi.sourceUrl(film.id)}#t=${selected.source_start},${selected.source_end}`} />
      <FindingList film={film} findings={selected.findings} act={act} busy={busy} />
      {selected.shots.map(s => <ShotEditor key={`${selected.key}-${s.source_shot}`} film={film} clipKey={selected.key} clipLabel={selected.label} shot={s} act={act} busy={busy} />)}
    </section>}
  </div>;
}

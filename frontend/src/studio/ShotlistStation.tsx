import { useCallback, useEffect, useMemo, useState } from "react";
import {
  DELIVERY_LABEL, confirmChange, fmtTime, studioApi,
  type ClipDetail, type Delivery, type FilmDetail, type Finding, type Line, type Presence, type Shot, type Visibility,
} from "./api";
import type { StationProps } from "./StudioPage";

const VISIBILITY_LABEL: Record<Visibility, string> = { visible: "Trong khung", partial: "Một phần", offscreen: "Ngoài khung", occluded: "Bị che" };

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

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

function PresenceEditor({ film, rows, states, onRows, onStates }: {
  film: FilmDetail; rows: Presence[]; states: Record<string, string>;
  onRows(r: Presence[]): void; onStates(s: Record<string, string>): void;
}) {
  const assets = film.assets ?? [];
  const byId = new Map(assets.map(a => [a.id, a]));
  const set = (i: number, change: Partial<Presence>) => onRows(rows.map((r, j) => (j === i ? { ...r, ...change } : r)));
  const unused = assets.filter(a => !rows.some(r => r.asset_id === a.id));
  return <div className="st-presence">
    <span className="st-lines__head">Ai và vật gì trong shot</span>
    {rows.map((r, i) => {
      const asset = byId.get(r.asset_id);
      return <div className="st-presence__row" key={r.asset_id}>
        <strong title={r.asset_id}>{asset?.name ?? r.asset_id}</strong>
        <select aria-label={`Mức hiện diện ${asset?.name ?? r.asset_id}`} value={r.visibility} onChange={e => set(i, { visibility: e.target.value as Visibility })}>
          {(Object.keys(VISIBILITY_LABEL) as Visibility[]).map(v => <option key={v} value={v}>{VISIBILITY_LABEL[v]}</option>)}
        </select>
        <input aria-label="Trạng thái" placeholder="Trạng thái (ví dụ: đeo vòng ở cổ tay trái)" value={r.state ?? ""} onChange={e => set(i, { state: e.target.value })} />
        <input aria-label="Vị trí" placeholder="Vị trí trong khung" value={r.position ?? ""} onChange={e => set(i, { position: e.target.value })} />
        {asset?.kind === "character" && asset.states.length > 0 && <select aria-label={`Trang phục ${asset.name}`} value={states[r.asset_id] ?? ""}
          onChange={e => onStates({ ...states, [r.asset_id]: e.target.value })}>
          <option value="" disabled>Trang phục…</option>
          {asset.states.map(st => <option key={st.key} value={st.key}>{st.label}</option>)}
        </select>}
        <button type="button" className="st-link" onClick={() => {
          onRows(rows.filter((_, j) => j !== i));
          const rest = { ...states };
          delete rest[r.asset_id];
          onStates(rest);
        }}>Bỏ</button>
      </div>;
    })}
    {unused.length > 0 && <select className="st-presence__add" aria-label="Thêm người hoặc vật" value="" onChange={e => {
      const a = byId.get(e.target.value);
      if (!a) return;
      onRows([...rows, { asset_id: a.id, visibility: "visible", state: "", position: "" }]);
      if (a.kind === "character" && a.states[0]) onStates({ ...states, [a.id]: a.states[0].key });
    }}>
      <option value="">+ Thêm người / vật…</option>
      {unused.map(a => <option key={a.id} value={a.id}>{a.name} · {a.kind}</option>)}
    </select>}
  </div>;
}

function ShotEditor({ film, clip, shot, isLastOfFilm, act, busy, reload }: {
  film: FilmDetail; clip: ClipDetail; shot: Shot; isLastOfFilm: boolean; act: StationProps["act"]; busy: string; reload(): void;
}) {
  const [lines, setLines] = useState<Line[]>(shot.lines);
  const [action, setAction] = useState(shot.action.join("\n"));
  const [rows, setRows] = useState<Presence[]>(shot.presence);
  const [states, setStates] = useState<Record<string, string>>(shot.states);
  const [cut, setCut] = useState(shot.end.toFixed(2));
  const [saved, setSaved] = useState("");
  // The page refreshes every few seconds; only a real change on the server resets the form.
  const serverVersion = JSON.stringify([shot.lines, shot.action, shot.presence, shot.states, shot.end]);
  useEffect(() => {
    setLines(shot.lines); setAction(shot.action.join("\n")); setRows(shot.presence); setStates(shot.states); setCut(shot.end.toFixed(2));
  }, [serverVersion]);
  const characters = film.characters ?? [];
  const changes = useMemo(() => {
    const body: { lines?: Line[]; action?: string[]; presence?: Presence[]; character_states?: Record<string, string> } = {};
    if (!same(lines, shot.lines)) body.lines = lines;
    if (action.trim() !== shot.action.join("\n").trim()) body.action = action.split("\n").map(x => x.trim()).filter(Boolean);
    if (!same(rows, shot.presence)) body.presence = rows;
    if (!same(states, shot.states)) body.character_states = states;
    return body;
  }, [lines, action, rows, states, shot]);
  const dirty = Object.keys(changes).length > 0;
  const setLine = (i: number, change: Partial<Line>) => setLines(ls => ls.map((l, j) => (j === i ? { ...l, ...change } : l)));
  const reset = () => { setLines(shot.lines); setAction(shot.action.join("\n")); setRows(shot.presence); setStates(shot.states); };
  async function save() {
    await act(`shot-${shot.source_shot}`, async () => {
      if (!await confirmChange(() => studioApi.editShot(film.id, shot.source_shot, film.revision!, { ...changes, dry_run: true }),
        `Lưu thay đổi shot ${shot.index} của ${clip.label}?`)) return;
      const out = await studioApi.editShot(film.id, shot.source_shot, film.revision!, changes);
      setSaved(`Đã lưu. ${out.impact.clips.join(", ")} cần đối chiếu lại nguồn trước khi gen.`);
      reload();
    });
  }
  async function moveCut() {
    const at = clip.source_start + Number(cut);
    await act(`cut-${shot.source_shot}`, async () => {
      if (!await confirmChange(() => studioApi.moveCut(film.id, shot.source_shot, film.revision!, at, true),
        `Dời điểm cắt sau shot ${shot.index} tới ${Number(cut).toFixed(2)} s?`)) return;
      await studioApi.moveCut(film.id, shot.source_shot, film.revision!, at);
      reload();
    });
  }
  return <article className="st-shot">
    <div className="st-shot__media">
      {shot.frame ? <img src={studioApi.frameUrl(film.id, shot.frame)} alt={`Khung hình gốc shot ${shot.index}`} loading="lazy" />
        : <div className="st-shot__noframe">Không có ảnh</div>}
      <span className="st-shot__time">#{shot.index} · {shot.start.toFixed(2)}–{shot.end.toFixed(2)} s</span>
      {!isLastOfFilm && <label className="st-cut" title="Thời điểm (giây trong clip) cắt sang shot sau">
        <span>Cắt ở</span>
        <input type="number" step="0.01" value={cut} onChange={e => setCut(e.target.value)} aria-label="Điểm cắt sau shot này" />
        <button type="button" className="st-link" disabled={!!busy || Math.abs(Number(cut) - shot.end) < 0.005 || film.revision == null}
          onClick={() => void moveCut()}>Dời</button>
      </label>}
    </div>
    <div className="st-shot__body">
      <p className="st-shot__camera">{[shot.framing, shot.camera].filter(Boolean).join(" · ")}</p>
      <label className="st-field st-field--compact">
        <span>Hành động (mỗi dòng một nhịp)</span>
        <textarea rows={Math.max(2, action.split("\n").length)} value={action} onChange={e => setAction(e.target.value)} />
      </label>
      <PresenceEditor film={film} rows={rows} states={states} onRows={setRows} onStates={setStates} />
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
        {dirty && <button type="button" className="st-link" onClick={reset}>Bỏ thay đổi</button>}
        {saved && !dirty && <span className="st-muted">{saved}</span>}
        {shot.index > 1 && <button type="button" className="st-link st-push" disabled={!!busy || film.revision == null}
          onClick={() => void act(`split-${clip.key}`, async () => {
            if (!await confirmChange(() => studioApi.split(film.id, clip.key, film.revision!, shot.index, true),
              `Tách ${clip.label} thành 2 clip, clip mới bắt đầu từ shot ${shot.index}?`)) return;
            await studioApi.split(film.id, clip.key, film.revision!, shot.index);
            reload();
          })}>Tách clip tại shot này</button>}
      </div>
    </div>
  </article>;
}

export function ShotlistStation({ film, act, busy, go, params }: StationProps) {
  const clips = film.clips;
  const selectedKey = useMemo(() => (clips.find(c => c.key === params.get("clip")) ?? clips.find(c => c.blocking.length) ?? clips[0])?.key,
    [clips, params]);
  const [clip, setClip] = useState<ClipDetail | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    if (!selectedKey) return;
    studioApi.clip(film.id, selectedKey).then(c => { setClip(c); setError(""); }).catch(e => setError(String(e.message ?? e)));
  }, [film.id, selectedKey]);
  // A new board revision (any save) or another clip reloads the clip's shots.
  useEffect(() => { load(); }, [load, film.revision]);
  if (!clips.length) return <p className="st-empty">Shotlist chưa có — trạm Nguồn đang phân tích video.</p>;
  const waiting = film.stage === "awaiting_review";
  const lastKey = clips[clips.length - 1]?.key;
  const position = clips.findIndex(c => c.key === selectedKey);
  return <div className="st-split">
    <nav className="st-cliplist" aria-label="Clip">
      {clips.map(c => <button type="button" key={c.key} className={`st-cliplist__item${c.key === selectedKey ? " is-active" : ""}`}
        onClick={() => go("shotlist", { clip: c.key })}>
        <strong>{c.label}</strong>
        <small>{fmtTime(c.source_start)}–{fmtTime(c.source_end)} · {c.shot_count} shot · {c.line_count} câu</small>
        {c.blocking.length > 0 ? <span className="st-pill st-pill--attention">{c.blocking.length} lỗi</span>
          : c.heard_differs ? <span className="st-pill st-pill--warning">khác nguồn</span> : null}
      </button>)}
    </nav>
    <section className="st-clip">
      {waiting && <div className="st-gatebar">
        <span>Phim đang dừng để bạn duyệt shotlist và material.</span>
        <button type="button" className="st-primary" disabled={!!busy} onClick={() => void act("continue", () => studioApi.continueFilm(film.id))}>Tiếp tục sản xuất</button>
      </div>}
      {error && <p className="st-error">{error}</p>}
      {!clip || clip.key !== selectedKey ? <p className="st-muted">Đang tải clip…</p> : <>
        <header className="st-clip__head">
          <div>
            <h2>{clip.label}</h2>
            <p>{fmtTime(clip.source_start)}–{fmtTime(clip.source_end)} · {clip.seconds.toFixed(1)} s · {clip.shots.length} shot</p>
          </div>
          <div className="st-actions st-actions--tight">
            {clip.key !== lastKey && <button type="button" disabled={!!busy || film.revision == null}
              onClick={() => void act(`merge-${clip.key}`, async () => {
                const next = clips[position + 1];
                if (!await confirmChange(() => studioApi.mergeNext(film.id, clip.key, film.revision!, true),
                  `Gộp ${clip.label} với ${next?.label ?? "clip sau"} thành một clip?`)) return;
                await studioApi.mergeNext(film.id, clip.key, film.revision!);
              })}>Gộp với clip sau</button>}
            <button type="button" disabled={!!busy || film.revision == null || film.review?.in_progress}
              onClick={() => void act("review", () => studioApi.review(film.id, film.revision!))}>
              {film.review?.in_progress ? "Đang đối chiếu…" : "Đối chiếu lại với nguồn"}</button>
          </div>
        </header>
        <video key={clip.key} className="st-clip__video" controls preload="metadata"
          src={`${studioApi.sourceUrl(film.id)}#t=${clip.source_start},${clip.source_end}`} />
        <FindingList film={film} findings={clip.findings} act={act} busy={busy} />
        {clip.shots.map((s, i) => <ShotEditor key={`${clip.key}-${s.source_shot}`} film={film} clip={clip} shot={s}
          isLastOfFilm={clip.key === lastKey && i === clip.shots.length - 1} act={act} busy={busy} reload={load} />)}
      </>}
    </section>
  </div>;
}

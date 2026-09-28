/**
 * Nodes for the /automation board.
 *
 * Kept in one file on purpose: premise → cast/places → shotlist → clips only
 * make sense as one chain, and splitting them across five files would cost
 * more in navigation than it buys in tidiness. They also do not share the
 * shotWorkflow node contract, so they cannot reuse BaseNodeShell.
 */
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useState, type ReactNode } from "react";

import {
  useAutomation,
  type CharacterNodeData,
  type EnvironmentNodeData,
  type AssetNodeData,
  asLines,
  type Plate,
  type SequenceNodeData,
  type VideoNodeData,
} from "../store/automation";
import {
  DEFAULT_RULES,
  RUNNING,
  STAGE_LABELS,
  STYLE_PRESETS,
  useVideoAnalysis,
} from "../store/videoAnalysis";
import { isStrictBoard, sameFingerprint, sourceReadyForShots } from "./contracts";

function Shell({
  title,
  badge,
  variant,
  selected,
  inbound = true,
  outbound = true,
  children,
}: {
  title: string;
  badge?: ReactNode;
  variant: string;
  selected?: boolean;
  inbound?: boolean;
  outbound?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`auto-node auto-node--${variant}${selected ? " auto-node--selected" : ""}`}>
      {inbound && <Handle type="target" position={Position.Left} className="auto-handle" />}
      <header className="auto-node__head">
        <span className="auto-node__title">{title}</span>
        {badge}
      </header>
      {children}
      {outbound && <Handle type="source" position={Position.Right} className="auto-handle" />}
    </div>
  );
}

/** Prompt + generate + result, the unit every plate on this board is made of.
 *  The prompt is editable and visible by default: the whole argument for
 *  assembling prompts from a template is that a person can read one before
 *  spending a generation on it. */
function PlatePanel({
  plate,
  label,
  hint,
  disabled,
  onEdit,
  onGenerate,
}: {
  plate: Plate;
  label: string;
  hint?: string;
  disabled?: boolean;
  onEdit(text: string): void;
  onGenerate(): void;
}) {
  const [open, setOpen] = useState(false);
  const running = plate.status === "running";

  return (
    <div className="auto-plate">
      <div className="auto-plate__bar">
        <button
          type="button"
          className="auto-plate__toggle"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? "▾" : "▸"} {label}
        </button>
        {plate.referenceUrl && <span className="auto-tag auto-tag--ok">đã lưu</span>}
        {plate.image?.startsWith("data:") && (
          <span
            className="auto-tag auto-tag--warn"
            title="R2 không nhận được file, nên ảnh này chỉ sống trong tab hiện tại và không vào board đã lưu."
          >
            chưa lưu được
          </span>
        )}
        <button
          type="button"
          className="auto-btn auto-btn--go"
          onClick={onGenerate}
          disabled={running || disabled || !plate.prompt.trim()}
          title={hint}
        >
          {running ? "đang gen…" : plate.image ? "gen lại" : "gen"}
        </button>
      </div>

      {open && (
        <textarea
          className="auto-textarea auto-textarea--prompt nodrag nowheel"
          value={plate.prompt}
          onChange={(e) => onEdit(e.target.value)}
          rows={7}
          spellCheck={false}
          placeholder="Prompt sẽ tự điền sau khi phân tích xong."
        />
      )}

      {plate.error && <p className="auto-error">{plate.error}</p>}
      {plate.image && (
        <a href={plate.image} download={`${label}.png`} className="auto-plate__img">
          <img src={plate.image} alt={label} />
        </a>
      )}
    </div>
  );
}

// The premise node reads straight from the store rather than from node data:
// it is a singleton, and the breakdown that rebuilds the board would otherwise
// have to carry the draft text through every rebuild.
export function AutoScriptNode({ selected }: NodeProps) {
  const [source, setSource] = useState<"script" | "video">("script");
  return (
    <Shell
      title={source === "script" ? "Kịch bản thô" : "Video mẫu"}
      variant="script"
      selected={selected}
      inbound={false}
    >
      <div className="auto-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={source === "script"}
          className={`auto-tab${source === "script" ? " auto-tab--on" : ""}`}
          onClick={() => setSource("script")}
        >
          Kịch bản thô
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={source === "video"}
          className={`auto-tab${source === "video" ? " auto-tab--on" : ""}`}
          onClick={() => setSource("video")}
        >
          Video mẫu
        </button>
      </div>
      {source === "script" ? <ScriptInput /> : <VideoInput />}
    </Shell>
  );
}

/** Upload a reference video, watch it analyse, open it for review. The review
 *  itself is a full-screen panel at page level — a 101-row shotlist does not
 *  fit in a node. */
function VideoInput() {
  const projectId = useAutomation((s) => s.currentProjectId);
  const videos = useVideoAnalysis((s) => s.videos);
  const uploading = useVideoAnalysis((s) => s.uploading);
  const error = useVideoAnalysis((s) => s.error);
  const upload = useVideoAnalysis((s) => s.upload);
  const openVideo = useVideoAnalysis((s) => s.open);
  const [preset, setPreset] = useState(STYLE_PRESETS[0].key);
  const [deep, setDeep] = useState(false);

  return (
    <>
      <p className="auto-hint">
        Máy đo điểm cắt và thời lượng; model chỉ mô tả trong từng shot, rồi chuyển thể sang thế giới mới
        mà giữ nguyên dựng.
      </p>
      <div className="auto-row">
        <label className="auto-select">
          <span>Chuyển thể thành</span>
          <select value={preset} onChange={(e) => setPreset(e.target.value as typeof preset)}>
            {STYLE_PRESETS.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
        <label className={`auto-btn auto-btn--primary auto-btn--file${uploading !== null ? " va-disabled" : ""}`}>
          {uploading !== null ? `đang tải lên ${Math.round(uploading * 100)}%` : "Tải video lên"}
          <input
            type="file"
            accept="video/mp4,video/quicktime,video/webm,.mkv,.m4v"
            disabled={uploading !== null}
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (!file) return;
              const text = STYLE_PRESETS.find((p) => p.key === preset)?.text ?? DEFAULT_RULES.visual_style;
              void upload(
                file,
                projectId,
                { ...DEFAULT_RULES, visual_style: text },
                deep ? "deep" : "standard",
              ).catch(() => undefined);
            }}
          />
        </label>
      </div>
      <label
        className="auto-check"
        title="Đọc kỹ từng shot: nhiều keyframe hơn (tới 7 khung cho shot dài), mỗi lượt gọi ít shot hơn nên model tập trung hơn, và shot nào model tự nhận là không chắc thì đẩy lên model mạnh. Tốn khoảng gấp đôi token."
      >
        <input type="checkbox" checked={deep} onChange={(e) => setDeep(e.target.checked)} />
        <span>Phân tích shot kỹ (chậm và tốn hơn)</span>
      </label>
      {error && <p className="auto-error">{error}</p>}
      {videos.length > 0 && (
        <ul className="va-list nowheel">
          {videos.map((v) => {
            const busy = RUNNING.includes(v.status);
            const pct = v.progress.total ? ((v.progress.done ?? 0) / v.progress.total) * 100 : 0;
            return (
              <li key={v.id}>
                <button type="button" className="va-list__item" onClick={() => void openVideo(v.id)}>
                  <span className="va-list__name">{v.name}</span>
                  <span className="va-list__meta">
                    {busy
                      ? `${STAGE_LABELS[v.progress.stage ?? ""] ?? "đang chờ"}${v.progress.total ? ` ${v.progress.done}/${v.progress.total}` : ""}`
                      : v.status === "failed"
                        ? "lỗi — mở để xem"
                        : v.status === "interrupted"
                          ? "bị ngắt — mở để chạy tiếp"
                          : `${v.shot_count} shot${v.duration ? ` · ${v.duration.toFixed(0)}s` : ""}${v.detail === "deep" ? " · kỹ" : ""}${v.status === "adapted" ? " · đã chuyển thể" : ""}`}
                  </span>
                  {busy && (
                    <span className="va-progress" aria-hidden>
                      <span style={{ width: `${pct}%` }} />
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

function ScriptInput() {
  const script = useAutomation((s) => s.script);
  const runtime = useAutomation((s) => s.runtimeSeconds);
  const status = useAutomation((s) => s.breakdownStatus);
  const error = useAutomation((s) => s.breakdownError);
  const setScript = useAutomation((s) => s.setScript);
  const setRuntime = useAutomation((s) => s.setRuntimeSeconds);
  const run = useAutomation((s) => s.runBreakdown);

  return (
    <>
      <textarea
        className="auto-textarea nodrag nowheel"
        value={script}
        onChange={(e) => setScript(e.target.value)}
        rows={9}
        placeholder="Ví dụ: một bộ phim về bạo lực nơi công sở, người bị bắt nạt sau này đứng lên trả thù những kẻ đã bắt nạt mình."
      />
      <div className="auto-row">
        <label className="auto-field">
          <span>Thời lượng</span>
          <input
            type="number"
            min={15}
            max={3600}
            step={15}
            value={runtime ?? ""}
            placeholder="tự tính"
            onChange={(e) => setRuntime(e.target.value ? Number(e.target.value) : null)}
          />
          <span className="auto-field__unit">giây</span>
        </label>
        <button
          type="button"
          className="auto-btn auto-btn--primary"
          onClick={() => void run()}
          disabled={status === "running" || !script.trim()}
        >
          {status === "running" ? "đang phân tích…" : "Phân tích"}
        </button>
      </div>
      {status === "running" && (
        <p className="auto-hint">Đang dựng nhân vật, bối cảnh và shotlist — mất một lúc.</p>
      )}
      {error && <p className="auto-error">{error}</p>}
    </>
  );
}

export function AutoCharacterNode({ id, data, selected }: NodeProps) {
  const d = data as CharacterNodeData;
  const setActiveState = useAutomation((s) => s.setActiveState);
  const editPrompt = useAutomation((s) => s.editPrompt);
  const generate = useAutomation((s) => s.generate);

  const active = d.states[d.activeState];
  const state = d.character.states.find((st) => st.key === d.activeState);
  const identityReady = Boolean(d.identity.referenceUrl);

  return (
    <Shell
      title={d.character.name}
      badge={<span className="auto-tag">{d.character.role}</span>}
      variant="character"
      selected={selected}
    >
      <p className="auto-node__sub">{d.character.summary}</p>
      <p className="auto-anchor">
        <span>neo nhận dạng</span>
        {d.character.identity_anchor}
      </p>

      <PlatePanel
        plate={d.identity}
        label="Chân dung identity"
        onEdit={(t) => editPrompt(id, "identity", t)}
        onGenerate={() => void generate(id, "identity")}
      />

      {d.character.states.length > 0 && (
        <>
          <div className="auto-tabs" role="tablist">
            {d.character.states.map((st) => (
              <button
                key={st.key}
                type="button"
                role="tab"
                aria-selected={st.key === d.activeState}
                className={`auto-tab${st.key === d.activeState ? " auto-tab--on" : ""}`}
                onClick={() => setActiveState(id, st.key)}
              >
                {st.label}
                {d.states[st.key]?.image && <i className="auto-dot" aria-hidden="true" />}
              </button>
            ))}
          </div>

          {active && state && (
            <PlatePanel
              plate={active}
              label={`Sheet — ${state?.label || state?.key || d.activeState || "look"}`}
              disabled={!identityReady}
              hint={
                identityReady
                  ? undefined
                  : "Gen chân dung identity trước — sheet này thừa kế khuôn mặt từ đó."
              }
              onEdit={(t) => editPrompt(id, d.activeState, t)}
              onGenerate={() => void generate(id, d.activeState)}
            />
          )}
        </>
      )}
    </Shell>
  );
}

export function AutoEnvironmentNode({ id, data, selected }: NodeProps) {
  const d = data as EnvironmentNodeData;
  const editPrompt = useAutomation((s) => s.editPrompt);
  const generate = useAutomation((s) => s.generate);

  return (
    <Shell
      title={d.environment.name}
      badge={<span className="auto-tag">bối cảnh</span>}
      variant="environment"
      selected={selected}
    >
      <p className="auto-node__sub">{d.environment.summary}</p>
      {d.environment.lock && (
        <p className="auto-anchor auto-anchor--lock">
          <span>khoá</span>
          {d.environment.lock}
        </p>
      )}
      <PlatePanel
        plate={d.plate}
        label="Plate"
        onEdit={(t) => editPrompt(id, "plate", t)}
        onGenerate={() => void generate(id, "plate")}
      />
    </Shell>
  );
}

export function AutoAssetNode({ id, data, selected }: NodeProps) {
  const d = data as AssetNodeData;
  const editPrompt = useAutomation((s) => s.editPrompt);
  const generate = useAutomation((s) => s.generate);
  return (
    <Shell title={d.asset.name} badge={<span className="auto-tag">{d.asset.kind === "prop" ? "đạo cụ" : "đám đông"}</span>}
      variant="environment" selected={selected}>
      <p className="auto-node__sub">{d.asset.description || d.asset.summary}</p>
      <PlatePanel plate={d.plate} label="Reference" onEdit={(text) => editPrompt(id, "plate", text)}
        onGenerate={() => void generate(id, "plate")} />
    </Shell>
  );
}

export function AutoSequenceNode({ data, selected }: NodeProps) {
  const d = data as SequenceNodeData;
  const cutSequence = useAutomation((s) => s.cutSequence);
  const [open, setOpen] = useState(false);

  const seq = d.sequence;
  const running = d.cutStatus === "running";
  const cutTotal = d.shots.reduce((sum, s) => sum + (s.duration_s || 0), 0);
  // A cut that does not add up to the sequence is a real problem downstream —
  // the clip is generated at the sequence's length, so a short cut leaves
  // dead air and a long one gets truncated.
  const drift = d.shots.length ? cutTotal - seq.duration_s : 0;

  return (
    <Shell
      title={`${seq.label} — ${seq.title}`}
      badge={<span className="auto-tag">{seq.duration_s.toFixed(0)}s</span>}
      variant="sequence"
      selected={selected}
    >
      <p className="auto-node__sub">{seq.beat || seq.summary}</p>
      {seq.source_range && (
        <p className="auto-anchor">
          <span>video mẫu · cắt đúng như bản gốc</span>
          {seq.source_range}
        </p>
      )}

      <div className="auto-plate__bar">
        <button
          type="button"
          className="auto-plate__toggle"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          disabled={!d.shots.length}
        >
          {d.shots.length ? (open ? "▾" : "▸") : "·"}{" "}
          {d.shots.length ? `${d.shots.length} shot · ${cutTotal.toFixed(1)}s` : "chưa cắt shot"}
        </button>
        <button
          type="button"
          className="auto-btn auto-btn--go"
          onClick={() => void cutSequence(seq.key)}
          disabled={running}
        >
          {running ? "đang cắt…" : d.shots.length ? "cắt lại" : "cắt shot"}
        </button>
      </div>

      {(d.functionOf?.length || d.raccord?.length) && open ? (
        <>
          {d.functionOf?.length ? (
            <p className="auto-anchor">
              <span>phải establish</span>
              {d.functionOf.join(" · ")}
            </p>
          ) : null}
          {d.raccord?.length ? (
            <p className="auto-anchor auto-anchor--lock">
              <span>khoá raccord</span>
              {d.raccord.join(" · ")}
            </p>
          ) : null}
        </>
      ) : null}
      {d.exitState && (
        <p className="auto-anchor">
          <span>kết ở đây · sequence sau mở từ đây</span>
          {d.exitState}
        </p>
      )}
      {Math.abs(drift) > 0.5 && (
        <p className="auto-hint auto-hint--warn">
          Tổng shot lệch {drift > 0 ? "+" : ""}
          {drift.toFixed(1)}s so với sequence.
        </p>
      )}
      {d.cutError && <p className="auto-error">{d.cutError}</p>}

      {open && (
        <div className="auto-shots nowheel">
          {d.shots.map((shot) => (
            <article key={shot.n} className="auto-shot">
              <div className="auto-shot__meta">
                <span className="auto-shot__n">{String(shot.n).padStart(2, "0")}</span>
                <span className="auto-shot__dur">
                  {shot.duration_s < 1 ? shot.duration_s.toFixed(2) : shot.duration_s.toFixed(1)}s
                </span>
                {shot.source_tc && <span className="auto-shot__dur">gốc #{shot.source_shot} {shot.source_tc}</span>}
                <span className="auto-shot__spec">
                  {shot.framing} · {shot.lens_mm}mm
                </span>
              </div>
              {shot.title && <p className="auto-shot__title">{shot.title}</p>}
              {shot.camera && <p className="auto-shot__cam">{shot.camera}</p>}
              {shot.framing_note && (
                <p className="auto-shot__sfx">
                  <span className="auto-shot__tag">KHUNG</span>
                  {shot.framing_note}
                </p>
              )}
              {shot.lighting && (
                <p className="auto-shot__sfx">
                  <span className="auto-shot__tag">SÁNG</span>
                  {shot.lighting}
                </p>
              )}
              {asLines(shot.action).map((beat, i) => (
                <p key={i} className="auto-shot__action">
                  {beat}
                </p>
              ))}
              {shot.dialogue?.map((line, i) => (
                <p key={i} className="auto-shot__line">
                  <b>{line.who}</b>
                  {line.line}
                </p>
              ))}
              {asLines(shot.performance).length > 0 && (
                <p className="auto-shot__sfx">
                  <span className="auto-shot__tag">DIỄN</span>
                  {asLines(shot.performance).join(" · ")}
                </p>
              )}
              {/* The one field worth colouring: naming the wrong take is what
                  keeps the model off it. */}
              {asLines(shot.avoid).length > 0 && (
                <p className="auto-shot__avoid">
                  <span className="auto-shot__tag">TRÁNH</span>
                  {asLines(shot.avoid).join(" · ")}
                </p>
              )}
              {asLines(shot.sfx).length > 0 && (
                <p className="auto-shot__sfx">
                  <span className="auto-shot__tag">SFX</span>
                  {asLines(shot.sfx).join(" · ")}
                </p>
              )}
              {shot.edit_note && (
                <p className="auto-shot__sfx">
                  <span className="auto-shot__tag">DỰNG</span>
                  {shot.edit_note}
                </p>
              )}
            </article>
          ))}
        </div>
      )}
    </Shell>
  );
}

export function AutoVideoNode({ data, selected }: NodeProps) {
  const d = data as VideoNodeData;
  const primeVideoPrompt = useAutomation((s) => s.primeVideoPrompt);
  const generateClip = useAutomation((s) => s.generateClip);
  const kyc = useAutomation((s) => s.kyc);
  const authored = useAutomation((s) => s.sourceVerification?.method === "authored_script");
  const sourceReady = useAutomation((s) => {
    const sequence = s.nodes.find((node) => node.id === `seq:${d.sequenceKey}`);
    return sequence?.data.kind === "sequence"
      && sourceReadyForShots(sequence.data.shots, s.sourceVerification);
  });
  const strict = useAutomation((s) => isStrictBoard(s.productionAssets, s.sourceVerification));
  const stale = useAutomation((s) => {
    // No fingerprint: a prompt from before fingerprints, or one placed by hand.
    // Only the strict contract calls that stale; elsewhere it is simply kept.
    if (!d.inputFingerprint) return isStrictBoard(s.productionAssets, s.sourceVerification) && Boolean(d.prompt);
    try { return !sameFingerprint(d.inputFingerprint, s.currentFingerprint(d.sequenceKey)); }
    catch { return true; }
  });
  const [open, setOpen] = useState(false);

  const patchNode = useAutomation((s) => s.patchNode);
  const previousClip = useAutomation((s) => {
    const ordered = s.nodes
      .filter((n) => n.data.kind === "sequence")
      .sort((a, b) => a.position.y - b.position.y)
      .map((n) => (n.data.kind === "sequence" ? n.data.sequence.key : ""));
    const at = ordered.indexOf(d.sequenceKey);
    if (at <= 0) return undefined;
    const prev = s.nodes.find((n) => n.id === `vid:${ordered[at - 1]}`);
    return prev?.data.kind === "video" ? prev.data : undefined;
  });

  const running = d.status === "running";
  const keyframed = Boolean(d.startFrame?.referenceUrl);
  const chained = Boolean(d.chainFromPrevious);
  const ready = (chained && Boolean(previousClip?.clipUrl)) || keyframed || d.refs.length > 0;

  return (
    <Shell
      title={`${d.label} — ${d.title}`}
      badge={
        <>
          <span className="auto-tag">{d.durationS}s</span>
          {d.promptBy && (
            <span className="auto-tag" title="Ai viết prompt này">
              {d.promptBy === "template" ? "template" : d.promptBy}
            </span>
          )}
          {keyframed && (
            <span
              className="auto-tag auto-tag--ok"
              title="Clip chạy từ frame đầu tới frame cuối đã gen. Chế độ này khoá hai đầu nên clip nối nhau khớp hơn — đổi lại Seedance không nhận thêm ref ảnh trong cùng lệnh."
            >
              khoá 2 đầu
            </span>
          )}
        </>
      }
      variant="video"
      selected={selected}
      outbound={false}
    >
      {/* Position is the binding, so show it: the reader can check that
          @image1 really is the character the prompt says it is. */}
      {d.refs.length > 0 ? (
        <div className="auto-refs">
          {d.refs.map((r) => {
            // Under KYC every ref must have a media row: the identity assets
            // are built from those, in order, and one missing entry shifts
            // every @imageN after it onto the wrong picture.
            const blocked = kyc && !r.mediaId;
            return (
              <span
                key={r.label}
                className={`auto-ref${blocked ? " auto-ref--off" : ""}`}
                title={blocked ? "KYC bật nhưng plate này chưa có media row — gen lại nó." : undefined}
              >
                <b>{r.label}</b>
                {r.name}
              </span>
            );
          })}
        </div>
      ) : (
        <p className="auto-node__sub">
          Chưa có ref. Gen sheet nhân vật và plate bối cảnh của sequence này trước —
          Seedance kéo ref qua URL nên ảnh phải lên được R2.
        </p>
      )}

      {previousClip && !strict && (
        <label
          className="auto-check"
          title="Clip này chạy tiếp từ clip trước (Seedance 'extend'): nhân vật, ánh sáng và màu được kế thừa từ video chứ không dựng lại từ ref. Đây là cách duy nhất giữ được người thật xuyên suốt — nhưng khung hình sẽ theo clip trước."
        >
          <input
            type="checkbox"
            checked={chained}
            disabled={running}
            onChange={(e) =>
              patchNode(`vid:${d.sequenceKey}`, {
                chainFromPrevious: e.target.checked,
              } as Partial<VideoNodeData>)
            }
          />
          <span>
            Nối tiếp {previousClip.label}
            {previousClip.clipUrl ? "" : " (clip đó chưa gen)"}
          </span>
        </label>
      )}

      {!strict && !chained && <KeyframeStrip data={d} />}
      {strict && (chained || keyframed) && (
        <div className="auto-node__sub">
          <p>Kiểm tra coverage áp dụng cho các ảnh reference. Chuyển clip này sang chế độ reference để gửi đủ ảnh đã kiểm tra.</p>
          <button type="button" className="auto-btn" disabled={running} onClick={() => patchNode(`vid:${d.sequenceKey}`, {
            chainFromPrevious: false, startFrame: undefined, endFrame: undefined,
          } as Partial<VideoNodeData>)}>Dùng ảnh reference</button>
        </div>
      )}

      <div className="auto-plate__bar">
        <button
          type="button"
          className="auto-plate__toggle"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          disabled={!d.prompt}
        >
          {d.prompt ? (open ? "▾" : "▸") : "·"} Prompt
        </button>
        <button
          type="button"
          className="auto-btn auto-btn--go"
          onClick={() => void primeVideoPrompt(d.sequenceKey, { writer: true }).catch(() => undefined)}
          disabled={running}
          title="GPT viết lại prompt theo chuẩn clip prompt, nối từ trạng thái cuối của clip trước — vài cent, chưa gen gì."
        >
          viết prompt
        </button>
        <button
          type="button"
          className="auto-btn auto-btn--go"
          onClick={() => void generateClip(d.sequenceKey)}
          disabled={running || !ready}
          title={ready ? undefined : "Cần ít nhất một ref đã lên R2."}
        >
          {running
            ? "đang gen…"
            : d.clipUrl
              ? "gen lại"
              : chained
                ? "gen nối tiếp"
                : keyframed
                  ? "gen video (2 đầu)"
                  : "gen video"}
        </button>
      </div>

      {open && d.prompt && (
        <textarea
          className="auto-textarea auto-textarea--prompt nodrag nowheel"
          value={d.prompt}
          readOnly
          rows={10}
          spellCheck={false}
        />
      )}

      {d.error && <p className="auto-error">{d.error}</p>}
      {strict && (
        <div className="auto-node__sub" role="status">
          <p>{authored ? `Kịch bản sáng tác · ${sourceReady ? "đã khóa dữ liệu dựng" : "cần khóa dữ liệu dựng"}`
            : `Agent 1 · ${sourceReady ? "các shot của clip đã đối chiếu" : "các shot của clip cần đối chiếu"}`}</p>
          <p>Agent 2 · {stale ? "dữ liệu đã đổi — cần viết lại" : d.coverage?.status === "verified" ? "prompt đã kiểm tra đủ nội dung" : "prompt chưa xác minh"}
            {d.coverage?.requirements && ` · ${d.coverage.matches?.length ?? 0}/${d.coverage.requirements.length} mục có dẫn chứng`}</p>
          {d.coverage?.semantic_review?.findings?.map((finding, i) => <p key={i}>{finding.message || finding.code}</p>)}
        </div>
      )}
      {d.warnings?.map((w, i) => (
        <p key={i} className="auto-hint">{w}</p>
      ))}
      {d.clipUrl && (
        <>
          <video className="auto-clip" src={d.clipUrl} controls preload="metadata" />
          {d.persisted === false && (
            <p className="auto-hint">
              Link tạm của nhà cung cấp — sẽ hết hạn. Tải về nếu muốn giữ.
            </p>
          )}
        </>
      )}
    </Shell>
  );
}

/** The two pictures a clip is pinned to.
 *
 *  Generated from the same cast sheets and location plate as everything else,
 *  so consecutive clips start and end on faces that match. The end frame of a
 *  clip and the start frame of the next are different camera setups — they sit
 *  either side of a cut — so each clip owns both of its own. */
function KeyframeStrip({ data }: { data: VideoNodeData }) {
  const generateKeyframe = useAutomation((s) => s.generateKeyframe);
  const slots: { which: "start" | "end"; label: string; plate?: Plate }[] = [
    { which: "start", label: "Frame đầu", plate: data.startFrame },
    { which: "end", label: "Frame cuối", plate: data.endFrame },
  ];

  return (
    <div className="auto-keyframes">
      {slots.map(({ which, label, plate }) => {
        const busy = plate?.status === "running";
        return (
          <div key={which} className="auto-keyframe">
            {plate?.image ? (
              <a href={plate.image} target="_blank" rel="noreferrer" className="auto-keyframe__img">
                <img src={plate.image} alt={label} />
              </a>
            ) : (
              <div className="auto-keyframe__empty">{label}</div>
            )}
            <button
              type="button"
              className="auto-btn auto-btn--go"
              disabled={busy}
              title="Gen tấm ảnh này từ sheet nhân vật + plate bối cảnh của clip."
              onClick={() => void generateKeyframe(data.sequenceKey, which).catch(() => undefined)}
            >
              {busy ? "…" : plate?.image ? `${label} ↻` : label}
            </button>
            {plate?.error && <p className="auto-error">{plate.error}</p>}
          </div>
        );
      })}
    </div>
  );
}

export const automationNodeTypes = {
  autoScript: AutoScriptNode,
  autoCharacter: AutoCharacterNode,
  autoEnvironment: AutoEnvironmentNode,
  autoAsset: AutoAssetNode,
  autoSequence: AutoSequenceNode,
  autoVideo: AutoVideoNode,
};

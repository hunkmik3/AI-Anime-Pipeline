/**
 * Nodes for the /automation board.
 *
 * Kept in one file on purpose: premise → cast/places → shotlist → clips only
 * make sense as one chain, and splitting them across five files would cost
 * more in navigation than it buys in tidiness. They also do not share the
 * shotWorkflow node contract, so they cannot reuse BaseNodeShell.
 */
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { VideoSourceInput } from "./VideoSourceInput";

import {
  useAutomation,
  videoDisplayRefs,
  type CharacterNodeData,
  type EnvironmentNodeData,
  type AssetNodeData,
  asLines,
  type Plate,
  type SequenceNodeData,
  type VideoNodeData,
} from "../store/automation";
import { isStrictBoard, sameFingerprint, sourceReadyForShots } from "./contracts";

// The clip workspace reuses the same editors and actions outside React Flow.
export const EmbeddedAutomationNodes = createContext(false);
type NodeViewProps = Pick<NodeProps, "id" | "data" | "selected">;

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
  const embedded = useContext(EmbeddedAutomationNodes);
  return (
    <div className={`auto-node auto-node--${variant}${selected ? " auto-node--selected" : ""}`}>
      {!embedded && inbound && <Handle type="target" position={Position.Left} className="auto-handle" />}
      <header className="auto-node__head">
        <span className="auto-node__title">{title}</span>
        {badge}
      </header>
      {children}
      {!embedded && outbound && <Handle type="source" position={Position.Right} className="auto-handle" />}
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
  onUpload,
}: {
  plate: Plate;
  label: string;
  hint?: string;
  disabled?: boolean;
  onEdit(text: string): void;
  onGenerate(): void;
  onUpload(file: File): Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const running = plate.status === "running";

  return (
    <div className="auto-plate">
      <div className="auto-plate__bar">
        <button
          type="button"
          className="auto-plate__toggle nodrag"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? "▾" : "▸"} {label} · prompt
        </button>
        {plate.referenceUrl && <span className="auto-tag auto-tag--ok">{plate.uploaded ? "ảnh upload" : "đã lưu"}</span>}
        <label className={`auto-btn auto-btn--file nodrag${uploading || running ? " va-disabled" : ""}`}>
          {uploading ? "Đang upload…" : plate.image || plate.referenceUrl ? "Thay ảnh" : "Upload ảnh"}
          <input type="file" accept="image/png,image/jpeg,image/webp" aria-label={`Upload ảnh ${label}`}
            disabled={uploading || running} onChange={async event => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (!file) return;
              setUploadError(""); setUploading(true);
              try { await onUpload(file); }
              catch (error) { setUploadError((error as Error).message); }
              finally { setUploading(false); }
            }} />
        </label>
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
          className="auto-btn auto-btn--go nodrag"
          onClick={onGenerate}
          disabled={running || uploading || disabled || !plate.prompt?.trim()}
          title={hint}
        >
          {running ? "đang gen…" : plate.image ? "gen lại" : "gen"}
        </button>
      </div>

      {open && (
        <textarea
          className="auto-textarea auto-textarea--prompt nodrag nowheel"
          aria-label={`Prompt ${label}`}
          value={plate.prompt ?? ""}
          onChange={(e) => onEdit(e.target.value)}
          rows={7}
          spellCheck={false}
          placeholder="Prompt sẽ tự điền sau khi phân tích xong."
        />
      )}

      {uploadError && <p className="auto-error" role="alert">{uploadError}</p>}
      {plate.error && <p className="auto-error">{plate.error}</p>}
      {(plate.referenceUrl || plate.image) && (
        <a href={plate.referenceUrl || plate.image} download={`${label}.png`} className="auto-plate__img nodrag">
          <img src={plate.referenceUrl || plate.image} alt={label} />
        </a>
      )}
    </div>
  );
}

// Keep the persisted node type compatible with existing boards; the entry UI
// now accepts source video only.
export function AutoScriptNode({ selected }: NodeViewProps) {
  return <Shell title="Video nguồn" variant="script" selected={selected} inbound={false}
    badge={<span className="source-input__badge">Bắt đầu tại đây</span>}>
    <VideoSourceInput />
  </Shell>;
}

export function AutoCharacterNode({ id, data, selected }: NodeViewProps) {
  const d = data as CharacterNodeData;
  const setActiveState = useAutomation((s) => s.setActiveState);
  const editPrompt = useAutomation((s) => s.editPrompt);
  const generate = useAutomation((s) => s.generate);

  const active = d.states?.[d.activeState];
  const state = d.character.states?.find((st) => st.key === d.activeState);
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
        onUpload={file => useAutomation.getState().uploadPlate(id, "identity", file)}
      />

      {(d.character.states?.length ?? 0) > 0 && (
        <>
          <div className="auto-tabs nodrag" role="tablist">
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
                {d.states?.[st.key]?.image && <i className="auto-dot" aria-hidden="true" />}
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
              onUpload={file => useAutomation.getState().uploadPlate(id, d.activeState, file)}
            />
          )}
        </>
      )}
    </Shell>
  );
}

export function AutoEnvironmentNode({ id, data, selected }: NodeViewProps) {
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
        onUpload={file => useAutomation.getState().uploadPlate(id, "plate", file)}
      />
    </Shell>
  );
}

export function AutoAssetNode({ id, data, selected }: NodeViewProps) {
  const d = data as AssetNodeData;
  const editPrompt = useAutomation((s) => s.editPrompt);
  const generate = useAutomation((s) => s.generate);
  return (
    <Shell title={d.asset.name} badge={<span className="auto-tag">{d.asset.kind === "prop" ? "đạo cụ" : "đám đông"}</span>}
      variant="environment" selected={selected}>
      <p className="auto-node__sub">{d.asset.description || d.asset.summary}</p>
      <PlatePanel plate={d.plate} label="Reference" onEdit={(text) => editPrompt(id, "plate", text)}
        onGenerate={() => void generate(id, "plate")}
        onUpload={file => useAutomation.getState().uploadPlate(id, "plate", file)} />
    </Shell>
  );
}

export function AutoSequenceNode({ data, selected }: NodeViewProps) {
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
          className="auto-plate__toggle nodrag"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          disabled={!d.shots.length}
        >
          {d.shots.length ? (open ? "▾" : "▸") : "·"}{" "}
          {d.shots.length ? `${d.shots.length} shot · ${cutTotal.toFixed(1)}s` : "chưa cắt shot"}
        </button>
        <button
          type="button"
          className="auto-btn auto-btn--go nodrag"
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

export function AutoVideoNode({ data, selected }: NodeViewProps) {
  const d = data as VideoNodeData;
  const referenceNodes = useAutomation((s) => s.nodes);
  const referenceAssets = useAutomation((s) => s.productionAssets);
  const collectVideoRefs = useAutomation((s) => s.collectVideoRefs);
  // Zustand snapshots must keep stable identity between store changes. Resolve
  // fresh display objects outside the selector to avoid a render loop.
  const refs = useMemo(() => {
    if (d.refs.length) return d.refs;
    try { return videoDisplayRefs(d, collectVideoRefs(d.sequenceKey).refs); }
    catch { return d.refs; }
  }, [d, referenceNodes, referenceAssets, collectVideoRefs]);
  const primeVideoPrompt = useAutomation((s) => s.primeVideoPrompt);
  const editVideoPrompt = useAutomation(s => s.editVideoPrompt);
  const [verifying, setVerifying] = useState(false);
  const generateClip = useAutomation((s) => s.generateClip);
  const kyc = useAutomation((s) => s.kyc);
  const authored = useAutomation((s) => s.sourceVerification?.method === "authored_script");
  const onePassSource = useAutomation((s) => s.sourceVerification?.method === "one_pass_production");
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
  // A server receipt without a browser fingerprint records the writing-time
  // review; absence of that local comparison is not evidence of changed inputs.
  const serverReviewedPrompt = !d.inputFingerprint && Boolean(d.prompt)
    && d.promptEngine === "cinematic-v1" && d.coverage?.status === "verified"
    && Boolean(d.contractDigest && d.coverageToken);
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
  const ready = (chained && Boolean(previousClip?.clipUrl)) || keyframed || refs.length > 0;

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
          {d.promptEngine === "cinematic-v1" && (
            <span className="auto-tag auto-tag--ok" title={`Đọc ${d.inspectedReferences?.length ?? 0} ảnh reference; viết cảnh, thoại và raccord theo cấu trúc đã chốt.`}>
              cinematic
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
      {refs.length > 0 ? (
        <div className="auto-refs nodrag">
          {refs.map((r) => {
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
          className="auto-check nodrag"
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
          <button type="button" className="auto-btn nodrag" disabled={running} onClick={() => patchNode(`vid:${d.sequenceKey}`, {
            chainFromPrevious: false, startFrame: undefined, endFrame: undefined,
          } as Partial<VideoNodeData>)}>Dùng ảnh reference</button>
        </div>
      )}

      <div className="auto-plate__bar">
        <button
          type="button"
          className="auto-plate__toggle nodrag"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? "▾" : "▸"} Sửa prompt
        </button>
        <button
          type="button"
          className="auto-btn auto-btn--go nodrag"
          onClick={() => void primeVideoPrompt(d.sequenceKey).catch(() => undefined)}
          disabled={running}
          title="GPT qua Avis đọc shotlist và ảnh reference, viết cảnh, thoại và raccord theo cấu trúc cinematic. Chỉ viết prompt, chưa gen video."
        >
          viết prompt
        </button>
        <button
          type="button"
          className="auto-btn auto-btn--go nodrag"
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

      {open && (
        <textarea
          className="auto-textarea auto-textarea--prompt nodrag nowheel"
          aria-label={`Prompt video ${d.label}`}
          value={d.prompt}
          onChange={event => editVideoPrompt(d.sequenceKey, event.target.value)}
          readOnly={running}
          rows={10}
          spellCheck={false}
        />
      )}

      {open && <p className="auto-hint">Prompt được lưu tự động. Bản tự chỉnh được giữ nguyên khi gen.</p>}
      {d.promptBy === "manual" && strict && <button type="button" className="auto-btn nodrag"
        disabled={running || verifying || !d.prompt.trim()} onClick={async () => {
          setVerifying(true);
          try { await useAutomation.getState().verifyVideoPrompt(d.sequenceKey); }
          catch (error) { patchNode(`vid:${d.sequenceKey}`, { error: (error as Error).message }); }
          finally { setVerifying(false); }
        }}>{verifying ? "Đang kiểm tra…" : "Kiểm tra prompt đã sửa"}</button>}
      {d.error && <p className="auto-error">{d.error}</p>}
      {open && Boolean(d.stagingDecisions?.length) && (
        <details className="auto-node__sub nodrag nowheel">
          <summary>Quyết định nối cảnh ({d.stagingDecisions!.length})</summary>
          <p>Chỉ dẫn dàn dựng để nối các trạng thái; không phải dữ kiện mới từ video gốc.</p>
          {d.stagingDecisions!.map((decision, i) => <p key={i}>
            <strong>Shot {decision.shot}:</strong> {decision.description}<br />{decision.basis}
          </p>)}
        </details>
      )}
      {strict && (
        <div className="auto-node__sub" role="status">
          <p>{authored ? `Kịch bản sáng tác · ${sourceReady ? "đã khóa dữ liệu dựng" : "cần khóa dữ liệu dựng"}`
            : onePassSource ? `Shotlist một lượt · ${sourceReady ? "đã chuẩn bị dữ liệu" : "cần xử lý dữ liệu"}`
            : `Agent 1 · ${sourceReady ? "các shot của clip đã đối chiếu" : "các shot của clip cần đối chiếu"}`}</p>
          <p>Agent 2 · {serverReviewedPrompt ? "prompt từ pipeline tự động · server đã kiểm tra đầu vào khi viết"
            : d.promptBy === "manual" && (!d.coverageToken || stale) ? "prompt tự chỉnh — sẽ kiểm tra trước khi gen"
            : stale ? "dữ liệu đã đổi — cần viết lại" : d.coverage?.status === "verified" ? "prompt đã kiểm tra đủ nội dung" : "prompt chưa xác minh"}
            {d.coverage?.requirements && ` · ${d.coverage.matches?.length ?? 0}/${d.coverage.requirements.length} mục có dẫn chứng`}</p>
          {d.coverage?.semantic_review?.findings?.map((finding, i) => <p key={i}>{finding.message || finding.code}</p>)}
        </div>
      )}
      {d.warnings?.map((w, i) => (
        <p key={i} className="auto-hint">{w}</p>
      ))}
      {d.clipUrl && (
        <>
          <video className="auto-clip nodrag" src={d.clipUrl} controls preload="metadata" />
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
              className="auto-btn auto-btn--go nodrag"
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

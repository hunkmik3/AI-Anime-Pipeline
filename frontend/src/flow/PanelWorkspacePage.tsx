import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  addPanelNote,
  addPanelVersions,
  generateForPanel,
  getPanel,
  getRequest,
  mediaUrl,
  resolvePanelNote,
  thumbUrl,
  type Panel,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import {
  FLOW_ASPECTS,
  FLOW_MODELS,
  FLOW_SIZES,
  modelMaxSize,
  modelProvider,
  type FlowAspect,
  type FlowSize,
} from "../store/flowStudio";
import { toast } from "../store/toast";

/**
 * One panel's workspace — where the artist actually works.
 *
 * This is the point of the whole refactor: the board that replaced Miro is not a
 * tracker you look at and then go elsewhere to generate. Original on the left,
 * results in the middle, the PM's notes on the right, and the composer bound to
 * THIS panel — its raw material is attached as the reference automatically,
 * because "match the original" is what every one of these generations is for.
 *
 * The panel imposes nothing on how you generate: model, size, aspect and variant
 * count are the artist's, exactly as in the free-form studio.
 */
export function PanelWorkspacePage() {
  const { panelId } = useParams();
  const pid = Number(panelId);
  const [panel, setPanel] = useState<Panel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [big, setBig] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const p = await getPanel(pid);
      setPanel(p);
      setBig((cur) => cur ?? p.latest_media_id ?? p.raw?.[0]?.media_id ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [pid]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) return <p className="inbox__err">{error}</p>;
  if (!panel) return <p className="rfoot">Loading…</p>;

  const locked = panel.status === "approved";

  return (
    <div className="shellpage pn__ws">
      <PageHeader
        crumb={<Link to={`/giantflow/${panel.project_id}`}>← All panels</Link>}
        title={panel.code}
        subtitle={`${STATUS_TEXT[panel.status]}${
          panel.assignee_name ? ` · ${panel.assignee_name}` : ""
        }`}
      />

      {locked ? (
        <div className="callout callout--ok">
          <b>Approved.</b> Generation is closed for this panel. A PM can reopen it
          if it needs more work.
        </div>
      ) : null}

      <div className="pn__ws-cols">
        {/* Original — pinned, because it is what the result is judged against. */}
        <aside className="pn__ws-raw">
          <h3 className="pn__ws-h">Raw material</h3>
          {(panel.raw ?? []).map((r) => (
            <button
              key={r.media_id}
              type="button"
              className={`pn__ws-thumb${big === r.media_id ? " is-on" : ""}`}
              onClick={() => setBig(r.media_id)}
            >
              <img src={thumbUrl(r.media_id, 320)} alt="" />
            </button>
          ))}
        </aside>

        <section className="pn__ws-main">
          <div className="pn__ws-stage">
            {big ? <img src={mediaUrl(big)} alt="" /> : <p className="rfoot">Nothing yet.</p>}
          </div>

          <div className="pn__ws-versions">
            {(panel.versions ?? []).length === 0 ? (
              <span className="pn__muted">No versions generated yet.</span>
            ) : (
              (panel.versions ?? []).map((v) => (
                <button
                  key={v.media_id}
                  type="button"
                  className={`pn__ws-thumb${big === v.media_id ? " is-on" : ""}`}
                  title={v.model_used ?? undefined}
                  onClick={() => setBig(v.media_id)}
                >
                  <img src={thumbUrl(v.media_id, 220)} alt="" />
                  <em>v{v.version}</em>
                </button>
              ))
            )}
          </div>

          <Composer panel={panel} disabled={locked} onDone={load} />
        </section>

        <aside className="pn__ws-notes">
          <h3 className="pn__ws-h">
            Notes
            {panel.unresolved_notes > 0 ? (
              <span className="pn__notes">{panel.unresolved_notes} open</span>
            ) : null}
          </h3>
          <NoteList panel={panel} onChanged={load} />
        </aside>
      </div>
    </div>
  );
}

const STATUS_TEXT: Record<Panel["status"], string> = {
  todo: "Not started",
  in_progress: "In progress",
  submitted: "In review",
  changes_requested: "Sent back for changes",
  approved: "Approved",
};

/** The engine, bound to this panel. Same controls as the studio composer. */
function Composer({
  panel,
  disabled,
  onDone,
}: {
  panel: Panel;
  disabled: boolean;
  onDone: () => Promise<void>;
}) {
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState(FLOW_MODELS[0].id);
  const [aspect, setAspect] = useState<FlowAspect>("9:16");
  const [size, setSize] = useState<FlowSize>("1K");
  const [count, setCount] = useState(1);
  const [preserve, setPreserve] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const cap = modelMaxSize(model);
  const sizes = FLOW_SIZES.filter(
    (s) => s === "1K" || cap === "4K" || (cap === "2K" && s === "2K"),
  );

  async function run() {
    const text = prompt.trim();
    if (!text) return;
    setBusy("Queuing…");
    try {
      const { request_id } = await generateForPanel(panel.id, {
        prompt: text,
        provider: modelProvider(model),
        image_model: model,
        aspect_ratio: aspect,
        image_size: size,
        variant_count: count,
        preserve_colors: preserve,
      });
      // Poll the shared request queue, the same way the studio does; the panel
      // takes custody of the result once it lands.
      let row = await getRequest(request_id);
      for (let i = 0; i < 400 && (row.status === "queued" || row.status === "running"); i++) {
        setBusy(row.status === "running" ? "Generating…" : "Waiting for a slot…");
        await new Promise((r) => setTimeout(r, i < 10 ? 800 : 2000));
        row = await getRequest(request_id);
      }
      if (row.status !== "done") throw new Error(row.error || "generation failed");
      const result = (row.result ?? {}) as Record<string, unknown>;
      const ids = (result.media_ids as string[] | undefined) ?? [];
      const one = result.media_id as string | undefined;
      const media = ids.length ? ids : one ? [one] : [];
      if (!media.length) throw new Error("no image came back");
      await addPanelVersions(panel.id, media, (result.image_model as string) ?? model);
      await onDone();
      toast(`${media.length} version(s) added.`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Generation failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="pn__composer">
      <textarea
        className="inbox__input"
        rows={2}
        placeholder={
          disabled
            ? "This panel is approved — reopen it to generate again."
            : "Describe the restyle… (the raw panel is attached as the reference automatically)"
        }
        value={prompt}
        disabled={disabled || !!busy}
        onChange={(e) => setPrompt(e.target.value)}
      />
      <div className="pn__composer-row">
        <select
          className="inbox__input pn__select"
          value={model}
          disabled={disabled || !!busy}
          onChange={(e) => {
            setModel(e.target.value);
            // Drop an unsupported size rather than silently sending one the model
            // ignores — 4K on a 2K model quietly returns 1K.
            const nextCap = modelMaxSize(e.target.value);
            if (size === "4K" && nextCap !== "4K") setSize("2K");
            if (size === "2K" && nextCap === "1K") setSize("1K");
          }}
        >
          {FLOW_MODELS.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
        <select
          className="inbox__input pn__select"
          value={aspect}
          disabled={disabled || !!busy}
          onChange={(e) => setAspect(e.target.value as FlowAspect)}
        >
          {FLOW_ASPECTS.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <select
          className="inbox__input pn__select"
          value={size}
          disabled={disabled || !!busy}
          onChange={(e) => setSize(e.target.value as FlowSize)}
        >
          {sizes.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          className="inbox__input pn__select"
          value={count}
          disabled={disabled || !!busy}
          onChange={(e) => setCount(Number(e.target.value))}
        >
          {[1, 2, 3, 4].map((n) => (
            <option key={n} value={n}>
              {n} image{n > 1 ? "s" : ""}
            </option>
          ))}
        </select>
        <label className="pn__check">
          <input
            type="checkbox"
            checked={preserve}
            disabled={disabled || !!busy}
            onChange={(e) => setPreserve(e.target.checked)}
          />
          Keep original colours
        </label>
        <button
          className="btn2 btn2--primary"
          disabled={disabled || !!busy || !prompt.trim()}
          onClick={() => void run()}
        >
          {busy ?? "Generate"}
        </button>
      </div>
    </div>
  );
}

function NoteList({ panel, onChanged }: { panel: Panel; onChanged: () => Promise<void> }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <>
      <ul className="pn__notelist">
        {(panel.notes ?? []).length === 0 ? (
          <li className="pn__muted">No notes yet.</li>
        ) : null}
        {(panel.notes ?? []).map((n) => (
          <li key={n.id} className={`pn__note${n.resolved ? " is-done" : ""}`}>
            <label>
              <input
                type="checkbox"
                checked={n.resolved}
                onChange={async (e) => {
                  try {
                    await resolvePanelNote(n.id, e.target.checked);
                    await onChanged();
                  } catch (err) {
                    toast(err instanceof Error ? err.message : "Failed");
                  }
                }}
              />
              <span>{n.body}</span>
            </label>
            <em>{n.author_name ?? ""}</em>
          </li>
        ))}
      </ul>
      <div className="pn__noteadd">
        <textarea
          className="inbox__input"
          rows={2}
          placeholder="Add a note…"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button
          className="btn2"
          disabled={busy || !text.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              await addPanelNote(panel.id, text.trim());
              setText("");
              await onChanged();
            } catch (e) {
              toast(e instanceof Error ? e.message : "Failed");
            } finally {
              setBusy(false);
            }
          }}
        >
          Add
        </button>
      </div>
    </>
  );
}

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  addPanelNote,
  resolvePanelNote,
  reviewPanel,
  reviewQueue,
  thumbUrl,
  type PanelEvent,
  type QueuePanel,
} from "../api/client";
import { relativeTime } from "../components/activity/activity-meta";
import { PageHeader } from "../components/shell/PageHeader";
import { useFlowStudioStore } from "../store/flowStudio";
import { useGiantflowRole } from "../store/giantflowRole";
import { FlowViewer } from "./FlowViewer";
import { GiantflowNav } from "./GiantflowNav";
import { toast } from "../store/toast";

/**
 * The PM's review queue — everything handed in, from every artist, in one place.
 *
 * A queue rather than a tree. Reviewing at this studio's scale means several
 * hundred panels arriving from several artists, and finding the ones waiting by
 * walking project → batch → panel is the shape of the Miro board this replaces,
 * not an improvement on it. The project tree is for organising work; this page
 * is for doing the reviewing.
 *
 * Each row is the pairing a verdict is actually made on — the original beside
 * the version the artist chose to submit, not their most recent attempt. The two
 * buttons are here, so a panel is judged and cleared without ever opening it;
 * the code links through for the times you need the whole history.
 */
export function PanelReviewPage() {
  const { can } = useGiantflowRole();
  const [rows, setRows] = useState<QueuePanel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [artist, setArtist] = useState<string>("all");

  const load = useCallback(async () => {
    try {
      setRows(await reviewQueue());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Switching the previewed role changes what the SERVER returns, so the page
  // has to ask again — otherwise you keep looking at the previous role's data.
  useEffect(() => {
    const onSwitch = () => void load();
    window.addEventListener("flowboard:view-as-changed", onSwitch);
    return () => window.removeEventListener("flowboard:view-as-changed", onSwitch);
  }, [load]);

  const all = rows ?? [];
  const artists = [...new Set(all.map((p) => p.assignee_name ?? "Unassigned"))].sort();
  const shown = artist === "all" ? all : all.filter((p) => (p.assignee_name ?? "Unassigned") === artist);

  // The page itself, not just its buttons: an artist who lands here by URL
  // should be told where their own work is, not shown a pile they cannot act on.
  if (!can("panel.review")) {
    return (
      <div className="shellpage pn__full">
        <GiantflowNav />
        <div className="inbox__empty">
          <b>Reviewing is the PM's job.</b>
          Your own panels, and what came back on them, are under My work.
        </div>
      </div>
    );
  }

  return (
    <div className="shellpage pn__full">
      <GiantflowNav />
      <PageHeader
        title="Review"
        subtitle={
          rows
            ? all.length === 0
              ? "Nothing waiting — every submitted panel has a verdict."
              : `${all.length} panel${all.length === 1 ? "" : "s"} waiting on you`
            : undefined
        }
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {rows === null ? <p className="rfoot">Loading…</p> : null}

      {all.length > 1 && artists.length > 1 ? (
        <div className="pn__filters">
          <div className="seg">
            <button
              className={`seg__btn${artist === "all" ? " is-on" : ""}`}
              onClick={() => setArtist("all")}
            >
              Everyone {all.length}
            </button>
            {artists.map((a) => (
              <button
                key={a}
                className={`seg__btn${artist === a ? " is-on" : ""}`}
                onClick={() => setArtist(a)}
              >
                {a} {all.filter((p) => (p.assignee_name ?? "Unassigned") === a).length}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {rows !== null && all.length === 0 ? (
        <div className="inbox__empty">
          <b>Nothing to review.</b>
          Submitted panels land here the moment an artist hands one in.
        </div>
      ) : null}

      <ul className="pn__queue">
        {shown.map((p) => (
          <ReviewRow key={p.id} panel={p} onDone={load} />
        ))}
      </ul>

      {/* Look only: this page is for ruling on work, not making it. */}
      <FlowViewer viewOnly />
    </div>
  );
}

function ReviewRow({ panel, onDone }: { panel: QueuePanel; onDone: () => Promise<void> }) {
  const [note, setNote] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const [busy, setBusy] = useState(false);
  const select = useFlowStudioStore((s) => s.select);

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      setNote("");
      await onDone();
      toast(ok);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  const open = panel.notes ?? [];
  const history = panel.history ?? [];
  // Who actually handed it over and when. `assignee_name` is whose BATCH it is,
  // which is usually but not always the same person, and never a time.
  const handover = [...history].reverse().find((e) => e.kind === "submitted");

  return (
    <li className="pn__qrow">
      {/* Original beside the submitted version — a verdict is a comparison, and
          it cannot be made from the result alone. Both open full size: a border
          that is "slightly off" is not judgeable at 200px. */}
      <div className="pn__qshots">
        <Shot mediaId={panel.raw_media_id} label="original" onOpen={select} />
        <Shot
          mediaId={panel.delivered_media_id}
          label={`v${panel.delivered_version}`}
          onOpen={select}
        />
      </div>

      <div className="pn__qmeta">
        <Link to={`/giantflow/panel/${panel.id}`} className="pn__qcode">
          {panel.code}
        </Link>
        <div className="pn__qsub">
          {panel.series_name} · {panel.batch_name}
        </div>
        <div className="pn__qwho">
          {handover ? (
            <>
              Submitted by <b>{handover.actor_name ?? "someone"}</b>
              {handover.created_at ? (
                <time dateTime={handover.created_at}
                      title={new Date(handover.created_at).toLocaleString()}>
                  {" · "}{relativeTime(handover.created_at)}
                </time>
              ) : null}
            </>
          ) : (
            <>Assigned to {panel.assignee_name ?? "nobody"}</>
          )}
        </div>

        {open.length > 0 ? (
          <ul className="pn__qnotes">
            {open.map((n) => (
              <li key={n.id}>
                <span>{n.body}</span>
                <button
                  type="button"
                  title="Mark as dealt with"
                  disabled={busy}
                  onClick={() => void run(() => resolvePanelNote(n.id, true), "Note cleared.")}
                >
                  ✓
                </button>
              </li>
            ))}
          </ul>
        ) : null}

        {history.length > 0 ? (
          <>
            <button
              type="button"
              className="pn__qhist-toggle"
              onClick={() => setShowHistory((v) => !v)}
            >
              {showHistory ? "▾" : "▸"} History ({history.length})
            </button>
            {showHistory ? <History events={history} /> : null}
          </>
        ) : null}
      </div>

      {/* ONE box. It used to be two — a standalone "+ Note" beside a separate
          send-back field — and which one you were typing in decided whether the
          panel came back, which is not something a text box should hide. Now the
          remark is written once and the button says what to do with it. */}
      <div className="pn__qacts">
        <textarea
          className="inbox__input pn__qnote-input"
          rows={3}
          placeholder="Add a note…"
          value={note}
          disabled={busy}
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="pn__qbtns">
          <button
            className="btn2 btn2--primary"
            disabled={busy}
            onClick={() => void run(() => reviewPanel(panel.id, true), `${panel.code} approved.`)}
          >
            ✓ Approve
          </button>
          <button
            className="btn2 btn2--danger"
            disabled={busy || !note.trim()}
            title={note.trim() ? "Send it back with this note" : "A send-back needs a reason"}
            onClick={() =>
              void run(
                () => reviewPanel(panel.id, false, [note.trim()]),
                `${panel.code} sent back.`,
              )
            }
          >
            ↩ Send back
          </button>
          <button
            className="btn2"
            disabled={busy || !note.trim()}
            title="Leave the remark without sending it back"
            onClick={() =>
              void run(() => addPanelNote(panel.id, note.trim()), "Note added.")
            }
          >
            Note only
          </button>
        </div>
      </div>
    </li>
  );
}

/** One picture, click to open full size in the studio viewer. */
function Shot({
  mediaId,
  label,
  onOpen,
}: {
  mediaId: string | null;
  label: string;
  onOpen: (id: string) => void;
}) {
  if (!mediaId) return <span className="pn__qshot" />;
  return (
    <button
      type="button"
      className="pn__qshot"
      title="Open full size — scroll to zoom, drag to pan"
      onClick={() => onOpen(mediaId)}
    >
      <img src={thumbUrl(mediaId, 520)} alt="" loading="lazy" />
      <em>{label}</em>
    </button>
  );
}

const EVENT_TEXT: Record<string, string> = {
  submitted: "submitted",
  approved: "approved",
  changes_requested: "sent back",
  reopened: "reopened",
  version_added: "new version",
};

/** The handovers, oldest first. Which remark answered which version is the one
 *  thing versions and notes cannot say between them. */
function History({ events }: { events: PanelEvent[] }) {
  return (
    <ol className="pn__qhist">
      {events.map((e) => (
        <li key={e.id} className={`is-${e.kind}`}>
          <b>{EVENT_TEXT[e.kind] ?? e.kind}</b>
          {e.actor_name ? <span> · {e.actor_name}</span> : null}
          {e.created_at ? (
            <time dateTime={e.created_at}>
              {new Date(e.created_at).toLocaleString()}
            </time>
          ) : null}
          {e.body ? <p>{e.body}</p> : null}
        </li>
      ))}
    </ol>
  );
}

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  myWork,
  resolvePanelNote,
  thumbUrl,
  type PanelEvent,
  type QueuePanel,
} from "../api/client";
import { relativeTime } from "../components/activity/activity-meta";
import { PageHeader } from "../components/shell/PageHeader";
import { useFlowStudioStore } from "../store/flowStudio";
import { FlowViewer } from "./FlowViewer";
import { GiantflowNav } from "./GiantflowNav";
import { toast } from "../store/toast";

/**
 * The artist's side of the handover: what came back, and why.
 *
 * The counterpart to the PM's queue. Until this existed the review loop was
 * broken in the middle — a PM could not send a panel back without giving a
 * reason, and the artist had nowhere to read the reason they were given. The
 * remark is the point of this page, so it is the first thing on the card rather
 * than something behind a click.
 *
 * Scoped by the batch's assignee, because that is where "who is doing this"
 * lives. Panels nobody has started are deliberately absent: they belong in the
 * batch grid, which is for picking up work. This page is for reacting to a
 * verdict.
 */
export function PanelMyWorkPage() {
  const [data, setData] = useState<{
    changes_requested: QueuePanel[];
    submitted: QueuePanel[];
    approved: QueuePanel[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await myWork());
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

  const back = data?.changes_requested ?? [];
  const waiting = data?.submitted ?? [];
  const done = data?.approved ?? [];
  const total = back.length + waiting.length + done.length;

  return (
    <div className="shellpage pn__full">
      <GiantflowNav />
      <PageHeader
        title="My work"
        subtitle={
          data
            ? back.length
              ? `${back.length} panel${back.length === 1 ? "" : "s"} came back — read the note, fix, submit again`
              : "Nothing to fix right now."
            : undefined
        }
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {data === null ? <p className="rfoot">Loading…</p> : null}

      {data !== null && total === 0 ? (
        <div className="inbox__empty">
          <b>Nothing here yet.</b>
          Panels appear once you submit one, or once a batch is assigned to you.
        </div>
      ) : null}

      <Section
        title="Sent back"
        hint="The PM's reason is on each card. Fix it, tick it off, then submit again."
        tone="back"
        panels={back}
        onChanged={load}
      />
      <Section title="Waiting on the PM" tone="wait" panels={waiting} onChanged={load} />
      <Section title="Approved" tone="done" panels={done} onChanged={load} />

      {/* Look only: this page is for reacting to a verdict, not making work. */}
      <FlowViewer viewOnly />
    </div>
  );
}

function Section({
  title,
  hint,
  tone,
  panels,
  onChanged,
}: {
  title: string;
  hint?: string;
  tone: "back" | "wait" | "done";
  panels: QueuePanel[];
  onChanged: () => Promise<void>;
}) {
  const select = useFlowStudioStore((s) => s.select);
  if (panels.length === 0) return null;
  return (
    <section className="pn__mysec">
      <h2 className={`pn__mysec-h is-${tone}`}>
        {title} <span>{panels.length}</span>
      </h2>
      {hint ? <p className="pn__mysec-hint">{hint}</p> : null}
      <ul className="pn__mygrid">
        {panels.map((p) => (
          <li key={p.id} className={`pn__mycard is-${tone}`}>
            {/* Click opens it full size. Reading "the border is off" and then
                having to leave the page to see the border is the whole problem. */}
            <button
              type="button"
              className="pn__mycard-img"
              title="Open full size — scroll to zoom, drag to pan"
              onClick={() => p.delivered_media_id && select(p.delivered_media_id)}
            >
              {p.delivered_media_id ? (
                <img src={thumbUrl(p.delivered_media_id, 400)} alt="" loading="lazy" />
              ) : null}
              <em>v{p.delivered_version}</em>
            </button>
            <div className="pn__mycard-body">
              <Link to={`/giantflow/panel/${p.id}`} className="pn__mycard-code">
                {p.code}
              </Link>
              <div className="pn__mycard-sub">
                {p.series_name} · {p.batch_name}
              </div>
              {/* The last thing that happened, and when — an artist opening this
                  page is asking "what changed and how long ago". */}
              {(() => {
                const last = [...(p.history ?? [])]
                  .reverse()
                  .find((e) =>
                    ["approved", "changes_requested", "submitted", "reopened"].includes(e.kind),
                  );
                if (!last) return null;
                return (
                  <div className="pn__mycard-when">
                    {VERB[last.kind] ?? last.kind}
                    {last.actor_name ? <> by <b>{last.actor_name}</b></> : null}
                    {last.created_at ? (
                      <time dateTime={last.created_at}
                            title={new Date(last.created_at).toLocaleString()}>
                        {" · "}{relativeTime(last.created_at)}
                      </time>
                    ) : null}
                  </div>
                );
              })()}
              {/* The whole reason this page exists. Ticking one off is the other
                  half: `unresolved_notes` drives the red badge on the batch card
                  and the project header, and with no way to clear a remark that
                  count could only ever climb. */}
              {(p.notes ?? []).map((n) => (
                <label key={n.id} className="pn__mycard-note">
                  <input
                    type="checkbox"
                    title="Mark this as fixed"
                    onChange={async (e) => {
                      try {
                        await resolvePanelNote(n.id, e.target.checked);
                        await onChanged();
                      } catch (err) {
                        toast(err instanceof Error ? err.message : "Failed");
                      }
                    }}
                  />
                  <span>
                    {n.body}
                    {n.author_name ? <em> — {n.author_name}</em> : null}
                  </span>
                </label>
              ))}
              {(p.history ?? []).length > 0 ? <MyHistory events={p.history ?? []} /> : null}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

const VERB: Record<string, string> = {
  submitted: "Submitted",
  approved: "Approved",
  changes_requested: "Sent back",
  reopened: "Reopened",
};

const EVENT_TEXT: Record<string, string> = {
  submitted: "submitted",
  approved: "approved",
  changes_requested: "sent back",
  reopened: "reopened",
  version_added: "new version",
};

/** The rounds this panel has been through, oldest first. */
function MyHistory({ events }: { events: PanelEvent[] }) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <button type="button" className="pn__qhist-toggle" onClick={() => setOpen(true)}>
        ▸ History ({events.length})
      </button>
    );
  }
  return (
    <>
      <button type="button" className="pn__qhist-toggle" onClick={() => setOpen(false)}>
        ▾ History ({events.length})
      </button>
      <ol className="pn__qhist">
        {events.map((e) => (
          <li key={e.id} className={`is-${e.kind}`}>
            <b>{EVENT_TEXT[e.kind] ?? e.kind}</b>
            {e.actor_name ? <span> · {e.actor_name}</span> : null}
            {e.created_at ? (
              <time dateTime={e.created_at}>{new Date(e.created_at).toLocaleString()}</time>
            ) : null}
            {e.body ? <p>{e.body}</p> : null}
          </li>
        ))}
      </ol>
    </>
  );
}


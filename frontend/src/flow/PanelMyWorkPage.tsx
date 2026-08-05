import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { myWork, thumbUrl, type QueuePanel } from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { GiantflowNav } from "./GiantflowNav";

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

  const back = data?.changes_requested ?? [];
  const waiting = data?.submitted ?? [];
  const done = data?.approved ?? [];
  const total = back.length + waiting.length + done.length;

  return (
    <div className="shellpage pn__wide">
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
        hint="The PM's reason is on each card. Fix it, then submit the new version."
        tone="back"
        panels={back}
      />
      <Section title="Waiting on the PM" tone="wait" panels={waiting} />
      <Section title="Approved" tone="done" panels={done} />
    </div>
  );
}

function Section({
  title,
  hint,
  tone,
  panels,
}: {
  title: string;
  hint?: string;
  tone: "back" | "wait" | "done";
  panels: QueuePanel[];
}) {
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
            <Link to={`/giantflow/panel/${p.id}`} className="pn__mycard-img">
              {p.delivered_media_id ? (
                <img src={thumbUrl(p.delivered_media_id, 400)} alt="" loading="lazy" />
              ) : null}
              <em>v{p.delivered_version}</em>
            </Link>
            <div className="pn__mycard-body">
              <Link to={`/giantflow/panel/${p.id}`} className="pn__mycard-code">
                {p.code}
              </Link>
              <div className="pn__mycard-sub">
                {p.project_name} · {p.batch_name}
              </div>
              {/* The whole reason this page exists. */}
              {(p.notes ?? []).map((n) => (
                <p key={n.id} className="pn__mycard-note">
                  {n.body}
                  {n.author_name ? <em> — {n.author_name}</em> : null}
                </p>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

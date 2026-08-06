import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  addPanelNote,
  resolvePanelNote,
  reviewPanel,
  reviewQueue,
  thumbUrl,
  type QueuePanel,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { useGiantflowRole } from "../store/giantflowRole";
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

  const all = rows ?? [];
  const artists = [...new Set(all.map((p) => p.assignee_name ?? "Unassigned"))].sort();
  const shown = artist === "all" ? all : all.filter((p) => (p.assignee_name ?? "Unassigned") === artist);

  // The page itself, not just its buttons: an artist who lands here by URL
  // should be told where their own work is, not shown a pile they cannot act on.
  if (!can("panel.review")) {
    return (
      <div className="shellpage pn__wide">
        <GiantflowNav />
        <div className="inbox__empty">
          <b>Reviewing is the PM's job.</b>
          Your own panels, and what came back on them, are under My work.
        </div>
      </div>
    );
  }

  return (
    <div className="shellpage pn__wide">
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
    </div>
  );
}

function ReviewRow({ panel, onDone }: { panel: QueuePanel; onDone: () => Promise<void> }) {
  const [note, setNote] = useState("");
  const [asking, setAsking] = useState(false);
  const [noting, setNoting] = useState(false);
  const [busy, setBusy] = useState(false);

  async function addNote() {
    setBusy(true);
    try {
      await addPanelNote(panel.id, note.trim());
      setNote("");
      setNoting(false);
      await onDone();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  async function verdict(approve: boolean, notes: string[] = []) {
    setBusy(true);
    try {
      await reviewPanel(panel.id, approve, notes);
      await onDone();
      toast(approve ? `${panel.code} approved.` : `${panel.code} sent back.`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed");
      setBusy(false);
    }
  }

  return (
    <li className="pn__qrow">
      {/* Original beside the submitted version — a verdict is a comparison, and
          it cannot be made from the result alone. */}
      <div className="pn__qshots">
        <span className="pn__qshot">
          {panel.raw_media_id ? (
            <img src={thumbUrl(panel.raw_media_id, 400)} alt="" loading="lazy" />
          ) : null}
          <em>original</em>
        </span>
        <span className="pn__qshot">
          {panel.delivered_media_id ? (
            <img src={thumbUrl(panel.delivered_media_id, 400)} alt="" loading="lazy" />
          ) : null}
          <em>v{panel.delivered_version}</em>
        </span>
      </div>

      <div className="pn__qmeta">
        <Link to={`/giantflow/panel/${panel.id}`} className="pn__qcode">
          {panel.code}
        </Link>
        <div className="pn__qsub">
          {panel.series_name} · {panel.batch_name}
        </div>
        <div className="pn__qwho">{panel.assignee_name ?? "Unassigned"}</div>
        {(panel.notes ?? []).length > 0 ? (
          <ul className="pn__qnotes">
            {(panel.notes ?? []).map((n) => (
              <li key={n.id}>
                {n.body}
                {/* A PM can retract their own remark; the artist ticks theirs off
                    from My work. Either way the open count has to be able to go
                    down, or the red badge it feeds is a ratchet. */}
                <button
                  type="button"
                  title="Mark as dealt with"
                  onClick={async () => {
                    try {
                      await resolvePanelNote(n.id, true);
                      await onDone();
                    } catch (e) {
                      toast(e instanceof Error ? e.message : "Failed");
                    }
                  }}
                >
                  ✓
                </button>
              </li>
            ))}
          </ul>
        ) : null}

        {/* Not every remark is a rejection. "Watch the colour on the next one"
            belongs on the panel without sending it back. */}
        {noting ? (
          <div className="pn__qnoteadd">
            <input
              className="inbox__input"
              autoFocus
              placeholder="Add a note (does not send it back)…"
              value={note}
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setNoting(false);
                if (e.key === "Enter" && note.trim()) void addNote();
              }}
            />
            <button className="btn2" disabled={busy || !note.trim()} onClick={() => void addNote()}>
              Add
            </button>
            <button className="btn2" disabled={busy} onClick={() => setNoting(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <button type="button" className="pn__qnotebtn" onClick={() => setNoting(true)}>
            ＋ Note
          </button>
        )}
      </div>

      <div className="pn__qacts">
        {asking ? (
          <>
            <textarea
              className="inbox__input"
              rows={2}
              autoFocus
              placeholder="What needs changing?"
              value={note}
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
            />
            <div className="pn__qbtns">
              <button
                className="btn2 btn2--danger"
                disabled={busy || !note.trim()}
                onClick={() => void verdict(false, [note.trim()])}
              >
                Send back
              </button>
              <button className="btn2" disabled={busy} onClick={() => setAsking(false)}>
                Cancel
              </button>
            </div>
          </>
        ) : (
          <div className="pn__qbtns">
            <button
              className="btn2 btn2--primary"
              disabled={busy}
              onClick={() => void verdict(true)}
            >
              ✓ Approve
            </button>
            {/* Sending back opens a box rather than doing it: the reason is
                required, and a button that fails after the click would be
                teaching the rule the hard way. */}
            <button className="btn2" disabled={busy} onClick={() => setAsking(true)}>
              Send back
            </button>
          </div>
        )}
      </div>
    </li>
  );
}

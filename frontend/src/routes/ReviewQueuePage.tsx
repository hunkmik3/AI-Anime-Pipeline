import { useCallback, useEffect, useState } from "react";
import {
  approveSubmission,
  listReviewQueue,
  rejectSubmission,
  type DeliverableEpisodeDTO,
  type SubmissionDTO,
} from "../api/client";
import { DeliveryCard } from "../components/DeliveryCard";
import { PageHeader } from "../components/shell/PageHeader";
import { useInboxStore } from "../store/inbox";
import { toast } from "../store/toast";

/**
 * Cuts waiting on this reviewer's verdict.
 *
 * Arranged by person, like Work — "what is waiting on me" is not something an
 * object page can answer.
 *
 * The player has to be here. The whole reason the app proxies Drive is so a cut can
 * be watched without leaving, without a Google account, and without the file ever
 * being shared outside the studio; sending the reviewer to Drive would undo all
 * three. So a row expands in place into the video and the two decisions, rather
 * than becoming a separate detail pane the way it used to — the old two-column
 * layout meant the queue and the thing you were judging competed for the width.
 *
 * Rejecting requires a reason: "sent back" with no note is a round-trip nobody can
 * act on.
 */

interface QueueItem {
  submission: SubmissionDTO;
  episode: DeliverableEpisodeDTO | null;
}

export function ReviewQueuePage() {
  const [items, setItems] = useState<QueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const refreshBadges = useInboxStore((s) => s.refresh);

  const load = useCallback(async () => {
    try {
      const r = await listReviewQueue();
      setItems(r.items);
      // Open the first one: with a queue, the next thing to do is the top of it.
      setOpenId((cur) =>
        cur && r.items.some((i) => i.submission.id === cur)
          ? cur
          : (r.items[0]?.submission.id ?? null),
      );
      void refreshBadges();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [refreshBadges]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="shellpage">
      <PageHeader
        title="Review"
        subtitle="Cuts waiting on your verdict. Watch it here, then approve it or send it back with a note saying what needs fixing."
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {items === null ? <p className="rfoot">Loading…</p> : null}

      {items !== null && items.length === 0 ? (
        <div className="inbox__empty">
          <b>Nothing waiting on you.</b>
          Submissions appear here when someone hands in an episode you review.
        </div>
      ) : null}

      <ul className="inbox">
        {(items ?? []).map(({ submission: s, episode: ep }) => (
          <DeliveryCard
            key={s.id}
            episode={ep}
            submission={s}
            tone="todo"
            status="awaiting your verdict"
            statusTone="warn"
            actions={
              <button
                className="btn2 btn2--ghost"
                onClick={() => setOpenId(openId === s.id ? null : s.id)}
              >
                {openId === s.id ? "Collapse" : "Review"}
              </button>
            }
          >
            {openId === s.id ? <ReviewPanel submission={s} onDone={load} /> : null}
          </DeliveryCard>
        ))}
      </ul>
    </div>
  );
}

function ReviewPanel({
  submission: s,
  onDone,
}: {
  submission: SubmissionDTO;
  onDone: () => Promise<void>;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [playError, setPlayError] = useState(false);

  async function decide(kind: "approve" | "reject") {
    if (busy) return;
    if (kind === "reject" && !note.trim()) {
      toast("Say what needs fixing before sending it back", "error");
      return;
    }
    setBusy(kind);
    try {
      if (kind === "approve") {
        await approveSubmission(s.id, note.trim() || undefined);
        toast("Approved.");
      } else {
        await rejectSubmission(s.id, note.trim());
        toast("Sent back to the assignee.");
      }
      await onDone();
    } catch (e) {
      toast(e instanceof Error ? e.message : "could not save the decision", "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="inbox__panel">
      {s.stream_url && !playError ? (
        <video
          className="inbox__player"
          src={s.stream_url}
          controls
          preload="metadata"
          onError={() => setPlayError(true)}
        />
      ) : (
        <p className="inbox__err">
          This cut can’t be played here.{" "}
          {s.drive_url ? (
            <a href={s.drive_url} target="_blank" rel="noreferrer">
              Open it in Drive
            </a>
          ) : null}{" "}
          — usually it means the file isn’t in the shared submissions folder, so the
          app’s Drive account can’t open it.
        </p>
      )}

      <div className="inbox__field">
        <span className="inbox__label">
          Note — required when sending back, optional when approving
        </span>
        <textarea
          className="inbox__input"
          rows={2}
          value={note}
          placeholder="e.g. lip sync drifts from 00:08, please regenerate SQ03"
          onChange={(e) => setNote(e.target.value)}
        />
      </div>

      <div className="inbox__actions">
        <button
          className="verdict verdict--approve"
          disabled={busy !== null}
          onClick={() => void decide("approve")}
        >
          {busy === "approve" ? "Approving…" : "Approve"}
        </button>
        <button
          className="verdict verdict--back"
          disabled={busy !== null}
          onClick={() => void decide("reject")}
        >
          {busy === "reject" ? "Sending back…" : "Send back"}
        </button>
      </div>
    </div>
  );
}

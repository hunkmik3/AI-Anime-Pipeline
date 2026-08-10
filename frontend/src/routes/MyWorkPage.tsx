import { useCallback, useEffect, useState } from "react";

import {
  listMySeries,
  submitSeries,
  type DeliverableSeriesDTO,
} from "../api/client";
import { DeliveryCard } from "../components/DeliveryCard";
import { PageHeader } from "../components/shell/PageHeader";
import { useInboxStore } from "../store/inbox";
import { toast } from "../store/toast";
import { StudioNav } from "../components/shell/StudioNav";

/**
 * What this person still has to hand in.
 *
 * Arranged by person rather than by object — "what do I have to do" is a real
 * question no object page can answer, which is why this and Review stay inboxes.
 *
 * A row is a SERIES, because a series is what gets handed in — one person takes
 * it, delivers one finished cut, and a PM signs off once. Rows were episodes until
 * then, so a twelve-episode series filled this page with twelve identical cards,
 * each repeating the same header and each offering to hand in a twelfth of the job.
 *
 * A row identifies the series, links into the work, and holds only the thing this
 * page exists for: handing the cut in. Grouped by whose move it is, so the top of
 * the page is always what is actually waiting on you.
 */

export function MyWorkPage() {
  const [series, setSeries] = useState<DeliverableSeriesDTO[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const refreshBadges = useInboxStore((s) => s.refresh);

  const load = useCallback(async () => {
    try {
      const r = await listMySeries();
      setSeries(r.series);
      // The nav badge and this page must not disagree about how much is waiting.
      void refreshBadges();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [refreshBadges]);

  useEffect(() => {
    void load();
  }, [load]);

  const status = (e: DeliverableSeriesDTO) => e.deliverable_status || "draft";
  const todo = (series ?? []).filter((e) => status(e) === "draft");
  const waiting = (series ?? []).filter((e) => status(e) === "submitted");
  const done = (series ?? []).filter((e) =>
    ["approved", "paid"].includes(status(e)),
  );

  return (
    <div className="shellpage">
      <StudioNav />
      <PageHeader
        title="Work"
        subtitle="Series handed to you. Generate the sequences on the canvas, edit the cut in your editor, upload it to Drive, then hand in one link for the whole series."
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {series === null ? <p className="rfoot">Loading…</p> : null}

      {series !== null && series.length === 0 ? (
        <div className="inbox__empty">
          <b>Nothing assigned to you yet.</b>
          Your PM hands over a whole series; it shows up here as soon as they do.
        </div>
      ) : null}

      {todo.length > 0 ? (
        <Group title={`Waiting on you (${todo.length})`}>
          {todo.map((ep) => (
            <Item
              key={ep.id}
              ep={ep}
              tone="todo"
              open={openId === ep.id}
              onToggle={() => setOpenId(openId === ep.id ? null : ep.id)}
              onDone={load}
            />
          ))}
        </Group>
      ) : null}

      {waiting.length > 0 ? (
        <Group title={`With the reviewer (${waiting.length})`}>
          {waiting.map((ep) => (
            <Item key={ep.id} ep={ep} tone="waiting" open={false} onDone={load} />
          ))}
        </Group>
      ) : null}

      {done.length > 0 ? (
        <Group title={`Delivered (${done.length})`}>
          {done.map((ep) => (
            <Item key={ep.id} ep={ep} tone="done" open={false} onDone={load} />
          ))}
        </Group>
      ) : null}
    </div>
  );
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rsection">
      <div className="rsection__head">
        <h3 className="rsection__title">{title}</h3>
      </div>
      <ul className="inbox">{children}</ul>
    </section>
  );
}

function Item({
  ep,
  tone,
  open,
  onToggle,
  onDone,
}: {
  ep: DeliverableSeriesDTO;
  tone: "todo" | "waiting" | "done";
  open: boolean;
  onToggle?: () => void;
  onDone: () => Promise<void>;
}) {
  const latest = ep.latest_submission;
  const sentBack = latest?.status === "rejected";
  const raw = ep.deliverable_status || "draft";
  const status =
    sentBack && raw === "draft" ? "sent back" : raw === "draft" ? "not handed in" : raw;
  const statusTone =
    status === "sent back"
      ? "bad"
      : raw === "submitted"
        ? "warn"
        : raw === "draft"
          ? "muted"
          : "good";

  return (
    <DeliveryCard
      series={ep}
      submission={latest}
      history={ep.submissions ?? []}
      tone={tone}
      status={status}
      statusTone={statusTone}
      actions={
        onToggle ? (
          <button className="btn2 btn2--primary" onClick={onToggle}>
            {open ? "Cancel" : sentBack ? "Hand in again" : "Hand in"}
          </button>
        ) : null
      }
    >
      {open ? <SubmitForm ep={ep} onDone={onDone} /> : null}
    </DeliveryCard>
  );
}

function SubmitForm({
  ep,
  onDone,
}: {
  ep: DeliverableSeriesDTO;
  onDone: () => Promise<void>;
}) {
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await submitSeries(ep.id, {
        drive_url: url.trim(),
        note: note.trim() || undefined,
      });
      toast("Handed in for review.");
      await onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div className="inbox__panel">
      {error ? <p className="inbox__err">{error}</p> : null}
      <div className="inbox__field">
        <span className="inbox__label">Drive link to the finished cut</span>
        <input
          className="inbox__input"
          value={url}
          placeholder="https://drive.google.com/file/d/…/view"
          onChange={(e) => setUrl(e.target.value)}
        />
        {/* The app plays the file through the studio's own Drive account, so it
            can stay Restricted — but it has to be in the shared folder, and that
            mistake is worth naming before it costs a round-trip. */}
        <p className="inbox__hint">
          The file can stay <b>Restricted</b> in Drive — the app plays it with the
          studio’s own account. It does need to sit in the shared submissions
          folder, or the reviewer can’t open it.
        </p>
      </div>
      <div className="inbox__field">
        <span className="inbox__label">Note to the reviewer (optional)</span>
        <textarea
          className="inbox__input"
          rows={2}
          value={note}
          placeholder="e.g. re-graded the night scene, swapped the SFX at 1:12"
          onChange={(e) => setNote(e.target.value)}
        />
      </div>
      <div className="inbox__actions">
        <button
          className="btn2 btn2--primary"
          disabled={busy || !url.trim()}
          onClick={() => void submit()}
        >
          {busy ? "Handing in…" : "Hand in for review"}
        </button>
      </div>
    </div>
  );
}

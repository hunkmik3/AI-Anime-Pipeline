import { useCallback, useEffect, useState } from "react";

import {
  listMyEpisodes,
  submitEpisode,
  type DeliverableEpisodeDTO,
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
 * Each row used to restate the episode inline (series, sequence count, status
 * chips, the whole submission history), so the same facts were maintained here, on
 * the episode page and in the review queue, and drifted apart. A row now identifies
 * the episode, links to it, and holds only the thing this page exists for: handing
 * the cut in. Grouped by whose move it is, so the top of the page is always the
 * work that is actually waiting on you.
 */

export function MyWorkPage() {
  const [episodes, setEpisodes] = useState<DeliverableEpisodeDTO[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const refreshBadges = useInboxStore((s) => s.refresh);

  const load = useCallback(async () => {
    try {
      const r = await listMyEpisodes();
      setEpisodes(r.episodes);
      // The nav badge and this page must not disagree about how much is waiting.
      void refreshBadges();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [refreshBadges]);

  useEffect(() => {
    void load();
  }, [load]);

  const status = (e: DeliverableEpisodeDTO) => e.deliverable_status || "draft";
  const todo = (episodes ?? []).filter((e) => status(e) === "draft");
  const waiting = (episodes ?? []).filter((e) => status(e) === "submitted");
  const done = (episodes ?? []).filter((e) =>
    ["approved", "paid"].includes(status(e)),
  );

  return (
    <div className="shellpage">
      <StudioNav />
      <PageHeader
        title="Work"
        subtitle="Episodes assigned to you. Generate the sequences on the canvas, edit the cut in your editor, upload it to Drive, then hand in the link here."
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {episodes === null ? <p className="rfoot">Loading…</p> : null}

      {episodes !== null && episodes.length === 0 ? (
        <div className="inbox__empty">
          <b>Nothing assigned to you yet.</b>
          Your PM assigns episodes; they show up here as soon as they do.
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
  ep: DeliverableEpisodeDTO;
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
      episode={ep}
      submission={latest}
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
  ep: DeliverableEpisodeDTO;
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
      await submitEpisode(ep.id, {
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

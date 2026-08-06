import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  createBatches,
  deleteBatch,
  importPanelFolder,
  listBatches,
  listPanelAssignees,
  listFlowMembers,
  getChapter,
  exportChapter,
  removeFlowMember,
  reorderBatches,
  setFlowMember,
  thumbUrl,
  updateBatch,
  type FlowMember,
  type PanelBatch,
  type FlowChapter,
} from "../api/client";
import { PersonPicker } from "../components/PersonPicker";
import { useGiantflowRole } from "../store/giantflowRole";
import { PanelHero, STAGES, sumCounts } from "./PanelHero";
import { toast } from "../store/toast";
import { useDragOrder } from "./useDragOrder";

/**
 * Inside a comic: its batches, one per artist.
 *
 * A batch owns its own imported folder, because the studio hands work out already
 * divided — artist X gets these panels, artist Y gets those. There is no
 * range-splitting step here for the same reason: there is never one big pile to
 * split.
 */
export function PanelBatchesPage() {
  const { chapterId } = useParams();
  const pid = Number(chapterId);
  const { can } = useGiantflowRole();
  const [batches, setBatches] = useState<PanelBatch[] | null>(null);
  const [chapter, setChapter] = useState<FlowChapter | null>(null);
  const [people, setPeople] = useState<{ user_id: string; name: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [showMembers, setShowMembers] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [rows, ch] = await Promise.all([listBatches(pid), getChapter(pid)]);
      setBatches(rows);
      setChapter(ch);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [pid]);

  useEffect(() => {
    void load();
    void listPanelAssignees().then(setPeople).catch(() => setPeople([]));
  }, [load]);

  const { list, dragProps } = useDragOrder(batches ?? [], async (ids) => {
    await reorderBatches(pid, ids);
    await load();
  });

  const rows = batches ?? [];
  const totals = rows.reduce(
    (a, b) => ({
      panels: a.panels + b.panel_count,
      notes: a.notes + b.open_notes,
    }),
    { panels: 0, notes: 0 },
  );
  // The project-wide spread, rolled up from its batches — no extra request, the
  // batch list already carries each one's counts.
  const counts = sumCounts(rows.map((b) => b.status_counts ?? {}));

  return (
    <div className="shellpage pn__wide">
      <PanelHero
        crumb={
          chapter ? (
            <Link to={`/giantflow/s/${chapter.series_id}`}>← Chapters</Link>
          ) : (
            <Link to="/giantflow">← Projects</Link>
          )
        }
        title={chapter?.name || "Chapter"}
        thumbMediaId={chapter?.thumb_media_id}
        counts={counts}
        total={totals.panels}
        facts={[
          `${rows.length} batch${rows.length === 1 ? "" : "es"}`,
          `${totals.panels} panel${totals.panels === 1 ? "" : "s"}`,
          ...(totals.notes ? [`${totals.notes} open note${totals.notes === 1 ? "" : "s"}`] : []),
        ]}
        actions={
          <>
            {/* Always shown, disabled until there is something to take. Hiding
                it until the first approval made the whole export feature
                invisible to anyone who had not seen it work already. */}
            <button
              className="btn2"
              disabled={!counts.approved}
              title={
                counts.approved
                  ? `Download ${counts.approved} approved panel(s) as a zip`
                  : "Nothing approved yet — approved panels are what gets exported"
              }
              onClick={async () => {
                  try {
                    const r = await exportChapter(pid);
                    toast(
                      `${r.written} approved panel(s) downloaded.` +
                        (r.skipped ? ` ${r.skipped} could not be read.` : ""),
                    );
                  } catch (e) {
                    toast(e instanceof Error ? e.message : "Export failed");
                  }
                }}
            >
              ↓ Export approved{counts.approved ? ` (${counts.approved})` : ""}
            </button>
          {can("batch.manage") ? (
            <>
            <button
              className="btn2"
              onClick={() => setShowMembers((v) => !v)}
            >
              People
            </button>
            <button
              className="btn2 btn2--primary"
              disabled={adding}
              onClick={() => setAdding(true)}
            >
              + Add batches
            </button>
            </>
          ) : null}
          </>
        }
      />

      {showMembers && chapter ? (
        // Membership lives on the SERIES; a chapter borrows its comic's people.
        <MembersPanel seriesId={chapter.series_id} people={people} />
      ) : null}

      {adding ? (
        <BatchDraftPanel
          people={people}
          busy={busy}
          onCancel={() => setAdding(false)}
          onCreate={async (rows) => {
            setBusy(true);
            try {
              const made = await createBatches(pid, rows);
              setAdding(false);
              await load();
              toast(`${made.length} batch${made.length === 1 ? "" : "es"} created.`);
            } catch (e) {
              toast(e instanceof Error ? e.message : "Failed");
            } finally {
              setBusy(false);
            }
          }}
        />
      ) : null}

      {error ? <p className="inbox__err">{error}</p> : null}
      {batches === null ? <p className="rfoot">Loading…</p> : null}

      {batches !== null && batches.length === 0 ? (
        <div className="inbox__empty">
          <b>No batches yet.</b>
          Create one per artist, then import that artist's folder of panels into it.
        </div>
      ) : null}

      <ul className="pn__batches">
        {list.map((b) => (
          <BatchCard
            key={b.id}
            batch={b}
            people={people}
            onChanged={load}
            drag={can("batch.manage") ? dragProps(b.id) : {}}
            manage={can("batch.manage")}
            canImport={can("batch.import")}
          />
        ))}
      </ul>
    </div>
  );
}

/**
 * Draft several batches, then create them together.
 *
 * A comic is divided among its artists in one sitting, so the form holds the
 * whole division: a row per batch, each with the two things a batch has — a name
 * and one artist. Starts with three rows because "one" would imply this is the
 * single-create form wearing a hat.
 */
function BatchDraftPanel({
  people,
  busy,
  onCancel,
  onCreate,
}: {
  people: { user_id: string; name: string }[];
  busy: boolean;
  onCancel: () => void;
  onCreate: (rows: { name: string; assignee_user_id: string | null }[]) => Promise<void>;
}) {
  const [rows, setRows] = useState<{ name: string; assignee: string | null }[]>([
    { name: "", assignee: null },
    { name: "", assignee: null },
    { name: "", assignee: null },
  ]);
  const filled = rows.filter((r) => r.name.trim()).length;

  function patch(i: number, next: Partial<{ name: string; assignee: string | null }>) {
    setRows((cur) => cur.map((r, k) => (k === i ? { ...r, ...next } : r)));
  }

  return (
    <div className="pn__draft">
      <div className="pn__draft-head">
        <b>New batches</b>
        <span className="pn__muted">
          One per artist. Blank rows are ignored.
        </span>
        <button className="pn__add-close" title="Cancel" onClick={onCancel}>
          ✕
        </button>
      </div>

      <ul className="pn__draft-rows">
        {rows.map((r, i) => (
          <li key={i} className="pn__draft-row">
            <input
              className="inbox__input"
              placeholder={`Batch ${i + 1} name…`}
              value={r.name}
              disabled={busy}
              autoFocus={i === 0}
              onChange={(e) => patch(i, { name: e.target.value })}
              onKeyDown={(e) => {
                // Enter on the last row adds another, so a whole division can be
                // typed without reaching for the mouse.
                if (e.key === "Enter" && i === rows.length - 1) {
                  setRows((cur) => [...cur, { name: "", assignee: null }]);
                }
              }}
            />
            <select
              className="inbox__input pn__select"
              value={r.assignee ?? ""}
              disabled={busy}
              onChange={(e) => patch(i, { assignee: e.target.value || null })}
            >
              <option value="">— unassigned —</option>
              {people.map((p) => (
                <option key={p.user_id} value={p.user_id}>
                  {p.name}
                </option>
              ))}
            </select>
            <button
              className="pn__add-close"
              title="Remove this row"
              disabled={busy || rows.length === 1}
              onClick={() => setRows((cur) => cur.filter((_, k) => k !== i))}
            >
              ✕
            </button>
          </li>
        ))}
      </ul>

      <div className="pn__draft-foot">
        <button
          className="btn2"
          disabled={busy}
          onClick={() => setRows((cur) => [...cur, { name: "", assignee: null }])}
        >
          + Add row
        </button>
        <button
          className="btn2 btn2--primary"
          disabled={busy || filled === 0}
          onClick={() =>
            void onCreate(
              rows
                .filter((r) => r.name.trim())
                .map((r) => ({ name: r.name.trim(), assignee_user_id: r.assignee })),
            )
          }
        >
          {busy
            ? "Creating…"
            : filled === 0
              ? "Create batches"
              : `Create ${filled} batch${filled === 1 ? "" : "es"}`}
        </button>
      </div>
    </div>
  );
}

/**
 * One artist's share of a comic.
 *
 * A card rather than a full-width row: a row gave a name and one number an
 * entire screen width, and the answer a PM wants — how far along is this, and
 * who has it — was a single "0 / 45 approved" adrift in empty space.
 *
 * The bar is stacked across all five states, not just approved-vs-rest. A batch
 * sitting untouched and a batch entirely awaiting review both read as "0
 * approved", and they are nothing alike.
 */
function BatchCard({
  batch,
  people,
  onChanged,
  drag,
  manage,
  canImport,
}: {
  batch: PanelBatch;
  people: { user_id: string; name: string }[];
  onChanged: () => Promise<void>;
  drag: Record<string, unknown>;
  /** Naming a batch, assigning it and deleting it are the PM's decisions. */
  manage: boolean;
  canImport: boolean;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [importing, setImporting] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(batch.name);
  const nameRef = useRef<HTMLInputElement | null>(null);

  const counts = batch.status_counts ?? {};
  const total = batch.panel_count;
  const stages = STAGES.map((s) => ({ ...s, n: counts[s.key] ?? 0 })).filter(
    (s) => s.n > 0,
  );
  const pct = total ? Math.round((batch.approved_count / total) * 100) : 0;

  useEffect(() => {
    if (editing) {
      setDraft(batch.name);
      requestAnimationFrame(() => nameRef.current?.select());
    }
  }, [editing, batch.name]);

  async function rename() {
    const clean = draft.trim();
    if (!clean || clean === batch.name) {
      setEditing(false);
      return;
    }
    try {
      await updateBatch(batch.id, { name: clean });
      setEditing(false);
      await onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Rename failed");
    }
  }

  async function onPick(files: FileList | null) {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    setImporting(`Uploading ${list.length}…`);
    try {
      const r = await importPanelFolder(batch.id, list);
      await onChanged();
      toast(
        `${r.panels.length} panels imported.` +
          (r.skipped_count ? ` ${r.skipped_count} file(s) skipped (not images).` : ""),
      );
    } catch (e) {
      toast(e instanceof Error ? e.message : "Import failed");
    } finally {
      setImporting(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <li className="pn__batch" {...drag}>
      <Link
        to={`/giantflow/batch/${batch.id}`}
        className="pn__batch-cover"
        draggable={false}
      >
        {batch.thumb_media_id ? (
          <img src={thumbUrl(batch.thumb_media_id, 420)} alt="" loading="lazy" />
        ) : (
          <span className="pn__batch-empty">No panels yet</span>
        )}
        {total > 0 ? <span className="pn__batch-count">{total} panels</span> : null}
        {batch.open_notes > 0 ? (
          <span className="pn__batch-notes">{batch.open_notes} notes</span>
        ) : null}
      </Link>

      <div className="pn__batch-body">
        <div className="pn__batch-top">
          {editing ? (
            <input
              ref={nameRef}
              className="pn__batch-rename"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => void rename()}
              onKeyDown={(e) => {
                if (e.key === "Enter") void rename();
                if (e.key === "Escape") {
                  setDraft(batch.name);
                  setEditing(false);
                }
              }}
            />
          ) : (
            <Link
              to={`/giantflow/batch/${batch.id}`}
              className="pn__batch-name"
              title={batch.name}
              draggable={false}
            >
              {batch.name}
            </Link>
          )}
          <span className="pn__batch-pct">{pct}%</span>
        </div>

        {total > 0 ? (
          <>
            <div className="pn__stack" role="img"
                 aria-label={stages.map((s) => `${s.n} ${s.label}`).join(", ")}>
              {stages.map((s) => (
                <span
                  key={s.key}
                  className={`pn__stack-seg is-${s.key}`}
                  style={{ width: `${(s.n / total) * 100}%` }}
                  title={`${s.n} ${s.label}`}
                />
              ))}
            </div>
            <div className="pn__legend">
              {stages.map((s) => (
                <span key={s.key} className={`pn__legend-item is-${s.key}`}>
                  <i /> {s.n} {s.label}
                </span>
              ))}
            </div>
          </>
        ) : (
          <p className="pn__batch-hint">Import this artist's folder to start.</p>
        )}

        <div className="pn__batch-foot">
          {/* One artist per batch — that is what a batch IS, so the picker sits on
              the card rather than hidden behind an edit screen. */}
          <PersonPicker
            label=""
            value={batch.assignee_user_id}
            people={people}
            disabled={!manage}
            onChange={async (uid) => {
              await updateBatch(batch.id, { assignee_user_id: uid, set_assignee: true });
              await onChanged();
            }}
          />
          <span className="pn__batch-acts">
            {total === 0 && canImport ? (
              <>
                <input
                  ref={fileRef}
                  type="file"
                  // Non-standard, but the only way to pick a FOLDER; each file's
                  // path inside it is what says which panel it belongs to.
                  {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
                  multiple
                  hidden
                  onChange={(e) => void onPick(e.target.files)}
                />
                <button
                  type="button"
                  className="pn__tile-btn"
                  disabled={!!importing}
                  onClick={() => fileRef.current?.click()}
                >
                  {importing ?? "Import"}
                </button>
              </>
            ) : null}
            {manage ? (
              <button
                type="button"
                className="pn__tile-btn"
                title="Rename this batch"
                onClick={() => setEditing(true)}
              >
                Rename
              </button>
            ) : null}
            {manage ? (
            <button
              type="button"
              className="pn__tile-btn pn__tile-btn--danger"
              title="Delete this batch and its panels"
              onClick={async () => {
                if (
                  !window.confirm(
                    `Delete “${batch.name}” and its ${batch.panel_count} panel(s)?`,
                  )
                )
                  return;
                try {
                  await deleteBatch(batch.id);
                  await onChanged();
                } catch (e) {
                  toast(e instanceof Error ? e.message : "Delete failed");
                }
              }}
            >
              ✕
            </button>
            ) : null}
          </span>
        </div>
      </div>
    </li>
  );
}

/**
 * Who is on this comic, and as what.
 *
 * Separate from the per-batch assignee, which says who DOES a share of the work.
 * This says what someone is allowed to do at all: an artist generates and
 * submits, a PM rules on submissions, a viewer looks. Without a screen for it the
 * roles existed only as rows nobody could reach.
 *
 * Anyone not listed still gets in as a viewer, and whoever a batch is assigned to
 * counts as an artist without a row here — assigning work already says "this is
 * yours", and making the PM repeat it would be one fact stored twice.
 */
function MembersPanel({
  seriesId,
  people,
}: {
  seriesId: number;
  people: { user_id: string; name: string }[];
}) {
  const [rows, setRows] = useState<FlowMember[] | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRows(await listFlowMembers(seriesId));
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed");
    }
  }, [seriesId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function put(userId: string, role: string) {
    setBusy(true);
    try {
      await setFlowMember(seriesId, userId, role);
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  const listed = new Set((rows ?? []).map((r) => r.user_id));
  const rest = people.filter((p) => !listed.has(p.user_id));

  return (
    <div className="pn__draft">
      <div className="pn__draft-head">
        <b>People on this comic</b>
        <span className="pn__muted">
          Everyone else can look but not act.
        </span>
      </div>

      <ul className="pn__draft-rows">
        {(rows ?? []).map((m) => (
          <li key={m.user_id} className="pn__draft-row">
            <span className="pn__member-name">{m.name}</span>
            <select
              className="inbox__input pn__select"
              value={m.role}
              disabled={busy}
              onChange={(e) => void put(m.user_id, e.target.value)}
            >
              {FLOW_ROLE_OPTIONS.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </select>
            <button
              className="pn__add-close"
              title="Remove from this comic"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await removeFlowMember(seriesId, m.user_id);
                  await load();
                } finally {
                  setBusy(false);
                }
              }}
            >
              ✕
            </button>
          </li>
        ))}
        {rows !== null && rows.length === 0 ? (
          <li className="pn__muted">Nobody added yet.</li>
        ) : null}
      </ul>

      {rest.length > 0 ? (
        <div className="pn__draft-foot">
          <select
            className="inbox__input pn__select"
            defaultValue=""
            disabled={busy}
            onChange={(e) => {
              if (e.target.value) void put(e.target.value, "artist");
              e.target.value = "";
            }}
          >
            <option value="">+ Add someone…</option>
            {rest.map((p) => (
              <option key={p.user_id} value={p.user_id}>
                {p.name}
              </option>
            ))}
          </select>
          <span className="pn__muted">Added as Artist; change the role after.</span>
        </div>
      ) : null}
    </div>
  );
}

/** Mirrors `FLOW_ROLES` in services/flow_permissions.py. */
const FLOW_ROLE_OPTIONS = [
  { id: "producer", label: "PM" },
  { id: "lead", label: "Lead" },
  { id: "artist", label: "Artist" },
  { id: "viewer", label: "Viewer" },
];


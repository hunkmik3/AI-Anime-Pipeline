import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  createBatch,
  deleteBatch,
  importPanelFolder,
  listBatches,
  listPanelAssignees,
  listPanelProjects,
  reorderBatches,
  updateBatch,
  type PanelBatch,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { PersonPicker } from "../components/PersonPicker";
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
  const { projectId } = useParams();
  const pid = Number(projectId);
  const [batches, setBatches] = useState<PanelBatch[] | null>(null);
  const [projectName, setProjectName] = useState("");
  const [people, setPeople] = useState<{ user_id: string; name: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [rows, projects] = await Promise.all([listBatches(pid), listPanelProjects()]);
      setBatches(rows);
      setProjectName(projects.find((p) => p.id === pid)?.name ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [pid]);

  useEffect(() => {
    void load();
    void listPanelAssignees().then(setPeople).catch(() => setPeople([]));
  }, [load]);

  async function create() {
    const clean = name.trim();
    if (!clean) return;
    setBusy(true);
    try {
      await createBatch(pid, clean);
      setName("");
      await load();
      toast("Batch created. Assign it and import its panels.");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  const { list, dragProps } = useDragOrder(batches ?? [], async (ids) => {
    await reorderBatches(pid, ids);
    await load();
  });

  const totals = (batches ?? []).reduce(
    (a, b) => ({
      panels: a.panels + b.panel_count,
      approved: a.approved + b.approved_count,
    }),
    { panels: 0, approved: 0 },
  );

  return (
    <div className="shellpage pn__wide">
      <PageHeader
        crumb={<Link to="/giantflow">Project</Link>}
        title={projectName || "Project"}
        subtitle={
          batches
            ? `${batches.length} batch${batches.length === 1 ? "" : "es"} · ${totals.approved} / ${totals.panels} panels approved`
            : undefined
        }
        actions={
          <div className="pn__newrow">
            <input
              className="inbox__input"
              placeholder="New batch name…"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void create();
              }}
            />
            <button
              className="btn2 btn2--primary"
              disabled={busy || !name.trim()}
              onClick={() => void create()}
            >
              Create batch
            </button>
          </div>
        }
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {batches === null ? <p className="rfoot">Loading…</p> : null}

      {batches !== null && batches.length === 0 ? (
        <div className="inbox__empty">
          <b>No batches yet.</b>
          Create one per artist, then import that artist's folder of panels into it.
        </div>
      ) : null}

      <ul className="pn__projects">
        {list.map((b) => (
          <BatchRow
            key={b.id}
            batch={b}
            people={people}
            onChanged={load}
            drag={dragProps(b.id)}
          />
        ))}
      </ul>
    </div>
  );
}

function BatchRow({
  batch,
  people,
  onChanged,
  drag,
}: {
  batch: PanelBatch;
  people: { user_id: string; name: string }[];
  onChanged: () => Promise<void>;
  drag: Record<string, unknown>;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [importing, setImporting] = useState<string | null>(null);
  const pct = batch.panel_count
    ? Math.round((batch.approved_count / batch.panel_count) * 100)
    : 0;

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
    <li className="pn__project" {...drag}>
      <Link
        to={`/giantflow/batch/${batch.id}`}
        className="pn__project-body"
        draggable={false}
      >
        <div className="pn__project-name">{batch.name}</div>
        <div className="pn__project-stat">
          {batch.panel_count === 0 ? (
            <span className="pn__muted">No panels yet — import a folder</span>
          ) : (
            <>
              <b>{batch.approved_count}</b> / {batch.panel_count} approved
              <span className="pn__bar">
                <span className="pn__bar-fill" style={{ width: `${pct}%` }} />
              </span>
              {batch.open_notes > 0 ? (
                <span className="pn__notes">{batch.open_notes} open notes</span>
              ) : null}
            </>
          )}
        </div>
      </Link>

      <div className="pn__project-acts">
        {/* One artist per batch — that is what a batch IS, so the picker sits on
            the row rather than hidden behind an edit screen. */}
        <PersonPicker
          label=""
          value={batch.assignee_user_id}
          people={people}
          onChange={async (uid) => {
            await updateBatch(batch.id, { assignee_user_id: uid, set_assignee: true });
            await onChanged();
          }}
        />
        {batch.panel_count === 0 ? (
          <>
            <input
              ref={fileRef}
              type="file"
              // Non-standard, but the only way to pick a FOLDER; each file's path
              // inside it is what says which panel it belongs to.
              {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
              multiple
              hidden
              onChange={(e) => void onPick(e.target.files)}
            />
            <button
              className="btn2"
              disabled={!!importing}
              onClick={() => fileRef.current?.click()}
            >
              {importing ?? "Import panels"}
            </button>
          </>
        ) : null}
        <button
          className="btn2 btn2--danger"
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
      </div>
    </li>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  createPanelSeries,
  deletePanelSeries,
  listPanelSeries,
  renamePanelSeries,
  reorderPanelSeries,
  setPanelSeriesCover,
  thumbUrl,
  uploadFlowImage,
  type PanelSeries,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { GiantflowNav } from "./GiantflowNav";
import { useGiantflowRole } from "../store/giantflowRole";
import { toast } from "../store/toast";
import { useDragOrder } from "./useDragOrder";

/**
 * The Series list — one card per comic on this project's slate.
 *
 * A series holds nothing but a name and its batches. The material lives one
 * level down: each batch is one artist's share and carries its own imported
 * folder, because the studio hands work out already divided rather than dumping
 * a chapter in one pile and splitting it afterwards.
 */
export function PanelSeriesPage() {
  const { projectId } = useParams();
  const pid = Number(projectId);
  const { can } = useGiantflowRole();
  const [series, setSeries] = useState<PanelSeries[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setSeries(await listPanelSeries(pid));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [pid]);

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

  const { list, dragProps } = useDragOrder(series ?? [], async (ids) => {
    await reorderPanelSeries(ids);
    await load();
  });

  async function create(name: string) {
    const clean = name.trim();
    if (!clean) return;
    setBusy(true);
    try {
      await createPanelSeries(pid, clean);
      await load();
      toast("Series created. Add a batch per artist inside it.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shellpage pn__wide">
      <GiantflowNav />
      <PageHeader
        crumb={<Link to="/giantflow">← Projects</Link>}
        title="Series"
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {series === null ? <p className="rfoot">Loading…</p> : null}

      {series !== null && series.length === 0 ? (
        <div className="inbox__empty">
          <b>No series yet.</b>
          Create one per comic. Inside it you make a batch per artist and import
          that artist's panels.
        </div>
      ) : null}

      <ul className="pn__tiles">
        {list.map((p) => (
          <SeriesCard
            key={p.id}
            series={p}
            onChanged={load}
            drag={can("project.manage") ? dragProps(p.id) : {}}
            manage={can("project.manage")}
          />
        ))}
        {/* Last, not first: the tiles are drag-reorderable and a fixed cell at the
            front would sit in the middle of every drag. */}
        {can("project.manage") ? <AddSeriesTile busy={busy} onCreate={create} /> : null}
      </ul>
    </div>
  );
}

/**
 * The last cell of the grid: an outline tile that becomes the create form.
 *
 * The form used to sit in the page header, far from the row of tiles it adds to.
 * Here the control is the same shape and place as the thing it makes, so the new
 * series appears where you were already looking.
 */
function AddSeriesTile({
  busy,
  onCreate,
}: {
  busy: boolean;
  onCreate: (name: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  async function submit() {
    if (!name.trim()) return;
    await onCreate(name);
    setName("");
    setOpen(false);
  }

  if (!open) {
    return (
      <li className="pn__tile pn__add">
        <button type="button" className="pn__add-btn" onClick={() => setOpen(true)}>
          <span className="pn__add-plus" aria-hidden="true">+</span>
          <span>Add Series</span>
        </button>
      </li>
    );
  }

  return (
    <li className="pn__tile pn__add is-open">
      <div className="pn__add-form">
        <button
          type="button"
          className="pn__add-close"
          title="Cancel"
          onClick={() => {
            setName("");
            setOpen(false);
          }}
        >
          ✕
        </button>
        <span className="pn__add-plus" aria-hidden="true">+</span>
        <input
          ref={inputRef}
          className="pn__add-input"
          placeholder="Series name…"
          value={name}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void submit();
            // Escape closes without creating — the same thing the ✕ does, for
            // someone whose hands are already on the keyboard.
            if (e.key === "Escape") {
              setName("");
              setOpen(false);
            }
          }}
        />
        <button
          type="button"
          className="pn__add-create"
          disabled={busy || !name.trim()}
          onClick={() => void submit()}
        >
          {busy ? "Creating…" : "Create"}
        </button>
      </div>
    </li>
  );
}

/** Pick one image file. A plain input rather than a component: it is two lines,
 *  and the tile is the only place that needs it. */
function pickImage(): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp";
    input.onchange = () => resolve(input.files?.[0] ?? null);
    input.click();
  });
}

function SeriesCard({
  series,
  onChanged,
  drag,
  manage,
}: {
  series: PanelSeries;
  onChanged: () => Promise<void>;
  drag: Record<string, unknown>;
  /** Creating, renaming, re-covering and deleting a comic is an admin's call. */
  manage: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(series.name);
  const nameRef = useRef<HTMLInputElement | null>(null);
  const pct = series.panel_count
    ? Math.round((series.approved_count / series.panel_count) * 100)
    : 0;

  useEffect(() => {
    if (editing) {
      setDraft(series.name);
      // select(), not focus(): renaming usually means replacing, and a caret at
      // the end would make you clear it by hand first.
      requestAnimationFrame(() => nameRef.current?.select());
    }
  }, [editing, series.name]);

  async function rename() {
    const clean = draft.trim();
    if (!clean || clean === series.name) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await renamePanelSeries(series.id, clean);
      setEditing(false);
      await onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Rename failed");
    } finally {
      setBusy(false);
    }
  }

  async function setCover() {
    const file = await pickImage();
    if (!file) return;
    setBusy(true);
    try {
      // Two steps on purpose: the upload caches bytes and hands back a media id,
      // which is then pointed at — the same id any other surface could reuse.
      const { media_id } = await uploadFlowImage(file);
      await setPanelSeriesCover(series.id, media_id);
      await onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="pn__tile" {...drag}>
      <Link to={`/giantflow/s/${series.id}`} className="pn__tile-body">
        <div className="pn__tile-thumb">
          {series.thumb_media_id ? (
            <img
              src={thumbUrl(series.thumb_media_id, 400)}
              alt=""
              loading="lazy"
              onError={(e) => {
                (e.currentTarget as HTMLImageElement).style.display = "none";
              }}
            />
          ) : (
            <svg viewBox="0 0 24 24" width="34" height="34" fill="none"
                 stroke="currentColor" strokeWidth="1.4" aria-hidden="true">
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <path d="M3 15l5-5 4 4 3-3 6 6" />
            </svg>
          )}
        </div>
        <div className="pn__tile-meta">
          {editing ? (
            // Outside the Link's job: an input inside a navigating anchor would
            // follow the link on every click.
            <input
              ref={nameRef}
              className="pn__tile-rename"
              value={draft}
              disabled={busy}
              onClick={(e) => e.preventDefault()}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => void rename()}
              onKeyDown={(e) => {
                if (e.key === "Enter") void rename();
                if (e.key === "Escape") {
                  setDraft(series.name);
                  setEditing(false);
                }
              }}
            />
          ) : (
            <div className="pn__tile-name" title={series.name}>
              {series.name}
            </div>
          )}
          <div className="pn__tile-sub">
            {series.chapter_count === 0
              ? "No chapters yet"
              : `${series.chapter_count} chapter${series.chapter_count === 1 ? "" : "s"} · ${series.approved_count}/${series.panel_count} approved`}
          </div>
          {series.panel_count > 0 ? (
            <div className="pn__tile-bar">
              <span style={{ width: `${pct}%` }} />
            </div>
          ) : null}
        </div>
      </Link>

      {manage ? (
      <div className="pn__tile-acts">
        <button
          type="button"
          className="pn__tile-btn"
          title="Rename this series"
          onClick={(e) => {
            e.preventDefault();
            setEditing(true);
          }}
        >
          Rename
        </button>
        <button
          type="button"
          className="pn__tile-btn"
          title="Upload a cover image"
          disabled={busy}
          onClick={(e) => {
            e.preventDefault();
            void setCover();
          }}
        >
          {busy ? "Uploading…" : series.has_cover ? "Change" : "Thumbnail"}
        </button>
        {/* Only offered once there IS a hand-set cover — clearing back to the
            first-panel fallback is meaningless otherwise. */}
        {series.has_cover ? (
          <button
            type="button"
            className="pn__tile-btn"
            title="Clear the cover (back to the first panel)"
            onClick={async (e) => {
              e.preventDefault();
              await setPanelSeriesCover(series.id, null);
              await onChanged();
            }}
          >
            Reset
          </button>
        ) : null}
        <button
          type="button"
          className="pn__tile-btn pn__tile-btn--danger"
          title="Delete this series"
          onClick={async (e) => {
            e.preventDefault();
            if (
              !window.confirm(
                `Delete \u201c${series.name}\u201d, its ${series.batch_count} batch(es) and ${series.panel_count} panel(s)?`,
              )
            )
              return;
            try {
              await deletePanelSeries(series.id);
              await onChanged();
            } catch (err) {
              toast(err instanceof Error ? err.message : "Delete failed");
            }
          }}
        >
          ✕
        </button>
      </div>
      ) : null}
    </li>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  createChapter,
  deleteChapter,
  listChapters,
  listPanelSeries,
  reorderChapters,
  thumbUrl,
  updateChapter,
  uploadFlowImage,
  type FlowChapter,
  type PanelSeries,
} from "../api/client";
import { useGiantflowRole } from "../store/giantflowRole";
import { toast } from "../store/toast";
import { GiantflowNav } from "./GiantflowNav";
import { PanelHero, sumCounts } from "./PanelHero";
import { useDragOrder } from "./useDragOrder";

/**
 * Inside a comic: its chapters.
 *
 * The tier the work is actually divided on. A comic ships an instalment at a
 * time and the artists are split per instalment, so "artist X takes panels 1-45"
 * is a statement about a chapter, not about the whole comic — which is why
 * batches hang off this rather than off the series.
 */
export function PanelChaptersPage() {
  const { seriesId } = useParams();
  const sid = Number(seriesId);
  const { can } = useGiantflowRole();
  const [chapters, setChapters] = useState<FlowChapter[] | null>(null);
  const [series, setSeries] = useState<PanelSeries | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [rows, all] = await Promise.all([listChapters(sid), listPanelSeries()]);
      setChapters(rows);
      setSeries(all.find((x) => x.id === sid) ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [sid]);

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

  const { list, dragProps } = useDragOrder(chapters ?? [], async (ids) => {
    await reorderChapters(sid, ids);
    await load();
  });

  const rows = chapters ?? [];
  const totals = rows.reduce((a, c) => a + c.panel_count, 0);
  const counts = sumCounts(rows.map((c) => c.status_counts ?? {}));

  async function create(name: string) {
    const clean = name.trim();
    if (!clean) return;
    setBusy(true);
    try {
      await createChapter(sid, clean);
      await load();
      toast("Chapter created. Divide it into a batch per artist.");
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shellpage pn__wide">
      <GiantflowNav />
      <PanelHero
        crumb={
          series ? (
            <Link to={`/giantflow/p/${series.project_id}`}>← Series</Link>
          ) : (
            <Link to="/giantflow">← Projects</Link>
          )
        }
        title={series?.name || "Series"}
        thumbMediaId={series?.thumb_media_id}
        counts={counts}
        total={totals}
        facts={[
          `${rows.length} chapter${rows.length === 1 ? "" : "s"}`,
          `${totals} panel${totals === 1 ? "" : "s"}`,
        ]}
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {chapters === null ? <p className="rfoot">Loading…</p> : null}

      {chapters !== null && rows.length === 0 ? (
        <div className="inbox__empty">
          <b>No chapters yet.</b>
          A comic ships an instalment at a time. Create a chapter, then divide it
          into a batch per artist.
        </div>
      ) : null}

      <ul className="pn__tiles">
        {list.map((c) => (
          <ChapterCard
            key={c.id}
            chapter={c}
            onChanged={load}
            drag={can("batch.manage") ? dragProps(c.id) : {}}
            manage={can("batch.manage")}
          />
        ))}
        {can("batch.manage") ? <AddChapterTile busy={busy} onCreate={create} /> : null}
      </ul>
    </div>
  );
}

function AddChapterTile({
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
          <span>Add Chapter</span>
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
          placeholder="Chapter name…"
          value={name}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void submit();
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

function pickImage(): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp";
    input.onchange = () => resolve(input.files?.[0] ?? null);
    input.click();
  });
}

function ChapterCard({
  chapter,
  onChanged,
  drag,
  manage,
}: {
  chapter: FlowChapter;
  onChanged: () => Promise<void>;
  drag: Record<string, unknown>;
  manage: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(chapter.name);
  const nameRef = useRef<HTMLInputElement | null>(null);
  const pct = chapter.panel_count
    ? Math.round((chapter.approved_count / chapter.panel_count) * 100)
    : 0;

  useEffect(() => {
    if (editing) {
      setDraft(chapter.name);
      requestAnimationFrame(() => nameRef.current?.select());
    }
  }, [editing, chapter.name]);

  async function rename() {
    const clean = draft.trim();
    if (!clean || clean === chapter.name) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await updateChapter(chapter.id, { name: clean });
      setEditing(false);
      await onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Rename failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="pn__tile" {...drag}>
      <Link to={`/giantflow/c/${chapter.id}`} className="pn__tile-body">
        <div className="pn__tile-thumb">
          {chapter.thumb_media_id ? (
            <img
              src={thumbUrl(chapter.thumb_media_id, 400)}
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
                  setDraft(chapter.name);
                  setEditing(false);
                }
              }}
            />
          ) : (
            <div className="pn__tile-name" title={chapter.name}>
              {chapter.name}
            </div>
          )}
          <div className="pn__tile-sub">
            {chapter.batch_count === 0
              ? "No batches yet"
              : `${chapter.batch_count} batch${chapter.batch_count === 1 ? "" : "es"} · ${chapter.approved_count}/${chapter.panel_count} approved`}
          </div>
          {chapter.panel_count > 0 ? (
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
            title="Rename this chapter"
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
            onClick={async (e) => {
              e.preventDefault();
              const file = await pickImage();
              if (!file) return;
              setBusy(true);
              try {
                const { media_id } = await uploadFlowImage(file);
                await updateChapter(chapter.id, { cover_media_id: media_id, set_cover: true });
                await onChanged();
              } catch (err) {
                toast(err instanceof Error ? err.message : "Upload failed");
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Uploading…" : chapter.has_cover ? "Change" : "Thumbnail"}
          </button>
          <button
            type="button"
            className="pn__tile-btn pn__tile-btn--danger"
            title="Delete this chapter"
            onClick={async (e) => {
              e.preventDefault();
              if (
                !window.confirm(
                  `Delete “${chapter.name}”, its ${chapter.batch_count} batch(es) and ${chapter.panel_count} panel(s)?`,
                )
              )
                return;
              try {
                await deleteChapter(chapter.id);
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

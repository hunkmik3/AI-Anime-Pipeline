import { useCallback, useEffect, useState } from "react";

import {
  listEpisodeNotes,
  resolveEditNote,
  type EditNoteDTO,
} from "../api/client";

/**
 * What the editor said about this episode, on the page where it gets fixed.
 *
 * The artist's half of the loop. Arranged by SEQUENCE, because somebody standing
 * on a canvas is asking "which of mine is wrong" — sending them to the editor's
 * timeline to find their own shot in it is asking them to do the app's job.
 *
 * Folded shut when nothing is open. A panel that says "0 notes" on every episode
 * is a panel people stop seeing, and then the one time it says 3 they miss it.
 */

function tc(sec: number): string {
  const m = Math.floor(sec / 60);
  return `${String(m).padStart(2, "0")}:${(sec - m * 60).toFixed(2).padStart(5, "0")}`;
}

export function EpisodeNotes({ sceneId }: { sceneId: string }) {
  const [notes, setNotes] = useState<EditNoteDTO[] | null>(null);
  const [open, setOpen] = useState(true);

  const load = useCallback(async () => {
    try {
      setNotes((await listEpisodeNotes(sceneId)).notes);
    } catch {
      setNotes([]);
    }
  }, [sceneId]);

  useEffect(() => {
    void load();
  }, [load]);

  const unresolved = (notes ?? []).filter((n) => !n.resolved);
  if (!notes || notes.length === 0) return null;

  // Group by sequence: the fix happens one sequence at a time, so the list that
  // is acted on is the list that is grouped that way.
  const bySeq = new Map<string, EditNoteDTO[]>();
  for (const n of notes) {
    const key = n.shot_code || "cả tập";
    bySeq.set(key, [...(bySeq.get(key) ?? []), n]);
  }

  return (
    <aside className={`epnotes${open ? " is-open" : ""}`}>
      <button className="epnotes__head" onClick={() => setOpen((v) => !v)}>
        <span className="epnotes__t">Editor ghi chú</span>
        {unresolved.length > 0 ? (
          <span className="epnotes__count">{unresolved.length}</span>
        ) : (
          <span className="epnotes__done">xong</span>
        )}
        <span className="epnotes__chev">{open ? "▾" : "▸"}</span>
      </button>

      {open ? (
        <div className="epnotes__body">
          {[...bySeq.entries()].map(([code, rows]) => (
            <div key={code} className="epnotes__seq">
              <div className="epnotes__seqh">
                {code}
                <b>{rows.filter((r) => !r.resolved).length || "✓"}</b>
              </div>
              {rows.map((n) => (
                <div
                  key={n.id}
                  className={`epnotes__n${n.resolved ? " is-done" : ""}`}
                >
                  <span className="epnotes__at">{tc(n.at_seconds)}</span>
                  {n.drawing_media_id ? (
                    <a className="epnotes__pic" href={`/media/${n.drawing_media_id}`}
                       target="_blank" rel="noreferrer" title="Nét vẽ của editor">
                      <img src={`/media/${n.drawing_media_id}`} alt="" />
                    </a>
                  ) : null}
                  <span className="epnotes__b">{n.body}</span>
                  <button
                    className="epnotes__act"
                    title={n.resolved ? "Mở lại" : "Đánh dấu đã sửa"}
                    onClick={async () => {
                      await resolveEditNote(n.id, !n.resolved);
                      await load();
                    }}
                  >
                    {n.resolved ? "↺" : "✓"}
                  </button>
                </div>
              ))}
            </div>
          ))}
        </div>
      ) : null}
    </aside>
  );
}

import { useMemo, useState } from "react";
import { createPortal } from "react-dom";

import { thumbUrl, type ReferenceItem } from "../api/client";
import { useReferencesStore } from "../store/references";

/**
 * Modal that lets the user pick a material from the current project's Asset
 * Library — the "choose from library" alternative to uploading a fresh file.
 * Reads the already-scoped references store (loaded per-project), so it only
 * ever shows this project's materials. Calls onPick with the chosen item.
 */
export function LibraryPicker({
  onPick,
  onClose,
  kinds,
}: {
  onPick: (ref: ReferenceItem) => void;
  onClose: () => void;
  /** Restrict to these kinds; default = image-like materials. */
  kinds?: ReferenceItem["kind"][];
}) {
  const items = useReferencesStore((s) => s.items);
  const loading = useReferencesStore((s) => s.loading);
  const [q, setQ] = useState("");

  const allow = kinds ?? ["image", "character", "visual_asset", "storyboard_shot"];
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return items
      .filter((r) => allow.includes(r.kind))
      .filter((r) => !needle || r.label.toLowerCase().includes(needle));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, q]);

  return createPortal(
    <div
      className="libpick-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="libpick" role="dialog" aria-label="Choose from library">
        <div className="libpick__head">
          <div>
            <h3 className="libpick__title">Choose from library</h3>
            <p className="libpick__sub">Materials saved to this project.</p>
          </div>
          <button className="libpick__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <input
          className="libpick__search"
          placeholder="Search materials…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          autoFocus
        />

        {loading && items.length === 0 ? (
          <div className="libpick__empty">Loading…</div>
        ) : shown.length === 0 ? (
          <div className="libpick__empty">
            No materials in this project's library yet. Upload some in the Asset library first.
          </div>
        ) : (
          <div className="libpick__grid">
            {shown.map((r) => (
              <button
                key={r.id}
                className="libpick__tile"
                title={r.label}
                onClick={() => onPick(r)}
              >
                <img
                  src={thumbUrl(r.mediaId, 220)}
                  alt={r.label}
                  loading="lazy"
                  decoding="async"
                />
                <span className="libpick__label">{r.label}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

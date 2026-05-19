import { useEffect, useMemo, useRef, useState } from "react";
import { mediaUrl, type ReferenceItem } from "../api/client";
import { filterReferences, useReferencesStore } from "../store/references";

/**
 * Right-side collapsible reference library.
 *
 * The panel renders a vertical list of saved references — each card is
 * a 64x64 thumbnail + label + shortId tag with hover-revealed actions
 * (pin toggle, rename in-place, delete with confirm). Cards are both
 * clickable (Phase 3 will wire spawn-at-canvas-center) and draggable
 * with a custom `application/x-flowboard-reference` MIME so canvas
 * drop handlers can detect the payload without colliding with the
 * existing file-upload drop path.
 *
 * A fixed-position vertical ★ tab on the right edge toggles the open
 * state, which is persisted to localStorage by the store.
 */
export function ReferencesPanel() {
  const items = useReferencesStore((s) => s.items);
  const loading = useReferencesStore((s) => s.loading);
  const error = useReferencesStore((s) => s.error);
  const panelOpen = useReferencesStore((s) => s.panelOpen);
  const query = useReferencesStore((s) => s.query);
  const setQuery = useReferencesStore((s) => s.setQuery);
  const togglePanel = useReferencesStore((s) => s.togglePanel);
  const remove = useReferencesStore((s) => s.remove);
  const rename = useReferencesStore((s) => s.rename);
  const togglePin = useReferencesStore((s) => s.togglePin);

  const filtered = useMemo(() => filterReferences(items, query), [items, query]);

  return (
    <>
      <button
        type="button"
        className="references-panel__toggle-tab"
        onClick={togglePanel}
        aria-label={panelOpen ? "Collapse references" : "Open references"}
        title={panelOpen ? "Collapse library" : "Open library"}
      >
        <span aria-hidden="true">{panelOpen ? "›" : "★"}</span>
      </button>
      <aside
        className={`references-panel${
          panelOpen ? " references-panel--open" : " references-panel--collapsed"
        }`}
        aria-hidden={!panelOpen}
      >
        <div className="references-panel__header">
          <span className="references-panel__title">
            <span aria-hidden="true">★</span> Library
          </span>
          <button
            type="button"
            className="references-panel__close"
            onClick={togglePanel}
            aria-label="Collapse references panel"
            title="Collapse"
          >
            ›
          </button>
        </div>

        <div className="references-panel__search">
          <input
            type="text"
            placeholder="🔍 search references…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search references"
          />
        </div>

        {error && <div className="references-panel__error">{error}</div>}

        {loading && items.length === 0 && (
          <div className="references-panel__empty">Loading…</div>
        )}

        {!loading && items.length === 0 && (
          <div className="references-panel__empty">
            Save a variant from any image node to start your library.
          </div>
        )}

        {!loading && items.length > 0 && filtered.length === 0 && (
          <div className="references-panel__empty">
            No references match "{query}".
          </div>
        )}

        <ul className="references-panel__list">
          {filtered.map((ref) => (
            <ReferenceCard
              key={ref.id}
              item={ref}
              onRename={(label) => rename(ref.id, label)}
              onTogglePin={() => togglePin(ref.id)}
              onDelete={() => remove(ref.id)}
            />
          ))}
        </ul>
      </aside>
    </>
  );
}

interface ReferenceCardProps {
  // Named `item` rather than `ref` to avoid React's reserved-prop
  // collision (refs are forwarded via a different prop in React 19+).
  item: ReferenceItem;
  onRename(label: string): Promise<void> | void;
  onTogglePin(): Promise<void> | void;
  onDelete(): Promise<void> | void;
}

function ReferenceCard({
  item,
  onRename,
  onTogglePin,
  onDelete,
}: ReferenceCardProps) {
  const [thumbBroken, setThumbBroken] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(item.label);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (renaming) {
      setDraft(item.label);
      setTimeout(() => inputRef.current?.select(), 20);
    }
  }, [renaming, item.label]);

  function handleDragStart(e: React.DragEvent<HTMLLIElement>) {
    const payload = {
      mediaId: item.mediaId,
      aiBrief: item.aiBrief,
      aspectRatio: item.aspectRatio,
      kind: item.kind,
      label: item.label,
    };
    e.dataTransfer.setData(
      "application/x-flowboard-reference",
      JSON.stringify(payload),
    );
    e.dataTransfer.effectAllowed = "copy";
  }

  function handleClick() {
    // TODO(Phase 3): wire spawn-at-canvas-center via board store
    // (addReferenceNode). Intentionally a no-op for now so the card is
    // already focusable + keyboard-clickable when spawn lands.
  }

  async function commitRename() {
    const next = draft.trim();
    setRenaming(false);
    if (!next || next === item.label) return;
    try {
      await onRename(next);
    } catch {
      // Swallow — surface via store.error already.
    }
  }

  async function handleDelete(e: React.MouseEvent) {
    e.stopPropagation();
    const ok = window.confirm(
      `Delete reference "${item.label}"? The underlying image stays in storage.`,
    );
    if (!ok) return;
    try {
      await onDelete();
    } catch {
      // Swallow — surfaced via store.error.
    }
  }

  // Short id derived from sourceNodeShortId if present; otherwise show
  // the numeric ref id. Provenance-first because users tend to scan the
  // panel by "which node was this from?".
  const shortIdTag = item.sourceNodeShortId
    ? `#${item.sourceNodeShortId}`
    : `#${item.id}`;

  const tooltip = item.aiBrief
    ? `${item.label}\n\n${item.aiBrief}`
    : item.label;

  return (
    <li
      className="reference-card"
      draggable
      onDragStart={handleDragStart}
      onClick={handleClick}
      title={tooltip}
    >
      <div className="reference-card__thumb">
        {thumbBroken ? (
          <div className="reference-card__thumb-missing" aria-hidden="true">
            📷
          </div>
        ) : (
          <img
            src={mediaUrl(item.mediaId)}
            alt=""
            onError={() => setThumbBroken(true)}
            draggable={false}
          />
        )}
      </div>

      <div className="reference-card__body">
        {renaming ? (
          <input
            ref={inputRef}
            className="reference-card__rename-input"
            value={draft}
            maxLength={120}
            onChange={(e) => setDraft(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitRename();
              if (e.key === "Escape") {
                setRenaming(false);
                setDraft(item.label);
              }
            }}
          />
        ) : (
          <span className="reference-card__label">{item.label}</span>
        )}
        <span className="reference-card__id">{shortIdTag}</span>
      </div>

      <div className="reference-card__actions">
        <button
          type="button"
          className={`reference-card__action-btn${
            item.pinned ? " reference-card__action-btn--active" : ""
          }`}
          onClick={(e) => {
            e.stopPropagation();
            onTogglePin();
          }}
          aria-label={item.pinned ? "Unpin reference" : "Pin reference"}
          title={item.pinned ? "Unpin" : "Pin to top"}
        >
          {item.pinned ? "★" : "☆"}
        </button>
        <button
          type="button"
          className="reference-card__action-btn"
          onClick={(e) => {
            e.stopPropagation();
            setRenaming(true);
          }}
          aria-label="Rename reference"
          title="Rename"
        >
          ✎
        </button>
        <button
          type="button"
          className="reference-card__action-btn reference-card__action-btn--danger"
          onClick={handleDelete}
          aria-label="Delete reference"
          title="Delete"
        >
          🗑
        </button>
      </div>
    </li>
  );
}

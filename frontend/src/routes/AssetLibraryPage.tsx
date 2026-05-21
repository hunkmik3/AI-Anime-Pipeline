import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { mediaUrl, type ReferenceItem } from "../api/client";
import { useProjectStore } from "../store/project";
import { useReferencesStore, filterReferences } from "../store/references";

type KindFilter = "all" | ReferenceItem["kind"];

/**
 * Cross-project saved-reference library. Phase 3 keeps filtering
 * client-side because the backend ``GET /api/references`` doesn't accept
 * ``project_id`` yet (it stores the FK but doesn't filter on it). The
 * "Scope" pill below makes the scope explicit so users aren't surprised
 * when they see refs from other projects.
 */
export function AssetLibraryPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const currentProject = useProjectStore((s) => s.currentProject);
  const selectProject = useProjectStore((s) => s.selectProject);

  const items = useReferencesStore((s) => s.items);
  const loading = useReferencesStore((s) => s.loading);
  const loadReferences = useReferencesStore((s) => s.load);
  const removeRef = useReferencesStore((s) => s.remove);
  const renameRef = useReferencesStore((s) => s.rename);
  const togglePin = useReferencesStore((s) => s.togglePin);

  const [search, setSearch] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [renameTarget, setRenameTarget] = useState<{ id: number; label: string } | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  useEffect(() => {
    if (projectId && projectId !== useProjectStore.getState().currentProjectId) {
      void selectProject(projectId);
    }
    void loadReferences();
  }, [projectId, selectProject, loadReferences]);

  const filtered = useMemo(() => {
    let base = filterReferences(items, search);
    if (kind !== "all") base = base.filter((r) => r.kind === kind);
    if (pinnedOnly) base = base.filter((r) => r.pinned);
    return base;
  }, [items, search, kind, pinnedOnly]);

  function startRename(item: ReferenceItem) {
    setRenameTarget({ id: item.id, label: item.label });
    setRenameDraft(item.label);
  }

  async function commitRename() {
    if (!renameTarget) return;
    const next = renameDraft.trim();
    if (next && next !== renameTarget.label) {
      await renameRef(renameTarget.id, next);
    }
    setRenameTarget(null);
  }

  return (
    <div className="page page--asset-library">
      <header className="page-header">
        <div>
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <Link to="/projects">Projects</Link>
            <span aria-hidden="true">/</span>
            {currentProject ? (
              <Link to={`/projects/${currentProject.id}`}>{currentProject.name}</Link>
            ) : (
              <span>…</span>
            )}
            <span aria-hidden="true">/</span>
            <span>Library</span>
          </nav>
          <h1 className="page-title">Asset library</h1>
          <p className="page-subtitle">
            {filtered.length} of {items.length} references
            <span className="page-pill" title="Phase 4 will scope refs by project on the server side">
              Scope: all projects
            </span>
          </p>
        </div>
      </header>

      <div className="filters">
        <input
          type="search"
          className="filters__search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search label or AI brief…"
        />
        <select
          className="filters__select"
          value={kind}
          onChange={(e) => setKind(e.target.value as KindFilter)}
        >
          <option value="all">All kinds</option>
          <option value="image">image</option>
          <option value="character">character</option>
          <option value="visual_asset">visual_asset</option>
          <option value="storyboard_shot">storyboard_shot</option>
        </select>
        <label className="filters__checkbox">
          <input
            type="checkbox"
            checked={pinnedOnly}
            onChange={(e) => setPinnedOnly(e.target.checked)}
          />
          Pinned only
        </label>
      </div>

      {loading && items.length === 0 ? (
        <div className="page-loading">Loading references…</div>
      ) : filtered.length === 0 ? (
        <div className="page-empty">
          {items.length === 0
            ? "No references saved yet. Star a generated variant or upload to add one."
            : "No references match the current filters."}
        </div>
      ) : (
        <ul className="reference-grid">
          {filtered.map((ref) => (
            <li key={ref.id} className={`reference-card${ref.pinned ? " reference-card--pinned" : ""}`}>
              <button
                type="button"
                className="reference-card__pin"
                onClick={() => void togglePin(ref.id)}
                aria-label={ref.pinned ? "Unpin" : "Pin"}
                title={ref.pinned ? "Unpin" : "Pin"}
              >
                {ref.pinned ? "★" : "☆"}
              </button>
              <div className="reference-card__thumb">
                {/* Backend serves the media bytes — thumbs are full-res
                    today; Phase 5/6 will introduce a thumbnail variant. */}
                <img src={mediaUrl(ref.mediaId)} alt={ref.label} loading="lazy" />
              </div>
              <div className="reference-card__meta">
                <div className="reference-card__label">{ref.label}</div>
                <div className="reference-card__hint">
                  {ref.kind} · {ref.aspectRatio ?? "—"}
                </div>
                {ref.aiBrief && (
                  <div className="reference-card__brief">
                    {ref.aiBrief.slice(0, 140)}
                  </div>
                )}
              </div>
              <div className="reference-card__actions">
                <button
                  type="button"
                  className="btn btn--small"
                  onClick={() => startRename(ref)}
                >
                  Rename
                </button>
                <button
                  type="button"
                  className="btn btn--small btn--danger"
                  onClick={() => {
                    if (window.confirm(`Delete reference "${ref.label}"?`)) {
                      void removeRef(ref.id);
                    }
                  }}
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {renameTarget && (
        <div
          className="project-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) setRenameTarget(null);
          }}
        >
          <div className="project-modal" role="dialog" aria-modal="true">
            <h2 className="project-modal__title">Rename reference</h2>
            <input
              type="text"
              className="project-modal__input"
              autoFocus
              value={renameDraft}
              onChange={(e) => setRenameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void commitRename();
                if (e.key === "Escape") setRenameTarget(null);
              }}
            />
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={() => setRenameTarget(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--primary"
                onClick={() => void commitRename()}
              >
                Save
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { mediaUrl, thumbUrl, uploadImage, type ReferenceItem } from "../api/client";
import { BreakableName } from "../components/BreakableName";
import { useProjectStore } from "../store/project";
import { useReferencesStore, filterReferences } from "../store/references";

type KindFilter = "all" | ReferenceItem["kind"];

const IMG_RE = /^image\/(png|jpe?g|webp|gif)$/i;

/** Open a native picker (multi-file, or a whole folder) and return the files. */
function pickFiles(opts: { multiple?: boolean; directory?: boolean }): Promise<File[]> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp,image/gif";
    if (opts.multiple) input.multiple = true;
    if (opts.directory) {
      // Non-standard but supported in Chrome/Edge/Safari — picks a folder and
      // returns every file inside (we filter to images).
      (input as unknown as { webkitdirectory: boolean }).webkitdirectory = true;
    }
    input.onchange = () => resolve(input.files ? Array.from(input.files) : []);
    input.click();
  });
}

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
  const saveRef = useReferencesStore((s) => s.save);
  const removeRef = useReferencesStore((s) => s.remove);
  const renameRef = useReferencesStore((s) => s.rename);
  const togglePin = useReferencesStore((s) => s.togglePin);

  const [search, setSearch] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [renameTarget, setRenameTarget] = useState<{ id: number; label: string } | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  // full-size image preview (lightbox)
  const [preview, setPreview] = useState<{ mediaId: string; label: string } | null>(null);

  useEffect(() => {
    if (projectId && projectId !== useProjectStore.getState().currentProjectId) {
      void selectProject(projectId);
    }
    // Scope the library to this project.
    void loadReferences(projectId ?? null);
  }, [projectId, selectProject, loadReferences]);

  useEffect(() => {
    if (!preview) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPreview(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [preview]);

  async function handleUploadMany(files: File[]) {
    if (!projectId) return;
    const imgs = files.filter((f) => IMG_RE.test(f.type));
    if (imgs.length === 0) {
      // eslint-disable-next-line no-alert
      alert("No image files found (png / jpg / webp / gif).");
      return;
    }
    setUploading(true);
    setProgress({ done: 0, total: imgs.length });
    let failures = 0;
    // Small concurrency pool so a big folder uploads quickly without hammering.
    const queue = [...imgs];
    let done = 0;
    async function worker() {
      for (;;) {
        const f = queue.shift();
        if (!f) return;
        try {
          const { media_id, aspect_ratio } = await uploadImage(f, projectId!);
          await saveRef({
            media_id,
            kind: "image",
            label: f.name.replace(/\.[^.]+$/, "").slice(0, 80) || "Upload",
            aspect_ratio: aspect_ratio ?? null,
            project_id: projectId,
          });
        } catch {
          failures++;
        } finally {
          done++;
          setProgress({ done, total: imgs.length });
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(4, imgs.length) }, worker));
    setUploading(false);
    setProgress(null);
    if (failures > 0) {
      // eslint-disable-next-line no-alert
      alert(`${failures} of ${imgs.length} file(s) failed to upload.`);
    }
  }

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
            <span className="page-pill" title="This library belongs to this project only">
              {currentProject ? currentProject.name : "This project"}
            </span>
          </p>
        </div>
        <div className="page-header__actions">
          <button
            type="button"
            className="btn btn--primary"
            disabled={uploading}
            onClick={async () => {
              const fs = await pickFiles({ multiple: true });
              if (fs.length) void handleUploadMany(fs);
            }}
          >
            {uploading && progress
              ? `Uploading ${progress.done}/${progress.total}…`
              : "⬆ Upload files"}
          </button>
          <button
            type="button"
            className="btn"
            disabled={uploading}
            title="Upload every image inside a folder"
            onClick={async () => {
              const fs = await pickFiles({ directory: true });
              if (fs.length) void handleUploadMany(fs);
            }}
          >
            📁 Upload folder
          </button>
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
            ? "No materials in this project's library yet. Use “⬆ Upload files” or “📁 Upload folder” to add some, or ★ a generated variant."
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
              <button
                type="button"
                className="reference-card__thumb"
                onClick={() => setPreview({ mediaId: ref.mediaId, label: ref.label })}
                title="Click to view full size"
                aria-label={`View ${ref.label} full size`}
              >
                <img
                  src={thumbUrl(ref.mediaId, 480)}
                  alt={ref.label}
                  loading="lazy"
                  decoding="async"
                />
              </button>
              <div className="reference-card__meta">
                <div className="reference-card__label" title={ref.label}>
                  <BreakableName text={ref.label} />
                </div>
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

      {preview && (
        <div
          className="lightbox"
          role="dialog"
          aria-modal="true"
          aria-label={preview.label}
          onClick={() => setPreview(null)}
        >
          <button
            className="lightbox__close"
            onClick={() => setPreview(null)}
            aria-label="Close"
          >
            ×
          </button>
          <img
            className="lightbox__img"
            src={mediaUrl(preview.mediaId)}
            alt={preview.label}
            onClick={(e) => e.stopPropagation()}
          />
          <div className="lightbox__caption">{preview.label}</div>
        </div>
      )}
    </div>
  );
}

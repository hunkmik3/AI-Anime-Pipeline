import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { thumbUrl, setProjectCover, uploadImage } from "../api/client";
import { BreakableName } from "../components/BreakableName";
import { useProjectStore } from "../store/project";
import { useAuthStore } from "../store/auth";
import { StudioNav } from "../components/shell/StudioNav";

/** Open a native file picker and resolve with the chosen image (or null). */
function pickImageFile(): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp";
    input.onchange = () => resolve(input.files?.[0] ?? null);
    input.click();
  });
}

/**
 * Top-level grid of projects. First view the user lands on after launch.
 * Phase 3 keeps thumbnails / activity-feed placeholders empty — real
 * thumbnails wait on Phase 5/6 (no shot videos yet) and cost rollups on
 * Phase 7. Today this page is a working stand-in: create a new project,
 * pick an existing one, or delete.
 */
export function ProjectListPage() {
  const projects = useProjectStore((s) => s.projects);
  const loading = useProjectStore((s) => s.loading);
  const error = useProjectStore((s) => s.error);
  const createProject = useProjectStore((s) => s.createProject);
  const deleteProject = useProjectStore((s) => s.deleteProject);
  const loadProjects = useProjectStore((s) => s.loadProjects);
  const isAdmin = useAuthStore((s) => s.isAdmin());
  const navigate = useNavigate();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [coverBusy, setCoverBusy] = useState<string | null>(null);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  async function handleCover(projectId: string, file: File) {
    setCoverBusy(projectId);
    try {
      const { media_id } = await uploadImage(file, projectId);
      await setProjectCover(projectId, media_id);
      await loadProjects();
    } catch (e) {
      // eslint-disable-next-line no-alert
      alert(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setCoverBusy(null);
    }
  }

  async function handleCreate() {
    if (busy) return;
    const name = draftName.trim() || "Untitled";
    setBusy(true);
    try {
      const project = await createProject(name);
      setDialogOpen(false);
      setDraftName("");
      if (project) navigate(`/projects/${project.id}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      await deleteProject(deleteTarget.id);
    } finally {
      setBusy(false);
      setDeleteTarget(null);
    }
  }

  return (
    <div className="page page--project-list">
      <StudioNav />
      <header className="page-header">
        <h1 className="page-title">Projects</h1>
        {isAdmin && (
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => {
              setDraftName("Untitled");
              setDialogOpen(true);
            }}
          >
            + New project
          </button>
        )}
      </header>

      {error && <div className="page-error" role="alert">{error}</div>}

      {loading && projects.length === 0 ? (
        <div className="page-loading">Loading projects…</div>
      ) : projects.length === 0 ? (
        <div className="page-empty">
          {isAdmin ? (
            <>
              No projects yet. Click <strong>New project</strong> to get started.
            </>
          ) : (
            "No projects assigned to you yet. Contact an admin to get one."
          )}
        </div>
      ) : (
        <ul className="project-grid">
          {projects.map((p) => {
            const label = p.name || "Untitled";
            // Deterministic monogram + gradient until a real cover image is set.
            const mono = label
              .split(/\s+/)
              .filter(Boolean)
              .slice(0, 2)
              .map((w) => w[0])
              .join("")
              .toUpperCase() || "U";
            let h = 0;
            for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) % 360;
            return (
            <li key={p.id} className="project-card">
              <Link to={`/projects/${p.id}`} className="project-card__body">
                <div
                  className="project-card__thumb"
                  aria-hidden="true"
                  style={{
                    background: `linear-gradient(135deg, hsl(${h} 40% 24%), hsl(${(h + 45) % 360} 44% 15%))`,
                  }}
                >
                  {p.thumb_media_id ? (
                    <img
                      className="project-card__img"
                      src={thumbUrl(p.thumb_media_id, 400)}
                      alt=""
                      loading="lazy"
                      onError={(e) => {
                        // fall back to the monogram if the media can't load
                        (e.currentTarget as HTMLImageElement).style.display = "none";
                      }}
                    />
                  ) : null}
                  <span className="project-card__mono">{mono}</span>

                  {/* hover-to-upload cover — a button (not the nav Link) that
                      opens a file picker in JS, so it never navigates. */}
                  <button
                    type="button"
                    className={`project-card__upload${coverBusy === p.id ? " is-busy" : ""}`}
                    title="Upload a cover thumbnail"
                    disabled={coverBusy === p.id}
                    onClick={async (e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      const f = await pickImageFile();
                      if (f) void handleCover(p.id, f);
                    }}
                  >
                    {coverBusy === p.id ? (
                      "Uploading…"
                    ) : (
                      <>
                        <svg
                          viewBox="0 0 24 24"
                          width="14"
                          height="14"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.8"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          aria-hidden="true"
                        >
                          <path d="M12 16V4M6 10l6-6 6 6M4 20h16" />
                        </svg>
                        {p.thumb_media_id ? "Change" : "Thumbnail"}
                      </>
                    )}
                  </button>
                </div>
                <div className="project-card__meta">
                  <div className="project-card__name" title={label}>
                    <BreakableName text={label} />
                  </div>
                  <div className="project-card__hint">
                    Created{" "}
                    {p.created_at
                      ? new Date(p.created_at).toLocaleDateString()
                      : "—"}
                  </div>
                </div>
              </Link>
              {isAdmin && (
                <button
                  type="button"
                  className="project-card__delete"
                  onClick={() => setDeleteTarget({ id: p.id, name: p.name })}
                  aria-label={`Delete ${p.name}`}
                  title="Delete project"
                >
                  ✕
                </button>
              )}
            </li>
            );
          })}
        </ul>
      )}

      {dialogOpen && (
        <div
          className="project-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget && !busy) setDialogOpen(false);
          }}
        >
          <div className="project-modal" role="dialog" aria-modal="true">
            <h2 className="project-modal__title">New project</h2>
            <p className="project-modal__hint">
              The project name shown in the sidebar and Project Dashboard. You
              can change it later.
            </p>
            <input
              type="text"
              className="project-modal__input"
              autoFocus
              maxLength={120}
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void handleCreate();
                if (e.key === "Escape" && !busy) setDialogOpen(false);
              }}
              disabled={busy}
            />
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={() => setDialogOpen(false)}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--primary"
                onClick={() => void handleCreate()}
                disabled={busy}
              >
                {busy ? "Creating…" : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div
          className="project-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget && !busy) setDeleteTarget(null);
          }}
        >
          <div className="project-modal" role="dialog" aria-modal="true">
            <h2 className="project-modal__title">Delete project?</h2>
            <p className="project-modal__hint">
              <strong>"{deleteTarget.name}"</strong> will be permanently deleted
              along with all episodes, sequences, nodes, edges and assets. This cannot
              be undone.
            </p>
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={() => setDeleteTarget(null)}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--danger"
                onClick={() => void handleDelete()}
                disabled={busy}
              >
                {busy ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useProjectStore } from "../store/project";

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
  const navigate = useNavigate();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

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
      <header className="page-header">
        <h1 className="page-title">Projects</h1>
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
      </header>

      {error && <div className="page-error" role="alert">{error}</div>}

      {loading && projects.length === 0 ? (
        <div className="page-loading">Loading projects…</div>
      ) : projects.length === 0 ? (
        <div className="page-empty">
          No projects yet. Click <strong>New project</strong> to get started.
        </div>
      ) : (
        <ul className="project-grid">
          {projects.map((p) => (
            <li key={p.id} className="project-card">
              <Link to={`/projects/${p.id}`} className="project-card__body">
                <div className="project-card__thumb" aria-hidden="true" />
                <div className="project-card__meta">
                  <div className="project-card__name">{p.name || "Untitled"}</div>
                  <div className="project-card__hint">
                    Created{" "}
                    {p.created_at
                      ? new Date(p.created_at).toLocaleDateString()
                      : "—"}
                  </div>
                </div>
              </Link>
              <button
                type="button"
                className="project-card__delete"
                onClick={() => setDeleteTarget({ id: p.id, name: p.name })}
                aria-label={`Delete ${p.name}`}
                title="Delete project"
              >
                ✕
              </button>
            </li>
          ))}
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
              Tên project hiển thị trong sidebar và Project Dashboard. Có thể
              đổi sau.
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
              <strong>"{deleteTarget.name}"</strong> sẽ bị xoá vĩnh viễn cùng
              với toàn bộ scenes, shots, nodes, edges và assets. Không thể
              khôi phục.
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

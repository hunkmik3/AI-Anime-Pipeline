import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useProjectStore } from "../store/project";

/**
 * Phase 3 project-aware sidebar. Reads from ``useProjectStore`` (post-
 * board era) and uses React Router links so clicks deep-link into
 * ``/projects/:id`` without going through any store imperative.
 */
export function ProjectSidebar() {
  const projects = useProjectStore((s) => s.projects);
  const activeId = useProjectStore((s) => s.currentProjectId);
  const createProject = useProjectStore((s) => s.createProject);
  const deleteProject = useProjectStore((s) => s.deleteProject);
  const renameProject = useProjectStore((s) => s.renameProject);

  const location = useLocation();
  const navigate = useNavigate();

  const [collapsed, setCollapsed] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const renameInputRef = useRef<HTMLInputElement>(null);
  const [newDialogOpen, setNewDialogOpen] = useState(false);
  const [newDialogName, setNewDialogName] = useState("");
  const [newDialogBusy, setNewDialogBusy] = useState(false);
  const newDialogInputRef = useRef<HTMLInputElement>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  useEffect(() => {
    if (renamingId !== null) {
      setTimeout(() => renameInputRef.current?.select(), 30);
    }
  }, [renamingId]);

  useEffect(() => {
    if (openMenuId === null) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (
        t
        && !t.closest(".project-sidebar__menu")
        && !t.closest(".project-sidebar__kebab")
      ) {
        setOpenMenuId(null);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [openMenuId]);

  function handleNew() {
    setNewDialogName("Untitled");
    setNewDialogOpen(true);
    setTimeout(() => newDialogInputRef.current?.select(), 30);
  }

  function closeNewDialog() {
    if (newDialogBusy) return;
    setNewDialogOpen(false);
    setNewDialogName("");
  }

  async function commitNewDialog() {
    if (newDialogBusy) return;
    const name = newDialogName.trim() || "Untitled";
    setNewDialogBusy(true);
    try {
      const project = await createProject(name);
      if (project) navigate(`/projects/${project.id}`);
    } finally {
      setNewDialogBusy(false);
      setNewDialogOpen(false);
      setNewDialogName("");
    }
  }

  useEffect(() => {
    if (!newDialogOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeNewDialog();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newDialogOpen, newDialogBusy]);

  function startRename(id: string, currentName: string) {
    setRenamingId(id);
    setRenameDraft(currentName);
    setOpenMenuId(null);
  }

  async function commitRename() {
    if (renamingId === null) return;
    const name = renameDraft.trim();
    if (!name) {
      setRenamingId(null);
      return;
    }
    await renameProject(renamingId, name);
    setRenamingId(null);
  }

  function openDeleteConfirm(id: string, name: string) {
    setOpenMenuId(null);
    setDeleteTarget({ id, name });
  }

  async function commitDelete() {
    if (!deleteTarget || deleteBusy) return;
    setDeleteBusy(true);
    try {
      await deleteProject(deleteTarget.id);
      // If the deleted one was active, bounce to /projects.
      if (location.pathname.includes(deleteTarget.id)) {
        navigate("/projects");
      }
    } finally {
      setDeleteBusy(false);
      setDeleteTarget(null);
    }
  }

  function cancelDelete() {
    if (deleteBusy) return;
    setDeleteTarget(null);
  }

  useEffect(() => {
    if (!deleteTarget) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") cancelDelete();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deleteTarget, deleteBusy]);

  return (
    <aside className={`project-sidebar${collapsed ? " project-sidebar--collapsed" : ""}`}>
      <div className="project-sidebar__header">
        {!collapsed && (
          <Link to="/projects" className="project-sidebar__title">
            Projects
          </Link>
        )}
        <button
          type="button"
          className="project-sidebar__icon-btn"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "›" : "‹"}
        </button>
      </div>
      {!collapsed && (
        <>
          <button
            type="button"
            className="project-sidebar__new"
            onClick={handleNew}
          >
            <span aria-hidden="true">+</span> New project
          </button>
          <ul className="project-sidebar__list">
            {projects.map((p) => {
              const isActive = p.id === activeId;
              const isRenaming = p.id === renamingId;
              return (
                <li
                  key={p.id}
                  className={`project-sidebar__item${isActive ? " project-sidebar__item--active" : ""}`}
                >
                  {isRenaming ? (
                    <input
                      ref={renameInputRef}
                      className="project-sidebar__rename-input"
                      value={renameDraft}
                      onChange={(e) => setRenameDraft(e.target.value)}
                      onBlur={commitRename}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") commitRename();
                        if (e.key === "Escape") setRenamingId(null);
                      }}
                    />
                  ) : (
                    <>
                      <Link
                        to={`/projects/${p.id}`}
                        className="project-sidebar__name"
                        title={p.name}
                      >
                        {p.name || "Untitled"}
                      </Link>
                      <button
                        type="button"
                        className="project-sidebar__kebab"
                        onClick={() =>
                          setOpenMenuId((cur) => (cur === p.id ? null : p.id))
                        }
                        aria-label="Project actions"
                      >
                        ⋯
                      </button>
                      {openMenuId === p.id && (
                        <div className="project-sidebar__menu" role="menu">
                          <button
                            type="button"
                            onClick={() => startRename(p.id, p.name)}
                          >
                            Rename
                          </button>
                          <button
                            type="button"
                            className="project-sidebar__menu-danger"
                            onClick={() => openDeleteConfirm(p.id, p.name)}
                          >
                            Delete
                          </button>
                        </div>
                      )}
                    </>
                  )}
                </li>
              );
            })}
            {projects.length === 0 && (
              <li className="project-sidebar__empty">No projects yet</li>
            )}
          </ul>
        </>
      )}

      {deleteTarget && (
        <div
          className="project-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) cancelDelete();
          }}
        >
          <div
            className="project-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-project-title"
          >
            <h2 id="delete-project-title" className="project-modal__title">
              Delete project?
            </h2>
            <p className="project-modal__hint">
              <strong>"{deleteTarget.name}"</strong> sẽ bị xoá vĩnh viễn cùng
              với tất cả scenes, shots, nodes, edges và assets bên trong.
              Không thể khôi phục.
            </p>
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={cancelDelete}
                disabled={deleteBusy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--danger"
                onClick={commitDelete}
                disabled={deleteBusy}
                autoFocus
              >
                {deleteBusy ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}

      {newDialogOpen && (
        <div
          className="project-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeNewDialog();
          }}
        >
          <div
            className="project-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="new-project-title"
          >
            <h2 id="new-project-title" className="project-modal__title">
              New project
            </h2>
            <p className="project-modal__hint">
              Tên project hiển thị trong sidebar. Có thể đổi sau.
            </p>
            <input
              ref={newDialogInputRef}
              className="project-modal__input"
              type="text"
              maxLength={120}
              value={newDialogName}
              onChange={(e) => setNewDialogName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitNewDialog();
                if (e.key === "Escape") closeNewDialog();
              }}
              placeholder="Untitled"
              disabled={newDialogBusy}
              autoFocus
            />
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={closeNewDialog}
                disabled={newDialogBusy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--primary"
                onClick={commitNewDialog}
                disabled={newDialogBusy}
              >
                {newDialogBusy ? "Creating…" : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}

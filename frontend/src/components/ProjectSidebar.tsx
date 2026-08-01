import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Brand } from "./shell/Brand";

import { useProjectStore } from "../store/project";
import { useSeriesStore } from "../store/series";
import { useSceneStore } from "../store/scene";
import { useAuthStore } from "../store/auth";
import { BreakableName } from "./BreakableName";

/**
 * Phase 3 project-aware sidebar. Reads from ``useProjectStore`` (post-
 * board era) and uses React Router links so clicks deep-link into
 * ``/projects/:id`` without going through any store imperative.
 */
export function ProjectSidebar({ showBrand = true }: { showBrand?: boolean } = {}) {
  const projects = useProjectStore((s) => s.projects);
  const activeId = useProjectStore((s) => s.currentProjectId);
  const createProject = useProjectStore((s) => s.createProject);
  const deleteProject = useProjectStore((s) => s.deleteProject);
  const renameProject = useProjectStore((s) => s.renameProject);
  // Phase 9.1: only admins create/rename/delete project structure. A normal
  // user just opens the projects assigned to them.
  const isAdmin = useAuthStore((s) => s.isAdmin());

  // Phase 10: inline hierarchy — expand a project to reveal its Series, and a
  // Series to reveal its Episodes/Chapters, all in the sidebar tree.
  const seriesByProject = useSeriesStore((s) => s.byProject);
  const loadSeries = useSeriesStore((s) => s.loadSeries);
  const scenesByProject = useSceneStore((s) => s.scenesByProject);
  const loadScenes = useSceneStore((s) => s.loadScenes);

  const location = useLocation();
  const navigate = useNavigate();

  const [collapsed, setCollapsed] = useState(false);
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());
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

  function ensureLoaded(pid: string) {
    if (!seriesByProject[pid]) void loadSeries(pid);
    if (!scenesByProject[pid]) void loadScenes(pid);
  }

  function toggleProject(pid: string) {
    setExpandedProjects((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else {
        next.add(pid);
        ensureLoaded(pid);
      }
      return next;
    });
  }

  // Auto-expand the active project so its tree is visible on arrival.
  useEffect(() => {
    if (!activeId) return;
    setExpandedProjects((prev) => {
      if (prev.has(activeId)) return prev;
      const next = new Set(prev);
      next.add(activeId);
      return next;
    });
    ensureLoaded(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

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
      {!collapsed && showBrand && (
        <div className="project-sidebar__logo-row">
          <Brand />
        </div>
      )}
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
          {isAdmin && (
            <button
              type="button"
              className="project-sidebar__new"
              onClick={handleNew}
            >
              <span aria-hidden="true">+</span> New project
            </button>
          )}
          <ul className="project-sidebar__list">
            {projects.map((p) => {
              const isActive = p.id === activeId;
              const isRenaming = p.id === renamingId;
              const isOpen = expandedProjects.has(p.id);
              const series = (seriesByProject[p.id] ?? [])
                .slice()
                .sort((a, b) => a.order_index - b.order_index);
              const scenes = scenesByProject[p.id] ?? [];
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
                      <div className="project-sidebar__row">
                        <button
                          type="button"
                          className="project-sidebar__twisty"
                          aria-label={isOpen ? "Collapse" : "Expand"}
                          aria-expanded={isOpen}
                          onClick={() => toggleProject(p.id)}
                        >
                          {isOpen ? "▾" : "▸"}
                        </button>
                        <Link
                          to={`/projects/${p.id}`}
                          className="project-sidebar__name"
                          title={p.name}
                        >
                          <BreakableName text={p.name || "Untitled"} />
                        </Link>
                        {isAdmin && (
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
                        )}
                        {isAdmin && openMenuId === p.id && (
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
                      </div>

                      {isOpen && (
                        <ul className="project-sidebar__tree">
                          {series.length === 0 ? (
                            <li className="project-sidebar__tree-empty">No series yet</li>
                          ) : (
                            series.map((se) => {
                              // Episodes are NOT listed here — the sidebar stops
                              // at Series; clicking one shows its episodes on the
                              // main project screen (deep-linked via #series-id).
                              const epCount = scenes.filter(
                                (sc) => sc.series_id === se.id,
                              ).length;
                              const seActive = location.hash === `#series-${se.id}`;
                              return (
                                <li key={se.id} className="project-sidebar__series">
                                  <Link
                                    to={`/projects/${p.id}#series-${se.id}`}
                                    className={`project-sidebar__series-row${seActive ? " is-active" : ""}`}
                                    title={se.name}
                                  >
                                    <span className="project-sidebar__series-name">
                                      {se.code ? (
                                        <span className="project-sidebar__code">{se.code}</span>
                                      ) : null}
                                      {se.name}
                                    </span>
                                    <span className="project-sidebar__count">{epCount}</span>
                                  </Link>
                                </li>
                              );
                            })
                          )}
                        </ul>
                      )}
                    </>
                  )}
                </li>
              );
            })}
            {projects.length === 0 && (
              <li className="project-sidebar__empty">
                {isAdmin
                  ? "No projects yet"
                  : "No projects assigned to you yet."}
              </li>
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
              <strong>"{deleteTarget.name}"</strong> will be permanently deleted
              along with all episodes, sequences, nodes, edges and assets inside it.
              This cannot be undone.
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
              The project name shown in the sidebar. You can change it later.
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

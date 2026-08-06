import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  createFlowProject,
  deleteFlowProject,
  listFlowProjects,
  reorderFlowProjects,
  thumbUrl,
  updateFlowProject,
  uploadFlowImage,
  type FlowProject,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { useGiantflowRole } from "../store/giantflowRole";
import { toast } from "../store/toast";
import { GiantflowNav } from "./GiantflowNav";
import { useDragOrder } from "./useDragOrder";

/**
 * The top of the tree: the studio's slates.
 *
 * Four tiers, not three — Project → Series → Batch → Panel. What used to sit on
 * this screen was the comics themselves; those are Series, and they moved one
 * level down. A Project holds a name and a cover and nothing else, because it is
 * a container, not a unit of work.
 */
export function PanelProjectsPage() {
  const { can } = useGiantflowRole();
  const [projects, setProjects] = useState<FlowProject[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setProjects(await listFlowProjects());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

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

  const { list, dragProps } = useDragOrder(projects ?? [], async (ids) => {
    await reorderFlowProjects(ids);
    await load();
  });

  async function create(name: string) {
    const clean = name.trim();
    if (!clean) return;
    setBusy(true);
    try {
      await createFlowProject(clean);
      await load();
      toast("Project created. Add the comics inside it as series.");
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shellpage pn__wide">
      <GiantflowNav />
      <PageHeader title="Projects" />

      {error ? <p className="inbox__err">{error}</p> : null}
      {projects === null ? <p className="rfoot">Loading…</p> : null}

      {projects !== null && projects.length === 0 ? (
        <div className="inbox__empty">
          <b>No projects yet.</b>
          A project is the slate. Create one, then add each comic inside it as a
          series.
        </div>
      ) : null}

      <ul className="pn__tiles">
        {list.map((p) => (
          <ProjectCard
            key={p.id}
            project={p}
            onChanged={load}
            drag={can("project.manage") ? dragProps(p.id) : {}}
            manage={can("project.manage")}
          />
        ))}
        {can("project.manage") ? <AddProjectTile busy={busy} onCreate={create} /> : null}
      </ul>
    </div>
  );
}

function AddProjectTile({
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
          <span>Add Project</span>
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
          placeholder="Project name…"
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

function ProjectCard({
  project,
  onChanged,
  drag,
  manage,
}: {
  project: FlowProject;
  onChanged: () => Promise<void>;
  drag: Record<string, unknown>;
  manage: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(project.name);
  const nameRef = useRef<HTMLInputElement | null>(null);
  const pct = project.panel_count
    ? Math.round((project.approved_count / project.panel_count) * 100)
    : 0;

  useEffect(() => {
    if (editing) {
      setDraft(project.name);
      requestAnimationFrame(() => nameRef.current?.select());
    }
  }, [editing, project.name]);

  async function rename() {
    const clean = draft.trim();
    if (!clean || clean === project.name) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await updateFlowProject(project.id, { name: clean });
      setEditing(false);
      await onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Rename failed");
    } finally {
      setBusy(false);
    }
  }

  async function setCover() {
    const file = await pickImage();
    if (!file) return;
    setBusy(true);
    try {
      const { media_id } = await uploadFlowImage(file);
      await updateFlowProject(project.id, { cover_media_id: media_id, set_cover: true });
      await onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="pn__tile" {...drag}>
      <Link to={`/giantflow/p/${project.id}`} className="pn__tile-body">
        <div className="pn__tile-thumb">
          {project.thumb_media_id ? (
            <img
              src={thumbUrl(project.thumb_media_id, 400)}
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
                  setDraft(project.name);
                  setEditing(false);
                }
              }}
            />
          ) : (
            <div className="pn__tile-name" title={project.name}>
              {project.name}
            </div>
          )}
          <div className="pn__tile-sub">
            {project.series_count === 0
              ? "No series yet"
              : `${project.series_count} series · ${project.approved_count}/${project.panel_count} approved`}
          </div>
          {project.panel_count > 0 ? (
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
            title="Rename this project"
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
            onClick={(e) => {
              e.preventDefault();
              void setCover();
            }}
          >
            {busy ? "Uploading…" : project.has_cover ? "Change" : "Thumbnail"}
          </button>
          {project.has_cover ? (
            <button
              type="button"
              className="pn__tile-btn"
              title="Clear the cover (back to the first series' cover)"
              onClick={async (e) => {
                e.preventDefault();
                await updateFlowProject(project.id, { cover_media_id: null, set_cover: true });
                await onChanged();
              }}
            >
              Reset
            </button>
          ) : null}
          <button
            type="button"
            className="pn__tile-btn pn__tile-btn--danger"
            title="Delete this project"
            onClick={async (e) => {
              e.preventDefault();
              if (
                !window.confirm(
                  `Delete “${project.name}”, its ${project.series_count} series and ${project.panel_count} panel(s)?`,
                )
              )
                return;
              try {
                await deleteFlowProject(project.id);
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

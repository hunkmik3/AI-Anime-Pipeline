import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  createPanelProject,
  deletePanelProject,
  listPanelProjects,
  type PanelProject,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { toast } from "../store/toast";

/**
 * Giantflow home — one card per comic being adapted.
 *
 * A project holds nothing but a name and its batches. The material lives one
 * level down: each batch is one artist's share and carries its own imported
 * folder, because the studio hands work out already divided rather than dumping
 * a chapter in one pile and splitting it afterwards.
 */
export function PanelProjectsPage() {
  const [projects, setProjects] = useState<PanelProject[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setProjects(await listPanelProjects());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function create() {
    const clean = name.trim();
    if (!clean) return;
    setBusy(true);
    try {
      await createPanelProject(clean);
      setName("");
      await load();
      toast("Project created. Add a batch per artist inside it.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shellpage">
      <PageHeader
        title="Giantflow"
        subtitle="Comic adaptation, panel by panel. A project is one comic; inside it, a batch per artist carries that artist's panels."
        actions={
          <div className="pn__newrow">
            <input
              className="inbox__input"
              placeholder="New project name…"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void create();
              }}
            />
            <button
              className="btn2 btn2--primary"
              disabled={busy || !name.trim()}
              onClick={() => void create()}
            >
              Create
            </button>
          </div>
        }
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {projects === null ? <p className="rfoot">Loading…</p> : null}

      {projects !== null && projects.length === 0 ? (
        <div className="inbox__empty">
          <b>No projects yet.</b>
          Create one per comic. Inside it you make a batch per artist and import
          that artist's panels.
        </div>
      ) : null}

      <ul className="pn__projects">
        {(projects ?? []).map((p) => (
          <ProjectCard key={p.id} project={p} onChanged={load} />
        ))}
      </ul>
    </div>
  );
}

function ProjectCard({
  project,
  onChanged,
}: {
  project: PanelProject;
  onChanged: () => Promise<void>;
}) {
  const pct = project.panel_count
    ? Math.round((project.approved_count / project.panel_count) * 100)
    : 0;

  return (
    <li className="pn__project">
      <Link to={`/giantflow/${project.id}`} className="pn__project-body">
        <div className="pn__project-name">{project.name}</div>
        <div className="pn__project-stat">
          {project.batch_count === 0 ? (
            <span className="pn__muted">No batches yet</span>
          ) : (
            <>
              <b>{project.batch_count}</b> batch{project.batch_count === 1 ? "" : "es"} ·{" "}
              <b>{project.approved_count}</b> / {project.panel_count} approved
              <span className="pn__bar">
                <span className="pn__bar-fill" style={{ width: `${pct}%` }} />
              </span>
            </>
          )}
        </div>
      </Link>

      <div className="pn__project-acts">
        <button
          className="btn2 btn2--danger"
          title="Delete this project and its panels"
          onClick={async () => {
            if (
              !window.confirm(
                `Delete “${project.name}”, its ${project.batch_count} batch(es) and ${project.panel_count} panel(s)? ` +
                  `The generated images stay on disk, but this catalogue does not.`,
              )
            )
              return;
            try {
              await deletePanelProject(project.id);
              await onChanged();
            } catch (e) {
              toast(e instanceof Error ? e.message : "Delete failed");
            }
          }}
        >
          ✕
        </button>
      </div>
    </li>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  createPanelProject,
  deletePanelProject,
  importPanelFolder,
  listPanelProjects,
  type PanelProject,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { toast } from "../store/toast";

/**
 * Giantflow home — one card per comic being adapted.
 *
 * A project is created empty and then has a raw-material folder imported into it,
 * rather than "create from folder" in one step: naming and importing fail for
 * different reasons, and a 300-file upload that dies on the last file should not
 * also lose the name you typed.
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
      toast("Project created. Import its panel folder next.");
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
        subtitle="Comic adaptation, panel by panel. Import a folder of cut panels, assign them, generate, review."
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
          Create one, then import the folder of panels the cutter delivered.
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
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [importing, setImporting] = useState<string | null>(null);
  const pct = project.panel_count
    ? Math.round((project.approved_count / project.panel_count) * 100)
    : 0;

  async function onPick(files: FileList | null) {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    setImporting(`Uploading ${list.length} file${list.length === 1 ? "" : "s"}…`);
    try {
      const r = await importPanelFolder(project.id, list);
      await onChanged();
      const skipped = r.skipped_count
        ? ` ${r.skipped_count} file(s) skipped (not images).`
        : "";
      toast(`${r.panels.length} panels imported.${skipped}`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Import failed");
    } finally {
      setImporting(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <li className="pn__project">
      <Link to={`/giantflow/${project.id}`} className="pn__project-body">
        <div className="pn__project-name">{project.name}</div>
        <div className="pn__project-stat">
          {project.panel_count === 0 ? (
            <span className="pn__muted">No panels yet</span>
          ) : (
            <>
              <b>{project.approved_count}</b> / {project.panel_count} approved
              <span className="pn__bar">
                <span className="pn__bar-fill" style={{ width: `${pct}%` }} />
              </span>
            </>
          )}
        </div>
      </Link>

      <div className="pn__project-acts">
        {/* Only offered while the project is empty: a second folder is refused
            server-side, because interleaving two numbering schemes cannot be
            undone by hand. */}
        {project.panel_count === 0 ? (
          <>
            <input
              ref={fileRef}
              type="file"
              // Non-standard but the only way to pick a FOLDER; the panel code
              // comes from each file's path inside it.
              {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
              multiple
              hidden
              onChange={(e) => void onPick(e.target.files)}
            />
            <button
              className="btn2"
              disabled={!!importing}
              onClick={() => fileRef.current?.click()}
            >
              {importing ?? "Import panel folder"}
            </button>
          </>
        ) : null}
        <button
          className="btn2 btn2--danger"
          title="Delete this project and its panels"
          onClick={async () => {
            if (
              !window.confirm(
                `Delete “${project.name}” and its ${project.panel_count} panel(s)? ` +
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

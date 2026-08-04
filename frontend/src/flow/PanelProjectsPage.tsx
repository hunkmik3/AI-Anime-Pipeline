import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  createPanelProject,
  deletePanelProject,
  listPanelProjects,
  setPanelProjectCover,
  thumbUrl,
  uploadFlowImage,
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

      <ul className="pn__tiles">
        {(projects ?? []).map((p) => (
          <ProjectCard key={p.id} project={p} onChanged={load} />
        ))}
      </ul>
    </div>
  );
}

/** Pick one image file. A plain input rather than a component: it is two lines,
 *  and the tile is the only place that needs it. */
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
}: {
  project: PanelProject;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const pct = project.panel_count
    ? Math.round((project.approved_count / project.panel_count) * 100)
    : 0;

  async function setCover() {
    const file = await pickImage();
    if (!file) return;
    setBusy(true);
    try {
      // Two steps on purpose: the upload caches bytes and hands back a media id,
      // which is then pointed at — the same id any other surface could reuse.
      const { media_id } = await uploadFlowImage(file);
      await setPanelProjectCover(project.id, media_id);
      await onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="pn__tile">
      <Link to={`/giantflow/${project.id}`} className="pn__tile-body">
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
          <div className="pn__tile-name" title={project.name}>
            {project.name}
          </div>
          <div className="pn__tile-sub">
            {project.batch_count === 0
              ? "No batches yet"
              : `${project.batch_count} batch${project.batch_count === 1 ? "" : "es"} · ${project.approved_count}/${project.panel_count} approved`}
          </div>
          {project.panel_count > 0 ? (
            <div className="pn__tile-bar">
              <span style={{ width: `${pct}%` }} />
            </div>
          ) : null}
        </div>
      </Link>

      <div className="pn__tile-acts">
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
        {/* Only offered once there IS a hand-set cover — clearing back to the
            first-panel fallback is meaningless otherwise. */}
        {project.has_cover ? (
          <button
            type="button"
            className="pn__tile-btn"
            title="Clear the cover (back to the first panel)"
            onClick={async (e) => {
              e.preventDefault();
              await setPanelProjectCover(project.id, null);
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
                `Delete \u201c${project.name}\u201d, its ${project.batch_count} batch(es) and ${project.panel_count} panel(s)?`,
              )
            )
              return;
            try {
              await deletePanelProject(project.id);
              await onChanged();
            } catch (err) {
              toast(err instanceof Error ? err.message : "Delete failed");
            }
          }}
        >
          ✕
        </button>
      </div>
    </li>
  );
}

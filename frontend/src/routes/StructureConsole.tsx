import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { listProjects, thumbUrl, type ProjectDTO } from "../api/client";
import { ProjectStructureModal } from "../components/ProjectStructureModal";
import { useAuthStore } from "../store/auth";

/** Which project roles get the console's Series tab. Artists/viewers work
 *  inside sequences and don't manage structure, so they never land here. */
const STRUCTURAL_ROLES = new Set(["producer", "lead"]);

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return (parts[0]?.[0] ?? "?").concat(parts[1]?.[0] ?? "").toUpperCase();
}

/**
 * Phase 10: the producer/lead console. Same Dasher shell as the admin console
 * but scoped to a single "Series" tab — it lists only the projects where the
 * caller is a producer or lead, and opens the shared structure modal so they
 * build Series → Episodes → Sequences without an admin and without a page jump.
 *
 * Admin-only areas (Members, Cost, Sign-ups, Audit, account provisioning) are
 * absent by construction — this component never calls an admin-only endpoint.
 */
export function StructureConsole() {
  const me = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  const [projects, setProjects] = useState<ProjectDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [structFor, setStructFor] = useState<ProjectDTO | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const all = await listProjects();
        if (alive) setProjects(all);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const mine = useMemo(
    () => projects.filter((p) => STRUCTURAL_ROLES.has(p.my_role ?? "")),
    [projects],
  );

  return (
    <div className={`dash${navOpen ? " dash--nav-open" : ""}`}>
      <aside className="dash__side">
        <Link to="/projects" className="dash__brand">
          <img src="/favicon.png" alt="" width={30} height={30} />
          <span className="dash__brand-txt">
            Giant Studio
            <small>Studio console</small>
          </span>
        </Link>

        <nav className="dash__nav" role="tablist" aria-label="Console sections">
          <button role="tab" aria-selected className="dash__nav-item is-active">
            <span aria-hidden="true">🎬</span>
            <span>Series</span>
          </button>
        </nav>

        <div className="dash__side-foot">
          <div className="dash__me">
            <span className="dash__me-avatar" aria-hidden="true">
              {initials(me?.display_name || me?.username || "?")}
            </span>
            <span className="dash__me-txt">
              <b>{me?.display_name || me?.username}</b>
              <small>Producer / Lead</small>
            </span>
          </div>
          <Link to="/projects" className="dash__side-link">
            ← Back to app
          </Link>
          <button className="dash__side-link" onClick={() => logout()}>
            Sign out
          </button>
        </div>
      </aside>

      <div className="dash__backdrop" aria-hidden="true" onClick={() => setNavOpen(false)} />

      <div className="dash__main">
        <header className="dash__topbar">
          <button className="dash__hamburger" aria-label="Open menu" onClick={() => setNavOpen(true)}>
            ☰
          </button>
          <div>
            <h1 className="dash__title">Series</h1>
            <p className="dash__sub">
              Build the structure of the projects you run — Series → Episodes/Chapters → Sequences.
            </p>
          </div>
        </header>

        <main className="dash__content">
          {error ? <div className="admin-error">{error}</div> : null}

          <div className="admin2__card">
            {loading ? (
              <div className="admin2__empty">Loading…</div>
            ) : mine.length === 0 ? (
              <div className="admin2__empty">
                You don't run any projects yet. An admin assigns you as a producer or lead first.
              </div>
            ) : (
              <table className="admin2__table admin2__table--cards">
                <thead>
                  <tr>
                    <th className="admin-proj__thumb-col" aria-label="Cover" />
                    <th>Project</th>
                    <th>Your role</th>
                    <th className="admin2__th-actions" aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {mine.map((p) => (
                    <tr key={p.id}>
                      <td>
                        <span className="admin-proj__thumb">
                          {p.thumb_media_id ? (
                            <img src={thumbUrl(p.thumb_media_id, 120)} alt="" loading="lazy" />
                          ) : (
                            <span className="admin-proj__thumb-empty">🖼</span>
                          )}
                        </span>
                      </td>
                      <td>
                        <b>{p.name || "Untitled"}</b>
                      </td>
                      <td>
                        <span className="role-chip">{p.my_role}</span>
                      </td>
                      <td className="admin2__row-actions admin-proj__actions">
                        <button
                          className="btn2 btn2--primary"
                          onClick={() => setStructFor(p)}
                          title="Build Series → Episodes → Sequences"
                        >
                          Structure
                        </button>
                        <Link className="btn2 btn2--ghost" to={`/projects/${p.id}`}>
                          Open app →
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </main>
      </div>

      {structFor ? (
        <ProjectStructureModal
          projectId={structFor.id}
          projectName={structFor.name}
          onClose={() => setStructFor(null)}
        />
      ) : null}
    </div>
  );
}

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { getProjectCost } from "../api/client";
import { useProjectStore } from "../store/project";

/**
 * Phase 3 cost rollup. Backend exposes a flat ``cost_usd`` per project;
 * scene/shot breakdown lands with the worker hook in Phase 7.
 */
export function CostDashboard() {
  const { projectId } = useParams<{ projectId: string }>();
  const currentProject = useProjectStore((s) => s.currentProject);
  const selectProject = useProjectStore((s) => s.selectProject);

  const [cost, setCost] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) return;
    if (projectId !== useProjectStore.getState().currentProjectId) {
      void selectProject(projectId);
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    getProjectCost(projectId)
      .then((r) => {
        if (cancelled) return;
        setCost(r.cost_usd);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, selectProject]);

  if (!projectId) {
    return <div className="page-empty">No project id in URL.</div>;
  }

  return (
    <div className="page page--cost-dashboard">
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
            <span>Cost</span>
          </nav>
          <h1 className="page-title">Cost</h1>
          <p className="page-subtitle">
            Aggregate USD spend across this project's generation jobs.
          </p>
        </div>
      </header>

      {loading ? (
        <div className="page-loading">Loading cost…</div>
      ) : error ? (
        <div className="page-error" role="alert">{error}</div>
      ) : (
        <div className="cost-card">
          <div className="cost-card__label">Total spend</div>
          <div className="cost-card__value">
            ${(cost ?? 0).toFixed(2)} USD
          </div>
          <p className="cost-card__hint">
            Per-scene and per-shot breakdown ships with Phase 7's cost
            rollup hook.
          </p>
        </div>
      )}
    </div>
  );
}

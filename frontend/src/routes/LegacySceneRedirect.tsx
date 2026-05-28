import { useEffect, useState } from "react";
import { Navigate, useParams } from "react-router-dom";

import { getScene } from "../api/client";

/**
 * Phase 8.3: the old /scenes/:sceneId route is superseded by the nested
 * /projects/:projectId/scenes/:sceneId. Resolve the scene's project and
 * redirect so any bookmarked/old link still lands on the new SceneCanvas.
 */
export function LegacySceneRedirect() {
  const { sceneId } = useParams<{ sceneId: string }>();
  const [to, setTo] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!sceneId) return;
    getScene(sceneId)
      .then((scene) => setTo(`/projects/${scene.project_id}/scenes/${sceneId}`))
      .catch(() => setFailed(true));
  }, [sceneId]);

  if (failed) return <Navigate to="/projects" replace />;
  if (to) return <Navigate to={to} replace />;
  return <div className="page-empty">Redirecting…</div>;
}

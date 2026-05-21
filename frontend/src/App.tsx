import { useEffect, useRef } from "react";
import {
  BrowserRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
} from "react-router-dom";

import { ProjectSidebar } from "./components/ProjectSidebar";
import { Toaster } from "./components/Toaster";
import { GenerationDialog } from "./components/GenerationDialog";
import { ResultViewer } from "./components/ResultViewer";
import { ForcedSetupGate } from "./components/ForcedSetupGate";

import { ProjectListPage } from "./routes/ProjectListPage";
import { ProjectDashboard } from "./routes/ProjectDashboard";
import { SceneView } from "./routes/SceneView";
import { ShotEditor } from "./routes/ShotEditor";
import { AssetLibraryPage } from "./routes/AssetLibraryPage";
import { CostDashboard } from "./routes/CostDashboard";

import { useProjectStore } from "./store/project";
import { useReferencesStore } from "./store/references";
import { migrateLegacyLocalStorage } from "./store/shot";

/**
 * Phase 3 router shell. The pre-router App.tsx loaded one Board and
 * rendered the canvas inline. Now the canvas only renders on the
 * ``/shots/:shotId`` route; everything else is a hierarchy view.
 */
export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<Navigate to="/projects" replace />} />
          <Route path="/projects" element={<ProjectListPage />} />
          <Route path="/projects/:projectId" element={<ProjectDashboard />} />
          <Route
            path="/projects/:projectId/library"
            element={<AssetLibraryPage />}
          />
          <Route
            path="/projects/:projectId/cost"
            element={<CostDashboard />}
          />
          <Route path="/scenes/:sceneId" element={<SceneView />} />
          <Route path="/shots/:shotId" element={<ShotEditor />} />
          <Route path="*" element={<Navigate to="/projects" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

function AppLayout() {
  const loadProjects = useProjectStore((s) => s.loadProjects);
  const loadReferences = useReferencesStore((s) => s.load);
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    migrateLegacyLocalStorage();
    void loadProjects();
    void loadReferences();
  }, [loadProjects, loadReferences]);

  return (
    <div className="app">
      <ProjectSidebar />
      <main className="app-main">
        <Outlet />
      </main>
      <Toaster />
      <GenerationDialog />
      <ResultViewer />
      <ForcedSetupGate />
    </div>
  );
}

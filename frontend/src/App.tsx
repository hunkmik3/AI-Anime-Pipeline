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
import { AccountMenu } from "./components/AccountMenu";

import { ProjectListPage } from "./routes/ProjectListPage";
import { SceneView } from "./routes/SceneView";
import { SceneCanvas } from "./routes/SceneCanvas";
import { LegacySceneRedirect } from "./routes/LegacySceneRedirect";
import { ShotEditor } from "./routes/ShotEditor";
import { AssetLibraryPage } from "./routes/AssetLibraryPage";
import { CostDashboard } from "./routes/CostDashboard";
import { LoginPage } from "./routes/LoginPage";
import { AdminPage } from "./routes/AdminPage";

import { useProjectStore } from "./store/project";
import { useReferencesStore } from "./store/references";
import { useAuthStore } from "./store/auth";
import { migrateLegacyLocalStorage } from "./store/shot";

/**
 * Phase 3 router shell + Phase 9 auth guard. ``/login`` is public; everything
 * else requires a valid session (RequireAuth) — unauthenticated users are
 * redirected to login. ``/admin`` additionally requires the admin role.
 */
export function App() {
  const loadMe = useAuthStore((s) => s.loadMe);
  const booted = useRef(false);
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    void loadMe(); // validate a persisted token on boot
  }, [loadMe]);

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          element={
            <RequireAuth>
              <AppLayout />
            </RequireAuth>
          }
        >
          <Route index element={<Navigate to="/projects" replace />} />
          <Route path="/projects" element={<ProjectListPage />} />
          {/* Phase 8.3: project hub (entry point) = SceneView. */}
          <Route path="/projects/:projectId" element={<SceneView />} />
          <Route
            path="/projects/:projectId/library"
            element={<AssetLibraryPage />}
          />
          <Route
            path="/projects/:projectId/cost"
            element={<CostDashboard />}
          />
          {/* Phase 8.3: multi-shot canvas, nested under its project. */}
          <Route
            path="/projects/:projectId/scenes/:sceneId"
            element={<SceneCanvas />}
          />
          {/* Phase 9: admin-only account management. */}
          <Route path="/admin" element={<RequireAdmin><AdminPage /></RequireAdmin>} />
          {/* Legacy redirects → new nested routes. */}
          <Route path="/scenes/:sceneId" element={<LegacySceneRedirect />} />
          <Route path="/shots/:shotId" element={<ShotEditor />} />
          <Route path="*" element={<Navigate to="/projects" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const ready = useAuthStore((s) => s.ready);
  const user = useAuthStore((s) => s.user);
  if (!ready) return <div className="app-booting">Đang tải…</div>;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user);
  if (user?.role !== "admin") return <Navigate to="/projects" replace />;
  return <>{children}</>;
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
      <AccountMenu />
      <Toaster />
      <GenerationDialog />
      <ResultViewer />
      <ForcedSetupGate />
    </div>
  );
}

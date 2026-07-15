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
import { ChangePasswordDialog } from "./components/ChangePasswordDialog";

import { ProjectListPage } from "./routes/ProjectListPage";
import { SceneView } from "./routes/SceneView";
import { SceneCanvas } from "./routes/SceneCanvas";
import { LegacySceneRedirect } from "./routes/LegacySceneRedirect";
import { ShotEditor } from "./routes/ShotEditor";
import { AssetLibraryPage } from "./routes/AssetLibraryPage";
import { LoginPage } from "./routes/LoginPage";
import { AdminPage } from "./routes/AdminPage";

import { useProjectStore } from "./store/project";
import { useAuthStore } from "./store/auth";
import { setToken } from "./api/authFetch";
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
    // Google SSO lands back at /login#sso_token=<token> — capture it before
    // validating, then strip it from the URL so it isn't left in history.
    const m = window.location.hash.match(/sso_token=([^&]+)/);
    if (m) {
      setToken(decodeURIComponent(m[1]));
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    void loadMe(); // validate the (persisted or just-captured) token on boot
  }, [loadMe]);

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />

        {/* Admin is a STANDALONE console — deliberately outside AppLayout so it
            has no project sidebar / canvas chrome. Its own shell, full width. */}
        <Route
          path="/admin"
          element={
            <RequireAuth>
              <RequireAdmin>
                <AdminShell />
              </RequireAdmin>
            </RequireAuth>
          }
        />

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
          {/* Phase 8.3: multi-shot canvas, nested under its project. */}
          <Route
            path="/projects/:projectId/scenes/:sceneId"
            element={<SceneCanvas />}
          />
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
  if (!ready) return <div className="app-booting">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  // Admin-provisioned temp password → block the app until the user sets a new
  // one. The dialog clears must_change_password on success and the gate lifts.
  if (user.must_change_password) return <ChangePasswordDialog forced />;
  return <>{children}</>;
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user);
  if (user?.role !== "admin") return <Navigate to="/projects" replace />;
  return <>{children}</>;
}

/**
 * Standalone admin console — a Dasher-style dashboard (light theme, left
 * sidebar nav + topbar) that lives OUTSIDE AppLayout, so none of the project
 * sidebar / canvas chrome bleeds in. AdminPage renders the whole Dasher frame;
 * this wrapper just carries the Toaster so admin actions still get feedback.
 */
function AdminShell() {
  return (
    <>
      <AdminPage />
      <Toaster />
    </>
  );
}

function AppLayout() {
  const loadProjects = useProjectStore((s) => s.loadProjects);
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    migrateLegacyLocalStorage();
    void loadProjects();
    // References are loaded per-project (scoped) by the pages that show them —
    // SceneView / SceneCanvas / AssetLibrary — not unscoped at app mount.
  }, [loadProjects]);

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

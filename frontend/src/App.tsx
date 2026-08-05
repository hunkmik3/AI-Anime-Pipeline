import { useEffect, useRef } from "react";
import {
  BrowserRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useParams,
} from "react-router-dom";

import { ProjectSidebar } from "./components/ProjectSidebar";
import { Toaster } from "./components/Toaster";
import { GenerationDialog } from "./components/GenerationDialog";
import { ResultViewer } from "./components/ResultViewer";
import { ForcedSetupGate } from "./components/ForcedSetupGate";
import { TopBar } from "./components/shell/TopBar";
import { ChangePasswordDialog } from "./components/ChangePasswordDialog";

import { ProjectListPage } from "./routes/ProjectListPage";
import { SceneView } from "./routes/SceneView";
import { SceneCanvas } from "./routes/SceneCanvas";
import { LegacySceneRedirect } from "./routes/LegacySceneRedirect";
import { ShotEditor } from "./routes/ShotEditor";
import { AssetLibraryPage } from "./routes/AssetLibraryPage";
import { MyWorkPage } from "./routes/MyWorkPage";
import { ReviewQueuePage } from "./routes/ReviewQueuePage";
import { LoginPage } from "./routes/LoginPage";
import { AdminPage } from "./routes/AdminPage";
import { EpisodePage } from "./routes/EpisodePage";
import { SeriesPage } from "./routes/SeriesPage";
import { FlowApp } from "./flow/FlowApp";
import { PanelProjectsPage } from "./flow/PanelProjectsPage";
import { PanelBatchesPage } from "./flow/PanelBatchesPage";
import { PanelGridPage } from "./flow/PanelGridPage";
import { PanelAllPage } from "./flow/PanelAllPage";
import { PanelMyWorkPage } from "./flow/PanelMyWorkPage";
import { PanelReviewPage } from "./flow/PanelReviewPage";
import { PanelWorkspacePage } from "./flow/PanelWorkspacePage";

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

        {/* /admin is the only console left. The producer-facing one (/manage) is
            gone: it was a second navigation for one role, holding controls that
            belong on the objects themselves. Old links land on /projects. */}
        <Route
          path="/admin"
          element={
            <RequireAuth>
              <AdminOnly>
                <AdminShell />
              </AdminOnly>
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
          {/* Phase 11: deliverable hand-in + review. /my-work kept as a
              redirect — the old link is in people's history and chat logs. */}
          <Route path="/work" element={<MyWorkPage />} />
          <Route path="/my-work" element={<Navigate to="/work" replace />} />
          {/* /manage is gone; its job moved onto the object pages. */}
          <Route path="/manage" element={<Navigate to="/projects" replace />} />
          <Route
            path="/manage/:projectId"
            element={<ManageRedirect />}
          />
          <Route path="/review" element={<ReviewQueuePage />} />
          {/* Flow Studio, brought over whole from the manga_extract repo. A
              standalone surface: its own board list, its own image engines, no tie
              to Project → Series → Episode yet — so it is a SHARED space, with no
              separation between users and no budget cap. Open to the whole team by
              decision; see docs/INTEGRATION_PLAN.md. */}
          {/* Giantflow is now panel production: a project per comic, a grid of
              panels inside it. The free-form image studio stays reachable at
              /giantflow/studio until panel generation replaces it. */}
          <Route path="/giantflow" element={<PanelProjectsPage />} />
          <Route path="/giantflow/studio" element={<FlowApp />} />
          <Route path="/giantflow/panel/:panelId" element={<PanelWorkspacePage />} />
          <Route path="/giantflow/panels" element={<PanelAllPage />} />
          <Route path="/giantflow/review" element={<PanelReviewPage />} />
          <Route path="/giantflow/my-work" element={<PanelMyWorkPage />} />
          <Route path="/giantflow/batch/:batchId" element={<PanelGridPage />} />
          <Route path="/giantflow/:projectId" element={<PanelBatchesPage />} />
          {/* Phase 8.3: project hub (entry point) = SceneView. */}
          <Route path="/projects/:projectId" element={<SceneView />} />
          <Route
            path="/projects/:projectId/library"
            element={<AssetLibraryPage />}
          />
          {/* One object, one page: a series and an episode each get their own
              page rather than being scattered across manage/admin. */}
          <Route
            path="/projects/:projectId/series/:seriesId"
            element={<SeriesPage />}
          />
          <Route
            path="/projects/:projectId/episodes/:sceneId"
            element={<EpisodePage />}
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

/**
 * /admin is admin-only. A non-admin who lands here (an old bookmark, a link from a
 * colleague) goes to their projects rather than being shown an error.
 */
/** An old /manage/:projectId link resolves to that project's page. */
function ManageRedirect() {
  const { projectId } = useParams();
  return <Navigate to={projectId ? `/projects/${projectId}` : "/projects"} replace />;
}

function AdminOnly({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user);
  if (!user) return null;
  if (user.role !== "admin") return <Navigate to="/projects" replace />;
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

/**
 * What layout the current route wants.
 *
 * Three shapes, decided in one place so no page has to arrange its own chrome:
 *   canvas   — full bleed: no top bar, keep the project tree to navigate out
 *   listing  — top bar, no tree: Work and Review have no hierarchy to walk, and
 *              the 384px project sidebar was just squeezing them
 *   default  — top bar + project tree
 */
function useLayoutMode(): { topBar: boolean; sidebar: boolean } {
  const { pathname } = useLocation();
  const isCanvas =
    /^\/projects\/[^/]+\/scenes\/[^/]+/.test(pathname) ||
    pathname.startsWith("/shots/");
  if (isCanvas) return { topBar: false, sidebar: true };
  // Work, Review and the Flow Studio all bring their own body layout — the
  // studio even has its own left rail — so the project tree would just be a
  // second column fighting for width. The bar stays: it is the way back out.
  if (
    pathname === "/work" ||
    pathname === "/review" ||
    // Every giantflow surface: the production project tree belongs to the other
    // hierarchy entirely, and on the panel grid it was 384px of unrelated
    // navigation stealing width from the thing the page is for.
    pathname.startsWith("/giantflow")
  ) {
    return { topBar: true, sidebar: false };
  }
  return { topBar: true, sidebar: true };
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

  const { topBar, sidebar } = useLayoutMode();

  return (
    <div className="app">
      {topBar ? <TopBar /> : null}
      <div className="app-body">
        {/* The bar carries the brand, so the sidebar only shows its own on the
            canvas — the one route with no bar. */}
        {sidebar ? <ProjectSidebar showBrand={!topBar} /> : null}
        <main className="app-main">
          <Outlet />
        </main>
      </div>
      <Toaster />
      <GenerationDialog />
      <ResultViewer />
      <ForcedSetupGate />
    </div>
  );
}

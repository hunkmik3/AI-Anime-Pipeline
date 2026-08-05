import { useEffect, useSyncExternalStore } from "react";

import { giantflowMe } from "../api/client";

/**
 * Giantflow roles, and a "view as" preview for checking each one's POV.
 *
 * **The preview is a preview, not a permission.** It changes what THIS browser
 * draws, nothing else. Every one of these actions is also gated server-side
 * against the real signed-in user, so switching to Viewer does not make an admin
 * safe to hand the laptop to — and switching to Admin does not grant an artist
 * anything. It exists so the person building this can see what each role sees
 * without keeping four test accounts logged in.
 *
 * The ranking and capability names mirror `services/permissions.py` and the table
 * in docs/GIANTFLOW_REFACTOR.md; a role here still grants nothing in the
 * production hierarchy.
 */
export type GiantflowRole = "admin" | "producer" | "lead" | "artist" | "viewer";

export const GIANTFLOW_ROLES: { id: GiantflowRole; label: string }[] = [
  { id: "admin", label: "Admin" },
  // "Producer" in the permission table is the PM in the studio's own words.
  { id: "producer", label: "PM" },
  { id: "artist", label: "Artist" },
  { id: "viewer", label: "Viewer" },
];

// Mirrors `services/flow_permissions.py`. The two must not drift — that module
// is the rule, this is only what gets drawn.
const RANK: Record<GiantflowRole, number> = {
  viewer: 0,
  artist: 1,
  lead: 2,
  producer: 3,
  admin: 4,
};

export type GiantflowCap =
  | "panel.read"
  | "panel.generate"
  | "panel.submit"
  | "panel.review"
  | "batch.manage"
  | "batch.import"
  | "project.manage";

/** capability → the least role that has it. */
const CAPS: Record<GiantflowCap, GiantflowRole> = {
  "panel.read": "viewer",
  "panel.generate": "artist",
  "panel.submit": "artist",
  // A PM approves and sends back; an artist marking their own work approved is
  // the whole reason there is a review step.
  "panel.review": "producer",
  "batch.manage": "producer",
  "batch.import": "producer",
  // Creating, renaming and deleting a comic is an admin's call.
  "project.manage": "admin",
};

const KEY = "flowboard.giantflow.viewAs";

let preview: GiantflowRole | null = (() => {
  try {
    const v = localStorage.getItem(KEY);
    return v && v in RANK ? (v as GiantflowRole) : null;
  } catch {
    return null;
  }
})();

const listeners = new Set<() => void>();

export function setViewAs(role: GiantflowRole | null): void {
  preview = role;
  try {
    if (role) localStorage.setItem(KEY, role);
    else localStorage.removeItem(KEY);
  } catch {
    /* private mode — the preview just won't survive a reload */
  }
  listeners.forEach((fn) => fn());
}

/**
 * The real role, fetched from the server rather than guessed.
 *
 * It used to be inferred from the account's system role — admins were admin,
 * everyone else was assumed to be a PM. That was a stand-in while giantflow had
 * no membership of its own, and it drew Approve buttons for artists. The answer
 * now comes from `/api/flowstudio/me`, which resolves it the same way the guard
 * on every endpoint does.
 *
 * `best_role` is the strongest role held on ANY comic, which is the right
 * granularity for the nav strip. Per-comic decisions read `projects`.
 */
let realRole: GiantflowRole = "viewer";
let projectRoles: Record<string, GiantflowRole> = {};
let loaded = false;
const realListeners = new Set<() => void>();

export async function loadRealRole(force = false): Promise<void> {
  if (loaded && !force) return;
  loaded = true;
  try {
    const me = await giantflowMe();
    realRole = (me.best_role in RANK ? me.best_role : "viewer") as GiantflowRole;
    projectRoles = Object.fromEntries(
      Object.entries(me.projects).map(([k, v]) => [k, (v in RANK ? v : "viewer") as GiantflowRole]),
    );
  } catch {
    // Signed out or offline: draw the least, never the most. Guessing high here
    // would show controls that 403 on click.
    realRole = "viewer";
    projectRoles = {};
  }
  realListeners.forEach((fn) => fn());
}

function subscribeReal(fn: () => void): () => void {
  realListeners.add(fn);
  return () => realListeners.delete(fn);
}

function snapshotReal(): GiantflowRole {
  return realRole;
}

/** This account's role on one comic, for the pages scoped to one. */
export function roleForProject(projectId: number | string | undefined): GiantflowRole {
  if (projectId === undefined) return realRole;
  return projectRoles[String(projectId)] ?? realRole;
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function snapshot(): GiantflowRole | null {
  return preview;
}

/** The role the UI should draw for: the preview if one is set, else the real one. */
export function useGiantflowRole(): {
  role: GiantflowRole;
  realRole: GiantflowRole;
  viewAs: GiantflowRole | null;
  can: (cap: GiantflowCap) => boolean;
} {
  const viewAs = useSyncExternalStore(subscribe, snapshot, snapshot);
  const realRole = useSyncExternalStore(subscribeReal, snapshotReal, snapshotReal);
  // Asked once per page load and cached: the answer is the same for every
  // component on screen, and it is advisory anyway — the server re-checks each
  // request, so a stale value costs a 403, never an unauthorised action.
  useEffect(() => {
    void loadRealRole();
  }, []);
  const role = viewAs ?? realRole;
  return {
    role,
    realRole,
    viewAs,
    can: (cap) => RANK[role] >= RANK[CAPS[cap]],
  };
}

import { useCallback, useEffect, useState, type ReactNode } from "react";

/**
 * One person's roles across both products, in one panel.
 *
 * Company-wide role (user / manager / admin), plus ONE Giant Studio role and ONE
 * Giantflow role. Simple model: a person holds the same role across every GS
 * project / every GF comic — picking a role applies it everywhere on that side,
 * "(none)" removes it. Admin & manager already rule everywhere, so the per-side
 * roles only matter for a `user`.
 */

type Role = "producer" | "artist" | "editor" | "viewer";

/** "Producer" is the code word; "PM" is what everyone says. Tagged ·GS/·GF so a
 *  Giant Studio role never reads the same as a Giantflow one. */
const ROLE_LABEL: Record<Role, string> = {
  producer: "PM",
  artist: "Artist",
  editor: "Editor",
  viewer: "Viewer",
};
// Giant Studio has an editor (pulls raw material, hands a cut back); Giantflow
// does not — so the two sides offer different role sets.
const STUDIO_ROLES: Role[] = ["producer", "artist", "editor", "viewer"];
const FLOW_ROLES: Role[] = ["producer", "artist", "viewer"];

// Rank so a representative role can be shown when older per-project grants are
// not yet uniform. Highest wins.
const RANK: Record<Role, number> = { producer: 4, artist: 3, editor: 2, viewer: 1 };

type StudioGrant = { project_id: string; name: string; role: Role; is_owner: boolean };
type FlowGrant = { series_id: number; name: string; role: Role };

type Roles = {
  user_id: string;
  username: string;
  display_name: string | null;
  system_role: string;
  studio: StudioGrant[];
  flow: FlowGrant[];
  /** How many targets a one-role-per-side grant would apply to. 0 = nothing to
   *  assign (e.g. no comics yet), so the dropdown is disabled instead of
   *  flashing and reverting. */
  studio_assignable?: number;
  flow_comics?: number;
  /** The GF designation on the account (producer|artist|viewer|null) — works
   *  even with zero comics; this is what the GF dropdown reads/sets. */
  flow_role?: string | null;
};

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const body = await r.json().catch(() => null);
    throw new Error(body?.detail ?? `${r.status} ${r.statusText}`);
  }
  return r.json() as Promise<T>;
}

/** The one role to show for a side, from however many grants exist. */
function topRole(grants: { role: Role }[]): Role | "" {
  if (grants.length === 0) return "";
  return grants.map((g) => g.role).sort((a, b) => RANK[b] - RANK[a])[0];
}

export function UserRolesDrawer({
  userId,
  onClose,
  onChanged,
}: {
  userId: string;
  onClose: () => void;
  /** Fired after any role change so the Employees table can refetch. */
  onChanged?: () => void;
}) {
  const [roles, setRoles] = useState<Roles | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRoles(await json<Roles>(await fetch(`/api/admin/users/${userId}/roles`)));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load roles");
    }
  }, [userId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(fn: () => Promise<Response>) {
    setBusy(true);
    setError(null);
    try {
      setRoles(await json<Roles>(await fn()));
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "That did not work");
    } finally {
      setBusy(false);
    }
  }

  // The one company-wide role. The PATCH endpoint returns the user, not the
  // roles payload, so it can't go through `run`.
  async function setSystemRole(nextRole: string) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/admin/users/${userId}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ role: nextRole }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `${res.status}`);
      }
      setRoles((prev) => (prev ? { ...prev, system_role: nextRole } : prev));
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change the role");
    } finally {
      setBusy(false);
    }
  }

  // One role for a whole side: picking applies it to every project/comic,
  // "(none)" clears it everywhere.
  const setSide = (side: "studio" | "flow", role: string) => {
    const url = `/api/admin/users/${userId}/roles/${side}-all`;
    return run(() =>
      role
        ? fetch(url, {
            method: "PUT",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ role }),
          })
        : fetch(url, { method: "DELETE" }),
    );
  };

  // Ignore the owner-implicit producer (from OWNING a project) — the dropdown is
  // for the assigned role, and an owner's producer-ship can't be set here anyway.
  // Without this, owning one project pins the dropdown to PM·GS no matter what
  // you pick, so a change looks like it did nothing.
  const studioRole = roles ? topRole(roles.studio.filter((g) => !g.is_owner)) : "";
  // GF is a per-account designation (works with 0 comics), not derived from
  // comic memberships — read it straight off the account.
  const flowRole = roles ? (roles.flow_role ?? "") : "";

  return (
    <div
      className="admin-activity-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="admin-activity" role="dialog" aria-label="Manage roles">
        <div className="admin-activity__head">
          <h2>Manage roles — {roles?.display_name || roles?.username || "…"}</h2>
          <button className="admin-activity__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {error ? <div className="admin-error">{error}</div> : null}
        {roles === null && !error ? <div className="admin-loading">Loading…</div> : null}

        {roles ? (
          <div className="roles" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <RoleRow label="Company-wide role" value={roles.system_role} busy={busy} onChange={setSystemRole}>
              <option value="user">user</option>
              <option value="manager">manager</option>
              <option value="admin">admin</option>
            </RoleRow>

            <p className="roles__note" style={{ margin: 0 }}>
              Below is one role per product — it applies across every project /
              comic on that side. Admin &amp; manager already rule everywhere, so
              these only matter for a <b>user</b>.
            </p>

            <RoleRow
              label="Giant Studio"
              tone="studio"
              value={studioRole}
              busy={busy}
              disabled={(roles.studio_assignable ?? 0) === 0}
              hint={
                (roles.studio_assignable ?? 0) === 0
                  ? "Chưa có project nào để gán (họ chỉ đang sở hữu project, hoặc chưa có project)."
                  : undefined
              }
              onChange={(r) => void setSide("studio", r)}
            >
              <option value="">(none)</option>
              {STUDIO_ROLES.map((x) => (
                <option key={x} value={x}>
                  {ROLE_LABEL[x]}·GS
                </option>
              ))}
            </RoleRow>

            <RoleRow
              label="Giantflow"
              tone="flow"
              value={flowRole}
              busy={busy}
              hint="Nhãn GF trên tài khoản — người được đánh dấu sẽ hiện ở danh sách giao batch (khi giao batch họ thành artist của comic đó)."
              onChange={(r) => void setSide("flow", r)}
            >
              <option value="">(none)</option>
              {FLOW_ROLES.map((x) => (
                <option key={x} value={x}>
                  {ROLE_LABEL[x]}·GF
                </option>
              ))}
            </RoleRow>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function RoleRow({
  label,
  tone,
  value,
  busy,
  disabled,
  hint,
  onChange,
  children,
}: {
  label: string;
  tone?: "studio" | "flow";
  value: string;
  busy: boolean;
  disabled?: boolean;
  hint?: string;
  onChange: (v: string) => void;
  children: ReactNode;
}) {
  const color = tone === "studio" ? "#4bd6a4" : tone === "flow" ? "#e0a24a" : undefined;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <label style={{ display: "flex", alignItems: "center", gap: 12, fontWeight: 600 }}>
        <span style={{ minWidth: 130, color }}>{label}</span>
        <select
          className="roles__select"
          value={value}
          disabled={busy || disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          {children}
        </select>
      </label>
      {hint ? (
        <span style={{ marginLeft: 142, fontSize: 12, opacity: 0.7 }}>{hint}</span>
      ) : null}
    </div>
  );
}

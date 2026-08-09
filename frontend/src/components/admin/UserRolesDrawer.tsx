import { useCallback, useEffect, useState } from "react";

/**
 * One person's standing across both products, in one place.
 *
 * The roles the studio talks about — "PM giantflow", "artist giantstudio" —
 * were always expressible: a producer row on a comic, an artist row on a
 * project. What was missing was anywhere to SEE them. Membership could only be
 * asked per project and per comic, so answering "what does this person have"
 * meant opening every project and every comic in turn, and granting meant
 * walking to each one's own page.
 *
 * The two products stay visually separate here for the same reason they are
 * separate in the backend: they are different tables with different guards, and
 * a grant that landed on the wrong one would look right on the screen it was
 * made from.
 */

type Role = "producer" | "artist" | "viewer";

/** Shown in the studio's own words. "Producer" is what the code calls it and
 *  "PM" is what everybody says out loud; the dropdown should say the second. */
const ROLE_LABEL: Record<Role, string> = {
  producer: "PM",
  artist: "Artist",
  viewer: "Viewer",
};
const ROLES: Role[] = ["producer", "artist", "viewer"];

type StudioGrant = { project_id: string; name: string; role: Role; is_owner: boolean };
type FlowGrant = { series_id: number; name: string; role: Role };

type Roles = {
  user_id: string;
  username: string;
  display_name: string | null;
  system_role: string;
  studio: StudioGrant[];
  flow: FlowGrant[];
};

type Option = { id: string; name: string };

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const body = await r.json().catch(() => null);
    throw new Error(body?.detail ?? `${r.status} ${r.statusText}`);
  }
  return r.json() as Promise<T>;
}

export function UserRolesDrawer({
  userId,
  onClose,
}: {
  userId: string;
  onClose: () => void;
}) {
  const [roles, setRoles] = useState<Roles | null>(null);
  const [projects, setProjects] = useState<Option[]>([]);
  const [comics, setComics] = useState<Option[]>([]);
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

  // The things a grant can be made ON. Loaded once and independently of the
  // grants themselves: a failure to list comics should not blank out the roles
  // the person already holds.
  useEffect(() => {
    void (async () => {
      try {
        const rows = await json<{ id: string; name: string }[]>(
          await fetch("/api/projects"),
        );
        setProjects(rows.map((p) => ({ id: p.id, name: p.name })));
      } catch {
        /* the picker stays empty; the list above still renders */
      }
      try {
        const rows = await json<{ id: number; name: string }[]>(
          await fetch("/api/flowstudio/series"),
        );
        setComics(rows.map((c) => ({ id: String(c.id), name: c.name })));
      } catch {
        /* same */
      }
    })();
  }, []);

  async function run(fn: () => Promise<Response>) {
    setBusy(true);
    setError(null);
    try {
      setRoles(await json<Roles>(await fn()));
    } catch (e) {
      setError(e instanceof Error ? e.message : "That did not work");
    } finally {
      setBusy(false);
    }
  }

  const grant = (side: "studio" | "flow", id: string, role: Role) =>
    run(() =>
      fetch(`/api/admin/users/${userId}/roles/${side}/${id}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ role }),
      }),
    );

  const revoke = (side: "studio" | "flow", id: string) =>
    run(() =>
      fetch(`/api/admin/users/${userId}/roles/${side}/${id}`, { method: "DELETE" }),
    );

  const heldStudio = new Set(roles?.studio.map((g) => g.project_id) ?? []);
  const heldFlow = new Set(roles?.flow.map((g) => String(g.series_id)) ?? []);

  return (
    <div
      className="admin-activity-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="admin-activity" role="dialog" aria-label="Project roles">
        <div className="admin-activity__head">
          <h2>
            Project roles — {roles?.display_name || roles?.username || "…"}
          </h2>
          <button className="admin-activity__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {error ? <div className="admin-error">{error}</div> : null}
        {roles === null && !error ? <div className="admin-loading">Loading…</div> : null}

        {roles ? (
          <div className="roles">
            <p className="roles__note">
              Company-wide role is <b>{roles.system_role}</b>. What follows is
              per project — someone can be PM of one comic and an artist on
              another.
            </p>

            <RoleSection
              title="Giant Studio"
              tone="studio"
              blurb="Dựng video — MoguTV, và panel đã duyệt chuyển sang từ Giantflow."
              rows={roles.studio.map((g) => ({
                id: g.project_id,
                name: g.name,
                role: g.role,
                // An owner is a producer implicitly, with no member row to
                // delete. Offering the button would be offering nothing.
                locked: g.is_owner ? "Owner" : null,
              }))}
              options={projects.filter((p) => !heldStudio.has(p.id))}
              busy={busy}
              onGrant={(id, role) => void grant("studio", id, role)}
              onRevoke={(id) => void revoke("studio", id)}
              addLabel="Add project"
            />

            <RoleSection
              title="Giantflow"
              tone="flow"
              blurb="Chuyển thể truyện tranh theo panel."
              rows={roles.flow.map((g) => ({
                id: String(g.series_id),
                name: g.name,
                role: g.role,
                locked: null,
              }))}
              options={comics.filter((c) => !heldFlow.has(c.id))}
              busy={busy}
              onGrant={(id, role) => void grant("flow", id, role)}
              onRevoke={(id) => void revoke("flow", id)}
              addLabel="Add comic"
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

type Row = { id: string; name: string; role: Role; locked: string | null };

function RoleSection({
  title,
  tone,
  blurb,
  rows,
  options,
  busy,
  onGrant,
  onRevoke,
  addLabel,
}: {
  title: string;
  tone: "studio" | "flow";
  blurb: string;
  rows: Row[];
  options: Option[];
  busy: boolean;
  onGrant: (id: string, role: Role) => void;
  onRevoke: (id: string) => void;
  addLabel: string;
}) {
  const [adding, setAdding] = useState(false);
  const [pick, setPick] = useState("");
  const [role, setRole] = useState<Role>("artist");

  return (
    <section className={`roles__sec is-${tone}`}>
      <h3 className="roles__h">{title}</h3>
      <p className="roles__blurb">{blurb}</p>

      {rows.length === 0 ? (
        <p className="roles__empty">Chưa có vai nào ở đây.</p>
      ) : (
        <ul className="roles__list">
          {rows.map((r) => (
            <li key={r.id} className="roles__row">
              <span className="roles__name" title={r.name}>
                {r.name}
              </span>
              {r.locked ? (
                <span className="roles__locked" title="Chủ project — đổi chủ trước rồi mới gỡ được">
                  {r.locked}
                </span>
              ) : (
                <>
                  <select
                    className="roles__select"
                    value={r.role}
                    disabled={busy}
                    onChange={(e) => onGrant(r.id, e.target.value as Role)}
                  >
                    {ROLES.map((x) => (
                      <option key={x} value={x}>
                        {ROLE_LABEL[x]}
                      </option>
                    ))}
                  </select>
                  <button
                    className="roles__x"
                    disabled={busy}
                    title="Gỡ khỏi đây"
                    onClick={() => onRevoke(r.id)}
                  >
                    ✕
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {adding ? (
        <div className="roles__add">
          <select
            className="roles__select roles__select--wide"
            value={pick}
            onChange={(e) => setPick(e.target.value)}
          >
            <option value="">— chọn —</option>
            {options.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
          <select
            className="roles__select"
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
          >
            {ROLES.map((x) => (
              <option key={x} value={x}>
                {ROLE_LABEL[x]}
              </option>
            ))}
          </select>
          <button
            className="roles__go"
            disabled={busy || !pick}
            onClick={() => {
              onGrant(pick, role);
              setPick("");
              setAdding(false);
            }}
          >
            Cấp
          </button>
          <button className="roles__cancel" onClick={() => setAdding(false)}>
            Huỷ
          </button>
        </div>
      ) : (
        <button
          className="roles__addbtn"
          disabled={options.length === 0}
          title={options.length === 0 ? "Đã có vai ở tất cả" : undefined}
          onClick={() => setAdding(true)}
        >
          + {addLabel}
        </button>
      )}
    </section>
  );
}

import { useEffect, useMemo, useState } from "react";

import {
  listAssignableUsers,
  listProjectMembers,
  setProjectMembers,
  type ProjectMemberDTO,
  type ProjectRole,
} from "../api/client";
import { useProjectStore } from "../store/project";

/** Non-owner roles a producer can assign, most to least authority. The owner is
 *  always a producer and can't be demoted here. */
const ASSIGNABLE_ROLES: { value: ProjectRole; label: string; hint: string }[] = [
  { value: "producer", label: "Producer", hint: "Runs the project + staffs it" },
  { value: "artist", label: "Artist", hint: "Works in sequences + canvas" },
  { value: "viewer", label: "Viewer", hint: "Read-only" },
];

/**
 * Phase 10: a producer staffs their own project — add/remove members and set
 * each one's role — without an admin round-trip. Backend gates this on
 * ``member.manage``; this dialog is only opened when the caller has it.
 */
export function ProjectMembersDialog({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const refreshList = useProjectStore((s) => s.loadProjects);
  const [members, setMembers] = useState<ProjectMemberDTO[]>([]);
  const [pool, setPool] = useState<{ user_id: string; name: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [addId, setAddId] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [m, p] = await Promise.all([
          listProjectMembers(projectId),
          listAssignableUsers(projectId),
        ]);
        if (!alive) return;
        setMembers(m.members);
        setPool(p);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  const assigned = useMemo(() => new Set(members.map((m) => m.user_id)), [members]);
  const addable = useMemo(
    () => pool.filter((u) => !assigned.has(u.user_id)),
    [pool, assigned],
  );

  function setRole(userId: string, role: ProjectRole) {
    setMembers((prev) =>
      prev.map((m) => (m.user_id === userId ? { ...m, role } : m)),
    );
  }

  function remove(userId: string) {
    setMembers((prev) => prev.filter((m) => m.user_id !== userId));
  }

  function addMember() {
    if (!addId) return;
    const u = pool.find((x) => x.user_id === addId);
    if (!u) return;
    setMembers((prev) => [
      ...prev,
      { user_id: u.user_id, name: u.name, role: "artist", is_owner: false },
    ]);
    setAddId("");
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const res = await setProjectMembers(
        projectId,
        members.map((m) => ({ user_id: m.user_id, role: m.role })),
      );
      setMembers(res.members);
      await refreshList();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="project-modal-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget && !saving) onClose();
      }}
    >
      <div className="project-modal project-modal--wide" role="dialog" aria-modal="true">
        <h2 className="project-modal__title">Project members</h2>
        <p className="project-modal__hint">
          Assign teammates and set what each may do. The owner is always a
          producer.
        </p>

        {loading ? (
          <div className="page-empty">Loading…</div>
        ) : (
          <>
            <ul className="member-list">
              {members.map((m) => (
                <li key={m.user_id} className="member-row">
                  <div className="member-row__who">
                    <span className="member-row__name">{m.name}</span>
                    {m.is_owner && <span className="role-chip">owner</span>}
                  </div>
                  <div className="member-row__controls">
                    <select
                      value={m.role}
                      disabled={m.is_owner}
                      onChange={(e) => setRole(m.user_id, e.target.value as ProjectRole)}
                      title={m.is_owner ? "The owner is always a producer" : "Role"}
                    >
                      {ASSIGNABLE_ROLES.map((r) => (
                        <option key={r.value} value={r.value}>
                          {r.label}
                        </option>
                      ))}
                    </select>
                    {!m.is_owner && (
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={() => remove(m.user_id)}
                        aria-label={`Remove ${m.name}`}
                      >
                        Remove
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>

            {addable.length > 0 && (
              <div className="member-add">
                <select value={addId} onChange={(e) => setAddId(e.target.value)}>
                  <option value="">+ Add a member…</option>
                  {addable.map((u) => (
                    <option key={u.user_id} value={u.user_id}>
                      {u.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={addMember}
                  disabled={!addId}
                >
                  Add
                </button>
              </div>
            )}
          </>
        )}

        {error && <p className="form-error">{error}</p>}

        <div className="project-modal__actions">
          <button
            type="button"
            className="project-modal__btn"
            onClick={onClose}
            disabled={saving}
          >
            Cancel
          </button>
          <button
            type="button"
            className="project-modal__btn project-modal__btn--primary"
            onClick={() => void save()}
            disabled={saving || loading}
          >
            {saving ? "Saving…" : "Save members"}
          </button>
        </div>
      </div>
    </div>
  );
}

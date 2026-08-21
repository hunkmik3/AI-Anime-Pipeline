import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import { useRevalidate } from "../../hooks/useRevalidate";

import {
  createScene,
  createSeries,
  generateSeriesStructure,
  getCrewNames,
  listProjects,
  listSeries,
  deleteScene,
  listAssignableUsers,
  listSeriesEpisodes,
  setEpisodeAssignee,
  setSeriesAssignee,
  patchScene,
  patchSeries,
  type SceneDTO,
  type SeriesDTO,
} from "../../api/client";
import { toast } from "../../store/toast";
import { BudgetCell, BudgetPanel } from "../BudgetPanel";
import { TierChip, TierPicker } from "../TierChip";

/**
 * Phase 10 Production CRM — the Series_Master / Episode_Tracker spreadsheet,
 * in-app. A roomy table of every series across all projects (with a live
 * episode rollup); expand a row to edit its episodes' pipeline status + the
 * four role assignees inline. Restored from the pre-server-baseline build.
 */

const SERIES_STATUS = ["Planning", "On-going", "Completed", "Cancelled"] as const;
const PRIORITY = ["High", "Medium", "Low"] as const;
const EP_STATUS = ["NotStarted", "Script", "Production", "Completed", "Dropped"] as const;

/** A series row enriched with its owning project's name. */
type Row = SeriesDTO & { project_name: string };

function statusClass(s: string): string {
  const k = s.toLowerCase();
  if (k === "completed") return "crm-chip--done";
  if (k === "on-going" || k === "production" || k === "script") return "crm-chip--active";
  if (k === "cancelled" || k === "dropped") return "crm-chip--dead";
  return "crm-chip--idle";
}
function priClass(p: string): string {
  const k = p.toLowerCase();
  return k === "high" ? "crm-chip--high" : k === "low" ? "crm-chip--low" : "crm-chip--med";
}

// ── Full series record (shown when a row is expanded) ────────────────────────
// Every series production field, in natural order. The compact table shows only
// the key ones; the rest live here so no data is hidden without over-wide rows.
// Only the fields NOT already shown as a main-table column live here, so the
// expanded row never repeats what's on the row above it. (Code, Full code,
// Series, Tier, Status, Priority, Producer, Assignee, Progress are up there.)
const SERIES_DETAIL: [string, string][] = [
  ["Start date", "start_date"],
  ["End date", "end_date"],
  ["Planned episodes", "total_episodes_planned"],
  ["Duration / ep (s)", "episode_duration_sec"],
  ["Genres", "genres"],
  ["Tropes", "tropes"],
  ["Folder", "folder_link"],
];
/** The extra series fields (those not already on the main row) as a sub-table,
 *  led by the Series name: field names as the header row, one row of values. */
function SeriesDetail({
  seriesName,
  production,
}: {
  seriesName: string;
  production: Record<string, string | number>;
}) {
  const has = (k: string) => {
    const v = production[k];
    return v !== undefined && v !== null && String(v).trim() !== "";
  };
  const shown: [string, string][] = [
    ["Series", "__name__"],
    ...SERIES_DETAIL.filter(([, k]) => has(k)),
  ];
  const renderValue = (k: string) => {
    if (k === "__name__") return seriesName;
    const v = String(production[k]);
    if (k === "folder_link" && /^https?:\/\//.test(v)) {
      return (
        <a href={v} target="_blank" rel="noreferrer" style={{ color: "#00a76f" }}>
          {v}
        </a>
      );
    }
    return v;
  };
  // Inline styles so the look is guaranteed regardless of stylesheet caching —
  // sized to match the MAIN table's header/cell (padding + font).
  const thStyle: React.CSSProperties = {
    textAlign: "left",
    padding: "14px 16px",
    fontSize: "0.72rem",
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.5px",
    color: "#8a97a3",
    background: "#1a222b",
    whiteSpace: "nowrap",
    borderBottom: "1px solid #2b3640",
  };
  const tdStyle: React.CSSProperties = {
    padding: "13px 16px",
    fontSize: "0.9rem",
    color: "#e7ecf0",
    verticalAlign: "top",
    maxWidth: 340,
    overflowWrap: "anywhere",
  };
  return (
    <div style={{ overflowX: "auto", margin: "0 0 4px" }}>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr>
            {shown.map(([label, k]) => (
              <th key={k} style={thStyle}>
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr>
            {shown.map(([, k]) => (
              <td key={k} style={tdStyle}>
                {renderValue(k)}
              </td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  );
}

// ── Episode table (inline-editable) ──────────────────────────────────────────

const CREW_ROLES = ["scriptwriter", "concept_creator", "ai_creator", "editor"] as const;

/** Episode id convention (mirrors the Episode_Tracker sheet + the backend's
 *  series_service.episode_code): `<SERIES_CODE>_EP<NN>` → "HUSB_EP01". */
function episodeCode(seriesCode: string, n: number): string {
  const c = (seriesCode || "").trim().toUpperCase();
  const num = String(n).padStart(2, "0");
  return c ? `${c}_EP${num}` : `EP${num}`;
}

function EpisodeTable({
  seriesId,
  seriesCode,
  projectId,
  plannedEpisodes,
  crewNames,
  canAdd,
  onCrewAdded,
  onEpisodeAdded,
}: {
  seriesId: string;
  seriesCode: string;
  projectId: string;
  /** Current production.total_episodes_planned — bumped when adding a one-off. */
  plannedEpisodes: number;
  crewNames: string[];
  canAdd: boolean;
  onCrewAdded: (name: string) => void;
  onEpisodeAdded: () => void;
}) {
  const [rows, setRows] = useState<SceneDTO[] | null>(null);
  const [adding, setAdding] = useState(false);
  // Everyone who may hold work on this project. Asked once per table rather
  // than per row — the list is the same for every episode in it.
  const [people, setPeople] = useState<{ user_id: string; name: string }[]>([]);

  useEffect(() => {
    void listAssignableUsers(projectId)
      .then(setPeople)
      .catch(() => setPeople([]));
  }, [projectId]);

  const loadRows = useCallback(async () => {
    try {
      setRows(await listSeriesEpisodes(seriesId));
    } catch {
      setRows((cur) => cur ?? []); // keep what we have on a transient failure
    }
  }, [seriesId]);

  useEffect(() => {
    void loadRows();
  }, [loadRows]);

  // Episode rows carry server-derived status/progress that a fire-and-forget
  // patch (or another PM) can change — keep them live without a reload.
  useRevalidate(() => void loadRows(), { intervalMs: 20000 });

  /** Add ONE extra episode beyond the plan (e.g. a late bonus episode), and
   *  raise Planned episodes to match so the plan never lags reality. */
  async function addEpisode() {
    if (adding) return;
    setAdding(true);
    try {
      const existing = rows ?? [];
      const n = existing.length + 1;
      await createScene(projectId, {
        name: `Episode ${n}`,
        series_id: seriesId,
        code: episodeCode(seriesCode, n),
        order_index: existing.length,
      });
      // Keep the plan in step: planned = max(current planned, new count).
      const nextPlanned = Math.max(plannedEpisodes, n);
      if (nextPlanned !== plannedEpisodes) {
        await patchSeries(seriesId, {
          production: { total_episodes_planned: nextPlanned },
        });
      }
      setRows(await listSeriesEpisodes(seriesId));
      onEpisodeAdded(); // refresh the series row (counts + planned)
      toast(`Added Episode ${n}. Planned episodes: ${nextPlanned}.`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "could not add episode", "error");
    } finally {
      setAdding(false);
    }
  }

  /** Add MANY episodes at once (asks how many). Uses the same top-up generator
   *  as the producer form, so it only creates what's missing; Planned episodes
   *  is raised to the new total. */
  async function addManyEpisodes() {
    if (adding) return;
    const have = (rows ?? []).length;
    // eslint-disable-next-line no-alert
    const raw = window.prompt(
      `How many episodes to add? (currently ${have})`,
      "10",
    );
    if (raw == null) return;
    const count = Math.max(0, parseInt(raw, 10) || 0);
    if (count <= 0) return;
    const target = have + count;
    if (target > 2000) {
      toast("Too many — the per-series limit is 2000 episodes", "error");
      return;
    }
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Create ${count} episode(s)? The series will have ${target}.`))
      return;
    setAdding(true);
    try {
      const r = await generateSeriesStructure(seriesId, {
        episodes: target,
        sequences_per_episode: 0, // empty episodes — artists add the sequences
      });
      const nextPlanned = Math.max(plannedEpisodes, target);
      if (nextPlanned !== plannedEpisodes) {
        await patchSeries(seriesId, {
          production: { total_episodes_planned: nextPlanned },
        });
      }
      setRows(await listSeriesEpisodes(seriesId));
      onEpisodeAdded();
      toast(
        `Added ${r.episodes_created} episode(s). Planned episodes: ${nextPlanned}.`,
      );
    } catch (e) {
      toast(e instanceof Error ? e.message : "could not add episodes", "error");
    } finally {
      setAdding(false);
    }
  }

  function patch(ep: SceneDTO, key: string, value: string) {
    setRows((cur) =>
      cur
        ? cur.map((r) =>
            r.id === ep.id
              ? {
                  ...r,
                  production: { ...(r.production ?? {}), [key]: value },
                  // Setting the status by hand is what stops it being derived. Not
                  // clearing this locally would leave the row claiming "worked out
                  // from the work" until the next reload, on the value the PM just
                  // chose themselves.
                  ...(key === "status" && value !== "NotStarted"
                    ? { status_auto: false }
                    : {}),
                }
              : r,
          )
        : cur,
    );
    void patchScene(ep.id, { production: { [key]: value } }).catch(() => {
      toast("Save failed", "error");
    });
  }

  async function removeEpisode(ep: SceneDTO) {
    if (
      // eslint-disable-next-line no-alert
      !window.confirm(
        `Xoá tập ${ep.code || ep.name}?\n\n` +
          "Toàn bộ sequence và canvas bên trong sẽ mất theo. Không hoàn tác được.",
      )
    )
      return;
    try {
      await deleteScene(ep.id);
      setRows(await listSeriesEpisodes(seriesId));
    } catch {
      toast("Xoá không được", "error");
    }
  }

  /** Who owns this episode: the one account that may submit its cut.
   *
   *  A separate control from the crew names beside it on purpose — those are
   *  the sheet's record of who did what, and they grant nothing. This one
   *  decides access. */
  function AssigneeSelect({ ep }: { ep: SceneDTO }) {
    const cur = ep.assignee_user_id ?? "";
    return (
      <select
        className="crm-input crm-input--crew"
        value={cur}
        onChange={async (e) => {
          const uid = e.target.value || null;
          setRows((c) =>
            c ? c.map((r) => (r.id === ep.id ? { ...r, assignee_user_id: uid } : r)) : c,
          );
          try {
            await setEpisodeAssignee(ep.id, uid);
          } catch {
            toast("Gán không được", "error");
          }
        }}
      >
        <option value="">— chưa giao —</option>
        {people.map((u) => (
          <option key={u.user_id} value={u.user_id}>
            {u.name}
          </option>
        ))}
      </select>
    );
  }

  /** Crew cell = a dropdown of known names + the current value + "＋ New…". */
  function CrewSelect({ ep, role }: { ep: SceneDTO; role: string }) {
    const cur = ep.production?.[role] == null ? "" : String(ep.production[role]);
    // Union of the global pool and the current value (so a name not yet in the
    // pool stays selectable) — sorted, case-insensitive.
    const opts = Array.from(new Set([...crewNames, ...(cur ? [cur] : [])])).sort(
      (a, b) => a.toLowerCase().localeCompare(b.toLowerCase()),
    );
    return (
      <select
        className="crm-input crm-input--crew"
        value={cur}
        onChange={(e) => {
          if (e.target.value === "__new__") {
            // eslint-disable-next-line no-alert
            const name = (window.prompt("New name") ?? "").trim();
            if (name) {
              onCrewAdded(name);
              patch(ep, role, name);
            }
            return;
          }
          patch(ep, role, e.target.value);
        }}
      >
        <option value="">—</option>
        {opts.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
        <option value="__new__">＋ New…</option>
      </select>
    );
  }

  // One-off "add an extra episode" control (also the only action in the empty
  // state) — inline-styled so it renders regardless of stylesheet caching.
  const addBtn = canAdd ? (
    <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
      <button
        type="button"
        className="btn2 btn2--ghost"
        onClick={() => void addEpisode()}
        disabled={adding}
        title="Add one extra episode beyond the plan (raises Planned episodes)"
      >
        {adding ? "Adding…" : "+ Add episode"}
      </button>
      <button
        type="button"
        className="btn2 btn2--ghost"
        onClick={() => void addManyEpisodes()}
        disabled={adding}
        title="Add many episodes at once (raises Planned episodes)"
      >
        {adding ? "Adding…" : "+ Add many…"}
      </button>
    </div>
  ) : null;

  if (rows === null) return <div className="crm-sub__loading">Loading episodes…</div>;
  if (rows.length === 0)
    return (
      <div className="crm-sub__empty">
        No episodes yet in this series.
        {addBtn ? <div>{addBtn}</div> : null}
      </div>
    );

  return (
    <div className="crm-sub">
      <table className="crm-eptable">
        <thead>
          <tr>
            <th>Episode</th>
            <th>Status</th>
            {/* The account that OWNS the episode — the only one who may hand
                the cut in, and what decides who can see it at all. Distinct
                from the four crew names beside it, which mirror the sheet and
                grant nothing. It lived on a per-episode page that is going
                away; without it here, nobody could assign work. */}
            <th>Assignee</th>
            <th>Scriptwriter</th>
            <th>Concept</th>
            <th>AI creator</th>
            <th>Editor</th>
            <th>Deadline</th>
            <th>Done</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((ep) => {
            const p = ep.production ?? {};
            const str = (k: string) => (p[k] == null ? "" : String(p[k]));
            return (
              <tr key={ep.id}>
                <td className="crm-eptable__ep">
                  <b>{ep.code || ep.name}</b>
                </td>
                <td>
                  {/* The value can be DERIVED: nobody updates a status field for
                      work they are in the middle of, so a row no one has set is
                      filled in from what has actually happened in the episode.
                      Flagged with a dot rather than styled apart — it is still the
                      real status, and picking a value here stores it and wins. */}
                  <select
                    className={`crm-input crm-input--status ${statusClass(str("status") || "NotStarted")}${
                      ep.status_auto ? " is-auto" : ""
                    }`}
                    title={
                      ep.status_auto
                        ? "Tự suy ra từ công việc trong episode — chọn một giá trị để chốt bằng tay"
                        : "Do người đặt"
                    }
                    value={str("status") || "NotStarted"}
                    onChange={(e) => patch(ep, "status", e.target.value)}
                  >
                    {EP_STATUS.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <AssigneeSelect ep={ep} />
                </td>
                {CREW_ROLES.map((role) => (
                  <td key={role}>
                    <CrewSelect ep={ep} role={role} />
                  </td>
                ))}
                <td>
                  <input
                    className="crm-input crm-input--date"
                    type="date"
                    defaultValue={str("deadline")}
                    onBlur={(e) => {
                      if (e.target.value !== str("deadline")) patch(ep, "deadline", e.target.value);
                    }}
                  />
                </td>
                <td>
                  <input
                    className="crm-input crm-input--date"
                    type="date"
                    defaultValue={str("complete_date")}
                    onBlur={(e) => {
                      if (e.target.value !== str("complete_date"))
                        patch(ep, "complete_date", e.target.value);
                    }}
                  />
                </td>
                <td>
                  <button
                    className="crm-del"
                    title="Xoá tập này"
                    onClick={() => void removeEpisode(ep)}
                  >
                    ✕
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {addBtn}
    </div>
  );
}

// ── New / Edit series form ───────────────────────────────────────────────────

interface ProjectLite {
  id: string;
  name: string;
}

function SeriesForm({
  projects,
  editing,
  staff,
  onClose,
  onSaved,
}: {
  projects: ProjectLite[];
  editing: Row | null;
  /** Real accounts. Handing over a series grants access to every episode under
   *  it, and only an account can be granted anything — so this picker offers
   *  accounts and not the sheet's free-text crew pool, which still feeds the
   *  four per-episode crew columns where a name is only a record. */
  staff: { id: string; name: string }[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const p0 = editing?.production ?? {};
  // Draft persistence: a form closed (Cancel / ✕ / Esc) WITHOUT saving keeps
  // what was typed, so reopening restores it. Keyed per new/edit; cleared on a
  // successful save.
  const draftKey = editing ? `crm:series:draft:${editing.id}` : "crm:series:draft:new";
  const draft0 = (() => {
    try {
      const raw = localStorage.getItem(draftKey);
      return raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  })();

  // The RAW restored value — it may name a project that no longer exists. Use
  // `projectId` below, never this.
  const [rawProjectId, setProjectId] = useState(
    (draft0?.projectId as string) ?? editing?.project_id ?? projects[0]?.id ?? "",
  );
  // A draft outlives the project it was written against: data gets wiped, a
  // project gets deleted, and the uuid in localStorage stops resolving. That was
  // invisible, because a <select> whose value matches no <option> renders as the
  // FIRST option — so the form showed a real project sitting there selected while
  // the state held a dead id, and Save came back "project not found" pointing at a
  // project plainly on the screen.
  //
  // Resolved in the same render as the select reads it, not in an effect: an
  // effect leaves one render where the box and the request disagree, and that
  // render is long enough to submit in.
  const projectId = projects.some((p) => p.id === rawProjectId)
    ? rawProjectId
    : projects[0]?.id ?? "";
  // Write the correction back so the stale id leaves the draft too — otherwise it
  // sits there and comes back the next time the list loads slowly. Skipped while
  // the list is still empty, which would otherwise clear a perfectly good draft.
  useEffect(() => {
    if (projects.length > 0 && projectId !== rawProjectId) setProjectId(projectId);
  }, [projects.length, projectId, rawProjectId]);

  const [name, setName] = useState((draft0?.name as string) ?? editing?.name ?? "");
  const [code, setCode] = useState((draft0?.code as string) ?? editing?.code ?? "");
  // Archive lock. Not draft-persisted — it is a state, not typed content.
  const [frozen, setFrozen] = useState<boolean>(editing?.frozen ?? false);
  const [f, setF] = useState<Record<string, string>>(() => {
    if (draft0?.f && typeof draft0.f === "object") return draft0.f as Record<string, string>;
    const init: Record<string, string> = {};
    for (const k of Object.keys(p0)) init[k] = p0[k] == null ? "" : String(p0[k]);
    return init;
  });
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: string) => setF((s) => ({ ...s, [k]: v }));

  // Persist the draft on every change.
  useEffect(() => {
    try {
      localStorage.setItem(draftKey, JSON.stringify({ projectId, name, code, f }));
    } catch {
      /* storage full / disabled — non-fatal */
    }
  }, [draftKey, projectId, name, code, f]);

  // Esc closes the form (same as Cancel / ✕ — draft is kept).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  function clearDraft() {
    try {
      localStorage.removeItem(draftKey);
    } catch {
      /* ignore */
    }
  }

  // The one number here that decides anything: how many empty Episodes Save
  // creates. There was a "seconds / video" input beside it, feeding a hard cap on
  // sequences per episode; the cap is gone (an artist splits a beat into two
  // angles when the cut needs it, and the ceiling that costs money is takes per
  // sequence) and the input went with it rather than staying on as a number
  // nothing reads.
  const plannedEps = Math.max(0, parseInt(f.total_episodes_planned ?? "", 10) || 0);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !name.trim() || !projectId) return;
    setBusy(true);
    try {
      // The sheet's name column is written from the same choice as the account
      // below, so the two cannot say different things about who has the series.
      const chosen = staff.find((u) => u.id === assigneeId);
      const production = { ...f, assignee: chosen?.name ?? "" };
      let seriesId: string;
      if (editing) {
        await patchSeries(editing.id, { name: name.trim(), code: code.trim(), production, frozen });
        seriesId = editing.id;
      } else {
        const created = await createSeries(projectId, {
          name: name.trim(),
          code: code.trim(),
          production,
        });
        seriesId = created.id;
      }
      // The handover itself — a separate call because it grants access to a whole
      // subtree, so it is audited and gated on member.manage rather than riding
      // along inside a metadata patch. Sent whenever it differs from what the row
      // already had, INCLUDING when it is cleared: taking the series back is the
      // same act as giving it away.
      if ((editing?.assignee_user_id ?? "") !== assigneeId) {
        await setSeriesAssignee(seriesId, assigneeId || null);
      }
      // Create the planned number of EMPTY episodes — artists add the sequences
      // themselves, as many as the cut needs. Idempotent: only missing episodes
      // are added, so re-saving tops up instead of duplicating.
      if (plannedEps > 0) {
        const r = await generateSeriesStructure(seriesId, {
          episodes: plannedEps,
          sequences_per_episode: 0,
        });
        toast(`Saved. +${r.episodes_created} empty episode(s)`);
      } else {
        toast(editing ? `Saved "${name.trim()}"` : `Created "${name.trim()}"`);
      }
      clearDraft(); // saved successfully → drop the recovery draft
      onSaved();
      onClose();
    } catch (err) {
      toast(err instanceof Error ? err.message : "save failed", "error");
    } finally {
      setBusy(false);
    }
  }

  // Who the series is handed to. An account, because this one grants access to
  // every episode under it — a name cannot be given a login. Rows written before
  // this field existed hold only a name; it is matched back to an account so the
  // dropdown opens on the right person instead of "chưa giao".
  const [assigneeId, setAssigneeId] = useState("");
  useEffect(() => {
    if (editing?.assignee_user_id) {
      setAssigneeId(editing.assignee_user_id);
      return;
    }
    const legacy = typeof p0.assignee === "string" ? p0.assignee.trim() : "";
    const match = legacy ? staff.find((u) => u.name === legacy) : undefined;
    setAssigneeId(match?.id ?? "");
  }, [editing?.assignee_user_id, p0.assignee, staff]);

  return (
    <div
      className="project-modal-backdrop"
      role="dialog"
      aria-modal="true"
      // Deliberately NO close-on-backdrop-click here — this is a long data-entry
      // form; a stray click outside must not wipe what's being typed. Close only
      // via Cancel or the ✕.
    >
      <form className="crm-form" onSubmit={submit}>
        <div className="crm-form__head">
          <h2>{editing ? "Edit series" : "New series"}</h2>
          <button type="button" className="crm-form__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <div className="crm-form__body">
          <section className="crm-form__section">
            <h3>Identity</h3>
            <div className="crm-form__grid">
              <label className="crm-field">
                <span>Project</span>
                <select
                  className="crm-field__input"
                  value={projectId}
                  disabled={!!editing}
                  onChange={(e) => setProjectId(e.target.value)}
                >
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="crm-field crm-field--wide">
                <span>Series name *</span>
                <input
                  className="crm-field__input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="She Wears Special Outfit For Boss"
                  autoFocus
                />
              </label>
              <label className="crm-field">
                <span>Code</span>
                <input
                  className="crm-field__input"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="OUTF"
                />
              </label>
              <label className="crm-field">
                <span>Full code (auto)</span>
                <input
                  className="crm-field__input"
                  value={f.full_code ?? ""}
                  readOnly
                  placeholder="auto-generated on save — MOGU_26001_OUTF_…"
                  title="Generated automatically from project, start date, code and name"
                />
              </label>
            </div>
          </section>

          <section className="crm-form__section">
            <h3>Classification</h3>
            <div className="crm-form__grid">
              <label className="crm-field crm-field--wide">
                <span>Tier</span>
                <TierPicker
                  value={f.tier == null ? "" : String(f.tier)}
                  onChange={(v) => set("tier", v)}
                />
              </label>
              <label className="crm-field">
                <span>Status</span>
                <select
                  className="crm-field__input"
                  value={f.status ?? "Planning"}
                  onChange={(e) => set("status", e.target.value)}
                >
                  {SERIES_STATUS.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
              <label className="crm-field">
                <span>Priority</span>
                <select
                  className="crm-field__input"
                  value={f.priority ?? "Medium"}
                  onChange={(e) => set("priority", e.target.value)}
                >
                  {PRIORITY.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
              {/* Archive lock. Freezing makes the series + all its episodes
                  view-only (no editing/gen); this is what "archive" set. Flip it
                  back to Open to reopen a previously-archived series. */}
              <label className="crm-field">
                <span>Khoá archive</span>
                <select
                  className="crm-field__input"
                  value={frozen ? "1" : "0"}
                  onChange={(e) => setFrozen(e.target.value === "1")}
                >
                  <option value="0">Mở — chỉnh sửa / gen được</option>
                  <option value="1">Khoá (archived) — chỉ xem</option>
                </select>
              </label>
            </div>
          </section>

          <section className="crm-form__section">
            <h3>Team</h3>
            {/* Producer is NOT asked for — whoever creates the series is its
                producer (that's already their project role). Recorded server-side. */}
            <div className="crm-form__grid">
              {/* This actually hands the series over now.
                  It used to write a NAME into the production bag, which is what
                  the sheet column is — and a name grants nothing, so a PM who
                  assigned here got a person who could not see the project. The
                  value is an ACCOUNT, saved to `series.assignee_user_id`, and
                  every episode under the series becomes theirs to work in and
                  hand in. The sheet's name column is written from the same
                  choice so the two cannot drift.
                  Not `producer_user_id`, which sits beside it in the model: that
                  is the first REVIEWER of this series' submissions. */}
              <label className="crm-field">
                <span>Assignee (builds the series)</span>
                <select
                  className="crm-field__input"
                  value={assigneeId}
                  onChange={(e) => setAssigneeId(e.target.value)}
                >
                  <option value="">— chưa giao —</option>
                  {staff.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.name}
                    </option>
                  ))}
                </select>
                <small className="crm-field__hint">
                  Cả series về tay người này — mọi episode bên trong. Giao riêng một
                  tập cho người khác ở bảng Episode thì tập đó theo người kia.
                </small>
              </label>
            </div>
          </section>

          <section className="crm-form__section">
            <h3>Schedule</h3>
            <div className="crm-form__grid">
              <label className="crm-field">
                <span>Start date</span>
                <input className="crm-field__input" type="date" value={f.start_date ?? ""} onChange={(e) => set("start_date", e.target.value)} />
              </label>
              <label className="crm-field">
                <span>End date</span>
                <input className="crm-field__input" type="date" value={f.end_date ?? ""} onChange={(e) => set("end_date", e.target.value)} />
              </label>
              <label className="crm-field">
                <span>Planned episodes</span>
                <input className="crm-field__input" type="number" min={0} value={f.total_episodes_planned ?? ""} onChange={(e) => set("total_episodes_planned", e.target.value)} />
              </label>
              <label className="crm-field">
                <span>Duration / episode (s)</span>
                <input className="crm-field__input" type="number" min={0} value={f.episode_duration_sec ?? ""} onChange={(e) => set("episode_duration_sec", e.target.value)} />
              </label>
              <label className="crm-field">
                <span>Folder link</span>
                <input className="crm-field__input" value={f.folder_link ?? ""} onChange={(e) => set("folder_link", e.target.value)} placeholder="Drive folder…" />
              </label>
            </div>
          </section>

          <section className="crm-form__section">
            <h3>Content</h3>
            <div className="crm-form__grid">
              <label className="crm-field">
                <span>Genres</span>
                <input className="crm-field__input" value={f.genres ?? ""} onChange={(e) => set("genres", e.target.value)} placeholder="Drama, Romance" />
              </label>
              <label className="crm-field">
                <span>Tropes</span>
                <input className="crm-field__input" value={f.tropes ?? ""} onChange={(e) => set("tropes", e.target.value)} placeholder="Climax, Payoff" />
              </label>
            </div>
          </section>

          <section className="crm-form__section">
            <h3>Episodes</h3>
            <p className="crm-gen__hint">
              On <b>Save</b>, <b>{plannedEps || "N"}</b> empty Episodes are created
              on the project home — idempotent, so existing episodes are kept.
              Artists add the sequences themselves, <b>as many as the cut needs</b>;
              nothing here limits that. The one ceiling is <b>5 video takes per
              sequence</b>, which a PM lifts from <i>Admin → Sequences</i>.
            </p>
            <div className="crm-gen__calc">
              {plannedEps > 0 ? (
                <>
                  → <b>{plannedEps}</b> empty episodes · sequences unlimited ·{" "}
                  <b>5</b> takes / sequence
                </>
              ) : (
                <span className="crm-gen__muted">
                  Set “Planned episodes” above to build them
                </span>
              )}
            </div>
          </section>
        </div>

        <div className="crm-form__foot">
          <button type="button" className="btn2 btn2--ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="btn2 btn2--primary" disabled={busy || !name.trim() || !projectId}>
            {busy ? "Saving…" : editing ? "Save" : "Create series"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ── Main CRM ─────────────────────────────────────────────────────────────────

export function ProductionCRM() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [projects, setProjects] = useState<ProjectLite[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [statusF, setStatusF] = useState("");
  const [priF, setPriF] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [formFor, setFormFor] = useState<Row | null | "new">(null);
  const [crewNames, setCrewNames] = useState<string[]>([]);
  // Real accounts, for the pickers. Two pools on purpose: `crewNames` is the
  // sheet's own staff list (people who may not have a login at all), and these
  // are accounts. The producer picker offers both, accounts first.
  const [staff, setStaff] = useState<{ id: string; name: string }[]>([]);

  useEffect(() => {
    void getCrewNames()
      .then((r) => setCrewNames(r.names))
      .catch(() => {});
    void fetch("/api/admin/users")
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: { id: string; username: string; display_name?: string | null }[]) =>
        setStaff(
          (rows ?? []).map((u) => ({ id: u.id, name: u.display_name || u.username })),
        ),
      )
      .catch(() => setStaff([]));
  }, []);

  const load = useCallback(async () => {
    try {
      const projs = await listProjects();
      setProjects(projs.map((p) => ({ id: p.id, name: p.name })));
      // Fan out per project (the series list is project-scoped) and flatten.
      const perProject = await Promise.all(
        projs.map((p) =>
          listSeries(p.id)
            .then((ss) => ss.map((s) => ({ ...s, project_name: p.name }) as Row))
            .catch(() => [] as Row[]),
        ),
      );
      setRows(perProject.flat());
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (rows ?? []).filter((r) => {
      const st = String(r.production?.status ?? "");
      const pr = String(r.production?.priority ?? "");
      if (statusF && st !== statusF) return false;
      if (priF && pr !== priF) return false;
      if (!needle) return true;
      return (
        r.name.toLowerCase().includes(needle) ||
        (r.code ?? "").toLowerCase().includes(needle) ||
        r.project_name.toLowerCase().includes(needle)
      );
    });
  }, [rows, q, statusF, priF]);

  if (err) return <div className="admin-error">{err}</div>;

  return (
    <div className="crm">
      <div className="crm__toolbar">
        <input className="crm__search" placeholder="Search series, code, project…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="crm__filter" value={statusF} onChange={(e) => setStatusF(e.target.value)}>
          <option value="">All status</option>
          {SERIES_STATUS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select className="crm__filter" value={priF} onChange={(e) => setPriF(e.target.value)}>
          <option value="">All priority</option>
          {PRIORITY.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <span className="crm__count">{shown.length} series</span>
        <button className="btn2 btn2--primary crm__new" onClick={() => setFormFor("new")}>
          + New series
        </button>
      </div>

      <div className="crm__card">
        {rows === null ? (
          <div className="crm-sub__loading">Loading…</div>
        ) : shown.length === 0 ? (
          <div className="admin2__empty">No series match.</div>
        ) : (
          <table className="crm-table">
            <thead>
              <tr>
                <th className="crm-table__ex" />
                <th>Code</th>
                <th>Full code</th>
                <th>Project</th>
                <th>Tier</th>
                <th>Producer</th>
                <th>Assignee</th>
                <th>Progress</th>
                <th>Budget</th>
                <th>Status</th>
                <th>Priority</th>
                <th className="admin2__th-actions" />
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => {
                const isOpen = open === r.id;
                const p = r.production ?? {};
                const done = r.stats?.by_status?.Completed ?? 0;
                const total = r.stats?.episodes ?? r.episode_count ?? 0;
                const planned = Number(p.total_episodes_planned ?? 0) || total;
                const pct = r.stats?.completion_pct ?? 0;
                return (
                  <Fragment key={r.id}>
                    <tr className="crm-table__row" onClick={() => setOpen(isOpen ? null : r.id)}>
                      <td className="admin2__muted">{isOpen ? "▾" : "▸"}</td>
                      <td className="crm-table__code">{r.code || "—"}</td>
                      <td className="crm-table__fullcode" title={p.full_code ? String(p.full_code) : ""}>
                        {p.full_code ? String(p.full_code) : "—"}
                      </td>
                      <td className="admin2__muted">{r.project_name}</td>
                      <td>
                        <TierChip tier={p.tier} />
                      </td>
                      <td className="crm-table__clip admin2__muted" title={p.producer ? String(p.producer) : ""}>
                        {p.producer ? String(p.producer) : "—"}
                      </td>
                      <td className="crm-table__clip admin2__muted" title={p.assignee ? String(p.assignee) : ""}>
                        {p.assignee ? String(p.assignee) : "—"}
                      </td>
                      <td>
                        <div className="crm-prog" title={`${done}/${planned} episodes`}>
                          <div className="crm-prog__bar">
                            <span style={{ width: `${Math.min(100, pct)}%` }} />
                          </div>
                          <span className="crm-prog__txt">
                            {done}/{planned || "?"}
                          </span>
                        </div>
                      </td>
                      <td><BudgetCell budget={r.budget} /></td>
                      <td>{p.status ? <span className={`crm-chip ${statusClass(String(p.status))}`}>{p.status}</span> : "—"}</td>
                      <td>{p.priority ? <span className={`crm-chip ${priClass(String(p.priority))}`}>{p.priority}</span> : "—"}</td>
                      <td className="crm-table__actions">
                        <button
                          className="btn2 btn2--ghost"
                          onClick={(e) => {
                            e.stopPropagation();
                            setFormFor(r);
                          }}
                        >
                          Edit
                        </button>
                      </td>
                    </tr>
                    {isOpen ? (
                      <tr className="crm-table__drill">
                        <td colSpan={11}>
                          <SeriesDetail seriesName={r.name} production={r.production ?? {}} />
                          <BudgetPanel scope="series" scopeId={r.id} canSetBase canGrant canDecide />
                          <EpisodeTable
                            // Remount (refetch) whenever the episode count changes
                            // — e.g. right after auto-generate adds episodes.
                            key={`${r.id}:${r.stats?.episodes ?? r.episode_count ?? 0}`}
                            seriesId={r.id}
                            seriesCode={r.code}
                            projectId={r.project_id}
                            plannedEpisodes={
                              parseInt(String(p.total_episodes_planned ?? ""), 10) || 0
                            }
                            crewNames={crewNames}
                            canAdd
                            onCrewAdded={(n) =>
                              setCrewNames((cur) =>
                                cur.includes(n) ? cur : [...cur, n],
                              )
                            }
                            onEpisodeAdded={() => void load()}
                          />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {formFor ? (
        <SeriesForm
          projects={projects}
          editing={formFor === "new" ? null : formFor}
          staff={staff}
          onClose={() => setFormFor(null)}
          onSaved={() => void load()}
        />
      ) : null}
    </div>
  );
}

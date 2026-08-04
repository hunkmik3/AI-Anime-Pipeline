import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  assignPanels,
  listPanelAssignees,
  listPanelProjects,
  listPanels,
  thumbUrl,
  type Panel,
  type PanelStatus,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { PersonPicker } from "../components/PersonPicker";
import { toast } from "../store/toast";

/**
 * The panel grid — what replaces the Miro board.
 *
 * Miro worked because it was a TABLE: one row per panel, original beside result,
 * notes at the end. This keeps that pairing but as cards, because at 200-300
 * panels the questions people actually ask are "which are waiting on me", "which
 * did the PM send back", "how many has Quân got left" — and those are answered by
 * filtering a dense grid, not by panning a canvas.
 */

const STATUS_LABEL: Record<PanelStatus, string> = {
  todo: "Not started",
  in_progress: "In progress",
  submitted: "In review",
  changes_requested: "Sent back",
  approved: "Approved",
};

const STATUS_ORDER: PanelStatus[] = [
  "todo",
  "in_progress",
  "submitted",
  "changes_requested",
  "approved",
];

export function PanelGridPage() {
  const { projectId } = useParams();
  const pid = Number(projectId);
  const [panels, setPanels] = useState<Panel[] | null>(null);
  const [projectName, setProjectName] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  const [assignees, setAssignees] = useState<{ user_id: string; name: string }[]>([]);
  const [status, setStatus] = useState<PanelStatus | "all">("all");
  const [assignee, setAssignee] = useState<string | "all" | "none">("all");
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const load = useCallback(async () => {
    try {
      const [rows, projects] = await Promise.all([listPanels(pid), listPanelProjects()]);
      setPanels(rows);
      setProjectName(projects.find((p) => p.id === pid)?.name ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [pid]);

  useEffect(() => {
    void load();
    void listPanelAssignees().then(setAssignees).catch(() => setAssignees([]));
  }, [load]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const p of panels ?? []) c[p.status] = (c[p.status] ?? 0) + 1;
    return c;
  }, [panels]);

  // Assignee filter options come from the panels themselves rather than the whole
  // user list: only people actually holding panels are worth filtering by.
  const people = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of panels ?? []) {
      if (p.assignee_user_id) m.set(p.assignee_user_id, p.assignee_name ?? "—");
    }
    return [...m.entries()];
  }, [panels]);

  const shown = useMemo(() => {
    return (panels ?? []).filter((p) => {
      if (status !== "all" && p.status !== status) return false;
      if (assignee === "none" && p.assignee_user_id) return false;
      if (assignee !== "all" && assignee !== "none" && p.assignee_user_id !== assignee)
        return false;
      return true;
    });
  }, [panels, status, assignee]);

  function toggle(id: number, shift: boolean, index: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (shift && prev.size > 0) {
        // Range select: the whole point is "artist 1 takes panels 1-30", so
        // clicking one end and shift-clicking the other has to work.
        const ids = shown.map((p) => p.id);
        const last = ids.findIndex((x) => prev.has(x));
        const [a, b] = [Math.min(last, index), Math.max(last, index)];
        for (let i = a; i <= b; i++) next.add(ids[i]);
        return next;
      }
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function assignTo(userId: string | null) {
    if (selected.size === 0) return;
    try {
      const r = await assignPanels(pid, [...selected], userId);
      setSelected(new Set());
      await load();
      toast(`${r.assigned} panel(s) ${userId ? "assigned" : "unassigned"}.`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Assign failed");
    }
  }

  return (
    <div className="shellpage pn__page">
      <PageHeader
        crumb={<Link to="/giantflow">Giantflow</Link>}
        title={projectName || "Panels"}
        subtitle={
          panels
            ? `${panels.length} panels · ${counts.approved ?? 0} approved`
            : undefined
        }
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {panels === null ? <p className="rfoot">Loading…</p> : null}

      {panels !== null && panels.length === 0 ? (
        <div className="inbox__empty">
          <b>No panels in this project.</b>
          Import the cutter's folder from the Giantflow home page.
        </div>
      ) : null}

      {panels !== null && panels.length > 0 ? (
        <>
          <div className="pn__filters">
            <div className="seg">
              <button
                className={`seg__btn${status === "all" ? " is-on" : ""}`}
                onClick={() => setStatus("all")}
              >
                All {panels.length}
              </button>
              {STATUS_ORDER.map((s) => (
                <button
                  key={s}
                  className={`seg__btn${status === s ? " is-on" : ""}`}
                  onClick={() => setStatus(s)}
                  disabled={!counts[s]}
                >
                  {STATUS_LABEL[s]} {counts[s] ?? 0}
                </button>
              ))}
            </div>

            <select
              className="inbox__input pn__select"
              value={assignee}
              onChange={(e) => setAssignee(e.target.value as typeof assignee)}
            >
              <option value="all">Everyone</option>
              <option value="none">Unassigned</option>
              {people.map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </select>
          </div>

          {/* The assign bar only exists while something is selected — a control
              that does nothing is worse than no control. */}
          {selected.size > 0 ? (
            <div className="pn__assignbar">
              <span>
                <b>{selected.size}</b> selected
              </span>
              <PersonPicker
                label=""
                value={null}
                people={assignees}
                noneLabel="Assign to…"
                onChange={async (uid) => {
                  await assignTo(uid);
                }}
              />
              <button className="btn2" onClick={() => void assignTo(null)}>
                Unassign
              </button>
              <button className="btn2" onClick={() => setSelected(new Set())}>
                Clear
              </button>
            </div>
          ) : null}

          <ul className="pn__grid">
            {shown.map((p, i) => (
              <PanelCard
                key={p.id}
                panel={p}
                selected={selected.has(p.id)}
                onToggle={(shift) => toggle(p.id, shift, i)}
              />
            ))}
          </ul>
          {shown.length === 0 ? (
            <p className="rfoot">No panels match that filter.</p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function PanelCard({
  panel,
  selected,
  onToggle,
}: {
  panel: Panel;
  selected: boolean;
  onToggle: (shift: boolean) => void;
}) {
  return (
    <li className={`pn__card pn__card--${panel.status}${selected ? " is-sel" : ""}`}>
      {/* Original and result side by side — the pairing every PM note is about
          ("BG bị lệch màu so với truyện gốc" only means something next to the
          original). */}
      <Link to={`/giantflow/panel/${panel.id}`} className="pn__card-shots">
        <span className="pn__shot">
          {panel.raw_media_id ? (
            <img src={thumbUrl(panel.raw_media_id, 220)} alt="" loading="lazy" />
          ) : null}
          <em className="pn__shot-tag">raw{panel.raw_count > 1 ? ` ×${panel.raw_count}` : ""}</em>
        </span>
        <span className="pn__shot">
          {panel.latest_media_id ? (
            <img src={thumbUrl(panel.latest_media_id, 220)} alt="" loading="lazy" />
          ) : (
            <em className="pn__shot-empty">not generated</em>
          )}
          {panel.version_count > 0 ? (
            <em className="pn__shot-tag">v{panel.version_count}</em>
          ) : null}
        </span>
      </Link>

      <div className="pn__card-meta">
        <label className="pn__pick" onClick={(e) => e.stopPropagation()}>
          <input
            type="checkbox"
            checked={selected}
            onChange={(e) =>
              onToggle((e.nativeEvent as MouseEvent).shiftKey === true)
            }
            onClick={(e) => {
              if ((e as unknown as MouseEvent).shiftKey) e.stopPropagation();
            }}
          />
          <b>{panel.code}</b>
        </label>
        <span className="pn__who">{panel.assignee_name ?? "unassigned"}</span>
        {panel.unresolved_notes > 0 ? (
          <span className="pn__notes" title="Unresolved notes">
            {panel.unresolved_notes} note{panel.unresolved_notes === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>
    </li>
  );
}

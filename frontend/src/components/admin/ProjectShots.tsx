import { useEffect, useState } from "react";

/** Per-shot cost breakdown for a project (admin oversight): shots grouped by
 *  scene, each expandable to every generation (model, cost, kept/wasted, who,
 *  when). Rendered inside an expanded project row in the admin ProjectsTab. */

const usd = (v?: number | null) => (v != null ? `$${v.toFixed(2)}` : "—");

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(String(res.status));
  return res.json() as Promise<T>;
}

interface ShotRow {
  shot_id: string;
  shot_label: string;
  order_index: number;
  scene_id: string;
  scene_name: string;
  scene_order: number;
  total_usd: number;
  kept_usd: number;
  wasted_usd: number;
  clips: number;
  takes: number;
  downloaded_clips: number;
  waste_pct: number;
}

interface GenRow {
  node_id: number;
  node_title: string;
  node_type: string;
  kind: string | null;
  model: string | null;
  resolution: string | null;
  cost_usd: number;
  kept: boolean;
  user_id: string | null;
  user_name: string;
  created_at: string | null;
}

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export function ShotGens({ shotId }: { shotId: string }) {
  const [rows, setRows] = useState<GenRow[] | null>(null);
  useEffect(() => {
    let alive = true;
    getJson<GenRow[]>(`/api/admin/stats/shots/${shotId}/gens`)
      .then((r) => alive && setRows(r))
      .catch(() => alive && setRows([]));
    return () => {
      alive = false;
    };
  }, [shotId]);

  if (rows === null) return <div className="pshots__loading">Loading…</div>;
  if (rows.length === 0)
    return <div className="pshots__empty">This sequence hasn't generated anything yet (no spend).</div>;

  const total = rows.reduce((s, g) => s + g.cost_usd, 0);
  return (
    <table className="pshots__gens">
      <thead>
        <tr>
          <th>Time</th>
          <th>Member</th>
          <th>What</th>
          <th>Model · resolution</th>
          <th>Cost</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((g, i) => (
          <tr key={`${g.node_id}-${i}`}>
            <td className="pshots__muted">{fmtTime(g.created_at)}</td>
            <td>{g.user_name}</td>
            <td>
              {g.node_title}
              <span className="pshots__muted"> · {g.kind || g.node_type}</span>
            </td>
            <td className="pshots__muted">
              {g.model || "—"}
              {g.resolution ? ` · ${g.resolution}` : ""}
            </td>
            <td>
              <b>{usd(g.cost_usd)}</b>
            </td>
          </tr>
        ))}
        <tr className="pshots__total">
          <td colSpan={4}>Sequence total</td>
          <td><b>{usd(total)}</b></td>
        </tr>
      </tbody>
    </table>
  );
}

export function ProjectShots({ projectId }: { projectId: string }) {
  const [shots, setShots] = useState<ShotRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getJson<ShotRow[]>(`/api/admin/stats/projects/${projectId}/shots`)
      .then((r) => alive && setShots(r))
      .catch((e) => alive && setErr(e instanceof Error ? e.message : "load failed"));
    return () => {
      alive = false;
    };
  }, [projectId]);

  if (err) return <div className="admin-error">{err}</div>;
  if (shots === null) return <div className="pshots__loading">Loading sequences…</div>;
  if (shots.length === 0)
    return <div className="pshots__empty">This project has no sequences yet.</div>;

  // group by scene, preserving scene order
  const scenes: { id: string; name: string; order: number; shots: ShotRow[] }[] = [];
  for (const sh of shots) {
    let g = scenes.find((x) => x.id === sh.scene_id);
    if (!g) {
      g = { id: sh.scene_id, name: sh.scene_name, order: sh.scene_order, shots: [] };
      scenes.push(g);
    }
    g.shots.push(sh);
  }
  scenes.sort((a, b) => a.order - b.order);

  return (
    <div className="pshots">
      {scenes.map((sc) => (
        <div key={sc.id} className="pshots__scene">
          <div className="pshots__scene-head">{sc.name}</div>
          <table className="pshots__table">
            <thead>
              <tr>
                <th className="pshots__th-ex" />
                <th>Sequence</th>
                <th>Total spent</th>
                <th>Clip</th>
                <th>Gens</th>
              </tr>
            </thead>
            <tbody>
              {sc.shots.map((sh) => {
                const isOpen = open === sh.shot_id;
                return (
                  <>
                    <tr
                      key={sh.shot_id}
                      className={`pshots__row${sh.total_usd > 0 ? " pshots__row--clickable" : ""}`}
                      onClick={() =>
                        sh.total_usd > 0 && setOpen(isOpen ? null : sh.shot_id)
                      }
                    >
                      <td className="pshots__muted">
                        {sh.total_usd > 0 ? (isOpen ? "▾" : "▸") : ""}
                      </td>
                      <td>
                        <b>{sh.shot_label}</b>
                      </td>
                      <td>
                        {sh.total_usd > 0 ? (
                          <b>{usd(sh.total_usd)}</b>
                        ) : (
                          <span className="pshots__muted">$0.00</span>
                        )}
                      </td>
                      <td className="pshots__muted">{sh.clips}</td>
                      <td className="pshots__muted">{sh.takes}</td>
                    </tr>
                    {isOpen ? (
                      <tr key={`${sh.shot_id}-d`}>
                        <td colSpan={5} className="pshots__drill">
                          <ShotGens shotId={sh.shot_id} />
                        </td>
                      </tr>
                    ) : null}
                  </>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

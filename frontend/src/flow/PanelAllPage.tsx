import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  allPanels,
  downloadPanelsCsv,
  listPanelSeries,
  thumbUrl,
  type PanelSeries,
  type QueuePanel,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { useFlowStudioStore } from "../store/flowStudio";
import { FlowViewer } from "./FlowViewer";
import { GiantflowNav } from "./GiantflowNav";
import { STAGES } from "./PanelHero";

/**
 * Every panel, across every batch and comic, with where it stands.
 *
 * The three pages that existed each answered one question and refused the
 * others: the batch grid is nailed to a single batch, Review to a single status,
 * My work to a single person. None of them could answer the two a manager
 * actually asks — "which of Quân's came back", "what has nobody started, and
 * where is it" — because each of those crosses a boundary the others enforce.
 *
 * A table, not cards. At two hundred rows the job is scanning and comparing, and
 * a card grid trades the alignment that makes scanning possible for pictures
 * you have already seen. The thumbnail stays because a panel code is not how
 * anyone recognises a panel.
 */
export function PanelAllPage() {
  const [rows, setRows] = useState<QueuePanel[] | null>(null);
  const [projects, setProjects] = useState<PanelSeries[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [status, setStatus] = useState<string | "all">("all");
  const [project, setProject] = useState<number | "all">("all");
  const [artist, setArtist] = useState<string>("all");
  const [q, setQ] = useState("");
  // Report export: a custom created/updated date window (both optional) + the
  // current comic/status/search filters, downloaded as CSV.
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [dateField, setDateField] = useState<"created_at" | "updated_at">("created_at");
  const [exporting, setExporting] = useState(false);
  // The studio's own viewer: full image, infinity zoom, pan, download. It only
  // needs a media id — the tools that act on a library row guard themselves.
  const select = useFlowStudioStore((s) => s.select);

  const load = useCallback(async () => {
    try {
      // Filtering happens server-side so the browser never holds a comic it is
      // not showing; the status counts below come from an unfiltered pass.
      setRows(
        await allPanels({
          series_id: project === "all" ? undefined : project,
          q,
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [project, q]);

  const onExport = useCallback(async () => {
    setExporting(true);
    setError(null);
    try {
      await downloadPanelsCsv({
        series_id: project === "all" ? undefined : project,
        q,
        status: status === "all" ? undefined : [status],
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        date_field: dateField,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(false);
    }
  }, [project, q, status, dateFrom, dateTo, dateField]);

  useEffect(() => {
    void load();
  }, [load]);

  // Switching the previewed role changes what the SERVER returns, so the page
  // has to ask again — otherwise you keep looking at the previous role's data.
  useEffect(() => {
    const onSwitch = () => void load();
    window.addEventListener("flowboard:view-as-changed", onSwitch);
    return () => window.removeEventListener("flowboard:view-as-changed", onSwitch);
  }, [load]);

  useEffect(() => {
    void listPanelSeries().then(setProjects).catch(() => setProjects([]));
  }, []);

  const all = rows ?? [];
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const p of all) c[p.status] = (c[p.status] ?? 0) + 1;
    return c;
  }, [all]);

  const shown = all.filter(
    (p) =>
      (status === "all" || p.status === status) &&
      (artist === "all" || (p.assignee_name ?? "Unassigned") === artist),
  );
  const artists = [...new Set(all.map((p) => p.assignee_name ?? "Unassigned"))].sort();

  return (
    // `pn__full`, not `pn__wide`: this is a table with a raw and a generated
    // image side by side, and every pixel the 1480px cap took away came off the
    // two picture columns — the only ones you actually look at.
    <div className="shellpage pn__full">
      <GiantflowNav />
      <PageHeader
        title="All panels"
        subtitle={
          rows ? `${shown.length} of ${all.length} shown` : undefined
        }
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {rows === null ? <p className="rfoot">Loading…</p> : null}

      <div className="pn__allbar">
        <input
          className="inbox__input pn__allsearch"
          placeholder="Search panel code…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select
          className="inbox__input pn__select"
          value={project}
          onChange={(e) => setProject(e.target.value === "all" ? "all" : Number(e.target.value))}
        >
          <option value="all">All comics</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select
          className="inbox__input pn__select"
          value={artist}
          onChange={(e) => setArtist(e.target.value)}
        >
          <option value="all">Everyone</option>
          {artists.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>

        {/* Report export — a custom date window + the filters above, as CSV. */}
        <span className="pn__exportgrp">
          <label className="pn__datewrap" title="Report window — start date (inclusive)">
            <span className="pn__dlabel">From</span>
            <input
              type="date"
              className="inbox__input pn__date"
              value={dateFrom}
              max={dateTo || undefined}
              onChange={(e) => setDateFrom(e.target.value)}
            />
          </label>
          <label className="pn__datewrap" title="Report window — end date (inclusive)">
            <span className="pn__dlabel">To</span>
            <input
              type="date"
              className="inbox__input pn__date"
              value={dateTo}
              min={dateFrom || undefined}
              onChange={(e) => setDateTo(e.target.value)}
            />
          </label>
          <select
            className="inbox__input pn__select"
            value={dateField}
            title="Which timestamp the date window filters on"
            onChange={(e) => setDateField(e.target.value as "created_at" | "updated_at")}
          >
            <option value="created_at">by created</option>
            <option value="updated_at">by updated</option>
          </select>
          <button
            type="button"
            className="pn__exportbtn"
            onClick={() => void onExport()}
            disabled={exporting}
          >
            {exporting ? "Exporting…" : "Export CSV"}
          </button>
        </span>
      </div>

      <div className="pn__filters">
        <div className="seg">
          <button
            className={`seg__btn${status === "all" ? " is-on" : ""}`}
            onClick={() => setStatus("all")}
          >
            All {all.length}
          </button>
          {STAGES.map((s) => (
            <button
              key={s.key}
              className={`seg__btn${status === s.key ? " is-on" : ""}`}
              disabled={!counts[s.key]}
              onClick={() => setStatus(s.key)}
            >
              {s.label} {counts[s.key] ?? 0}
            </button>
          ))}
        </div>
      </div>

      {rows !== null && shown.length === 0 ? (
        <div className="inbox__empty">
          <b>No panels match.</b>
          Clear a filter, or import a folder into a batch.
        </div>
      ) : null}

      {shown.length > 0 ? (
        <table className="pn__table pn__table--miro">
          <thead>
            <tr>
              <th>Comic / batch</th>
              <th>Panel</th>
              {/* The pairing the Miro board was built around: what came in
                  beside what came out. Judging one without the other is not a
                  thing anyone does. */}
              <th>Raw material</th>
              <th>Generated material</th>
              <th>Artist</th>
              <th>Status</th>
              <th>Ver.</th>
              <th>Notes</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((p) => (
              <tr key={p.id}>
                <td className="pn__tmuted pn__tcomic">
                  <div>{p.series_name}</div>
                  <div className="pn__tbatch">{p.batch_name}</div>
                </td>
                <td>
                  <Link to={`/giantflow/panel/${p.id}`} className="pn__tcode">
                    {p.code}
                  </Link>
                </td>
                <td className="pn__tshot">
                  {p.raw_media_id ? (
                    <button
                      type="button"
                      title="Open full size — scroll to zoom, drag to pan"
                      onClick={() => select(p.raw_media_id!)}
                    >
                      <img src={thumbUrl(p.raw_media_id, 640)} alt="" loading="lazy" />
                    </button>
                  ) : (
                    <span className="pn__tmuted">—</span>
                  )}
                </td>
                <td className="pn__tshot">
                  {p.delivered_media_id ? (
                    <button
                      type="button"
                      title="Open full size — scroll to zoom, drag to pan"
                      onClick={() => select(p.delivered_media_id!)}
                    >
                      <img src={thumbUrl(p.delivered_media_id, 640)} alt="" loading="lazy" />
                    </button>
                  ) : (
                    <span className="pn__tmuted">not generated</span>
                  )}
                </td>
                <td className="pn__tmuted">{p.assignee_name ?? "—"}</td>
                <td>
                  <span className={`pn__pill is-${p.status}`}>
                    {STAGES.find((s) => s.key === p.status)?.label ?? p.status}
                  </span>
                </td>
                <td className="pn__tmuted">
                  {p.version_count > 0 ? `v${p.delivered_version}/${p.version_count}` : "—"}
                </td>
                <td>
                  {p.unresolved_notes > 0 ? (
                    <span className="pn__notes">{p.unresolved_notes}</span>
                  ) : (
                    <span className="pn__tmuted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {/* Mounted once for the whole table, driven by `selectedMediaId`. Look
          only: this page manages work, it does not make any. */}
      <FlowViewer viewOnly />
    </div>
  );
}

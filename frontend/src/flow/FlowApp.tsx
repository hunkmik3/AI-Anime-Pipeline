import { useEffect, useRef, useState } from "react";
import { getFlowUsage, thumbUrl, type FlowUsage } from "../api/client";
// Scoped to this route — the studio's rules are all namespaced .fc__/.fv__/.fn__
// and the app's own styles.css defines none of those prefixes.
import "../flowstudio.css";
import { useFlowProjectsStore } from "../store/flowProjects";
import { CHAR_PREFIX, REF_PREFIX, SCENE_PREFIX, groupName, humanizeGenError, sanitizeErrorDetail, useFlowStudioStore, type GenJob } from "../store/flowStudio";
import { FlowComposer } from "./FlowComposer";
import { FlowViewer } from "./FlowViewer";

/**
 * Flow Studio — a Google-Flow-style image workspace, fully separate from the
 * Manga branch (own URL, own projects). Left rail = the project list; center =
 * the project's asset grid + bottom composer; clicking a tile opens a detail
 * overlay. Generation settings live in the composer popover.
 */
export function FlowApp() {

  const assets = useFlowStudioStore((s) => s.assets);
  const loading = useFlowStudioStore((s) => s.loading);
  const generating = useFlowStudioStore((s) => s.generating);
  const loadAssets = useFlowStudioStore((s) => s.load);
  const select = useFlowStudioStore((s) => s.select);
  const selectedMediaId = useFlowStudioStore((s) => s.selectedMediaId);
  const uploadAsset = useFlowStudioStore((s) => s.uploadAsset);
  const reusePrompt = useFlowStudioStore((s) => s.reusePrompt);
  const togglePin = useFlowStudioStore((s) => s.togglePin);
  const tagToPrompt = useFlowStudioStore((s) => s.tagToPrompt);
  const error = useFlowStudioStore((s) => s.error);
  const clearError = useFlowStudioStore((s) => s.clearError);
  const notice = useFlowStudioStore((s) => s.notice);
  const clearNotice = useFlowStudioStore((s) => s.clearNotice);

  const projects = useFlowProjectsStore((s) => s.projects);
  const activeId = useFlowProjectsStore((s) => s.activeId);
  const loadProjects = useFlowProjectsStore((s) => s.load);
  const switchTo = useFlowProjectsStore((s) => s.switchTo);
  const createProject = useFlowProjectsStore((s) => s.create);
  const renameProject = useFlowProjectsStore((s) => s.rename);
  const removeProject = useFlowProjectsStore((s) => s.remove);

  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const dragDepth = useRef(0);

  useEffect(() => {
    loadProjects();
  }, [loadProjects]);
  // Reload the active project's library whenever the project changes.
  useEffect(() => {
    loadAssets();
  }, [loadAssets, activeId]);

  let gridAssets = assets;
  if (query.trim()) {
    const q = query.trim().toLowerCase();
    gridAssets = gridAssets.filter(
      (a) => a.label.toLowerCase().includes(q) || (a.prompt ?? "").toLowerCase().includes(q),
    );
  }
  gridAssets = [...gridAssets].sort((a, b) => Number(b.pinned) - Number(a.pinned));

  const onDragEnter = (e: React.DragEvent) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    dragDepth.current += 1;
    setDragOver(true);
  };
  const onDragOver = (e: React.DragEvent) => {
    if ([...e.dataTransfer.types].includes("Files")) e.preventDefault();
  };
  const onDragLeave = () => {
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragOver(false);
  };
  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = 0;
    setDragOver(false);
    const files = Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith("image/"));
    for (const f of files) await uploadAsset(f);
  };

  const newProject = () => {
    const name = window.prompt("New project name:", `Project ${projects.length + 1}`);
    if (name === null) return;
    createProject(name.trim() || "Untitled");
  };

  return (
    <div className={`flow-app${collapsed ? " flow-app--rail-collapsed" : ""}`}>
      {/* ── left rail = project list ── */}
      <aside className="fn">
        <div className="fn__brand">
          <img className="fn__symbol" src="/favicon.png" alt="Giantflow" />
          {!collapsed && <span className="fn__title">GiantFlow</span>}
        </div>

        {!collapsed && (
          <button type="button" className="fn__newproj" onClick={newProject}>
            ＋ New project
          </button>
        )}

        <div className="fn__projects">
          {projects.map((p) => (
            <div
              key={p.id}
              className={`fn__prow${p.id === activeId ? " is-on" : ""}`}
              onClick={() => p.id !== undefined && switchTo(p.id)}
              title={p.name}
            >
              <span className="fn__prow-name">{collapsed ? "▦" : p.name}</span>
              {!collapsed && (
                <span className="fn__prow-actions">
                  <button
                    type="button"
                    className="fn__prow-act"
                    title="Rename"
                    onClick={(e) => {
                      e.stopPropagation();
                      const n = window.prompt("Rename project:", p.name);
                      if (n && n.trim() && p.id !== undefined) renameProject(p.id, n.trim());
                    }}
                  >
                    ✎
                  </button>
                  {projects.length > 1 && (
                    <button
                      type="button"
                      className="fn__prow-act"
                      title="Delete project"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (p.id !== undefined && window.confirm(`Delete project "${p.name}"?`)) removeProject(p.id);
                      }}
                    >
                      🗑
                    </button>
                  )}
                </span>
              )}
            </div>
          ))}
        </div>

        <div className="fn__spacer" />
        <FlowUsageBadge collapsed={collapsed} />
        <button
          type="button"
          className="fn__ghost"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "»" : "« Collapse"}
        </button>
      </aside>

      {/* ── center: top bar + grid + composer ── */}
      <main
        className="fc-center"
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        {dragOver && (
          <div className="fc-drop">
            <div className="fc-drop__inner">⬇ Drop images here to upload</div>
          </div>
        )}

        <div className="fc-top">
          <input
            className="fc-search"
            placeholder="Search by prompt…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="fc-count">{gridAssets.length} items</span>
        </div>

        {error && (() => {
          const friendly = humanizeGenError(error);
          const detail = sanitizeErrorDetail(error); // exact cause, backend vendor name masked
          return (
            <div className="fc-banner fc-banner--err" onClick={clearError} role="alert" title={detail}>
              ⚠ {friendly ?? detail}
              {/* Keep the exact cause (502, 413, safety, …) visible under the
                  friendly line — with the provider name masked out. */}
              {friendly && (
                <div style={{ fontSize: "0.82em", opacity: 0.7, marginTop: 2, fontWeight: 400 }}>
                  {detail}
                </div>
              )}
              <span className="fc-banner__x">✕</span>
            </div>
          );
        })()}
        {notice && (
          <div className="fc-banner fc-banner--info" onClick={clearNotice} role="status">
            ℹ {notice}
            <span className="fc-banner__x">✕</span>
          </div>
        )}

        <div className="fc-scroll">
          {loading ? (
            <div className="fc-empty">Loading…</div>
          ) : gridAssets.length === 0 && !generating ? (
            <div className="fc-empty">
              <div className="fc-empty__icon">❀</div>
              <div className="fc-empty__text">
                {query.trim()
                  ? "No matching images found."
                  : "Start creating: type a prompt below, or drag and drop images here."}
              </div>
            </div>
          ) : (
            <div className="fc-grid">
              {generating && <GenPlaceholders />}
              {gridAssets.map((a) => (
                <div
                  key={a.refId}
                  role="button"
                  tabIndex={0}
                  className={`fc-card${selectedMediaId === a.mediaId ? " is-selected" : ""}`}
                  onClick={() => select(a.mediaId)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      select(a.mediaId);
                    }
                  }}
                  title={a.prompt ?? a.label}
                >
                  <img src={thumbUrl(a.mediaId, 400)} alt={a.label} loading="lazy" decoding="async" />
                  {a.pinned && <span className="fc-badge fc-badge--pin">📌</span>}
                  {groupName(a.tags, CHAR_PREFIX) && <span className="fc-badge">👤</span>}
                  {groupName(a.tags, SCENE_PREFIX) && <span className="fc-badge fc-badge--scene">🎬</span>}
                  <div className="fc-card__bar">
                    <button
                      type="button"
                      className="fc-card__act"
                      title="Add image to prompt"
                      onClick={(e) => {
                        e.stopPropagation();
                        const cn = groupName(a.tags, CHAR_PREFIX);
                        const sn = groupName(a.tags, SCENE_PREFIX);
                        tagToPrompt(cn || sn || a.label || "image", a.mediaId, [a.mediaId]);
                      }}
                    >
                      @
                    </button>
                    {a.prompt && (
                      <button
                        type="button"
                        className="fc-card__act"
                        title="Reuse prompt"
                        onClick={(e) => {
                          e.stopPropagation();
                          const refIds = a.tags
                            .filter((t) => t.startsWith(REF_PREFIX))
                            .map((t) => t.slice(REF_PREFIX.length));
                          reusePrompt(a.prompt ?? "", refIds);
                        }}
                      >
                        ↩
                      </button>
                    )}
                    <button
                      type="button"
                      className={`fc-card__act${a.pinned ? " is-on" : ""}`}
                      title={a.pinned ? "Unpin" : "Pin"}
                      onClick={(e) => {
                        e.stopPropagation();
                        togglePin(a.refId);
                      }}
                    >
                      📌
                    </button>
                  </div>
                  {a.prompt && <div className="fc-card__cap">{a.prompt}</div>}
                </div>
              ))}
            </div>
          )}
        </div>

        <FlowComposer />
      </main>

      <FlowViewer />
    </div>
  );
}

function cssAspect(a: string): string {
  const [w, h] = a.split(":");
  return w && h ? `${w} / ${h}` : "1 / 1";
}

/** Expected seconds PER IMAGE, used only to pace the estimate — no image API
 *  reports true intra-image progress.
 *
 *  These are MEASURED, not guessed, because the previous values were wrong in a
 *  way that showed: they keyed off `model.includes("pro")`, which matched
 *  `dola-seedream-5-0-pro` and paced it at 30s — so the bar sat at 88% for the
 *  remaining ~55s of an 85s generation and looked stuck.
 *
 *    dola-seedream-5-0-pro (Avis)  1K: 85s measured
 *    gemini-3.1-flash-image        1K: 16s, 4K: 45s measured
 *    gemini-2.5-flash-image        1K: 11s measured (it ignores imageSize)
 *
 *  Seedream goes through an async job queue rather than a synchronous call,
 *  which is most of why it is ~5x the flash models — so it gets its own base
 *  rather than sharing one keyed on the word "pro".
 */
function expectedSecondsPerImage(model: string, size: string): number {
  const seedream = model.includes("seedream");
  const base = seedream ? 85 : model.includes("3-pro") ? 22 : 14;
  // Seedream caps at 2K and prices 2K at exactly 2x 1K; the flash models climb
  // steeply into real 4K (18.8 MB per image, most of it download).
  const mult = seedream
    ? size === "2K" || size === "4K"
      ? 1.8
      : 1
    : size === "4K"
      ? 3.2
      : size === "2K"
        ? 1.6
        : 1;
  return base * mult;
}

/** Re-render every 300ms while generating, so percentages computed from a
 *  timestamp stay live. Returns nothing — it exists purely to tick. */
function useTick(active: boolean): void {
  const [, force] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => force((n) => n + 1), 300);
    return () => clearInterval(id);
  }, [active]);
}

/** This tile's climbing %, from ITS OWN start time.
 *
 *  Two bugs made the old version misreport. It held the percentages in state
 *  behind an effect keyed on the tile COUNT, so when one of four images landed
 *  the count changed, the effect re-ran, its `start` was re-stamped and every
 *  remaining tile dropped back to 0. And it indexed percentages by array
 *  POSITION, which renumbers when a finished job is removed — so a tile could
 *  inherit a different tile's number.
 *
 *  Now it is a pure function of (this job's startedAt, now): nothing to reset,
 *  no shared clock, and a finished sibling cannot touch it. Eases 0→88 over the
 *  expected time, then crawls 88→99 if it overruns; a job the backend has marked
 *  done reads a true 100.
 */
function tilePct(job: GenJob | null, expS: number): number {
  if (!job) return 0;
  if (job.done >= job.total) return 100;
  const e = (Date.now() - job.startedAt) / 1000;
  const linear = Math.min((e / expS) * 88, 88);
  const tail = e <= expS ? 0 : (1 - Math.exp(-(e - expS) / (expS * 1.5))) * 11;
  return Math.min(99, Math.round(linear + tail));
}

/** Daily usage badge in the rail. Counts images this tool generated today
 *  (≈ Atrium usage, since this key is tool-only) and an estimated remaining
 *  quota. Refetches whenever a generation lands (assets count changes). */
function fmtCountdown(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${h}:${pad(m)}:${pad(sec)}`;
}

function FlowUsageBadge({ collapsed }: { collapsed: boolean }) {
  const assetCount = useFlowStudioStore((s) => s.assets.length);
  const generating = useFlowStudioStore((s) => s.generating);
  const [usage, setUsage] = useState<FlowUsage | null>(null);
  const [secsLeft, setSecsLeft] = useState<number | null>(null);
  useEffect(() => {
    let live = true;
    getFlowUsage()
      .then((u) => live && setUsage(u))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [assetCount, generating]);
  // Tick the reset countdown down locally every second (no refetch needed).
  useEffect(() => {
    if (usage?.seconds_until_reset == null) {
      setSecsLeft(null);
      return;
    }
    setSecsLeft(usage.seconds_until_reset);
    const id = setInterval(
      () => setSecsLeft((s) => (s == null ? s : Math.max(0, s - 1))),
      1000,
    );
    return () => clearInterval(id);
  }, [usage?.seconds_until_reset]);
  if (!usage) return null;
  const resetTitle = usage.resets_at ? `Limit resets at ${new Date(usage.resets_at).toLocaleString()}` : undefined;
  const g = usage.engines?.gemini;
  const s = usage.engines?.seedream;
  const usd = (n: number) => `$${n.toFixed(2)}`;
  const gToday = g ? g.today : usage.today;
  const gQuota = g ? g.daily_quota : usage.daily_quota;
  const gRemaining = g ? g.remaining_est : usage.remaining_est;
  const gPct = Math.min(100, Math.round((gToday / Math.max(1, gQuota)) * 100));
  if (collapsed) {
    const title = [
      `Gemini: ${gToday}/${gQuota}`,
      s ? `Seedream: ${usd(s.cost_total)} (${s.total} imgs)` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    return (
      <div className="fn__usage fn__usage--mini" title={title}>
        {usage.today}
      </div>
    );
  }
  return (
    <>
      {/* Gemini / Atrium — daily quota, in its own box */}
      <div className="fn__usage" title={`Gemini: ~${gRemaining} images remaining today (est.)`}>
        <div className="fn__usage-row">
          <span>Gemini</span>
          <span>
            {gToday}/{gQuota}
          </span>
        </div>
        <div className="fn__usage-bar">
          <div className="fn__usage-fill" style={{ width: `${gPct}%` }} />
        </div>
        <div className="fn__usage-sub">~{gRemaining} images remaining today (est.)</div>
        {secsLeft != null && (
          <div className="fn__usage-reset" title={resetTitle}>
            <span>↻ Resets in</span>
            <span className="fn__usage-reset-time">{secsLeft > 0 ? fmtCountdown(secsLeft) : "now…"}</span>
          </div>
        )}
      </div>
      {/* Seedream — pay-per-use, no daily reset: a running lifetime total */}
      {s && (
        <div
          className="fn__usage"
          style={{ marginTop: 8 }}
          title={`Seedream: ${s.total} images total @ ${usd(s.usd_per_image)}/image`}
        >
          <div className="fn__usage-row">
            <span>Seedream</span>
            <span>{usd(s.cost_total)}</span>
          </div>
          <div className="fn__usage-sub">{s.total} imgs total</div>
        </div>
      )}
    </>
  );
}

/** Flow-style loading tiles — gradient frame in the chosen aspect ratio, image
 *  icon top-left, own climbing % top-right. */
function GenPlaceholders() {
  // Aggregate every in-flight generation into one set of tiles (several can run
  // at once now), so each pending image shows its own climbing %.
  const jobs = useFlowStudioStore((s) => s.genJobs);
  const aspect = useFlowStudioStore((s) => s.settings.aspect);
  const model = useFlowStudioStore((s) => s.settings.model);
  const size = useFlowStudioStore((s) => s.settings.size);
  const total = Math.max(1, jobs.reduce((n, j) => n + j.total, 0));
  const expS = expectedSecondsPerImage(model, size);
  useTick(jobs.length > 0);
  const reusePrompt = useFlowStudioStore((s) => s.reusePrompt);
  // One tile per expected image, tagged with the job it belongs to so each can
  // reuse that job's EXACT prompt + material even while it's still generating.
  const tiles: { job: GenJob | null; i: number }[] = [];
  let n = 0;
  for (const job of jobs) for (let k = 0; k < job.total; k++) tiles.push({ job, i: n++ });
  while (tiles.length < total) tiles.push({ job: null, i: n++ });
  return (
    <>
      {tiles.map(({ job, i }) => (
        <div key={`gen-${job?.id ?? i}`} className="fc-card fc-card--loading" style={{ aspectRatio: cssAspect(aspect) }}>
          <span className="fc-load__icon" aria-hidden="true">🖼</span>
          <span className="fc-load__pct">{tilePct(job, expS)}%</span>
          {job?.prompt && (
            <button
              type="button"
              className="fc-card__act fc-load__reuse"
              title="Reuse this exact prompt + material"
              onClick={(e) => {
                e.stopPropagation();
                reusePrompt(job.prompt, job.refs);
              }}
            >
              ↩
            </button>
          )}
        </div>
      ))}
    </>
  );
}

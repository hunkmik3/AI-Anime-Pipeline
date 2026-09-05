import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { mediaUrl } from "../../../api/client";

/**
 * Per-node generation history — every attempt ever run on this node, newest
 * first: when, what it cost, whether it failed, and the clip it produced.
 *
 * Deliberately includes failed and in-flight takes: "why did that one break?"
 * is the main reason to open this. A missing cost means the attempt was never
 * billed (failed → refunded, or still running), which is different from $0.
 */

export interface HistoryRow {
  request_id: number;
  status: string; // queued | running | done | failed
  error: string | null;
  created_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  model: string | null;
  resolution: string | null;
  duration_seconds: number | null;
  prompt: string | null;
  cost_usd: number | null;
  ledger_status: string | null;
  user_name: string | null;
  media_ids: string[];
  kept: boolean;
  // Audio (Seed Audio) takes — render an <audio> element + these settings.
  kind?: "video" | "audio";
  audio_format?: string | null;
  sample_rate?: number | null;
  speech_rate?: number | null;
  loudness_rate?: number | null;
  pitch_rate?: number | null;
  // Material used (voice refs / image ref) — so a take can be "reused" into a
  // fresh node with the same prompt + settings + material.
  references?: string[];
  image_ref?: string | null;
}

const usd = (v: number | null) => (v != null ? `$${v.toFixed(2)}` : "—");

/** Save one clip. mediaUrl is same-origin, so a plain download anchor works. */
function downloadClip(
  mediaId: string,
  base: string,
  i: number,
  total: number,
  ext = "mp4",
) {
  const a = document.createElement("a");
  a.href = mediaUrl(mediaId);
  const suffix = total > 1 ? `-${i + 1}` : "";
  a.download = `${(base || "clip").replace(/[^A-Za-z0-9_-]+/g, "_")}${suffix}.${ext}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Human-readable audio settings line (format · rate · speed/vol/pitch). */
function audioSettings(r: HistoryRow): string {
  const parts: string[] = [];
  if (r.audio_format) parts.push(String(r.audio_format).toUpperCase());
  if (r.sample_rate) parts.push(`${r.sample_rate} Hz`);
  if (r.speech_rate) parts.push(`speed ${r.speech_rate > 0 ? "+" : ""}${r.speech_rate}`);
  if (r.loudness_rate) parts.push(`vol ${r.loudness_rate > 0 ? "+" : ""}${r.loudness_rate}`);
  if (r.pitch_rate) parts.push(`pitch ${r.pitch_rate > 0 ? "+" : ""}${r.pitch_rate}`);
  return parts.join(" · ");
}

function fmtWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

function fmtTook(ms: number | null): string {
  if (!ms || ms < 0) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export function NodeHistoryModal({
  rfId,
  title,
  kind = "video",
  onReuse,
  onClose,
}: {
  rfId: string;
  title?: string;
  kind?: "video" | "audio";
  // When given, each take shows a "Reuse" button that spawns a fresh node with
  // this take's prompt + settings + material.
  onReuse?: (row: HistoryRow) => void;
  onClose: () => void;
}) {
  const isAudio = kind === "audio";
  const dlExt = (r: HistoryRow) => (isAudio ? r.audio_format || "mp3" : "mp4");
  const [rows, setRows] = useState<HistoryRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Which take is open in the big player (its clip ids + a label + download ext).
  const [viewer, setViewer] = useState<{ ids: string[]; label: string; ext: string } | null>(
    null,
  );
  // request_id of the take whose prompt was just copied (shows a ✓ briefly).
  const [copiedId, setCopiedId] = useState<number | null>(null);

  async function copyPrompt(r: HistoryRow) {
    if (!r.prompt) return;
    try {
      await navigator.clipboard.writeText(r.prompt);
    } catch {
      // Fallback for non-secure contexts / older browsers.
      const ta = document.createElement("textarea");
      ta.value = r.prompt;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        /* give up silently */
      }
      ta.remove();
    }
    setCopiedId(r.request_id);
    window.setTimeout(() => setCopiedId((c) => (c === r.request_id ? null : c)), 1500);
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // Esc closes the player first, the whole modal second.
      if (viewer) setViewer(null);
      else onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, viewer]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch(`/api/nodes/${rfId}/history`);
        if (!res.ok) throw new Error(`history failed (${res.status})`);
        const data = await res.json();
        if (alive) setRows(data);
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : "load failed");
      }
    })();
    return () => {
      alive = false;
    };
  }, [rfId]);

  const spent = (rows ?? []).reduce((s, r) => s + (r.cost_usd ?? 0), 0);

  return createPortal(
    <div
      className="cpw-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Generation history"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="nhist">
        <div className="nhist__head">
          <div>
            <h2 className="nhist__title">Generation history</h2>
            <p className="nhist__sub">
              {title ? `${title} · ` : ""}
              {rows === null
                ? "loading…"
                : `${rows.length} attempt${rows.length === 1 ? "" : "s"} · ${usd(spent)} spent`}
            </p>
          </div>
          <button className="nhist__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {err ? <div className="admin-error">{err}</div> : null}

        {rows === null ? (
          <div className="nhist__empty">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="nhist__empty">Nothing generated on this node yet.</div>
        ) : (
          <div className="nhist__list">
            {rows.map((r) => (
              <div
                key={r.request_id}
                className={`nhist__row${r.kept ? " nhist__row--kept" : ""}`}
              >
                {r.media_ids[0] ? (
                  <button
                    className="nhist__thumb nhist__thumb--play"
                    title="Play"
                    onClick={() =>
                      setViewer({ ids: r.media_ids, label: title || "clip", ext: dlExt(r) })
                    }
                  >
                    {isAudio ? (
                      <span className="nhist__thumb-audio" aria-hidden>🔊</span>
                    ) : (
                      <video
                        src={mediaUrl(r.media_ids[0])}
                        preload="metadata"
                        muted
                        onMouseEnter={(e) => void e.currentTarget.play().catch(() => {})}
                        onMouseLeave={(e) => {
                          e.currentTarget.pause();
                          e.currentTarget.currentTime = 0;
                        }}
                      />
                    )}
                    <span className="nhist__play-badge" aria-hidden>▶</span>
                    {r.media_ids.length > 1 ? (
                      <span className="nhist__variant-count">{r.media_ids.length}</span>
                    ) : null}
                  </button>
                ) : (
                  <div className="nhist__thumb">
                    <span className="nhist__thumb-empty" aria-hidden>
                      {r.status === "failed" ? "⚠" : "…"}
                    </span>
                  </div>
                )}

                <div className="nhist__meta">
                  <div className="nhist__line1">
                    <span className={`nhist__status nhist__status--${r.status}`}>
                      {r.status}
                    </span>
                    {r.kept ? <span className="nhist__kept-tag">on the node</span> : null}
                    <span className="nhist__when">{fmtWhen(r.created_at)}</span>
                    {fmtTook(r.duration_ms) ? (
                      <span className="nhist__took">took {fmtTook(r.duration_ms)}</span>
                    ) : null}
                  </div>
                  <div className="nhist__line2">
                    {isAudio ? (
                      <>
                        {audioSettings(r) || "audio"}
                        {r.duration_seconds ? ` · ${r.duration_seconds.toFixed(1)}s` : ""}
                        {r.references && r.references.length
                          ? ` · 🔊 ${r.references.length} ref${r.references.length > 1 ? "s" : ""}`
                          : ""}
                        {r.image_ref ? " · 🖼 image" : ""}
                        {r.user_name ? ` · ${r.user_name}` : ""}
                      </>
                    ) : (
                      <>
                        {r.model ?? "—"}
                        {r.resolution ? ` · ${r.resolution}` : ""}
                        {r.duration_seconds ? ` · ${r.duration_seconds}s` : ""}
                        {r.user_name ? ` · ${r.user_name}` : ""}
                      </>
                    )}
                  </div>
                  {r.error ? <div className="nhist__err">{r.error}</div> : null}
                  {r.prompt ? (
                    <div className="nhist__prompt" title={r.prompt}>
                      {r.prompt}
                    </div>
                  ) : null}
                </div>

                <div className="nhist__right">
                  <div className="nhist__cost">
                    <b>{usd(r.cost_usd)}</b>
                    {/* A blank cost is not $0 — say why nothing was charged. */}
                    {r.cost_usd == null ? (
                      <span className="nhist__cost-note">
                        {r.ledger_status === "reserved" ? "on hold" : "not charged"}
                      </span>
                    ) : null}
                  </div>
                  {r.media_ids.length || onReuse || r.prompt ? (
                    <div className="nhist__actions">
                      {r.prompt ? (
                        <button
                          className="nhist__act"
                          title="Copy this prompt to the clipboard"
                          onClick={() => void copyPrompt(r)}
                        >
                          {copiedId === r.request_id ? "✓ Copied" : "⧉ Copy prompt"}
                        </button>
                      ) : null}
                      {r.media_ids.length ? (
                        <button
                          className="nhist__act"
                          onClick={() =>
                            setViewer({ ids: r.media_ids, label: title || "clip", ext: dlExt(r) })
                          }
                        >
                          View
                        </button>
                      ) : null}
                      {r.media_ids.length ? (
                        <button
                          className="nhist__act"
                          title="Download this take"
                          onClick={() =>
                            r.media_ids.forEach((m, i) =>
                              downloadClip(m, title || "clip", i, r.media_ids.length, dlExt(r)),
                            )
                          }
                        >
                          ⬇ Download
                        </button>
                      ) : null}
                      {onReuse ? (
                        <button
                          className="nhist__act nhist__act--reuse"
                          title="Create a new node with this take's prompt, settings & material"
                          onClick={() => {
                            onReuse(r);
                            onClose();
                          }}
                        >
                          ⧉ Reuse
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Full-size player for a chosen take — correct aspect, controls, download.
          Every clip renders (4:3 / 16:9 / 9:16 all fit via object-fit: contain). */}
      {viewer ? (
        <div
          className="nhist-viewer"
          role="dialog"
          aria-modal="true"
          aria-label="Play generation"
          onClick={(e) => {
            if (e.target === e.currentTarget) setViewer(null);
          }}
        >
          <button className="nhist-viewer__close" onClick={() => setViewer(null)} aria-label="Close">
            ×
          </button>
          <div className="nhist-viewer__stage">
            {viewer.ids.map((m, i) => (
              <div key={m} className="nhist-viewer__item">
                {isAudio ? (
                  <audio
                    src={mediaUrl(m)}
                    controls
                    autoPlay={i === 0}
                    className="nhist-viewer__audio"
                  />
                ) : (
                  <video
                    src={mediaUrl(m)}
                    controls
                    autoPlay={i === 0}
                    playsInline
                    className="nhist-viewer__video"
                  />
                )}
                <button
                  className="nhist__act nhist-viewer__dl"
                  onClick={() => downloadClip(m, viewer.label, i, viewer.ids.length, viewer.ext)}
                >
                  ⬇ Download{viewer.ids.length > 1 ? ` #${i + 1}` : ""}
                </button>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>,
    document.body,
  );
}

/** Small header button that opens the history for a node. */
export function NodeHistoryButton({
  rfId,
  title,
  kind = "video",
  onReuse,
}: {
  rfId: string;
  title?: string;
  kind?: "video" | "audio";
  onReuse?: (row: HistoryRow) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className="node-header__btn node-header__btn--history"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
        aria-label="Generation history"
        title="Generation history"
        tabIndex={0}
      >
        ⏱
      </button>
      {open ? (
        <NodeHistoryModal
          rfId={rfId}
          title={title}
          kind={kind}
          onReuse={onReuse}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

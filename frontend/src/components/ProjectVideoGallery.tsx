import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { api, mediaUrl, thumbUrl } from "../api/client";

/**
 * Project-level "what have we generated" gallery. Lists every video clip made
 * in the project, grouped Episode → Sequence, newest take first. Click a card
 * to play the clip full-size and read its prompt + settings.
 * Data: GET /api/projects/:id/video-gens (see stats_service.project_video_gens).
 */

interface Gen {
  request_id: number;
  node_id: number | null;
  node_title: string;
  created_at: string | null;
  status: string;
  media_ids: string[];
  references: { media_id: string; label: string | null }[];
  prompt: string | null;
  model: string | null;
  resolution: string | null;
  aspect_ratio: string | null;
  duration_seconds: number | null;
  cost_usd: number | null;
}
interface Seq {
  shot_id: string;
  shot_label: string;
  gens: Gen[];
}
interface Ep {
  scene_id: string;
  name: string;
  sequences: Seq[];
}
interface GalleryData {
  total: number;
  episodes: Ep[];
}

/** Card thumbnail. Uses the cheap server-side first-frame webp (a plain <img>)
 *  instead of mounting a <video> per card — a project with dozens of clips
 *  would otherwise spin up dozens of video decoders and lag. Falls back to a
 *  hover-to-play <video> only if the thumbnail can't be produced. */
function ClipThumb({ mediaId }: { mediaId: string }) {
  const [useVideo, setUseVideo] = useState(false);
  if (useVideo) {
    return (
      <video
        src={mediaUrl(mediaId)}
        muted
        preload="metadata"
        playsInline
        onMouseEnter={(e) => void e.currentTarget.play().catch(() => {})}
        onMouseLeave={(e) => {
          e.currentTarget.pause();
          e.currentTarget.currentTime = 0;
        }}
      />
    );
  }
  return (
    <img
      src={thumbUrl(mediaId, 320)}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setUseVideo(true)}
    />
  );
}

function Badges({ gen, model }: { gen: Gen; model?: boolean }) {
  return (
    <span className="pvg__badges">
      {model && gen.model ? <span className="pvg__badge">{gen.model}</span> : null}
      {gen.resolution ? <span className="pvg__badge">{gen.resolution}</span> : null}
      {gen.duration_seconds ? <span className="pvg__badge">{gen.duration_seconds}s</span> : null}
      {gen.aspect_ratio ? <span className="pvg__badge">{gen.aspect_ratio}</span> : null}
      {model && gen.cost_usd != null ? (
        <span className="pvg__badge pvg__badge--cost">${gen.cost_usd.toFixed(2)}</span>
      ) : null}
    </span>
  );
}

function GenViewer({ gen, onClose }: { gen: Gen; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div
      className="pvg-viewer"
      role="dialog"
      aria-modal="true"
      aria-label="Generated clip"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="pvg-viewer__box">
        <button className="pvg-viewer__close" onClick={onClose} aria-label="Close">
          ×
        </button>
        <div className="pvg-viewer__players">
          {gen.media_ids.map((m, i) => (
            <video
              key={m}
              src={mediaUrl(m)}
              controls
              autoPlay={i === 0}
              playsInline
              className="pvg-viewer__video"
            />
          ))}
        </div>
        <div className="pvg-viewer__info">
          <div className="pvg-viewer__head">
            <b>{gen.node_title}</b>
            <Badges gen={gen} model />
          </div>
          {gen.created_at ? (
            <div className="pvg-viewer__when">{new Date(gen.created_at).toLocaleString()}</div>
          ) : null}
          {gen.prompt ? (
            <div className="pvg-viewer__prompt">
              <div className="pvg-viewer__prompt-label">Prompt</div>
              <pre>{gen.prompt}</pre>
            </div>
          ) : (
            <div className="pvg-viewer__when">No prompt recorded.</div>
          )}
          {gen.references.length > 0 ? (
            <div className="pvg-viewer__refs">
              <div className="pvg-viewer__prompt-label">
                Reference images ({gen.references.length})
              </div>
              <div className="pvg-viewer__refs-grid">
                {gen.references.map((r) => (
                  <a
                    key={r.media_id}
                    className="pvg-viewer__ref"
                    href={mediaUrl(r.media_id)}
                    target="_blank"
                    rel="noreferrer"
                    title={r.label ?? "reference"}
                  >
                    <img src={thumbUrl(r.media_id, 240)} alt={r.label ?? ""} loading="lazy" />
                    {r.label ? <span className="pvg-viewer__ref-label">{r.label}</span> : null}
                  </a>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function ProjectVideoGallery({
  projectId,
  sceneIds = null,
}: {
  projectId: string;
  /** When set, show only clips from these episodes — i.e. the selected series.
   *  Null = the whole project (the default). */
  sceneIds?: Set<string> | null;
}) {
  const [data, setData] = useState<GalleryData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [viewer, setViewer] = useState<Gen | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setErr(null);
    api<GalleryData>(`/api/projects/${projectId}/video-gens`)
      .then((d) => {
        if (alive) setData(d);
      })
      .catch((e) => {
        if (alive) setErr(e instanceof Error ? e.message : "load failed");
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  // Scope to the selected series when one is set: keep only its episodes and
  // recount, so the header total matches what's shown.
  const episodes =
    data && sceneIds ? data.episodes.filter((ep) => sceneIds.has(ep.scene_id)) : data?.episodes ?? [];
  const total =
    data && sceneIds
      ? episodes.reduce((n, ep) => n + ep.sequences.reduce((m, sq) => m + sq.gens.length, 0), 0)
      : data?.total ?? 0;

  return (
    <section className="pvg">
      <header className="dashboard-section__header">
        <h2>Generated videos{data ? ` · ${total}` : ""}</h2>
        <p className="dashboard-section__hint">
          Every clip generated in {sceneIds ? "this series" : "this project"}, by episode &amp;
          sequence. Tap a clip to play it and see its prompt &amp; settings.
        </p>
      </header>

      {err ? (
        <div className="page-empty">Couldn't load the gallery: {err}</div>
      ) : data === null ? (
        <div className="page-empty">Loading…</div>
      ) : total === 0 ? (
        <div className="page-empty">
          No videos generated in {sceneIds ? "this series" : "this project"} yet.
        </div>
      ) : (
        <div className="pvg__episodes">
          {episodes.map((ep) => (
            <div key={ep.scene_id} className="pvg__ep">
              <h3 className="pvg__ep-title">{ep.name}</h3>
              {ep.sequences.map((seq) => (
                <div key={seq.shot_id} className="pvg__seq">
                  <div className="pvg__seq-title">
                    {seq.shot_label}
                    <span className="pvg__count">{seq.gens.length}</span>
                  </div>
                  <div className="pvg__grid">
                    {seq.gens.map((g) => (
                      <div
                        key={g.request_id}
                        className="pvg__card"
                        role="button"
                        tabIndex={0}
                        onClick={() => setViewer(g)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            setViewer(g);
                          }
                        }}
                        title={g.prompt ?? g.node_title}
                      >
                        <span className="pvg__thumb">
                          {g.media_ids[0] ? (
                            <ClipThumb mediaId={g.media_ids[0]} />
                          ) : null}
                          <span className="pvg__play" aria-hidden>
                            ▶
                          </span>
                          {g.media_ids.length > 1 ? (
                            <span className="pvg__variants">{g.media_ids.length}</span>
                          ) : null}
                          {g.references.length > 0 ? (
                            <span
                              className="pvg__refs-badge"
                              title={`${g.references.length} reference image(s)`}
                            >
                              🖼 {g.references.length}
                            </span>
                          ) : null}
                          {g.media_ids[0] ? (
                            <a
                              className="pvg__dl"
                              href={mediaUrl(g.media_ids[0])}
                              download={`${(g.node_title || "clip").replace(/[^\w.-]+/g, "_")}.mp4`}
                              onClick={(e) => e.stopPropagation()}
                              title="Download clip"
                              aria-label="Download clip"
                            >
                              <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
                                   stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                                   strokeLinejoin="round" aria-hidden>
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                                <polyline points="7 10 12 15 17 10" />
                                <line x1="12" y1="15" x2="12" y2="3" />
                              </svg>
                            </a>
                          ) : null}
                        </span>
                        <span className="pvg__meta">
                          <b className="pvg__title">{g.node_title}</b>
                          <Badges gen={g} />
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      {viewer ? <GenViewer gen={viewer} onClose={() => setViewer(null)} /> : null}
    </section>
  );
}

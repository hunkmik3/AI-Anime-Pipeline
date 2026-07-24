import { useEffect, useRef, useState } from "react";
import { mediaUrl, thumbUrl } from "../../../api/client";

const MAX_VIDEO_RETRIES = 5;

export function VideoTile({
  mediaId,
  posterMediaId,
  aspectRatio,
  isProcessing,
  isError,
  slotError,
  alt,
  onClick,
}: {
  mediaId: string | undefined;
  posterMediaId?: string | undefined;
  /** Clip aspect ratio ("9:16", "16:9", "4:3", "1:1"…) so the tile shows the
   *  full frame at its true shape instead of cropping to a fixed 16:9 box. */
  aspectRatio?: string | undefined;
  isProcessing: boolean;
  isError: boolean;
  slotError?: string | null;
  alt: string;
  onClick?: () => void;
}) {
  // "9:16" → "9 / 16" for the CSS aspect-ratio; fall back to 16:9.
  const tileStyle = {
    aspectRatio: aspectRatio ? aspectRatio.replace(":", " / ") : undefined,
  };
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState(false);
  // Prefer a cheap server-side still (webp first frame) over mounting a live
  // <video> per tile — dozens of <video preload="metadata"> elements are what
  // make canvases with many sequences/videos lag. Only fall back to a real
  // <video> if that thumbnail can't be produced.
  const [videoFallback, setVideoFallback] = useState(false);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setLoaded(false);
    setAttempt(0);
    setVideoFallback(false);
    return () => {
      if (retryTimerRef.current !== null) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
    };
  }, [mediaId]);

  const blockedTitle = slotError
    ? `Variant blocked: ${slotError} — click for details`
    : undefined;

  const placeholder = (
    <div
      className={`video-placeholder${isProcessing ? " video-placeholder--processing" : ""}${isError ? " video-placeholder--error" : ""}${slotError ? " video-placeholder--blocked" : ""}`}
      aria-hidden="true"
      title={blockedTitle}
    >
      {slotError ? (
        <>
          <span className="video-blocked-icon">⚠</span>
          <span className="video-blocked-label">Blocked</span>
        </>
      ) : (
        <>
          <span className="video-play">▶</span>
          <span className="video-duration">0:00</span>
        </>
      )}
    </div>
  );

  if (!mediaId) {
    const cls = `video-tile${slotError ? " video-tile--blocked" : ""}${onClick ? " video-tile--clickable" : ""}`;
    return (
      <div
        className={cls}
        style={tileStyle}
        role={onClick ? "button" : undefined}
        tabIndex={onClick ? 0 : undefined}
        aria-label={blockedTitle ?? (onClick ? `Open variant ${alt}` : undefined)}
        title={blockedTitle}
        onClick={onClick}
        onKeyDown={(e) => {
          if (!onClick) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onClick();
          }
        }}
      >
        {placeholder}
      </div>
    );
  }

  const givenUp = attempt >= MAX_VIDEO_RETRIES;
  const src = attempt > 0 ? `${mediaUrl(mediaId)}?retry=${attempt}` : mediaUrl(mediaId);
  const cls =
    `video-tile video-tile--filled` +
    (onClick ? " video-tile--clickable" : "");

  return (
    <div
      className={cls}
      style={tileStyle}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      aria-label={onClick ? `Open variant ${alt}` : undefined}
      onClick={onClick}
      onKeyDown={(e) => {
        if (!onClick) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
    >
      {!loaded && placeholder}
      {!givenUp && posterMediaId ? (
        <img
          key={`poster-${attempt}`}
          className="video-tile__poster"
          src={mediaUrl(posterMediaId)}
          alt={alt}
          onLoad={() => setLoaded(true)}
          onError={() => {
            retryTimerRef.current = setTimeout(() => {
              setAttempt((a) => a + 1);
            }, 2000);
          }}
        />
      ) : !givenUp && !videoFallback ? (
        // Phase 10: for r2v/custom-ref videos (no poster) show the server's
        // first-frame webp thumbnail — a plain <img> is far cheaper than a
        // live <video> element, so a canvas with many clips no longer mounts
        // dozens of decoders. If the thumb can't be produced (older clip,
        // ffmpeg miss → route serves the raw video → img errors) we fall back
        // to the original <video> thumbnail behaviour below.
        <img
          className="video-tile__poster"
          src={thumbUrl(mediaId)}
          alt={alt}
          loading="lazy"
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => setVideoFallback(true)}
        />
      ) : !givenUp ? (
        // Phase 8.3b fallback: rely on the <video> element itself to render
        // its first frame. `preload="metadata"` is required — without it the
        // browser fetches nothing and the user sees an empty player forever
        // (placeholder ▶/0:00 never gets replaced because onLoadedData never
        // fires). `loadedmetadata` fires as soon as the first frame is known.
        <video
          key={attempt}
          className="node-card__thumbnail"
          data-kind="video"
          src={src}
          preload="metadata"
          muted
          playsInline
          aria-label={alt}
          style={loaded ? undefined : { display: "none" }}
          onLoadedMetadata={() => setLoaded(true)}
          onLoadedData={() => setLoaded(true)}
          onError={() => {
            retryTimerRef.current = setTimeout(() => {
              setAttempt((a) => a + 1);
            }, 2000);
          }}
        />
      ) : null}
      {/* Play affordance — shown when there's either a poster image OR the
          video's first frame has rendered (loaded). Tells the user the result
          is ready & clickable, fixing the "looks empty after gen" issue. */}
      {(posterMediaId || loaded) && (
        <span className="video-tile__play-badge" aria-hidden="true">▶</span>
      )}
    </div>
  );
}

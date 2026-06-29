import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { extractFrame, mediaUrl } from "../../../api/client";
import { useGenerationStore } from "../../../store/generation";
import { useShotWorkflowStore } from "../../../store/shotWorkflow";

/**
 * Phase 8.4 — frame extraction UI on a finished Video node.
 *
 * Inline: a large thumbnail button. Click → a full frame-picker modal
 * (portal'd to <body> so the React Flow canvas transform can't clip/scale it)
 * with a big preview + precise scrubbing, then "Extract this frame" cuts the
 * still (POST /api/media/{id}/extract-frame) into a new Visual asset node.
 */
// Extract-frame is temporarily hidden. Flip to true to restore the full
// scrub-and-extract UI (the modal code below stays wired).
const EXTRACT_FRAME_ENABLED = false;

export function VideoScrubber({
  videoRfId,
  mediaId,
  shotId,
}: {
  videoRfId: string;
  mediaId: string;
  shotId?: string;
}) {
  const [open, setOpen] = useState(false);

  // Hidden: render a plain inline video preview (no extract affordance).
  if (!EXTRACT_FRAME_ENABLED) {
    return (
      <div className="video-scrubber nodrag" onClick={(e) => e.stopPropagation()}>
        <video
          className="video-scrubber__thumb"
          src={mediaUrl(mediaId)}
          controls
          preload="metadata"
          playsInline
        />
      </div>
    );
  }

  return (
    <div className="video-scrubber nodrag" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        className="video-scrubber__open"
        onClick={() => setOpen(true)}
        title="Scrub the video and extract a frame for continuity"
      >
        <video
          className="video-scrubber__thumb"
          src={mediaUrl(mediaId)}
          preload="metadata"
          muted
          playsInline
        />
        <span className="video-scrubber__open-label">⤢ Extract frame…</span>
      </button>
      {open &&
        createPortal(
          <FrameScrubberModal
            videoRfId={videoRfId}
            mediaId={mediaId}
            shotId={shotId}
            onClose={() => setOpen(false)}
          />,
          document.body,
        )}
    </div>
  );
}

const FPS_STEP = 1 / 24; // ~one frame at 24fps for the ◀/▶ nudge buttons

function FrameScrubberModal({
  videoRfId,
  mediaId,
  shotId,
  onClose,
}: {
  videoRfId: string;
  mediaId: string;
  shotId?: string;
  onClose: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [duration, setDuration] = useState(0);
  const [time, setTime] = useState(0);
  const [ready, setReady] = useState(false);
  const [extracting, setExtracting] = useState(false);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  function onLoadedMetadata() {
    const d = videoRef.current?.duration ?? 0;
    if (!isFinite(d) || d <= 0) return;
    setDuration(d);
    const last = Math.max(0, d - 0.05); // last frame = strongest continuity
    seekTo(last);
    setReady(true);
  }

  function seekTo(v: number) {
    const clamped = Math.max(0, Math.min(v, duration || v));
    setTime(clamped);
    if (videoRef.current) videoRef.current.currentTime = clamped;
  }

  async function onExtract() {
    if (!ready || extracting) return;
    setExtracting(true);
    try {
      const res = await extractFrame(mediaId, { time, shotId });
      const id = await useShotWorkflowStore
        .getState()
        .addExtractedFrameNode(videoRfId, res.media_id, res.time);
      if (id) {
        useGenerationStore
          .getState()
          .setNotice(`Frame extracted at ${res.time.toFixed(1)}s as a Visual asset`);
        onClose();
      } else {
        useGenerationStore.setState({ error: "Couldn't place the extracted frame" });
      }
    } catch (err) {
      useGenerationStore.setState({
        error: err instanceof Error ? err.message : "Frame extraction failed",
      });
    } finally {
      setExtracting(false);
    }
  }

  return (
    <div className="frame-modal-backdrop" onClick={onClose}>
      <div
        className="frame-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Extract frame"
        onClick={(e) => e.stopPropagation()}
      >
        <button className="frame-modal__close" onClick={onClose} aria-label="Close">
          ×
        </button>
        <div className="frame-modal__stage">
          <video
            ref={videoRef}
            className="frame-modal__video"
            src={mediaUrl(mediaId)}
            preload="auto"
            muted
            playsInline
            onLoadedMetadata={onLoadedMetadata}
          />
        </div>
        <div className="frame-modal__controls">
          <button
            type="button"
            className="frame-modal__step"
            onClick={() => seekTo(time - 1)}
            disabled={!ready}
            title="Back 1s"
          >
            ⏮
          </button>
          <button
            type="button"
            className="frame-modal__step"
            onClick={() => seekTo(time - FPS_STEP)}
            disabled={!ready}
            title="Previous frame"
          >
            ◀
          </button>
          <input
            type="range"
            className="frame-modal__range"
            min={0}
            max={duration || 0}
            step={0.01}
            value={time}
            disabled={!ready}
            onChange={(e) => seekTo(parseFloat(e.target.value))}
            aria-label="Scrub to a frame"
          />
          <button
            type="button"
            className="frame-modal__step"
            onClick={() => seekTo(time + FPS_STEP)}
            disabled={!ready}
            title="Next frame"
          >
            ▶
          </button>
          <button
            type="button"
            className="frame-modal__step"
            onClick={() => seekTo(time + 1)}
            disabled={!ready}
            title="Forward 1s"
          >
            ⏭
          </button>
          <span className="frame-modal__time">
            {time.toFixed(2)}s / {duration.toFixed(2)}s
          </span>
        </div>
        <div className="frame-modal__actions">
          <button
            type="button"
            className="frame-modal__extract"
            onClick={onExtract}
            disabled={!ready || extracting}
          >
            {extracting ? "Extracting…" : "⤿ Extract this frame"}
          </button>
          <button type="button" className="frame-modal__cancel" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

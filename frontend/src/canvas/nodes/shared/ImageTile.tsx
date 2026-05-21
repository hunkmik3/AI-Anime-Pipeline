import { useEffect, useRef, useState } from "react";
import { mediaUrl } from "../../../api/client";

const MAX_IMG_RETRIES = 5;

export function ImageTile({
  rfId,
  mediaId,
  isProcessing,
  alt,
  onClick,
  onUseAsRef,
  onSaveToLibrary,
}: {
  rfId: string;
  mediaId: string | undefined;
  isProcessing: boolean;
  alt: string;
  onClick?: () => void;
  onUseAsRef?: () => void;
  onSaveToLibrary?: () => void;
}) {
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setLoaded(false);
    setAttempt(0);
    return () => {
      if (retryTimerRef.current !== null) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
    };
  }, [mediaId, rfId]);

  if (!mediaId) {
    return (
      <div
        className={`thumbnail-tile${isProcessing ? " thumbnail-tile--processing" : ""}`}
        aria-hidden="true"
      >
        <span className="thumbnail-tile__icon">▣</span>
      </div>
    );
  }

  const givenUp = attempt >= MAX_IMG_RETRIES;
  const src = attempt > 0 ? `${mediaUrl(mediaId)}?retry=${attempt}` : mediaUrl(mediaId);
  const cls =
    `thumbnail-tile thumbnail-tile--filled` +
    (onClick ? " thumbnail-tile--clickable" : "");

  return (
    <div
      className={cls}
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
      {!loaded && (
        <div className="thumbnail-tile__placeholder" aria-hidden="true" />
      )}
      {!givenUp && (
        <img
          key={attempt}
          className="thumbnail-tile__img"
          src={src}
          alt={alt}
          style={loaded ? undefined : { display: "none" }}
          onLoad={() => setLoaded(true)}
          onError={() => {
            retryTimerRef.current = setTimeout(() => {
              setAttempt((a) => a + 1);
            }, 2000);
          }}
        />
      )}
      {onUseAsRef && (
        <button
          type="button"
          className="thumbnail-tile__use-btn"
          onClick={(e) => {
            e.stopPropagation();
            onUseAsRef();
          }}
          title="Use this variant as the reference for a downstream node"
          aria-label="Use this variant as reference"
        >
          Use →
        </button>
      )}
      {onSaveToLibrary && (
        <button
          type="button"
          className="thumbnail-tile__save-btn"
          onClick={(e) => {
            e.stopPropagation();
            onSaveToLibrary();
          }}
          title="Save this variant to the library"
          aria-label="Save to library"
        >
          ★
        </button>
      )}
    </div>
  );
}

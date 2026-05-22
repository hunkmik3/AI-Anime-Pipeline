import { useState } from "react";

import { parseScript, type ParsedShot } from "../api/client";
import { useShotStore } from "../store/shot";

interface Props {
  sceneId: string;
  onClose(): void;
}

type Stage = "input" | "review";

/**
 * Phase 6.4. Paste VN-or-any-language script → LLM parses into shot
 * breakdowns (camera angle, characters, environment, beat notes,
 * dialogue) → user reviews → bulk-creates shots with parsed metadata
 * pre-populated on ``script_text``.
 *
 * The endpoint preserves ``script_text`` verbatim in the source
 * language; meta fields come back in English so downstream prompt
 * synthesis stays consistent.
 */
export function ScriptInputDialog({ sceneId, onClose }: Props) {
  const createShot = useShotStore((s) => s.createShot);
  const [text, setText] = useState("");
  const [parsing, setParsing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stage, setStage] = useState<Stage>("input");
  const [shots, setShots] = useState<ParsedShot[]>([]);

  async function handleParse() {
    if (parsing || !text.trim()) return;
    setError(null);
    setParsing(true);
    try {
      const res = await parseScript(sceneId, text);
      if (res.shots.length === 0) {
        setError("Parser returned no shots — try a longer script.");
        return;
      }
      setShots(res.shots);
      setStage("review");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setParsing(false);
    }
  }

  function buildShotScript(shot: ParsedShot): string {
    // Persist the parsed breakdown alongside the verbatim script line
    // so the ShotEditor surfaces camera / character / environment
    // hints without a second API round trip.
    const lines = [shot.script_text];
    const meta: string[] = [];
    if (shot.camera_angle) meta.push(`Camera: ${shot.camera_angle}`);
    if (shot.characters_in_frame.length > 0) {
      meta.push(`In frame: ${shot.characters_in_frame.join(", ")}`);
    }
    if (shot.environment) meta.push(`Setting: ${shot.environment}`);
    if (shot.dialogue) meta.push(`Dialogue: ${shot.dialogue}`);
    if (shot.beat_notes) meta.push(`Beat: ${shot.beat_notes}`);
    if (meta.length > 0) lines.push("", ...meta);
    return lines.join("\n");
  }

  async function handleBulkCreate() {
    if (creating) return;
    setError(null);
    setCreating(true);
    try {
      for (const shot of shots) {
        await createShot(sceneId, buildShotScript(shot));
      }
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }

  const busy = parsing || creating;

  return (
    <div
      className="project-modal-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        className="project-modal project-modal--wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="script-dialog-title"
      >
        <h2 id="script-dialog-title" className="project-modal__title">
          Script → shots
        </h2>

        {stage === "input" && (
          <>
            <p className="project-modal__hint">
              Paste your Vietnamese (or any-language) scene script. The
              LLM parser breaks it into discrete cinematic shots with
              camera angle, characters in frame, environment, and beat
              notes — review before creating.
            </p>
            <textarea
              className="form-field__textarea"
              rows={14}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={
                "Cảnh 1: An đứng giữa quảng trường, máy quay ngang vai…\n\nMây đi tới, An quay lại…"
              }
              disabled={parsing}
            />
            {error && (
              <div className="page-error" role="alert">
                {error}
              </div>
            )}
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={onClose}
                disabled={parsing}
              >
                Cancel
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--primary"
                onClick={() => void handleParse()}
                disabled={parsing || !text.trim()}
              >
                {parsing ? "Parsing…" : "Parse script"}
              </button>
            </div>
          </>
        )}

        {stage === "review" && (
          <>
            <p className="project-modal__hint">
              {shots.length} shot{shots.length === 1 ? "" : "s"} parsed.
              Review and create — each shot's ``script_text`` will
              include the verbatim line plus the camera / character /
              environment hints below it.
            </p>
            <div
              className="project-modal__scrolllist"
              style={{
                maxHeight: 360,
                overflow: "auto",
                border: "1px solid var(--border, #444)",
                borderRadius: 4,
                padding: 8,
              }}
            >
              {shots.map((shot, idx) => (
                <div
                  key={idx}
                  style={{
                    padding: "8px 0",
                    borderBottom:
                      idx < shots.length - 1
                        ? "1px solid var(--border-subtle, #333)"
                        : "none",
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>
                    Shot {shot.order} · {shot.camera_angle || "(no angle)"}
                  </div>
                  <pre
                    style={{
                      whiteSpace: "pre-wrap",
                      margin: 0,
                      fontSize: "0.9em",
                    }}
                  >
                    {shot.script_text}
                  </pre>
                  <div
                    style={{
                      fontSize: "0.8em",
                      opacity: 0.7,
                      marginTop: 4,
                    }}
                  >
                    {shot.characters_in_frame.length > 0 && (
                      <span>In frame: {shot.characters_in_frame.join(", ")} · </span>
                    )}
                    {shot.environment && <span>Setting: {shot.environment}</span>}
                  </div>
                  {shot.dialogue && (
                    <div style={{ fontSize: "0.8em", opacity: 0.7 }}>
                      Dialogue: {shot.dialogue}
                    </div>
                  )}
                  {shot.beat_notes && (
                    <div style={{ fontSize: "0.8em", opacity: 0.6, fontStyle: "italic" }}>
                      {shot.beat_notes}
                    </div>
                  )}
                </div>
              ))}
            </div>
            {error && (
              <div className="page-error" role="alert">
                {error}
              </div>
            )}
            <div className="project-modal__actions">
              <button
                type="button"
                className="project-modal__btn"
                onClick={() => setStage("input")}
                disabled={busy}
              >
                Back
              </button>
              <button
                type="button"
                className="project-modal__btn project-modal__btn--primary"
                onClick={() => void handleBulkCreate()}
                disabled={busy || shots.length === 0}
              >
                {creating
                  ? "Creating…"
                  : `Create ${shots.length} shot${shots.length === 1 ? "" : "s"}`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

import { useState } from "react";

import { useShotStore } from "../store/shot";

interface Props {
  sceneId: string;
  onClose(): void;
}

/**
 * Phase 3 stub. The full UX (paste VN script → LLM parses into shots
 * with camera / characters / environment hints → user reviews → bulk
 * create) waits for the ``/api/prompt/parse-script`` endpoint that ships
 * in Phase 6.
 *
 * Today, the dialog accepts plain script text and offers a manual
 * fallback: each paragraph (split by blank line) becomes one shot whose
 * ``script_text`` is the paragraph contents. That keeps the workflow
 * useful in the meantime without faking an LLM call.
 */
export function ScriptInputDialog({ sceneId, onClose }: Props) {
  const createShot = useShotStore((s) => s.createShot);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const paragraphs = text
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean);

  async function handleBulkCreate() {
    if (busy) return;
    if (paragraphs.length === 0) {
      setError("Paste a script first — paragraphs separated by blank lines.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      for (const p of paragraphs) {
        await createShot(sceneId, p);
      }
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

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
        <p className="project-modal__hint">
          Paste your Vietnamese (or any-language) script. Each blank-line
          paragraph becomes one shot whose <code>script_text</code> is the
          paragraph. The LLM auto-parser (camera, characters, environment
          breakdown) lands in <strong>Phase 6</strong>; until then this is
          a manual splitter.
        </p>
        <textarea
          className="form-field__textarea"
          rows={14}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={"Cảnh 1: An đứng giữa quảng trường, máy quay ngang vai…\n\nCảnh 2: Mây đi tới. Camera dolly-in lên mặt cô."}
          disabled={busy}
        />
        <p className="project-modal__hint" aria-live="polite">
          {paragraphs.length > 0
            ? `Will create ${paragraphs.length} shot(s).`
            : "Detected 0 paragraphs."}
        </p>
        {error && <div className="page-error" role="alert">{error}</div>}
        <div className="project-modal__actions">
          <button
            type="button"
            className="project-modal__btn"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            type="button"
            className="project-modal__btn project-modal__btn--primary"
            onClick={() => void handleBulkCreate()}
            disabled={busy || paragraphs.length === 0}
          >
            {busy ? "Creating…" : `Create ${paragraphs.length || ""} shot${paragraphs.length === 1 ? "" : "s"}`}
          </button>
        </div>
      </div>
    </div>
  );
}

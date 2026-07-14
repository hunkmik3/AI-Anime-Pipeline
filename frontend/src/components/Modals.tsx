import { useEffect, useState } from "react";

/** Shared overlay: fixed backdrop, ESC to close, click-outside to close. */
function Backdrop({
  onClose,
  label,
  children,
}: {
  onClose: () => void;
  label: string;
  children: React.ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="cpw-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={label}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      {children}
    </div>
  );
}

/** Confirmation dialog (replaces window.confirm). */
export function ConfirmDialog({
  title,
  message,
  confirmLabel = "Xác nhận",
  danger = false,
  onConfirm,
  onClose,
}: {
  title: string;
  message: React.ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Backdrop onClose={onClose} label={title}>
      <div className="modal-card">
        <h2 className="modal-card__title">{title}</h2>
        <div className="modal-card__msg">{message}</div>
        <div className="modal-card__actions">
          <button className="modal-btn modal-btn--ghost" onClick={onClose}>
            Huỷ
          </button>
          <button
            className={`modal-btn${danger ? " modal-btn--danger" : ""}`}
            onClick={onConfirm}
            autoFocus
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </Backdrop>
  );
}

/** Single-input prompt dialog (replaces window.prompt). */
export function PromptDialog({
  title,
  label,
  type = "text",
  initial = "",
  placeholder,
  submitLabel = "Lưu",
  validate,
  onSubmit,
  onClose,
}: {
  title: string;
  label: string;
  type?: "text" | "password" | "number" | "email";
  initial?: string;
  placeholder?: string;
  submitLabel?: string;
  validate?: (v: string) => string | null; // return an error message or null
  onSubmit: (v: string) => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState(initial);
  const [err, setErr] = useState<string | null>(null);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const v = value.trim();
    const msg = validate?.(v) ?? null;
    if (msg) {
      setErr(msg);
      return;
    }
    onSubmit(v);
  }

  return (
    <Backdrop onClose={onClose} label={title}>
      <form className="modal-card" onSubmit={submit}>
        <h2 className="modal-card__title">{title}</h2>
        <label className="login-field">
          <span>{label}</span>
          <input
            type={type}
            value={value}
            autoFocus
            placeholder={placeholder}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        {err ? <div className="login-error">{err}</div> : null}
        <div className="modal-card__actions">
          <button type="button" className="modal-btn modal-btn--ghost" onClick={onClose}>
            Huỷ
          </button>
          <button type="submit" className="modal-btn">
            {submitLabel}
          </button>
        </div>
      </form>
    </Backdrop>
  );
}

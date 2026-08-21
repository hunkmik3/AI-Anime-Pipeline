import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

export interface EmployeeDetails {
  employee_code: string;
  staff_category: string;
  job_title: string;
  rank: string;
  employment_status: string; // active | resigned | terminated
}

const STAFF_CATEGORIES = ["Full-time", "Freelancer", "Probation", "Intern"];
const EMPLOYMENT = [
  { value: "active", label: "Active" },
  { value: "resigned", label: "Resigned" },
  { value: "terminated", label: "Terminated" },
];

/** "Edit details" modal for an employee — the staff-spreadsheet fields
 *  (code, category, job, rank, employment status). Employment status other
 *  than Active also blocks the account's login (handled server-side). */
export function EmployeeDetailsDialog({
  name,
  initial,
  busy,
  onSubmit,
  onClose,
}: {
  name: string;
  initial: Partial<EmployeeDetails>;
  busy?: boolean;
  onSubmit: (d: EmployeeDetails) => void;
  onClose: () => void;
}) {
  const [code, setCode] = useState(initial.employee_code ?? "");
  const [category, setCategory] = useState(initial.staff_category ?? "");
  const [job, setJob] = useState(initial.job_title ?? "");
  const [rank, setRank] = useState(initial.rank ?? "");
  const [status, setStatus] = useState(initial.employment_status || "active");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    onSubmit({
      employee_code: code.trim(),
      staff_category: category.trim(),
      job_title: job.trim(),
      rank: rank.trim(),
      employment_status: status,
    });
  }

  // Allow a category that isn't one of the presets (legacy value) to still show.
  const categoryOptions =
    category && !STAFF_CATEGORIES.includes(category)
      ? [category, ...STAFF_CATEGORIES]
      : STAFF_CATEGORIES;

  return createPortal(
    <div
      className="cpw-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Edit employee details"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <form className="modal-card" onSubmit={submit}>
        <h2 className="modal-card__title">Edit details — {name}</h2>
        <label className="login-field">
          <span>Employee code</span>
          <input
            value={code}
            autoFocus
            placeholder="e.g. RME001"
            onChange={(e) => setCode(e.target.value)}
          />
        </label>
        <label className="login-field">
          <span>Staff category</span>
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">—</option>
            {categoryOptions.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label className="login-field">
          <span>Job</span>
          <input
            value={job}
            placeholder="e.g. AI Creator, Compositor…"
            onChange={(e) => setJob(e.target.value)}
          />
        </label>
        <label className="login-field">
          <span>Rank</span>
          <input value={rank} onChange={(e) => setRank(e.target.value)} />
        </label>
        <label className="login-field">
          <span>Employment status</span>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            {EMPLOYMENT.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        {status !== "active" ? (
          <p className="modal-card__msg" style={{ color: "#e0a23a" }}>
            Setting this employee to “{status}” will block their login and end any
            active session.
          </p>
        ) : null}
        <div className="modal-card__actions">
          <button type="button" className="modal-btn modal-btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="modal-btn" disabled={busy}>
            {busy ? "Saving…" : "Save details"}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  );
}

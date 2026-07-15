import { useEffect, useRef, useState } from "react";

export interface KebabItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
}

/** Row actions dropdown ("⋯") — replaces a row of cramped inline buttons. */
export function KebabMenu({ items }: { items: KebabItem[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="kebab" ref={ref}>
      <button
        type="button"
        className="kebab__btn"
        aria-haspopup="menu"
        aria-expanded={open}
        title="Actions"
        onClick={() => setOpen((o) => !o)}
      >
        ⋯
      </button>
      {open ? (
        <div className="kebab__menu" role="menu">
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitem"
              className={`kebab__item${it.danger ? " kebab__item--danger" : ""}`}
              onClick={() => {
                setOpen(false);
                it.onSelect();
              }}
            >
              {it.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

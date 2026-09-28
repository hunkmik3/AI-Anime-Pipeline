import { useEffect, useRef, useState } from "react";

export interface KebabItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
}

/** Row actions dropdown ("⋯") — replaces a row of cramped inline buttons.
 *
 * `trigger` swaps the ⋯ for anything else (the header's avatar button, say).
 * The part worth reusing is below it: click-away, Escape, and the focus
 * bookkeeping — writing that a second time for every new dropdown is how two
 * menus end up closing differently. */
export function KebabMenu({
  items,
  trigger,
  align = "left",
  className,
}: {
  items: KebabItem[];
  trigger?: React.ReactNode;
  align?: "left" | "right";
  className?: string;
}) {
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
    <div className={`kebab${className ? ` ${className}` : ""}`} ref={ref}>
      <button
        type="button"
        className={trigger ? "kebab__trigger" : "kebab__btn"}
        aria-haspopup="menu"
        aria-expanded={open}
        title={trigger ? undefined : "Actions"}
        onClick={() => setOpen((o) => !o)}
      >
        {trigger ?? "⋯"}
      </button>
      {open ? (
        <div
          className={`kebab__menu${align === "right" ? " kebab__menu--right" : ""}`}
          role="menu"
        >
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

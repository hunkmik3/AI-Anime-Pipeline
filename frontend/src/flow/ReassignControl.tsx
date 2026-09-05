import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";

import { listPanelAssignees, reassignPanel, type Panel } from "../api/client";
import { toast } from "../store/toast";

/**
 * Hand THIS panel to a different person, or return it to the batch.
 *
 * A per-panel transfer — distinct from assigning the whole batch. It moves one
 * panel into someone else's My-work without disturbing the other panels or
 * rewriting who filed what: every image, note and event the previous person
 * made stays theirs. PM/admin only (callers gate on `batch.manage`; the server
 * rechecks regardless).
 *
 * The menu is PORTALLED to the body and positioned `fixed` off the button, so it
 * is never clipped by the card's `overflow: hidden` — the reason it can live on a
 * tight grid card and in the roomy workspace header from one component.
 */
export function ReassignControl({
  panel,
  onChanged,
  compact = false,
}: {
  panel: Panel;
  onChanged: (p: Panel) => void;
  /** Card mode: an icon-only trigger, so it fits the grid card's button row. */
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [people, setPeople] = useState<{ user_id: string; name: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [pos, setPos] = useState<{
    top: number;
    right: number;
    maxHeight: number;
  } | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (open && people.length === 0) {
      void listPanelAssignees().then(setPeople).catch(() => {});
    }
  }, [open, people.length]);

  // Anchor the portalled menu to the button, right edges aligned. Flip ABOVE the
  // button when there isn't room below (a card at the bottom of the page), and
  // cap the height to the space actually available so the tail is always
  // reachable by scrolling the list rather than falling off-screen.
  useLayoutEffect(() => {
    if (!open || !btnRef.current) return;
    const r = btnRef.current.getBoundingClientRect();
    const margin = 10;
    const right = Math.max(margin, window.innerWidth - r.right);
    const below = window.innerHeight - r.bottom - margin; // room under the button
    const above = r.top - margin; // room over the button
    // Prefer opening downward; flip up only when down is cramped. Either way the
    // height is capped to the room chosen and the top is clamped on-screen, so a
    // card at the very bottom still gets a fully-visible, scrollable menu.
    const useAbove = below < 200 && above > below;
    const maxHeight = Math.max(150, Math.min(360, useAbove ? above : below));
    const top = useAbove
      ? Math.max(margin, r.top - 4 - maxHeight)
      : r.bottom + 4;
    setPos({ top, right, maxHeight });
  }, [open]);

  // Close on outside click (button AND menu both count as inside), or on any
  // scroll/resize — a fixed menu would otherwise detach from its button.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || menuRef.current?.contains(t)) return;
      setOpen(false);
    };
    // Close when the PAGE scrolls (a fixed menu would detach from its button),
    // but NOT when scrolling the menu's own long list — that scroll's target is
    // inside the menu, so let it through.
    const onScroll = (e: Event) => {
      const t = e.target as Node;
      if (menuRef.current && t instanceof Node && menuRef.current.contains(t)) return;
      setOpen(false);
    };
    const onResize = () => setOpen(false);
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
    };
  }, [open]);

  async function pick(userId: string | null) {
    setBusy(true);
    try {
      onChanged(await reassignPanel(panel.id, userId));
      toast(userId ? "Đã chuyển panel." : "Đã trả panel về batch.");
      setOpen(false);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  const stop = (e: ReactMouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  return (
    <div className={`pn__reassign${compact ? " pn__reassign--compact" : ""}`}>
      <button
        ref={btnRef}
        type="button"
        className={compact ? "pn__dl pn__reassign-icon" : "btn2 pn__reassign-btn"}
        disabled={busy}
        aria-label="Chuyển panel cho người khác"
        onClick={(e) => {
          stop(e);
          setOpen((o) => !o);
        }}
        title={
          panel.reassigned
            ? `Đã chuyển riêng cho ${panel.assignee_name ?? ""} — bấm để đổi / trả về batch`
            : "Chuyển panel này cho người khác"
        }
      >
        {compact ? (
          <span className={`pn__reassign-glyph${panel.reassigned ? " is-on" : ""}`}>👤</span>
        ) : (
          <>
            👤 {panel.assignee_name ?? "Chưa giao"}
            {panel.reassigned ? <span className="pn__reassign-tag">đã chuyển</span> : null} ▾
          </>
        )}
      </button>

      {open && pos
        ? createPortal(
            <div
              ref={menuRef}
              className="pn__reassign-menu"
              style={{
                position: "fixed",
                top: pos.top,
                right: pos.right,
                maxHeight: pos.maxHeight,
              }}
              onClick={stop}
            >
              <div className="pn__reassign-head">Chuyển panel cho</div>
              {panel.reassigned ? (
                <button
                  type="button"
                  className="pn__reassign-item is-clear"
                  disabled={busy}
                  onClick={(e) => {
                    stop(e);
                    void pick(null);
                  }}
                >
                  ↩ Trả về batch{panel.batch_assignee_name ? ` (${panel.batch_assignee_name})` : ""}
                </button>
              ) : null}
              {people.length === 0 ? (
                <div className="pn__reassign-empty">Đang tải…</div>
              ) : (
                people.map((p) => (
                  <button
                    key={p.user_id}
                    type="button"
                    className={`pn__reassign-item${
                      p.user_id === panel.assignee_user_id ? " is-on" : ""
                    }`}
                    disabled={busy || p.user_id === panel.assignee_user_id}
                    onClick={(e) => {
                      stop(e);
                      void pick(p.user_id);
                    }}
                  >
                    {p.name}
                    {p.user_id === panel.assignee_user_id ? " ✓" : ""}
                  </button>
                ))
              )}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}

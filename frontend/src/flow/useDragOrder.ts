import { useCallback, useRef, useState } from "react";

/**
 * Drag-to-reorder over a plain list, using native HTML5 drag events.
 *
 * No library: the whole behaviour is "remember what you picked up, reorder the
 * array as you pass over things, save when you let go". A drag framework for that
 * would be more code to read, not less.
 *
 * The order is applied **locally first** and saved afterwards. A drag that only
 * settles after a round-trip feels broken; the save is one small POST, and if it
 * fails the reload is the correction rather than a silent lie.
 *
 * The pending order lives in a **ref**, with state only used to trigger a
 * re-render. Reading it from state was a real bug: `drop` can arrive in the same
 * tick as the last `dragover`, before React has re-rendered, so the closure still
 * held the previous value and the drop committed nothing. A slow human drag
 * re-renders in between and hides it — exactly the kind of timing-dependent
 * behaviour not to rely on.
 */
export function useDragOrder<T extends { id: number }>(
  items: T[],
  onCommit: (ids: number[]) => Promise<unknown>,
) {
  const [, bump] = useState(0);
  const pending = useRef<T[] | null>(null);
  const dragId = useRef<number | null>(null);
  const moved = useRef(false);

  // While dragging, render the working copy; otherwise whatever the server said.
  const list = pending.current ?? items;

  const onDragStart = useCallback(
    (id: number) => {
      dragId.current = id;
      moved.current = false;
      pending.current = [...items];
      bump((n) => n + 1);
    },
    [items],
  );

  const onDragOver = useCallback((overId: number) => {
    const from = dragId.current;
    const arr = pending.current;
    if (from == null || arr == null || from === overId) return;
    const i = arr.findIndex((x) => x.id === from);
    const j = arr.findIndex((x) => x.id === overId);
    if (i < 0 || j < 0 || i === j) return;
    const next = [...arr];
    next.splice(j, 0, next.splice(i, 1)[0]);
    pending.current = next;
    moved.current = true;
    bump((n) => n + 1);
  }, []);

  const onDrop = useCallback(async () => {
    const arr = pending.current;
    const changed = moved.current;
    dragId.current = null;
    moved.current = false;
    if (!arr || !changed) {
      pending.current = null;
      bump((n) => n + 1);
      return;
    }
    try {
      await onCommit(arr.map((x) => x.id));
    } finally {
      // Drop the working copy either way: on success the reload matches it, on
      // failure the reload is the correction.
      pending.current = null;
      bump((n) => n + 1);
    }
  }, [onCommit]);

  const dragProps = useCallback(
    (id: number) => ({
      draggable: true,
      onDragStart: () => onDragStart(id),
      onDragOver: (e: React.DragEvent) => {
        e.preventDefault();
        onDragOver(id);
      },
      onDrop: (e: React.DragEvent) => {
        e.preventDefault();
        void onDrop();
      },
      // dragend covers a drag abandoned outside any target, which would otherwise
      // leave the working copy on screen forever.
      onDragEnd: () => void onDrop(),
    }),
    [onDragStart, onDragOver, onDrop],
  );

  return { list, dragProps, dragging: dragId.current != null };
}

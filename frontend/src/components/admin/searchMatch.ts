/**
 * One matcher for every admin table, so search behaves the same wherever the
 * box appears.
 *
 * Written down once because the alternative is each tab growing its own
 * `.toLowerCase().includes()` and quietly disagreeing — one trims, one does
 * not, one searches a field the next forgot. Case- and accent-insensitive:
 * the directory has names like "Quỳnh Nguyễn" in it, and typing "quynh"
 * should find her.
 */

function fold(s: string): string {
  return s
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    // Đ/đ is a letter, not D with a mark, so NFD leaves it alone.
    .replace(/[Đđ]/g, "d")
    .toLowerCase();
}

/** True when every whitespace-separated term appears in one of the fields.
 *  Multi-term so "huy gmail" narrows rather than finding nothing. */
export function matches(query: string, ...fields: (string | number | null | undefined)[]): boolean {
  const q = query.trim();
  if (!q) return true;
  const hay = fold(fields.filter((f) => f !== null && f !== undefined).join("  "));
  return fold(q)
    .split(/\s+/)
    .every((term) => hay.includes(term));
}

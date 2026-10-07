/**
 * Parse a server timestamp to epoch milliseconds, treating a timezone-less
 * value as UTC.
 *
 * Some endpoints serialize datetimes WITHOUT a timezone offset, including
 * Postgres timestamp-without-time-zone columns and SQLite read-backs
 * (e.g. "2026-06-16T10:17:18.634773"). A bare `Date.parse`/`new Date` reads the offset-less form as the
 * machine's LOCAL time, so any elapsed-time math is wrong by the local UTC
 * offset — on a UTC+7 machine that pegged the video progress bar at 90%
 * instantly and skewed "x ago" labels by ~7h. Stamping a trailing `Z` on
 * offset-less values makes them parse as UTC, matching how the backend stored
 * them.
 */
export function parseServerTimeMs(iso: string | undefined | null): number {
  if (!iso) return NaN;
  const hasTz = /([zZ]|[+-]\d\d:?\d\d)$/.test(iso);
  return Date.parse(hasTz ? iso : `${iso}Z`);
}

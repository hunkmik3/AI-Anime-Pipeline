import { useEffect, useState } from "react";

/**
 * The comic side of the studio, in the console that could not see it.
 *
 * `stats_service` reads eight tables and not one of them belongs to Giantflow,
 * so this console answered "how is MoguTV going" in detail and had nothing at
 * all to say about a hundred panels across seven comics.
 *
 * Two numbers sit side by side here that are not interchangeable, and the page
 * says so rather than adding them up: an **Atrium** image spends the daily cap
 * and costs nothing, a **Seedream** image costs money and spends no cap. A
 * dollar figure alone cannot tell you the studio is an hour from a wall.
 */

type Counts = Record<string, number>;

type Overview = {
  comics: number;
  chapters: number;
  batches: number;
  panels: number;
  status_counts: Counts;
  approved: number;
  approved_pct: number;
  spent_usd: number;
  runs: number;
  cost_per_approved: number;
};

type ComicRow = {
  series_id: number;
  name: string;
  chapters: number;
  panels: number;
  status_counts: Counts;
  approved: number;
  approved_pct: number;
  spent_usd: number;
  runs: number;
};

type ArtistRow = {
  user_id: string | null;
  name: string;
  panels: number;
  status_counts: Counts;
  sent_back: number;
  approved: number;
  spent_usd: number;
  runs: number;
  runs_per_approved: number;
};

type Quota = {
  quota: number;
  used: number;
  remaining: number;
  seedream_images: number;
  seconds_until_reset: number;
};

type Unattributed = { spent_usd: number; runs: number };

/** The five panel states, in the order work moves through them. Same order and
 *  same colours as the bars inside Giantflow, so the two teach one vocabulary. */
const STAGES = [
  { key: "todo", label: "to do" },
  { key: "in_progress", label: "in progress" },
  { key: "submitted", label: "in review" },
  { key: "changes_requested", label: "sent back" },
  { key: "approved", label: "approved" },
] as const;

async function json<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

const usd = (n: number) => `$${n.toFixed(n < 1 && n > 0 ? 4 : 2)}`;

function untilReset(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

export function ComicsTab() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [comics, setComics] = useState<ComicRow[]>([]);
  const [artists, setArtists] = useState<ArtistRow[]>([]);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [orphan, setOrphan] = useState<Unattributed | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const [o, c, a, q, u] = await Promise.all([
          json<Overview>("/api/admin/stats/comics"),
          json<ComicRow[]>("/api/admin/stats/comics/by-comic"),
          json<ArtistRow[]>("/api/admin/stats/comics/by-artist"),
          json<Quota>("/api/admin/stats/comics/quota"),
          json<Unattributed>("/api/admin/stats/unattributed"),
        ]);
        setOverview(o);
        setComics(c);
        setArtists(a);
        setQuota(q);
        setOrphan(u);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not load");
      }
    })();
  }, []);

  if (error) return <div className="admin-error">{error}</div>;
  if (!overview || !quota) return <div className="admin-loading">Loading…</div>;

  const quotaPct = Math.min(100, (quota.used / quota.quota) * 100);

  return (
    <div className="comics">
      {/* Progress and the cap, together. Two different kinds of "how much is
          left" that a single figure would blur. */}
      <div className="comics__top">
        <Stat label="Comics" value={overview.comics} />
        <Stat label="Panels" value={overview.panels} />
        <Stat
          label="Approved"
          value={`${overview.approved}`}
          sub={`${overview.approved_pct}%`}
          tone="good"
        />
        <Stat label="Images made" value={overview.runs} />
        <Stat
          label="Spent"
          value={usd(overview.spent_usd)}
          sub={
            overview.cost_per_approved
              ? `${usd(overview.cost_per_approved)} / approved panel`
              : undefined
          }
        />
      </div>

      <section className="comics__sec">
        <h3 className="comics__h">
          Today's image quota
          <span className="comics__hint">
            Every Atrium / banana model shares one pool — the ceiling is on the
            account, not the model.
          </span>
        </h3>
        <div className="comics__quota">
          <div className="comics__quota-bar">
            <span
              className={quotaPct > 90 ? "is-full" : quotaPct > 70 ? "is-warm" : ""}
              style={{ width: `${quotaPct}%` }}
            />
          </div>
          <div className="comics__quota-nums">
            <b>{quota.used.toLocaleString()}</b> / {quota.quota.toLocaleString()} used
            <span> · {quota.remaining.toLocaleString()} left</span>
            <span> · resets in {untilReset(quota.seconds_until_reset)}</span>
          </div>
        </div>
        <p className="comics__note">
          Seedream is billed, not capped — {quota.seedream_images.toLocaleString()}{" "}
          image{quota.seedream_images === 1 ? "" : "s"} today at the fixed
          per-image price. Atrium images cost quota and no money, which is why
          spend alone cannot tell you a wall is coming.
        </p>
      </section>

      <section className="comics__sec">
        <h3 className="comics__h">By comic</h3>
        <div className="comics__scroll">
          <table className="comics__table">
            <thead>
              <tr>
                <th>Comic</th>
                <th className="num">Chapters</th>
                <th className="num">Panels</th>
                <th>Progress</th>
                <th className="num">Approved</th>
                <th className="num">Images</th>
                <th className="num">Spent</th>
              </tr>
            </thead>
            <tbody>
              {comics.map((c) => (
                <tr key={c.series_id}>
                  <td className="comics__name">{c.name}</td>
                  <td className="num">{c.chapters}</td>
                  <td className="num">{c.panels}</td>
                  <td className="comics__barcell">
                    <StageBar counts={c.status_counts} total={c.panels} />
                  </td>
                  <td className="num">
                    {c.approved}
                    <small> {c.approved_pct}%</small>
                  </td>
                  <td className="num">{c.runs}</td>
                  <td className="num">{usd(c.spent_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="comics__sec">
        <h3 className="comics__h">
          By artist
          <span className="comics__hint">
            Through the batch they hold — a panel has no owner of its own.
          </span>
        </h3>
        <div className="comics__scroll">
          <table className="comics__table">
            <thead>
              <tr>
                <th>Artist</th>
                <th className="num">Panels</th>
                <th>Progress</th>
                <th className="num">Approved</th>
                <th className="num" title="Rounds returned, not panels — one panel sent back three times is three">
                  Sent back
                </th>
                <th className="num" title="Images made per panel that shipped">
                  Images / panel
                </th>
                <th className="num">Spent</th>
              </tr>
            </thead>
            <tbody>
              {artists.map((a) => (
                <tr key={a.user_id ?? "none"}>
                  <td className="comics__name">{a.name}</td>
                  <td className="num">{a.panels}</td>
                  <td className="comics__barcell">
                    <StageBar counts={a.status_counts} total={a.panels} />
                  </td>
                  <td className="num">{a.approved}</td>
                  <td className={`num${a.sent_back ? " is-warn" : ""}`}>{a.sent_back}</td>
                  <td className="num">{a.runs_per_approved || "—"}</td>
                  <td className="num">{usd(a.spent_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {orphan && orphan.runs > 0 ? (
        <section className="comics__sec">
          <h3 className="comics__h">Spend we cannot place</h3>
          <p className="comics__note">
            <b>{usd(orphan.spent_usd)}</b> across {orphan.runs} run
            {orphan.runs === 1 ? "" : "s"} that belong to no project and no
            panel — their request row was deleted, and the cost record outlived
            what it was for. Shown rather than dropped: a ledger that silently
            omits what it cannot explain still totals correctly, so nobody goes
            looking.
          </p>
        </section>
      ) : null}
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string | number;
  sub?: string;
  tone?: "good";
}) {
  return (
    <div className="comics__stat">
      <span className="comics__stat-l">{label}</span>
      <b className={tone ? `is-${tone}` : undefined}>{value}</b>
      {sub ? <small>{sub}</small> : null}
    </div>
  );
}

/** The whole five-state spread, not just "approved out of total".
 *  40 untouched with 5 approved and 40 in review with 5 approved read
 *  identically as a percentage, and they are nothing alike to a producer. */
function StageBar({ counts, total }: { counts: Counts; total: number }) {
  if (!total) return <span className="comics__bar is-empty" />;
  return (
    <span className="comics__bar" role="img" aria-label={
      STAGES.map((s) => `${counts[s.key] ?? 0} ${s.label}`).join(", ")
    }>
      {STAGES.map((s) => {
        const n = counts[s.key] ?? 0;
        if (!n) return null;
        return (
          <i
            key={s.key}
            className={`is-${s.key}`}
            style={{ width: `${(n / total) * 100}%` }}
            title={`${n} ${s.label}`}
          />
        );
      })}
    </span>
  );
}

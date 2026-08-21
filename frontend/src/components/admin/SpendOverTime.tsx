import { useCallback, useEffect, useState } from "react";

import { useRevalidate } from "../../hooks/useRevalidate";

import {
  getSpendTimeline,
  getUserCosts,
  type SpendPeriod,
  type TimelineDTO,
  type UserCostDTO,
} from "../../api/client";

/**
 * Credits burned over time, and by whom.
 *
 * The summary tiles say *how much*; the ledger says *on what*. Neither says *when*,
 * and a grand total can't tell steady spend from one expensive week. This does both
 * halves of that: a period breakdown, and each period expanding into who was working
 * in it.
 *
 * Deliveries sit next to spend on purpose. Credits burned with nothing handed in is
 * a different situation from the same spend that shipped four episodes, and either
 * number alone invites the wrong conclusion.
 */

const PERIODS: readonly { key: SpendPeriod; label: string; buckets: number }[] = [
  { key: "day", label: "Daily", buckets: 30 },
  { key: "week", label: "Weekly", buckets: 16 },
  { key: "month", label: "Monthly", buckets: 12 },
  { key: "year", label: "Yearly", buckets: 6 },
];

function usd(v: number): string {
  return `$${v.toFixed(2)}`;
}

export function SpendOverTime() {
  const [period, setPeriod] = useState<SpendPeriod>("day");
  const [data, setData] = useState<TimelineDTO | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const spec = PERIODS.find((p) => p.key === period)!;
    setData(null);
    getSpendTimeline(period, spec.buckets)
      .then((d) => {
        if (!alive) return;
        setData(d);
        // Open the most recent period that actually has activity — the newest
        // bucket is often today with nothing in it yet.
        setOpen(d.buckets.find((b) => b.generations > 0)?.key ?? null);
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [period]);

  const peak = data?.totals.peak_usd || 1;

  return (
    <section className="panel">
      <header className="panel__head">
        <h3 className="panel__title">Credits over time</h3>
        <div className="seg" role="tablist">
          {PERIODS.map((p) => (
            <button
              key={p.key}
              role="tab"
              aria-selected={period === p.key}
              className={`seg__btn${period === p.key ? " is-on" : ""}`}
              onClick={() => setPeriod(p.key)}
            >
              {p.label}
            </button>
          ))}
        </div>
      </header>

      {error ? <p className="rfoot">{error}</p> : null}
      {!data && !error ? <p className="rfoot">Loading…</p> : null}

      {data ? (
        data.buckets.length === 0 ? (
          <p className="rfoot">Nothing billed yet.</p>
        ) : (
          <>
            <p className="panel__sub">
              {usd(data.totals.total_usd)} across {data.totals.generations}{" "}
              generations · {data.totals.delivered} episodes delivered. Click a row
              for who was working in it.
            </p>
            <ul className="tl">
              {data.buckets.map((b) => {
                const isOpen = open === b.key;
                const quiet = b.generations === 0;
                const isPeak = !quiet && b.total_usd === data.totals.peak_usd;
                const topPerson = b.people[0]?.total_usd || 1;
                return (
                  <li
                    key={b.key}
                    className={`tl__row${quiet ? " tl__row--quiet" : ""}${
                      isPeak ? " tl__row--peak" : ""
                    }${isOpen ? " is-open" : ""}`}
                  >
                    <button
                      className="tl__head"
                      onClick={() => setOpen(isOpen ? null : b.key)}
                      disabled={quiet}
                    >
                      <span className="tl__label">{b.label}</span>
                      <span className="tl__bar">
                        {/* Kept and re-rolled stacked, so an expensive period shows
                            whether the money went into work that shipped. The scale
                            stays linear against the peak — a busy day really is 300×
                            a quiet one — but each visible slice keeps a 3px floor so
                            a small day reads as "a little" rather than as nothing. */}
                        {b.kept_usd > 0 ? (
                          <span
                            className="tl__fill tl__fill--kept"
                            style={{ width: `max(3px, ${(b.kept_usd / peak) * 100}%)` }}
                          />
                        ) : null}
                        {b.retake_usd > 0 ? (
                          <span
                            className="tl__fill tl__fill--retake"
                            style={{ width: `max(3px, ${(b.retake_usd / peak) * 100}%)` }}
                          />
                        ) : null}
                      </span>
                      <span className="tl__val">{quiet ? "—" : usd(b.total_usd)}</span>
                      <span className="tl__meta">
                        {quiet ? (
                          "no activity"
                        ) : (
                          <>
                            {b.generations} gens
                            {b.delivered ? ` · ${b.delivered} delivered` : ""}
                          </>
                        )}
                      </span>
                    </button>

                    {isOpen && b.people.length > 0 ? (
                      <ul className="tl__people">
                        {b.people.map((p) => (
                          <li key={p.user_id ?? p.name}>
                            <span className="tl__who">{p.name}</span>
                            {/* Scaled within the period, not against the global peak
                                — the question here is who did most of THIS day. */}
                            <span className="tl__whoBar">
                              <span
                                className="tl__whoFill"
                                style={{ width: `max(3px, ${(p.total_usd / topPerson) * 100}%)` }}
                              />
                            </span>
                            <span className="tl__whoMeta">
                              {p.generations} gens
                              {p.delivered ? ` · ${p.delivered} delivered` : ""}
                            </span>
                            <span className="tl__whoVal">{usd(p.total_usd)}</span>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </li>
                );
              })}
            </ul>
            <p className="rfoot">
              The bar splits spend that <b>shipped</b> from spend <b>re-rolled</b>
              away. Older charges with no shot slot recorded count in the total but
              can’t be split, so a tall bar with a short fill is history, not waste.
            </p>
          </>
        )
      ) : null}
    </section>
  );
}

/**
 * Spend per person, all time — the "who" the timeline shows per period.
 *
 * Shows waste next to spend because the two together are the only way to read the
 * number fairly: someone with high spend and low waste is producing, someone with
 * the same spend and high waste is struggling with the prompts.
 */
export function SpendByPerson() {
  const [rows, setRows] = useState<UserCostDTO[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await getUserCosts());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useRevalidate(() => void load(), { intervalMs: 20000 });

  if (error) return <section className="panel"><p className="rfoot">{error}</p></section>;
  if (!rows) return <section className="panel"><p className="rfoot">Loading…</p></section>;

  const spenders = rows.filter((r) => r.spent_usd > 0).sort((a, b) => b.spent_usd - a.spent_usd);
  const top = spenders[0]?.spent_usd || 1;

  return (
    <section className="panel">
      <header className="panel__head">
        <h3 className="panel__title">Credits by person</h3>
        <span className="panel__aside">all time</span>
      </header>

      {spenders.length === 0 ? (
        <p className="rfoot">Nobody has generated anything yet.</p>
      ) : (
        <ul className="tl">
          {spenders.map((u) => (
            <li key={u.user_id} className="tl__row">
              <div className="tl__head tl__head--static">
                <span className="tl__label">{u.display_name || u.username}</span>
                <span className="tl__bar">
                  <span
                    className="tl__fill tl__fill--kept"
                    style={{ width: `${(u.kept_usd / top) * 100}%` }}
                  />
                  <span
                    className="tl__fill tl__fill--retake"
                    style={{ width: `${(u.wasted_usd / top) * 100}%` }}
                  />
                </span>
                <span className="tl__val">{usd(u.spent_usd)}</span>
                <span className="tl__meta">
                  {u.kept_clips} clips · {u.takes} takes
                  {u.waste_pct ? ` · ${u.waste_pct}% re-rolled` : ""}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="rfoot">
        Re-rolled share matters more than the raw figure: the same spend with low
        waste is production, with high waste it is someone fighting the prompt.
      </p>
    </section>
  );
}

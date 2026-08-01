import { useCallback, useEffect, useState } from "react";

import {
  getLedgerFilterOptions,
  getSpendLedger,
  type LedgerDTO,
  type LedgerFilters,
} from "../../api/client";

/**
 * The detailed tracker: every billed generation, one row per take.
 *
 * The overview says *how much* was spent. This says *on what* — who ran it, which
 * episode and sequence it landed in, which model, and whether it was the take they
 * settled on or a re-roll. Before this, those slices lived on separate screens
 * (per-shot, per-user, per-project), so tracing one charge meant opening several
 * and joining them by eye.
 *
 * Two columns carry most of the meaning:
 *   Take    N of M on the same shot slot — the retake story, in place
 *   Kept    the newest take on a slot is the one that shipped; the rest are real
 *           money deliberately spent on attempts that didn't make the cut
 *
 * `totals` always describes the whole filtered set, never just the visible page: a
 * total that silently covered only what fit on screen would be worse than none.
 */

const PAGE = 100;

export function SpendLedgerTab() {
  const [filters, setFilters] = useState<LedgerFilters>({});
  const [page, setPage] = useState(0);
  const [data, setData] = useState<LedgerDTO | null>(null);
  const [opts, setOpts] = useState<{
    projects: { id: string; name: string }[];
    people: { id: string; name: string }[];
    models: string[];
  }>({ projects: [], people: [], models: [] });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getLedgerFilterOptions().then(setOpts).catch(() => undefined);
  }, []);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setData(await getSpendLedger({ ...filters, limit: PAGE, offset: page * PAGE }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [filters, page]);

  useEffect(() => {
    void load();
  }, [load]);

  function set<K extends keyof LedgerFilters>(k: K, v: LedgerFilters[K]) {
    setPage(0); // a new filter invalidates the page you were on
    setFilters((f) => {
      const next = { ...f };
      if (v === undefined || v === "") delete next[k];
      else next[k] = v;
      return next;
    });
  }

  const active = Object.keys(filters).length > 0;
  const t = data?.totals;
  const shown = data?.rows.length ?? 0;
  const from = (data?.offset ?? 0) + (shown ? 1 : 0);
  const to = (data?.offset ?? 0) + shown;
  const totalRows = data?.total_rows ?? 0;

  return (
    <div className="report">
      <div className="ledger__bar">
        <select
          className="ledger__filter"
          value={filters.project_id ?? ""}
          onChange={(e) => set("project_id", e.target.value)}
          aria-label="Project"
        >
          <option value="">All projects</option>
          {opts.projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>

        <select
          className="ledger__filter"
          value={filters.user_id ?? ""}
          onChange={(e) => set("user_id", e.target.value)}
          aria-label="Person"
        >
          <option value="">Everyone</option>
          {opts.people.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>

        <select
          className="ledger__filter"
          value={filters.model ?? ""}
          onChange={(e) => set("model", e.target.value)}
          aria-label="Model"
        >
          <option value="">Any model</option>
          {opts.models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>

        <select
          className="ledger__filter"
          value={filters.kept === undefined ? "" : filters.kept ? "kept" : "retake"}
          onChange={(e) =>
            set("kept", e.target.value === "" ? undefined : e.target.value === "kept")
          }
          aria-label="Kept or re-roll"
        >
          <option value="">Kept and re-rolls</option>
          <option value="kept">Kept only</option>
          <option value="retake">Re-rolls only</option>
        </select>

        {active ? (
          <button className="ledger__clear" onClick={() => { setFilters({}); setPage(0); }}>
            Clear filters
          </button>
        ) : null}
      </div>

      {t ? (
        <div className="stats">
          <div className="stat">
            <div className="stat__label">Generations</div>
            <div className="stat__value">{t.generations}</div>
          </div>
          <div className="stat">
            <div className="stat__label">Total</div>
            <div className="stat__value">${t.total_usd.toFixed(2)}</div>
          </div>
          <div className="stat">
            <div className="stat__label">Kept</div>
            <div className="stat__value">${t.kept_usd.toFixed(2)}</div>
          </div>
          <div className={`stat${t.retake_pct >= 25 ? " stat--alert" : ""}`}>
            <div className="stat__label">Re-rolls</div>
            <div className="stat__value">
              ${t.retake_usd.toFixed(2)}
              <small className="stat__aside"> {t.retake_pct}%</small>
            </div>
          </div>
          {/* Its own line, not folded into Kept. Older charges carry no node, so
              there are no sibling takes to compare and whether they shipped is
              unknowable — counting them as kept made re-rolls look far rarer than
              they are. */}
          {t.unclassified_usd > 0 ? (
            <div
              className="stat"
              title={`${t.unclassified_count} charges with no shot slot recorded — can't be told apart into shipped vs re-rolled`}
            >
              <div className="stat__label">Unclassified</div>
              <div className="stat__value">${t.unclassified_usd.toFixed(2)}</div>
            </div>
          ) : null}
          <div className="stat">
            <div className="stat__label">People</div>
            <div className="stat__value">{t.people}</div>
          </div>
          <div className="stat">
            <div className="stat__label">Downloaded</div>
            <div className="stat__value">{t.downloaded}</div>
          </div>
        </div>
      ) : null}

      {error ? <p className="rfoot">Couldn’t load the ledger: {error}</p> : null}
      {!data && !error ? <p className="rfoot">Loading…</p> : null}

      {data ? (
        data.rows.length === 0 ? (
          <p className="rfoot">
            {active
              ? "Nothing matches those filters."
              : "No generations have been billed yet."}
          </p>
        ) : (
          <>
            <div className="rtable-wrap">
              <table className="rtable">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Who</th>
                    <th>Project</th>
                    <th>Episode</th>
                    <th>Sequence</th>
                    <th>Model</th>
                    <th className="num" title="Which attempt on this shot slot">
                      Take
                    </th>
                    <th title="The newest take on a slot is the one that shipped">
                      Kept
                    </th>
                    <th title="The only signal a clip was actually used">DL</th>
                    <th className="num">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r) => (
                    <tr
                      key={r.usage_id ?? `${r.node_id}-${r.take}-${r.when}`}
                      className={r.kept ? "" : "is-retake"}
                    >
                      <td className="ledger__when">{fmtWhen(r.when)}</td>
                      <td className="rtable__name">{r.user_name ?? "—"}</td>
                      <td>
                        {r.project_name ?? (
                          <span className="nil" title="Not attached to a project">
                            unattributed
                          </span>
                        )}
                      </td>
                      <td>{r.episode ?? <span className="nil">—</span>}</td>
                      <td>{r.sequence ?? <span className="nil">—</span>}</td>
                      <td className="ledger__model">{r.model ?? "—"}</td>
                      <td className="num">
                        {r.takes_on_node > 1 ? (
                          <span title={`take ${r.take} of ${r.takes_on_node}`}>
                            {r.take}/{r.takes_on_node}
                          </span>
                        ) : (
                          <span className="nil">1</span>
                        )}
                      </td>
                      <td>
                        {r.kept ? (
                          <span className="tick tick--yes" title="Shipped">
                            ✓
                          </span>
                        ) : (
                          <span className="tick tick--no" title="Re-rolled">
                            ↻
                          </span>
                        )}
                      </td>
                      <td>
                        {r.downloaded ? (
                          <span className="tick tick--yes">✓</span>
                        ) : (
                          <span className="nil">·</span>
                        )}
                      </td>
                      <td className="num">${r.cost_usd.toFixed(3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="ledger__foot">
              <span>
                {from}–{to} of {totalRows}
              </span>
              <span className="ledger__pager">
                <button
                  disabled={page === 0 || busy}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  ← Newer
                </button>
                <button disabled={to >= totalRows || busy} onClick={() => setPage((p) => p + 1)}>
                  Older →
                </button>
              </span>
            </div>
            <p className="rfoot">
              <b>Take</b> is which attempt this was on the same shot slot; the last one
              is what shipped and earlier ones are re-rolls — money spent on attempts
              that didn’t make the cut. <b>DL</b> means someone downloaded it, the only
              signal the app gets that a clip was actually used.
            </p>
          </>
        )
      ) : null}
    </div>
  );
}

function fmtWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

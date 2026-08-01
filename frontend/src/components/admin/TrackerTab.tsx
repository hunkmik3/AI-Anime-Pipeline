import { useEffect, useState } from "react";

import {
  getKpiOverview,
  type KpiOverviewDTO,
  type KpiPersonDTO,
  type KpiProjectRowDTO,
} from "../../api/client";

/**
 * The board's tracker: delivery, rework and spend across the whole app.
 *
 * Admin/BOD only, which is why it lives here rather than on the project surface a
 * PM uses — this compares colleagues' output, and that is not information the app
 * hands to a peer.
 *
 * There are no scores or targets. What counts as good is a management decision
 * that has not been stated, and inventing a pass mark would impose a rule nobody
 * approved on the people being measured. Nothing about hours or speed either: the
 * data records when work was assigned and submitted, never when someone actually
 * started, so a productivity figure would be fabricated.
 *
 * Layout notes, because the first version read badly:
 *   · capped reading width — stretched to 1600px the eye lost the row between a
 *     name and its numbers
 *   · no intro paragraph: the page subtitle already says what this is, and saying
 *     it twice in different words made the screen look like a draft
 *   · unstaffed work is a callout at the top, not a muted italic row at the
 *     bottom — it is the finding, not a footnote
 */

function pct(v: number | null): string {
  return v == null ? "—" : `${Math.round(v)}%`;
}

function money(v: number | null | undefined): string {
  return v == null ? "—" : `$${v.toFixed(2)}`;
}

/** A number that isn't there yet reads as absent, not as zero. */
function Num({ value, suffix }: { value: number | null | undefined; suffix?: string }) {
  if (value == null || value === 0) return <span className="nil">—</span>;
  return (
    <>
      {value}
      {suffix}
    </>
  );
}

/** Colour is a reading aid that draws the eye to "most work comes back". It does
 *  not assert where the pass mark is — see the module note. */
function firstPassClass(rate: number | null): string {
  if (rate == null) return "nil";
  if (rate >= 0.8) return "q-good";
  if (rate >= 0.5) return "q-mid";
  return "q-bad";
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "alert" | "good";
}) {
  return (
    <div className={`stat${tone ? ` stat--${tone}` : ""}`}>
      <div className="stat__label">{label}</div>
      <div className="stat__value">{value}</div>
    </div>
  );
}

function Bar({ value }: { value: number | null }) {
  return (
    <span className="pbar">
      <span className="pbar__track">
        <span className="pbar__fill" style={{ width: `${value ?? 0}%` }} />
      </span>
      <span className="pbar__pct">{pct(value)}</span>
    </span>
  );
}

function ProjectTable({ rows }: { rows: KpiProjectRowDTO[] }) {
  return (
    <div className="rtable-wrap">
      <table className="rtable">
        <thead>
          <tr>
            <th>Project</th>
            <th className="num">Episodes</th>
            <th className="num">Delivered</th>
            <th className="num">In review</th>
            <th className="num">Unstaffed</th>
            <th>Progress</th>
            <th className="num">Spent</th>
            <th className="num">Budget</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.project_id}>
              <td className="rtable__name">{p.project_name}</td>
              <td className="num">{p.episodes}</td>
              <td className="num">
                <Num value={p.delivered} />
              </td>
              <td className="num">
                <Num value={p.in_review} />
              </td>
              <td className="num">
                <Num value={p.unassigned} />
              </td>
              <td>
                <Bar value={p.completion_pct} />
              </td>
              <td className="num">{money(p.budget?.spent_usd ?? 0)}</td>
              <td className="num">
                {p.budget?.unlimited ? (
                  <span className="nil" title="No ceiling set">
                    ∞
                  </span>
                ) : (
                  money(p.budget?.effective_usd)
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PeopleTable({ rows }: { rows: KpiPersonDTO[] }) {
  return (
    <div className="rtable-wrap">
      <table className="rtable">
        <thead>
          <tr>
            <th>Person</th>
            <th className="num">Assigned</th>
            <th className="num">Delivered</th>
            <th className="num">In review</th>
            <th className="num">Attempts</th>
            <th className="num">Sent back</th>
            <th className="num" title="Share of approvals that landed on the first attempt">
              First pass
            </th>
            <th className="num">Tries / ep</th>
            <th className="num">Credits</th>
            <th className="num">$ / ep</th>
            <th className="num" title="Submit → verdict. Measures the reviewer, not the artist.">
              Review days
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.user_id ?? "unstaffed"} className={p.user_id ? "" : "is-unstaffed"}>
              <td className="rtable__name">{p.name ?? "Nobody assigned"}</td>
              <td className="num">{p.assigned}</td>
              <td className="num">
                <Num value={p.delivered} />
              </td>
              <td className="num">
                <Num value={p.in_review} />
              </td>
              <td className="num">
                <Num value={p.attempts} />
              </td>
              <td className="num">
                <Num value={p.rejections} />
              </td>
              <td className={`num ${firstPassClass(p.first_pass_rate)}`}>
                {p.first_pass_rate == null
                  ? "—"
                  : `${Math.round(p.first_pass_rate * 100)}%`}
              </td>
              <td className="num">
                {p.avg_attempts == null ? (
                  <span className="nil">—</span>
                ) : (
                  p.avg_attempts.toFixed(2)
                )}
              </td>
              <td className="num">
                {p.credits_usd ? money(p.credits_usd) : <span className="nil">—</span>}
              </td>
              <td className="num">
                {p.credits_per_delivered_usd == null ? (
                  <span className="nil">—</span>
                ) : (
                  money(p.credits_per_delivered_usd)
                )}
              </td>
              <td className="num">
                {p.avg_review_days == null ? (
                  <span className="nil">—</span>
                ) : (
                  p.avg_review_days.toFixed(1)
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function TrackerTab() {
  const [data, setData] = useState<KpiOverviewDTO | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const out = await getKpiOverview();
        if (alive) setData(out);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (error) return <p className="rfoot">Couldn’t load the tracker: {error}</p>;
  if (!data) return <p className="rfoot">Loading…</p>;

  const t = data.totals;
  const hasWork = t.episodes > 0;

  return (
    <div className="report">
      <div className="stats">
        <Stat label="Episodes" value={String(t.episodes)} />
        <Stat label="Delivered" value={String(t.delivered)} tone={t.delivered ? "good" : undefined} />
        <Stat label="In review" value={String(t.in_review)} />
        <Stat
          label="Unstaffed"
          value={String(t.unassigned)}
          tone={t.unassigned ? "alert" : undefined}
        />
        <Stat label="Completion" value={pct(t.completion_pct)} />
        <Stat label="Credits spent" value={money(t.credits_usd)} />
        <Stat label="Projects" value={String(t.projects)} />
      </div>

      {hasWork && t.unassigned > 0 ? (
        <p className="callout">
          <span>
            <b>
              {t.unassigned} of {t.episodes} episodes have nobody on them.
            </b>{" "}
            Only an episode’s assignee can hand it in, so none of these can be
            delivered until a PM assigns someone.
          </span>
        </p>
      ) : hasWork ? (
        <p className="callout callout--ok">
          <span>
            <b>Every episode has an owner.</b> Nothing is waiting on staffing.
          </span>
        </p>
      ) : null}

      <section className="rsection">
        <div className="rsection__head">
          <h3 className="rsection__title">By project</h3>
        </div>
        {data.projects.length === 0 ? (
          <p className="rfoot">No projects yet.</p>
        ) : (
          <ProjectTable rows={data.projects} />
        )}
      </section>

      <section className="rsection">
        <div className="rsection__head">
          <h3 className="rsection__title">By person</h3>
          <span className="rsection__note">Totals across every project</span>
        </div>
        {data.people.length === 0 ? (
          <p className="rfoot">No episodes assigned yet.</p>
        ) : (
          <>
            <PeopleTable rows={data.people} />
            <p className="rfoot">
              <b>First pass</b> is the share of approvals that landed without coming
              back. <b>Review days</b> is submit → verdict, so it measures how
              quickly reviewers respond rather than the artist. A dash means there
              is nothing recorded yet.
            </p>
          </>
        )}
      </section>
    </div>
  );
}

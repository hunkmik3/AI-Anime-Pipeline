import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  getBudget,
  getObjectHistory,
  getProject,
  getSeries,
  listAssignableUsers,
  listScenes,
  patchSeries,
  setSeriesProducer,
  type BudgetSummaryDTO,
  type HistoryEntryDTO,
  type ProjectDetailDTO,
  type SceneDTO,
  type SeriesDTO,
} from "../api/client";
import { BackTo } from "../components/shell/BackTo";
import { PersonPicker } from "../components/PersonPicker";
import { QuotaField } from "../components/QuotaField";
import { TierPicker } from "../components/TierChip";

/**
 * One series, and everything about it.
 *
 * The same move as the episode page, one level up. A series' own information —
 * tier, status, priority, genres, dates, the folder link, its producer, its budget,
 * its progress — lived in a flat table under `/admin` that merged every series of
 * every project, edited through a modal. That table answered "how do all series
 * compare", which is a real question, but it was also the *only* place a single
 * series could be looked at.
 *
 * Now the cross-project table stays for comparison, and this is where a series is
 * actually worked on.
 */

type Tab = "episodes" | "details" | "history";

const TABS: readonly { key: Tab; label: string }[] = [
  { key: "episodes", label: "Episodes" },
  { key: "details", label: "Details" },
  { key: "history", label: "History" },
];

/** Grouped the way a producer thinks about them, not the order the bag stores them. */
const FIELD_GROUPS: readonly {
  title: string;
  fields: readonly { key: string; label: string; kind?: "date" | "int" | "long" }[];
}[] = [
  {
    title: "Schedule",
    fields: [
      { key: "start_date", label: "Start", kind: "date" },
      { key: "end_date", label: "End", kind: "date" },
      { key: "total_episodes_planned", label: "Episodes planned", kind: "int" },
      { key: "episode_duration_sec", label: "Episode length (sec)", kind: "int" },
      { key: "sec_per_video", label: "Seconds per sequence", kind: "int" },
    ],
  },
  {
    title: "Content",
    fields: [
      { key: "genres", label: "Genres" },
      { key: "tropes", label: "Tropes" },
      { key: "logline", label: "Logline", kind: "long" },
    ],
  },
  {
    title: "Market",
    fields: [
      { key: "target_market", label: "Target market" },
      { key: "secondary_markets", label: "Secondary markets" },
      { key: "target_audience", label: "Audience" },
      { key: "language_original", label: "Original language" },
    ],
  },
  {
    title: "Links",
    fields: [{ key: "folder_link", label: "Drive folder" }],
  },
];

const STATUSES = ["Planning", "On-going", "Completed", "Cancelled"];
const PRIORITIES = ["High", "Medium", "Low"];

export function SeriesPage() {
  const { projectId = "", seriesId = "" } = useParams();

  const [tab, setTab] = useState<Tab>("episodes");
  const [series, setSeries] = useState<SeriesDTO | null>(null);
  const [project, setProject] = useState<ProjectDetailDTO | null>(null);
  const [scenes, setScenes] = useState<SceneDTO[]>([]);
  const [budget, setBudget] = useState<BudgetSummaryDTO | null>(null);
  const [history, setHistory] = useState<HistoryEntryDTO[]>([]);
  const [people, setPeople] = useState<{ user_id: string; name: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    const s = await getSeries(seriesId);
    setSeries(s);
    const [proj, scn, bud, hist, pool] = await Promise.allSettled([
      getProject(projectId),
      listScenes(projectId, seriesId),
      getBudget("series", seriesId),
      getObjectHistory("series", seriesId, 50),
      listAssignableUsers(projectId),
    ]);
    if (proj.status === "fulfilled") setProject(proj.value);
    if (scn.status === "fulfilled") setScenes(scn.value);
    if (bud.status === "fulfilled") setBudget(bud.value);
    if (hist.status === "fulfilled") setHistory(hist.value.entries);
    if (pool.status === "fulfilled") setPeople(pool.value);
  }, [projectId, seriesId]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        await load();
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [load]);

  const prod = useMemo(
    () => (series?.production ?? {}) as Record<string, string | number>,
    [series],
  );

  async function saveField(key: string, value: string) {
    setSaving(true);
    setError(null);
    try {
      // Only the touched key is sent; the backend merges over the bag, so an
      // unrelated field can't be wiped by saving this one.
      await patchSeries(seriesId, { production: { [key]: value } });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <div className="shellpage"><p className="rfoot">Loading…</p></div>;

  if (!series) {
    return (
      <div className="shellpage">
        <p className="ep__err">{error ?? "This series isn’t available to you."}</p>
        <Link to={`/projects/${projectId}`} className="btn2 btn2--ghost">
          ← Back to the project
        </Link>
      </div>
    );
  }

  const can = (c: string): boolean =>
    Boolean((project?.can as Record<string, boolean> | undefined)?.[c]);
  const canEdit = can("series.update");
  const unit = series.unit_label || "Episode";
  const delivered = scenes.filter((s) =>
    ["approved", "paid"].includes(s.deliverable_status || "draft"),
  ).length;
  const unstaffed = scenes.filter((s) => !s.assignee_user_id).length;

  return (
    <div className="shellpage">
      <div className="pagehead">
        <div className="pagehead__crumb">
          <BackTo />
          <Link to="/projects">Projects</Link> /{" "}
          <Link to={`/projects/${projectId}`}>{project?.name ?? "Project"}</Link>
        </div>
        <div className="pagehead__row">
          <div className="pagehead__titles">
            <h1 className="pagehead__title">
              {series.code ? <span className="ep__code">{series.code}</span> : null}
              {series.name}
            </h1>
            <p className="pagehead__sub">
              {scenes.length} {unit.toLowerCase()}
              {scenes.length === 1 ? "" : "s"} · {delivered} delivered
              {unstaffed ? ` · ${unstaffed} unstaffed` : ""}
            </p>
          </div>
        </div>
      </div>

      <div className="ep__facts">
        <div className="ep__fact">
          <span className="ep__fact-label">Producer</span>
          <PersonPicker
            label=""
            value={series.producer_user_id}
            people={people}
            disabled={!can("member.manage")}
            onChange={async (uid) => {
              await setSeriesProducer(seriesId, uid);
              await load();
            }}
          />
          {!series.producer_user_id ? (
            <span className="ep__warn">
              Submissions in this series fall through to the PM.
            </span>
          ) : null}
        </div>

        <div className="ep__fact">
          <span className="ep__fact-label">Budget</span>
          {can("member.manage") ? (
            <QuotaField scope="series" id={seriesId} />
          ) : (
            <span className="ep__fact-value">
              {budget?.unlimited
                ? "No ceiling"
                : `$${(budget?.spent_usd ?? 0).toFixed(2)} of $${(
                    budget?.effective_usd ?? 0
                  ).toFixed(2)}`}
            </span>
          )}
        </div>

        <div className="ep__fact">
          <span className="ep__fact-label">Progress</span>
          <span className="pbar">
            <span className="pbar__track">
              <span
                className="pbar__fill"
                style={{
                  width: `${scenes.length ? (delivered / scenes.length) * 100 : 0}%`,
                }}
              />
            </span>
            <span className="pbar__pct">
              {scenes.length ? Math.round((delivered / scenes.length) * 100) : 0}%
            </span>
          </span>
        </div>
      </div>

      {error ? <p className="ep__err">{error}</p> : null}

      <div className="pagetabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            className={`pagetabs__tab${tab === t.key ? " is-active" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {t.key === "episodes" && scenes.length ? (
              <span className="pagetabs__count">{scenes.length}</span>
            ) : null}
          </button>
        ))}
      </div>

      <div className="ep__body">
        {tab === "episodes" ? (
          scenes.length === 0 ? (
            <p className="rfoot">No {unit.toLowerCase()}s yet.</p>
          ) : (
            <ul className="sp__eps">
              {scenes
                .slice()
                .sort((a, b) => a.order_index - b.order_index)
                .map((sc) => (
                  <li key={sc.id}>
                    <Link
                      to={`/projects/${projectId}/episodes/${sc.id}`}
                      className="sp__ep"
                    >
                      <span className="sp__ep-code">{sc.code || sc.name}</span>
                      <span className="sp__ep-name">{sc.name}</span>
                      <span className="sp__ep-right">
                        {!sc.assignee_user_id ? (
                          <span className="sp__ep-unstaffed">unstaffed</span>
                        ) : (
                          <span className="sp__ep-who">{sc.assignee_name}</span>
                        )}
                        <StatusDot status={sc.deliverable_status} />
                      </span>
                    </Link>
                  </li>
                ))}
            </ul>
          )
        ) : null}

        {tab === "details" ? (
          <div className="sp__fields">
            <FieldGroup title="Classification">
              <Field label="Tier">
                <TierPicker
                  value={String(prod.tier ?? "")}
                  disabled={!canEdit || saving}
                  onChange={(v) => void saveField("tier", v)}
                />
              </Field>
              <Field label="Status">
                <Choice
                  value={String(prod.status ?? "")}
                  options={STATUSES}
                  disabled={!canEdit || saving}
                  onChange={(v) => void saveField("status", v)}
                />
              </Field>
              <Field label="Priority">
                <Choice
                  value={String(prod.priority ?? "")}
                  options={PRIORITIES}
                  disabled={!canEdit || saving}
                  onChange={(v) => void saveField("priority", v)}
                />
              </Field>
            </FieldGroup>

            {FIELD_GROUPS.map((g) => (
              <FieldGroup key={g.title} title={g.title}>
                {g.fields.map((f) => (
                  <Field key={f.key} label={f.label} wide={f.kind === "long"}>
                    <TextField
                      value={prod[f.key] == null ? "" : String(prod[f.key])}
                      kind={f.kind}
                      disabled={!canEdit || saving}
                      onCommit={(v) => void saveField(f.key, v)}
                    />
                  </Field>
                ))}
              </FieldGroup>
            ))}

            {!canEdit ? (
              <p className="rfoot">These are read-only for your role on this project.</p>
            ) : null}
          </div>
        ) : null}

        {tab === "history" ? (
          history.length === 0 ? (
            <p className="rfoot">Nothing recorded yet.</p>
          ) : (
            <ol className="ep__hist">
              {history.map((e) => (
                <li key={e.id}>
                  <span className="ep__hist-when">{fmt(e.created_at)}</span>
                  <span className="ep__hist-action">{e.action.replace(/[._]/g, " ")}</span>
                  <span className="ep__hist-who">{e.actor ?? "system"}</span>
                  {e.detail ? <span className="ep__hist-detail">{e.detail}</span> : null}
                </li>
              ))}
            </ol>
          )
        ) : null}
      </div>
    </div>
  );
}

// ── field building blocks ───────────────────────────────────────────────────

function FieldGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="sp__group">
      <h3 className="sp__group-title">{title}</h3>
      <div className="sp__group-grid">{children}</div>
    </section>
  );
}

function Field({
  label,
  wide,
  children,
}: {
  label: string;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className={`sp__field${wide ? " sp__field--wide" : ""}`}>
      <span className="sp__field-label">{label}</span>
      {children}
    </label>
  );
}

/**
 * Saves on blur rather than on every keystroke — each save is a PATCH that lands in
 * the change log, and one entry per character typed would bury the real edits.
 */
function TextField({
  value,
  kind,
  disabled,
  onCommit,
}: {
  value: string;
  kind?: "date" | "int" | "long";
  disabled?: boolean;
  onCommit: (v: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const commit = () => {
    if (draft !== value) onCommit(draft);
  };

  if (kind === "long") {
    return (
      <textarea
        className="sp__input sp__input--long"
        rows={3}
        value={draft}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
      />
    );
  }
  return (
    <input
      className="sp__input"
      type={kind === "date" ? "date" : kind === "int" ? "number" : "text"}
      value={draft}
      disabled={disabled}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
}

function Choice({
  value,
  options,
  disabled,
  onChange,
}: {
  value: string;
  options: readonly string[];
  disabled?: boolean;
  onChange: (v: string) => void;
}) {
  return (
    <select
      className="sp__input"
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">—</option>
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

function StatusDot({ status }: { status?: string }) {
  const s = status || "draft";
  const tone =
    s === "approved" || s === "paid" ? "good" : s === "submitted" ? "warn" : "muted";
  return <span className={`sp__dot sp__dot--${tone}`} title={s} aria-label={s} />;
}

function fmt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}

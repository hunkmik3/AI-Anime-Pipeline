import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  createShot,
  deleteScene,
  deleteShot,
  getBudget,
  getObjectHistory,
  getProject,
  getScene,
  getSeries,
  listAssignableUsers,
  listEpisodeSubmissions,
  listShots,
  patchScene,
  setEpisodeAssignee,
  type BudgetSummaryDTO,
  type HistoryEntryDTO,
  type ProjectDetailDTO,
  type SceneDTO,
  type SeriesDTO,
  type ShotDTO,
  type SubmissionDTO,
} from "../api/client";
import { BackTo } from "../components/shell/BackTo";
import { PersonPicker } from "../components/PersonPicker";
import { QuotaField } from "../components/QuotaField";
import { useAuthStore } from "../store/auth";

/**
 * One episode, and everything about it.
 *
 * This page is the fix for the arrangement problem: an episode's information used
 * to be spread over seven screens — the project home had its sequences, `/manage`
 * had its assignee and quota, `/admin` had its cost in one tab and its delivery
 * state in another, `/review` and `/work` each held part of its submission
 * history, and its change log sat behind a button somewhere else. There was no page
 * for an episode, so everything *about* one had to be filed under a feature
 * instead, and the same hierarchy ended up drawn five different ways.
 *
 * Arranged by object, like the pages that already work: the thing you are looking
 * at is the page you are on, and the features are sections on it.
 */

type Tab = "sequences" | "delivery" | "spend" | "history";

const TABS: readonly { key: Tab; label: string }[] = [
  { key: "sequences", label: "Sequences" },
  { key: "delivery", label: "Delivery" },
  { key: "spend", label: "Spend" },
  { key: "history", label: "History" },
];

const STATUS_TONE: Record<string, string> = {
  draft: "muted",
  submitted: "warn",
  approved: "good",
  paid: "info",
};

export function EpisodePage() {
  const { projectId = "", sceneId = "" } = useParams();
  const navigate = useNavigate();
  const isAdmin = useAuthStore((s) => s.user?.role === "admin");
  const [busy, setBusy] = useState(false);

  const [tab, setTab] = useState<Tab>("sequences");
  const [scene, setScene] = useState<SceneDTO | null>(null);
  const [series, setSeries] = useState<SeriesDTO | null>(null);
  const [project, setProject] = useState<ProjectDetailDTO | null>(null);
  const [shots, setShots] = useState<ShotDTO[]>([]);
  const [subs, setSubs] = useState<SubmissionDTO[]>([]);
  const [budget, setBudget] = useState<BudgetSummaryDTO | null>(null);
  const [history, setHistory] = useState<HistoryEntryDTO[]>([]);
  const [people, setPeople] = useState<{ user_id: string; name: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    const sc = await getScene(sceneId);
    setScene(sc);
    // Everything else is independent of everything else, so one failure (a budget
    // the caller may not read, an empty history) must not blank the whole page.
    const [proj, ser, sh, sub, bud, hist, pool] = await Promise.allSettled([
      getProject(projectId),
      sc.series_id ? getSeries(sc.series_id) : Promise.resolve(null),
      listShots(sceneId),
      listEpisodeSubmissions(sceneId),
      getBudget("scene", sceneId),
      getObjectHistory("scene", sceneId, 50),
      listAssignableUsers(projectId),
    ]);
    if (proj.status === "fulfilled") setProject(proj.value);
    if (ser.status === "fulfilled") setSeries(ser.value);
    if (sh.status === "fulfilled") setShots(sh.value);
    if (sub.status === "fulfilled") setSubs(sub.value.submissions);
    if (bud.status === "fulfilled") setBudget(bud.value);
    if (hist.status === "fulfilled") setHistory(hist.value.entries);
    if (pool.status === "fulfilled") setPeople(pool.value);
  }, [projectId, sceneId]);

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

  if (loading) return <div className="shellpage"><p className="rfoot">Loading…</p></div>;

  if (!scene) {
    return (
      <div className="shellpage">
        <p className="ep__err">{error ?? "This episode isn’t available to you."}</p>
        <Link to={`/projects/${projectId}`} className="btn2 btn2--ghost">
          ← Back to the project
        </Link>
      </div>
    );
  }

  const can = (c: string): boolean =>
    Boolean((project?.can as Record<string, boolean> | undefined)?.[c]);
  const status = scene.deliverable_status || "draft";
  const latest = subs[0];
  const sentBack = latest?.status === "rejected" && status === "draft";

  return (
    <div className="shellpage">
      <div className="pagehead">
        <div className="pagehead__crumb">
          <BackTo />
          <Link to="/projects">Projects</Link> /{" "}
          <Link to={`/projects/${projectId}`}>{project?.name ?? "Project"}</Link>
          {series ? <> / {series.code || series.name}</> : null}
        </div>
        <div className="pagehead__row">
          <div className="pagehead__titles">
            <h1 className="pagehead__title">
              {scene.code ? <span className="ep__code">{scene.code}</span> : null}
              {scene.name}
            </h1>
            <p className="pagehead__sub">
              {shots.length} sequence{shots.length === 1 ? "" : "s"}
              {series ? ` · ${series.name}` : ""}
            </p>
          </div>
          <div className="pagehead__actions">
            {can("episode.update") ? (
              <button
                className="btn2 btn2--ghost"
                disabled={busy}
                onClick={async () => {
                  // eslint-disable-next-line no-alert
                  const name = window.prompt("Episode name", scene.name);
                  if (name == null) return;
                  setBusy(true);
                  try {
                    await patchScene(sceneId, { name: name.trim() || scene.name });
                    await load();
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Rename
              </button>
            ) : null}
            {can("episode.delete") ? (
              <button
                className="btn2 btn2--ghost"
                disabled={busy}
                onClick={async () => {
                  // eslint-disable-next-line no-alert
                  if (
                    !window.confirm(
                      `Delete “${scene.name}”? Its sequences and their generated work go too.`,
                    )
                  )
                    return;
                  setBusy(true);
                  try {
                    await deleteScene(sceneId);
                    navigate(`/projects/${projectId}`);
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                    setBusy(false);
                  }
                }}
              >
                Delete
              </button>
            ) : null}
            <Link
              to={`/projects/${projectId}/scenes/${sceneId}`}
              className="btn2 btn2--primary"
            >
              Open canvas →
            </Link>
          </div>
        </div>
      </div>

      {/* The three facts that decide what happens next, above the detail. */}
      <div className="ep__facts">
        <div className="ep__fact">
          <span className="ep__fact-label">Assignee</span>
          <PersonPicker
            label=""
            value={scene.assignee_user_id}
            people={people}
            disabled={!can("episode.update")}
            onChange={async (uid) => {
              await setEpisodeAssignee(sceneId, uid);
              await load();
            }}
          />
          {!scene.assignee_user_id ? (
            <span className="ep__warn">
              Nobody can hand this in until someone is assigned.
            </span>
          ) : null}
        </div>

        <div className="ep__fact">
          <span className="ep__fact-label">Quota</span>
          {can("member.manage") ? (
            <QuotaField scope="scene" id={sceneId} />
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
          <span className="ep__fact-label">Delivery</span>
          <span className={`ep__status ep__status--${STATUS_TONE[status] ?? "muted"}`}>
            {sentBack ? "sent back" : status}
          </span>
          {sentBack && latest?.review_note ? (
            <span className="ep__warn">“{latest.review_note}”</span>
          ) : null}
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
            {t.key === "delivery" && subs.length ? (
              <span className="pagetabs__count">{subs.length}</span>
            ) : null}
          </button>
        ))}
      </div>

      <div className="ep__body">
        {tab === "sequences" ? (
          <Sequences
            shots={shots}
            projectId={projectId}
            sceneId={sceneId}
            canCreate={can("sequence.create")}
            canDelete={can("sequence.delete")}
            busy={busy}
            onMutate={async (fn) => {
              setBusy(true);
              setError(null);
              try {
                await fn();
                await load();
              } catch (e) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          />
        ) : null}
        {tab === "delivery" ? <Delivery subs={subs} /> : null}
        {tab === "spend" ? (
          <Spend budget={budget} sceneId={sceneId} isAdmin={isAdmin} />
        ) : null}
        {tab === "history" ? <History entries={history} /> : null}
      </div>
    </div>
  );
}

// ── sections ────────────────────────────────────────────────────────────────

function Sequences({
  shots,
  projectId,
  sceneId,
  canCreate,
  canDelete,
  busy,
  onMutate,
}: {
  shots: ShotDTO[];
  projectId: string;
  sceneId: string;
  canCreate: boolean;
  canDelete: boolean;
  busy: boolean;
  onMutate: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  return (
    <>
      {shots.length === 0 ? (
        <p className="rfoot">
          No sequences yet. An episode is split into sequences, and each one is
          storyboarded on the canvas.
        </p>
      ) : (
        <ul className="ep__seqs">
          {shots
            .slice()
            .sort((a, b) => a.order_index - b.order_index)
            .map((q, i) => (
              <li key={q.id} className="ep__seq-wrap">
                <Link
                  to={`/projects/${projectId}/scenes/${sceneId}`}
                  className="ep__seq"
                  title="Open in the canvas"
                >
                  <span className="ep__seq-code">
                    {q.code || `SQ${String(i + 1).padStart(2, "0")}`}
                  </span>
                  <span className="ep__seq-status">{q.status || "idle"}</span>
                </Link>
                {canDelete ? (
                  <button
                    className="ep__seq-del"
                    disabled={busy}
                    aria-label={`Delete ${q.code || "sequence"}`}
                    title="Delete this sequence"
                    onClick={() => void onMutate(() => deleteShot(q.id))}
                  >
                    ✕
                  </button>
                ) : null}
              </li>
            ))}
        </ul>
      )}
      {canCreate ? (
        <button
          className="btn2 btn2--ghost ep__seq-add"
          disabled={busy}
          onClick={() =>
            void onMutate(async () => {
              // eslint-disable-next-line no-alert
              const code = window.prompt("Sequence code (optional)", "") ?? "";
              await createShot(sceneId, { code: code.trim() });
            })
          }
        >
          + Sequence
        </button>
      ) : null}
    </>
  );
}

function Delivery({ subs }: { subs: SubmissionDTO[] }) {
  if (subs.length === 0) {
    return (
      <p className="rfoot">
        Nothing handed in yet. The assignee submits the finished cut as a Drive link.
      </p>
    );
  }
  return (
    <ol className="ep__subs">
      {subs.map((s) => (
        <li key={s.id} className={`ep__sub ep__sub--${s.status}`}>
          <div className="ep__sub-head">
            <b>v{s.version}</b>
            <span className={`ep__status ep__status--${
              s.status === "approved" ? "good" : s.status === "rejected" ? "bad" : "warn"
            }`}>
              {s.status}
            </span>
            <span className="ep__sub-who">
              {s.submitted_by_name ?? "someone"} · {fmt(s.submitted_at)}
            </span>
            {s.stream_url ? (
              <a className="ep__sub-play" href={s.stream_url} target="_blank" rel="noreferrer">
                Watch
              </a>
            ) : null}
          </div>
          {s.note ? <p className="ep__sub-note">“{s.note}”</p> : null}
          {s.review_note ? (
            <p className="ep__sub-verdict">
              <b>{s.reviewed_by_name ?? "Reviewer"}:</b> {s.review_note}
            </p>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function Spend({
  budget,
  sceneId,
  isAdmin,
}: {
  budget: BudgetSummaryDTO | null;
  sceneId: string;
  isAdmin: boolean;
}) {
  if (!budget) return <p className="rfoot">Spend isn’t available to you here.</p>;
  const used = budget.used_usd ?? 0;
  return (
    <>
      <div className="stats">
        <div className="stat">
          <div className="stat__label">Spent</div>
          <div className="stat__value">${(budget.spent_usd ?? 0).toFixed(2)}</div>
        </div>
        <div className="stat">
          <div className="stat__label">Ceiling</div>
          <div className="stat__value">
            {budget.unlimited ? "∞" : `$${budget.effective_usd.toFixed(2)}`}
          </div>
        </div>
        <div className={`stat${!budget.unlimited && (budget.remaining_usd ?? 0) <= 0 ? " stat--alert" : ""}`}>
          <div className="stat__label">Remaining</div>
          <div className="stat__value">
            {budget.unlimited ? "—" : `$${(budget.remaining_usd ?? 0).toFixed(2)}`}
          </div>
        </div>
        {budget.reserved_usd ? (
          <div className="stat">
            <div className="stat__label">In flight</div>
            <div className="stat__value">${budget.reserved_usd.toFixed(2)}</div>
          </div>
        ) : null}
      </div>
      {/* The per-generation detail lives in one place (the ledger) rather than being
          re-implemented here; this links into it pre-filtered. Admin-only, because
          that ledger is company-wide money. */}
      {isAdmin ? (
        <p className="rfoot">
          <Link to={`/admin?tab=ledger&scene_id=${sceneId}`}>
            See every generation for this episode →
          </Link>{" "}
          — who ran it, which model, and whether it shipped or was re-rolled.
        </p>
      ) : (
        <p className="rfoot">
          {used > 0
            ? "This is settled spend from generations in this episode's sequences."
            : "Nothing generated in this episode yet."}
        </p>
      )}
    </>
  );
}

function History({ entries }: { entries: HistoryEntryDTO[] }) {
  if (entries.length === 0) {
    return <p className="rfoot">Nothing recorded yet — changes from here on show up.</p>;
  }
  return (
    <ol className="ep__hist">
      {entries.map((e) => (
        <li key={e.id}>
          <span className="ep__hist-when">{fmt(e.created_at)}</span>
          <span className="ep__hist-action">{e.action.replace(/[._]/g, " ")}</span>
          <span className="ep__hist-who">{e.actor ?? "system"}</span>
          {e.detail ? <span className="ep__hist-detail">{e.detail}</span> : null}
        </li>
      ))}
    </ol>
  );
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

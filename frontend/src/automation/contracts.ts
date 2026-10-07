/** Evidence from the source video and the assets a production must cover. */
export type AssetKind = "character" | "background_group" | "prop" | "environment";

export interface ProductionAsset {
  id: string;
  kind: AssetKind;
  name: string;
  description: string;
  role?: string;
  source_name?: string;
  production_key?: string;
  production_name?: string;
  reference_required: boolean;
  member_ids?: string[];
  depends_on_asset_ids?: string[];
  evidence_ids?: string[];
}

export interface AssetPresence {
  asset_id: string;
  source_shot?: number;
  visibility: "visible" | "partial" | "occluded" | "offscreen" | "uncertain";
  position?: string;
  state?: string;
  holder_id?: string;
  hand?: string;
  contains_ids?: string[];
  evidence_ids?: string[];
}

export interface SceneInventory {
  schema_version: number;
  assets: ProductionAsset[];
  scenes: { id: string; shot_ids: number[]; present_asset_ids: string[] }[];
  shots: Record<string, { scene_id: string; asset_presence: AssetPresence[]; evidence_ids?: string[] }>;
}

export type SourceIssueCategory = "technical" | "visual" | "uncertainty";

export interface SourceIssueSummary {
  input_findings: number;
  active_issues: number;
  duplicates_collapsed: number;
  by_category: Record<SourceIssueCategory, number>;
  processed_shots: number[];
  retained_verified_shots: number[];
  corrected_shots: number[];
  corrected_and_verified_shots: number[];
  verified_shots: number[];
  unresolved_shots: number[];
  technical_actions: number;
}

export interface SourceVerification {
  status: "verified" | "needs_review" | "unverified" | "legacy" | "observed";
  method?: string;
  structural_checks_passed?: boolean;
  prepared_shots?: number[];
  observation_only?: boolean;
  independent_review?: boolean;
  locked_shots?: Record<string, Record<string, unknown>>;
  /** Server seal of authored production intent; not source-video evidence. */
  signature?: string;
  project_id?: string;
  reviewed_shots?: number[];
  unresolved_shots?: number[];
  findings?: { code?: string; message?: string; shot?: number | null; level?: string; accepted?: boolean;
    category?: SourceIssueCategory; provenance?: unknown }[];
  /** Counts describe machine review, independently of a later human acceptance. */
  issue_summary?: SourceIssueSummary;
  /** Informational limits of source-frame verification; never an unresolved finding. */
  scope_notes?: { code?: string; message?: string; shot?: number | null }[];
  evidence?: { id: string; shot: number; frame: string; timestamp_s: number }[];
  trace?: unknown[];
  digest?: string;
  scope?: string;
  inventory_digest?: string;
  shot_digests?: Record<string, string>;
  asset_digests?: Record<string, string>;
  /** A person's acceptance of what the verifier left unresolved. */
  review?: { accepted_by: string; accepted_at: string; note?: string; machine_status?: string;
    accepted_shots?: number[] };
}

export interface PromptCoverage {
  status: string;
  requirements?: { id: string; shot?: number; kind?: string }[];
  matches?: { requirement_id: string; shot?: number; quote?: string }[];
  findings?: { message?: string; code?: string }[];
  semantic_review?: { status: string; findings?: { message?: string; code?: string }[] };
  verification_method?: string;
}

export interface ReferenceAsset {
  id: string;
  name: string;
  kind: AssetKind;
  description?: string;
  ref_label: string;
  ref_url: string;
  media_id?: string;
}

function canonical(value: unknown): string {
  const sort = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(sort);
    if (item && typeof item === "object") {
      return Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)).map(([key, entry]) => [key, sort(entry)]));
    }
    return item;
  };
  return JSON.stringify(sort(value));
}

/** 64 bits of a fast string hash (two 32-bit lanes). Freshness, not security:
 *  the server's signed receipt is what a generation is checked against. */
function hash64(text: string): string {
  let h1 = 0xdeadbeef ^ text.length;
  let h2 = 0x41c6ce57 ^ text.length;
  for (let i = 0; i < text.length; i++) {
    const ch = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (h2 >>> 0).toString(16).padStart(8, "0") + (h1 >>> 0).toString(16).padStart(8, "0");
}

/** Canonical local freshness key, separate from the server's verified digest.
 * Object key order is irrelevant; arrays deliberately preserve shot/ref order.
 * A hash of the canonical JSON, not the JSON itself: the contract carries the
 * film's whole source report, and one copy of it per clip in every autosave
 * was megabytes of board. */
export function contractFingerprint(value: unknown): string {
  return `h1:${hash64(canonical(value))}`;
}

/** Whether a stored fingerprint still describes this contract. Nodes written
 *  before fingerprints were hashed stored the canonical JSON itself; hashing
 *  it gives the key it would have had. */
export function sameFingerprint(stored: string | undefined, current: string): boolean {
  if (!stored) return false;
  return stored.startsWith("h1:") ? stored === current : `h1:${hash64(stored)}` === current;
}

/** A report that only says the video predates the source inventory. */
export function isLegacyVerification(v: SourceVerification | null | undefined): boolean {
  return Boolean(v && (v.status === "legacy" || (v.findings ?? []).some((f) => f.code === "legacy_analysis")));
}

/** Whether this board's clips need a verified source and a coverage receipt —
 *  the same rule the server applies (prompt_coverage.is_strict). */
export function isStrictBoard(productionAssets: unknown[] | undefined, v: SourceVerification | null | undefined): boolean {
  return (productionAssets?.length ?? 0) > 0 || (Boolean(v) && !isLegacyVerification(v));
}


/** Per-clip source readiness. Preserve the full film report and its findings;
 * unrelated pending shots do not prevent a verified clip from being written.
 * This mirrors prompt_coverage.source_readiness_issues, not a human approval. */
export function sourceReadyForShots(
  shots: { source_shots?: number[]; source_shot?: number; id?: string; provenance?: string }[],
  report: SourceVerification | null | undefined,
): boolean {
  if (report?.method === "one_pass_production") {
    // Observation + structural readiness, never independent visual verification.
    // The server also validates the exact source locks and inventory digests.
    if (report.status !== "observed" || !report.structural_checks_passed || !report.digest || !shots.length) return false;
    const prepared = new Set((report.prepared_shots ?? []).map(String));
    const selected = new Set<string>();
    for (const shot of shots) {
      const sources = shot.source_shots?.length ? shot.source_shots
        : shot.source_shot === undefined ? [] : [shot.source_shot];
      if (sources.length !== 1 || !prepared.has(String(sources[0]))) return false;
      selected.add(String(sources[0]));
    }
    return !(report.findings ?? []).some((f) => !f || f.shot == null || selected.has(String(f.shot)));
  }
  if (report?.method === "authored_script") {
    // Display readiness only. The server verifies the signature, current
    // project and exact shot/asset digests before reviewing or generating.
    return report.status === "verified" && Boolean(report.digest && report.signature)
      && shots.length > 0 && shots.every((shot) => shot.provenance === "authored_adaptation"
        && Boolean(shot.id && report.shot_digests?.[shot.id]));
  }
  if (!report || !["verified", "needs_review"].includes(report.status)
    || report.method !== "source_frames" || !report.digest) return false;
  if (report.status === "needs_review" && !report.scope?.trim()) return false;
  const selected = new Set<string>();
  for (const shot of shots) {
    const sources = shot.source_shots?.length ? shot.source_shots
      : shot.source_shot === undefined ? [] : [shot.source_shot];
    if (!sources.length) return false;
    for (const source of sources) selected.add(String(source));
  }
  if (!selected.size) return false;
  const reviewed = new Set((report.reviewed_shots ?? []).map(String));
  const unresolved = new Set((report.unresolved_shots ?? []).map(String));
  if ([...selected].some((source) => !reviewed.has(source) || unresolved.has(source))) return false;
  const accepted = report.status === "verified" && Boolean(report.review?.accepted_by);
  return !(report.findings ?? []).some((finding) => {
    if (!finding) return true;
    if (finding.accepted === true && accepted) return false;
    return finding.shot == null || !Number.isInteger(Number(finding.shot))
      || Number(finding.shot) <= 0 || selected.has(String(finding.shot));
  });
}

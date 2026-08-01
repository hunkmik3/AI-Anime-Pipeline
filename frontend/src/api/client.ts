export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    throw new Error(await errorMessage(res));
  }
  return res.json() as Promise<T>;
}

/**
 * The reason a request failed, in words.
 *
 * This used to throw `"400 Bad Request"` and drop the body on the floor — so the
 * backend's careful, actionable messages ("paste a Google Drive video link, e.g.
 * …", "v1 of this episode is already waiting for review") never reached anyone.
 * Every error in the app read as a status code.
 *
 * FastAPI puts the reason in `detail`, in three shapes: a plain string, an object
 * with a `message` (the budget gate does this so the UI can also read a code), or
 * an array of validation problems.
 */
async function errorMessage(res: Response): Promise<string> {
  const fallback = `${res.status} ${res.statusText}`.trim() || "request failed";
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return fallback; // not JSON (a proxy error page, an empty 502)
  }
  const detail = (body as { detail?: unknown } | null)?.detail;

  if (typeof detail === "string" && detail.trim()) return detail;

  if (Array.isArray(detail)) {
    // Pydantic validation: name the field, not just "invalid".
    const parts = detail
      .map((d) => {
        const e = d as { loc?: unknown[]; msg?: string };
        const field = Array.isArray(e.loc) ? e.loc.filter((x) => x !== "body").join(".") : "";
        return [field, e.msg].filter(Boolean).join(": ");
      })
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }

  if (detail && typeof detail === "object") {
    const d = detail as { message?: string; code?: string };
    if (d.message) return d.message;
    if (d.code) return d.code;
  }
  return fallback;
}

// Map cryptic Flow / pipeline error tokens to a sentence the user can act on.
// Returns null when the token is unrecognised, so the caller falls through to
// the raw message.
function humanizeBackendError(token: string): string | null {
  const t = token.toLowerCase();
  if (t === "paygate_tier_unknown") {
    return (
      "Flowboard doesn't know your Google Flow plan tier yet — the "
      + "extension hasn't seen a Flow request that exposes it. Open "
      + "https://labs.google/fx/tools/flow in a tab and reload it once, "
      + "then retry. Flowboard refuses to dispatch in this state to "
      + "avoid silently serving Ultra users at the Pro checkpoint."
    );
  }
  if (t === "no_media_id_in_upload_response") {
    return (
      "Google Flow accepted the upload but didn't return a media handle — "
      + "this usually means the image was silently rejected by Flow's "
      + "content filter (logos, watermarks, copyrighted brand imagery). "
      + "Try a different image or download it locally and upload as a file. "
      + "Check the agent terminal for the full Flow response."
    );
  }
  if (t.includes("captcha_failed: no current window")) {
    return (
      "Chrome has no open windows for the extension to attach a Flow tab to. "
      + "Open any Chrome window (or click the extension's '⋯ → Open Flow') "
      + "and retry — Flowboard will reuse the existing window automatically."
    );
  }
  if (t.startsWith("captcha_failed:")) {
    // CAPTCHA failures are rarely the user's fault — surface the underlying
    // reason verbatim but keep the prefix so power-users can grep for it.
    return token;
  }
  if (t.startsWith("public_error_")) {
    // Veo / Imagen content filters are returned verbatim by Flow — these
    // are already self-describing, just prettify the prefix.
    return token.replace(/^PUBLIC_ERROR_/i, "Flow rejected: ").replace(/_/g, " ");
  }
  return null;
}

async function extractErrorMessage(res: Response): Promise<string> {
  let detail: unknown;
  try {
    detail = await res.json();
  } catch {
    try {
      detail = await res.text();
    } catch {
      return `${res.status} ${res.statusText}`;
    }
  }
  const inner =
    typeof detail === "object" && detail !== null && "detail" in detail
      ? (detail as { detail: unknown }).detail
      : detail;
  if (typeof inner === "string" && inner) {
    return humanizeBackendError(inner) ?? inner;
  }
  if (inner && typeof inner === "object") {
    const obj = inner as Record<string, unknown>;
    if (typeof obj.message === "string" && obj.message) {
      return humanizeBackendError(obj.message) ?? obj.message;
    }
    try {
      return JSON.stringify(inner);
    } catch {
      // fall through
    }
  }
  return `${res.status} ${res.statusText}`;
}

export interface WsStats {
  connected: boolean;
  flow_key_present: boolean;
  token_age_s: number | null;
  pending: number;
  request_count: number;
  success_count: number;
  failed_count: number;
  last_error: string | null;
}

export interface HealthResponse {
  ok: boolean;
  extension_connected: boolean;
  ws_stats?: WsStats;
}

export function getHealth() {
  return api<HealthResponse>("/api/health");
}

// ── DTOs ────────────────────────────────────────────────────────────────────

export type NodeType =
  | "character"
  | "image"
  | "video"
  | "prompt"
  | "note"
  | "visual_asset"
  | "storyboard"
  | "script"
  | "bible_ref"
  | "master_shot"
  | "approval_gate"
  | "audio_ref"
  | "video_ref"
  | "seed_audio";
export type NodeStatus = "idle" | "queued" | "running" | "done" | "error" | "partial";

export interface NodeDTO {
  id: number;
  shot_id: string;
  short_id: string;
  type: NodeType;
  x: number;
  y: number;
  w: number;
  h: number;
  data: Record<string, unknown>;
  status: NodeStatus;
  created_at: string;
}

export interface EdgeDTO {
  id: number;
  shot_id: string;
  source_id: number;
  target_id: number;
  kind: string;
  // null when the upstream is single-variant (or the edge hasn't been
  // pinned yet — natural fallback to source.mediaId at dispatch time).
  // 0-based index into the source node's `data.mediaIds[]` when the
  // user has explicitly picked a variant.
  source_variant_idx: number | null;
}

// ── API methods ──────────────────────────────────────────────────────────────

export function createNode(input: {
  shot_id: string;
  type: NodeType;
  x: number;
  y: number;
  data?: object;
}): Promise<NodeDTO> {
  return api<NodeDTO>("/api/nodes", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * Shallow-merge patch for `node.data` — the backend (see
 * agent/flowboard/routes/nodes.py::update_node) merges this dict into
 * the existing JSON column instead of replacing it.
 *
 * Conventions:
 *   - Keys present in the patch override existing values.
 *   - Keys absent from the patch are PRESERVED (this is what the type
 *     guarantees over a wholesale replace).
 *   - A value of `null` is the explicit "delete this key" sentinel.
 *     Use it instead of `undefined` to clear fields like `aiBrief`
 *     after a regen — `undefined` gets dropped by JSON.stringify and
 *     would leave the stale value in place after the merge.
 *   - Merge depth is ONE LEVEL. Nested dict values are wholesale-
 *     replaced, not deep-merged. None of FlowboardNodeData's current
 *     fields nest, so this is a non-issue today; revisit if a future
 *     field stores objects.
 *
 * Pre-merge call sites that built the full `data` from scratch and
 * forgot a sibling field caused a real data-loss regression
 * (`aspectRatio` was wiped on every image gen by the auto-brief
 * patch). Sticking to deltas-only with this type as the contract
 * prevents that whole class of bug.
 */
export type DataPatch = Record<string, unknown>;

export function patchNode(
  id: number,
  patch: Partial<
    Pick<Omit<NodeDTO, "data">, "x" | "y" | "w" | "h" | "status">
  > & { data?: DataPatch },
): Promise<NodeDTO> {
  return api<NodeDTO>(`/api/nodes/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteNode(id: number): Promise<{ ok: true; deleted_edges: number[] }> {
  return api<{ ok: true; deleted_edges: number[] }>(`/api/nodes/${id}`, {
    method: "DELETE",
  });
}

export function createEdge(input: {
  shot_id: string;
  source_id: number;
  target_id: number;
  kind?: string;
  source_variant_idx?: number | null;
}): Promise<EdgeDTO> {
  return api<EdgeDTO>("/api/edges", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * Update an edge's variant pin without recreating it. Pass
 * `source_variant_idx: null` explicitly to clear the pin (revert to
 * the source's active mediaId at dispatch time). Omit the field to
 * leave it untouched.
 */
export function patchEdge(
  id: number,
  patch: { source_variant_idx?: number | null },
): Promise<EdgeDTO> {
  return api<EdgeDTO>(`/api/edges/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteEdge(id: number): Promise<{ ok: true }> {
  return api<{ ok: true }>(`/api/edges/${id}`, {
    method: "DELETE",
  });
}

// ── Chat ─────────────────────────────────────────────────────────────────────

export type ChatRole = "user" | "assistant" | "system";

export interface ChatMessageDTO {
  id: number;
  project_id: string;
  role: ChatRole;
  content: string;
  mentions: string[];
  created_at: string;
}

export interface PlanDTO {
  id: number;
  shot_id: string;
  spec: {
    nodes: Array<{ tmp_id?: string; type: string; params?: Record<string, unknown> }>;
    edges: Array<{ from: string; to: string; kind?: string }>;
    layout_hint?: string;
  };
  status: "draft" | "approved" | "running" | "done" | "failed";
  created_at: string;
}

export interface ChatSendResponse {
  user: ChatMessageDTO;
  assistant: ChatMessageDTO;
  plan?: PlanDTO;
}

// Phase 3: chat is dead code (App.tsx hides ChatSidebar). Signatures
// updated to UUID strings to match the post-Phase-2 backend so this file
// type-checks; a real chat rebuild ships in a later phase.
export function listChatMessages(projectId: string) {
  return api<ChatMessageDTO[]>(`/api/projects/${projectId}/chat`);
}

export function sendChatMessage(
  projectId: string,
  message: string,
  mentions: string[],
) {
  return api<ChatSendResponse>("/api/chat", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, message, mentions }),
  });
}

// ── Generation ───────────────────────────────────────────────────────────────

export interface BoardProject {
  flow_project_id: string;
  created: boolean;
}

export interface RequestDTO {
  id: number;
  node_id: number | null;
  type: string;
  params: Record<string, unknown>;
  // 'canceled' = user cancelled the request from the activity bell.
  // 'timeout' = backend's 5-minute video-gen budget elapsed; the row
  // self-transitions out of running. Both are terminal states.
  status: "queued" | "running" | "done" | "failed" | "canceled" | "timeout";
  result: Record<string, unknown>;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export function ensureProjectFlowProject(projectId: string) {
  return api<BoardProject>(`/api/projects/${projectId}/flow-project`, {
    method: "POST",
  });
}

export function getProjectFlowProject(projectId: string) {
  return api<BoardProject>(`/api/projects/${projectId}/flow-project`).catch(
    () => null,
  );
}

// ── Auth / profile ───────────────────────────────────────────────────────

export interface AuthMe {
  // Each field is null until the extension resolves the Bearer token
  // against Google's userinfo endpoint and pushes the profile to agent.
  email: string | null;
  name: string | null;
  picture: string | null;
  verified_email: boolean | null;
  // Paygate tier — primary source is the agent's own /v1/credits fetch
  // triggered when the extension pushes a Bearer token. Falls back to
  // the legacy passive sniff (extension reading userPaygateTier out of
  // outgoing Flow request bodies) if the agent fetch fails.
  paygate_tier: "PAYGATE_TIER_ONE" | "PAYGATE_TIER_TWO" | null;
  // Subscription SKU from /v1/credits — e.g. "WS_ULTRA" / "WS_PRO".
  // Available alongside paygate_tier; null until the credits fetch lands.
  sku: string | null;
  // Subscription credits remaining — bonus info from /v1/credits.
  // Frontend can display under the tier badge if desired.
  credits: number | null;
}

export function getAuthMe() {
  return api<AuthMe>("/api/auth/me").catch(() => null);
}

export interface AuthLogoutResult {
  ok: boolean;
  // Whether the agent could push a `logout` message to the extension
  // over its open WebSocket. False when no extension is connected —
  // agent-side caches were still cleared so the dashboard reflects
  // the logged-out state immediately.
  extension_notified: boolean;
}

export function logoutExtension() {
  return api<AuthLogoutResult>("/api/auth/logout", { method: "POST" });
}

export interface AuthScanResult {
  // True when the extension WebSocket is currently connected to the
  // agent. False means the user must install / enable / open Chrome.
  extension_connected: boolean;
  has_user_info: boolean;
  has_paygate_tier: boolean;
  // True when the agent had to ask the extension to re-fetch userinfo
  // (i.e. WS open but cache empty). Backend sets this only in that
  // narrow case; otherwise false.
  userinfo_nudged: boolean;
  // True when the agent successfully resolved tier from /v1/credits
  // during this scan call. False if the call failed (token expired,
  // network error, etc.) or if tier was already cached.
  tier_fetched: boolean;
}

export function scanExtension() {
  return api<AuthScanResult>("/api/auth/scan", { method: "POST" });
}

export function createRequest(body: {
  type: string;
  node_id?: number;
  params: Record<string, unknown>;
}) {
  return api<RequestDTO>("/api/requests", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getRequest(id: number) {
  return api<RequestDTO>(`/api/requests/${id}`);
}

// ── Plans + Pipeline runs ────────────────────────────────────────────────────

export interface PipelineRunDTO {
  id: number;
  plan_id: number;
  status: "pending" | "running" | "done" | "failed";
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export function getPlan(planId: number) {
  return api<PlanDTO>(`/api/plans/${planId}`);
}

export function runPlan(planId: number) {
  return api<PipelineRunDTO>(`/api/plans/${planId}/run`, { method: "POST" });
}

export function getPipelineRun(runId: number) {
  return api<PipelineRunDTO>(`/api/pipeline-runs/${runId}`);
}

// ── Media ────────────────────────────────────────────────────────────────────

export interface MediaStatus {
  available: boolean;
  has_url: boolean;
  mime?: string;
  reason?: string;
}

export function getMediaStatus(mediaId: string): Promise<MediaStatus> {
  const clean = mediaId.replace(/^media\//, "");
  return api<MediaStatus>(`/api/media/${encodeURIComponent(clean)}/status`);
}

export function mediaUrl(mediaId: string): string {
  const clean = mediaId.replace(/^media\//, "");
  return `/media/${encodeURIComponent(clean)}`;
}

/** Downscaled thumbnail (cached WEBP) — use for grids/pickers so we don't ship
 *  multi-MB full-res images for tiny tiles. Falls back to the original on the
 *  server for non-images. */
export function thumbUrl(mediaId: string, w = 256): string {
  const clean = mediaId.replace(/^media\//, "");
  return `/api/media/${encodeURIComponent(clean)}/thumb?w=${w}`;
}

// ── Upload ───────────────────────────────────────────────────────────────────

export interface UploadResponse {
  media_id: string;
  mime: string;
  size: number;
  // Detected by the agent from the image bytes; one of
  // IMAGE_ASPECT_RATIO_{SQUARE,PORTRAIT,LANDSCAPE}. Optional because legacy
  // responses (or formats we couldn't sniff) skip the field.
  aspect_ratio?: string;
  width?: number;
  height?: number;
}

export async function uploadImage(
  file: File,
  projectId: string,
  nodeId?: number,
): Promise<UploadResponse> {
  const form = new FormData();
  form.append("project_id", projectId);
  if (nodeId !== undefined) form.append("node_id", String(nodeId));
  form.append("file", file);

  // Don't set Content-Type — the browser sets it with the correct boundary.
  const res = await fetch("/api/upload", { method: "POST", body: form });
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return res.json() as Promise<UploadResponse>;
}

// ── Frame extraction (Phase 8.4 — continuity) ────────────────────────────────

export interface ExtractFrameResponse {
  media_id: string;
  asset_id: number;
  time: number;
  duration: number;
  width: number;
  height: number;
  mime: string;
}

/**
 * Extract a still frame from a generated video at `time` seconds. The frame
 * becomes a new image media_id (kind=image) that can drive the next shot's
 * first_frame (i2v) for continuity.
 */
export async function extractFrame(
  mediaId: string,
  opts: { time: number; shotId?: string; requestId?: number },
): Promise<ExtractFrameResponse> {
  const clean = mediaId.replace(/^media\//, "");
  const res = await fetch(`/api/media/${encodeURIComponent(clean)}/extract-frame`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      time: opts.time,
      shot_id: opts.shotId,
      request_id: opts.requestId,
    }),
  });
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return res.json() as Promise<ExtractFrameResponse>;
}

export interface AudioUploadResponse {
  media_id: string;
  mime: string;
  size: number;
}

/**
 * Upload an audio reference (Phase 7 — Seedance 2.0 reference_audio).
 * Cached locally and mirrored to R2 on video submit; not pushed to Flow.
 */
export async function uploadAudio(
  file: File,
  projectId: string,
  nodeId?: number,
): Promise<AudioUploadResponse> {
  const form = new FormData();
  form.append("project_id", projectId);
  if (nodeId !== undefined) form.append("node_id", String(nodeId));
  form.append("file", file);
  const res = await fetch("/api/upload-audio", { method: "POST", body: form });
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return res.json() as Promise<AudioUploadResponse>;
}

/**
 * Upload a reference video (Phase 8.1.5d — Seedance 2.0 reference_video,
 * contract §11.9). Cached locally + mirrored to R2 on video submit.
 */
export async function uploadVideo(
  file: File,
  projectId: string,
  nodeId?: number,
): Promise<AudioUploadResponse> {
  const form = new FormData();
  form.append("project_id", projectId);
  if (nodeId !== undefined) form.append("node_id", String(nodeId));
  form.append("file", file);
  const res = await fetch("/api/upload-video", { method: "POST", body: form });
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return res.json() as Promise<AudioUploadResponse>;
}

export interface VisionDescribeResponse {
  media_id: string;
  description: string;
}

export interface AutoPromptResponse {
  node_id: number;
  prompt: string;
}

export interface AutoPromptBatchResponse {
  node_id: number;
  prompts: string[];
}

export async function autoPromptBatch(
  nodeId: number,
  count: number,
  opts?: { camera?: string },
): Promise<AutoPromptBatchResponse> {
  const res = await fetch("/api/prompt/auto-batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_id: nodeId, count, camera: opts?.camera }),
  });
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return res.json() as Promise<AutoPromptBatchResponse>;
}

export async function autoPrompt(
  nodeId: number,
  opts?: { camera?: string },
): Promise<AutoPromptResponse> {
  const res = await fetch("/api/prompt/auto", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_id: nodeId, camera: opts?.camera }),
  });
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return res.json() as Promise<AutoPromptResponse>;
}

export interface ParsedShot {
  order: number;
  script_text: string;
  camera_angle: string;
  characters_in_frame: string[];
  environment: string;
  dialogue: string | null;
  beat_notes: string;
}

export interface ParseScriptResponse {
  scene_id: string;
  shots: ParsedShot[];
}

/**
 * Phase 6.4. Break a (Vietnamese-or-any-language) scene script into
 * structured shot breakdowns via the configured Auto-Prompt provider.
 * The LLM preserves `script_text` verbatim in the source language and
 * emits meta fields (camera, environment, beat notes) in English.
 */
export async function parseScript(
  sceneId: string,
  scriptText: string,
): Promise<ParseScriptResponse> {
  const res = await fetch("/api/prompt/parse-script", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scene_id: sceneId, script_text: scriptText }),
  });
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return res.json() as Promise<ParseScriptResponse>;
}

export async function describeMedia(mediaId: string): Promise<VisionDescribeResponse> {
  const res = await fetch("/api/vision/describe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ media_id: mediaId }),
  });
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return res.json() as Promise<VisionDescribeResponse>;
}

export async function uploadImageFromUrl(
  url: string,
  projectId: string,
  nodeId?: number,
): Promise<UploadResponse> {
  const res = await fetch("/api/upload-url", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, project_id: projectId, node_id: nodeId }),
  });
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return res.json() as Promise<UploadResponse>;
}


// ── LLM provider Settings ─────────────────────────────────────────────────
// See .omc/plans/multi-llm-provider-legacy.md → UI Specification → Frontend ↔
// backend contract for the full shape.

export type LLMProviderName = "claude" | "gemini" | "openai";
export type LLMFeature = "auto_prompt" | "vision" | "planner";
export type LLMProviderMode = "cli" | "api" | "none";
export type LLMLastError =
  | "not_installed"
  | "not_authenticated"
  | "no_key"
  | "unreachable"
  | "unknown";

export interface LLMProviderInfo {
  name: LLMProviderName;
  supportsVision: boolean;
  available: boolean;
  configured: boolean;
  requiresKey: boolean;
  mode: LLMProviderMode;
  lastError?: LLMLastError;
  lastTest?: { ok: boolean; latencyMs?: number; error?: string };
}

export interface LLMConfig {
  // null when the user hasn't picked a provider for this feature yet.
  // Backend no longer fabricates a default; the forced-setup gate uses
  // `configured` (below) to keep the dialog open until the user chooses.
  auto_prompt: LLMProviderName | null;
  vision: LLMProviderName | null;
  planner: LLMProviderName | null;
  // True only when all 3 features are pinned at the same provider —
  // the single-provider UI invariant. Drives the forced-setup dialog.
  configured: boolean;
}

export async function getLlmProviders(): Promise<LLMProviderInfo[]> {
  // Backend returns snake-case keys mapped from Python — but the route
  // already emits camelCase for the public surface. Re-typed here so
  // the spread/destructure pattern in the UI components stays clean.
  const res = await fetch("/api/llm/providers");
  if (!res.ok) throw new Error(`getLlmProviders: ${res.status}`);
  return res.json() as Promise<LLMProviderInfo[]>;
}

export async function getLlmConfig(): Promise<LLMConfig> {
  const res = await fetch("/api/llm/config");
  if (!res.ok) throw new Error(`getLlmConfig: ${res.status}`);
  return res.json() as Promise<LLMConfig>;
}

export async function setLlmConfig(
  partial: Partial<LLMConfig>,
): Promise<{ ok: boolean }> {
  const res = await fetch("/api/llm/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(partial),
  });
  if (!res.ok) throw new Error(await extractErrorMessage(res));
  return res.json();
}

export async function setLlmApiKey(
  name: LLMProviderName,
  apiKey: string | null,
): Promise<{ ok: boolean }> {
  // null clears the key. Backend chmods secrets.json to 0o600 after
  // every write; the key is never echoed back via getLlmProviders.
  const res = await fetch(`/api/llm/providers/${name}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ apiKey }),
  });
  if (!res.ok) throw new Error(await extractErrorMessage(res));
  return res.json();
}

export interface LlmTestResult {
  ok: boolean;
  latencyMs?: number;
  error?: string;
}

export async function testLlmProvider(
  name: LLMProviderName,
): Promise<LlmTestResult> {
  // Cost-bounded by the backend: 1-token ping, 15s deadline. Returns
  // ok:false (NOT a non-200 HTTP status) on any failure mode so the
  // UI can render the error inline without try/catch boilerplate.
  const res = await fetch(`/api/llm/providers/${name}/test`, { method: "POST" });
  if (!res.ok) {
    return { ok: false, error: `HTTP ${res.status}` };
  }
  return res.json();
}


// ── Activity feed ─────────────────────────────────────────────────────────
// Read-only surface over the Request table. Captures every backend op:
// gen_image / gen_video / edit_image (worker), auto_prompt /
// auto_prompt_batch / vision / planner (LLM layer via record_activity).

export type ActivityType =
  | "auto_prompt" | "auto_prompt_batch"
  | "vision" | "planner"
  | "gen_image" | "gen_video" | "edit_image"
  | "upload" | "upload_url";
export type ActivityStatus = "queued" | "running" | "done" | "failed";

export interface ActivityListItem {
  id: number;
  type: ActivityType | string; // string fallback for forward-compat
  status: ActivityStatus | string;
  node_id: number | null;
  node_short_id: string | null;
  created_at: string;
  finished_at: string | null;
  duration_ms: number | null;
}

export interface ActivityDetail extends ActivityListItem {
  params: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string | null;
}

export async function getActivityList(opts?: {
  limit?: number;
  beforeId?: number;
  type?: string[];
}): Promise<{ items: ActivityListItem[]; next_before_id: number | null }> {
  const search = new URLSearchParams();
  if (opts?.limit) search.set("limit", String(opts.limit));
  if (opts?.beforeId) search.set("before_id", String(opts.beforeId));
  if (opts?.type && opts.type.length > 0) search.set("type", opts.type.join(","));
  const q = search.toString();
  const res = await fetch(`/api/activity${q ? `?${q}` : ""}`);
  if (!res.ok) throw new Error(`getActivityList: ${res.status}`);
  return res.json();
}

export async function getActivityDetail(id: number): Promise<ActivityDetail> {
  const res = await fetch(`/api/activity/${id}`);
  if (!res.ok) throw new Error(`getActivityDetail: ${res.status}`);
  return res.json();
}

// Cancel a queued or running request. The activity row id IS the
// underlying Request.id, so the same numeric handle works against
// /api/requests. Backend returns 409 when the row has already settled
// (done/failed/timeout/canceled).
export async function cancelActivity(id: number): Promise<void> {
  const res = await fetch(`/api/requests/${id}/cancel`, { method: "POST" });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`cancelActivity: ${res.status} ${detail}`);
  }
}


// ── References ───────────────────────────────────────────────────────────
// User-curated cross-board library of saved media. Backend mirror:
// agent/flowboard/routes/references.py + db.models.Reference.
// JSON wire format is snake_case (mirrors SQLModel column names);
// camelCase is reserved for the TS surface, so each helper maps the
// rows on the way back.

export interface ReferenceItem {
  id: number;
  mediaId: string;
  // Best-effort signed CDN URL captured at save time. May expire — the
  // canonical bytes live in storage/media/{mediaId}.{ext}; this field
  // exists purely as a re-ingest hint when the file goes missing.
  url: string | null;
  label: string;
  kind: "image" | "character" | "visual_asset" | "storyboard_shot";
  // Snapshot of the source node's aiBrief at save time; lets cross-board
  // spawn skip the re-vision call entirely.
  aiBrief: string | null;
  aspectRatio: string | null;
  projectId: string | null;
  // Flow Studio (/giantflow) groups its images by its own board rather than by
  // project, because it is not wired into the hierarchy yet.
  sourceBoardId: number | null;
  tags: string[];
  pinned: boolean;
  position: number;
  sourceShotId: string | null;
  sourceNodeShortId: string | null;
  createdAt: string;
}

// Wire-shape POST body — snake_case to match the FastAPI schema 1:1.
export interface ReferenceCreateInput {
  media_id: string;
  kind: ReferenceItem["kind"];
  label?: string;
  ai_brief?: string | null;
  aspect_ratio?: string | null;
  url?: string | null;
  project_id?: string | null;
  source_shot_id?: string | null;
  source_board_id?: number | null;
  source_node_short_id?: string | null;
  tags?: string[];
}

// Wire-shape PATCH body. Same snake_case convention.
export interface ReferencePatchInput {
  label?: string;
  pinned?: boolean;
  position?: number;
  tags?: string[];
}

interface ReferenceRowWire {
  id: number;
  media_id: string;
  url: string | null;
  label: string;
  kind: string;
  ai_brief: string | null;
  aspect_ratio: string | null;
  project_id?: string | null;
  tags: string[] | null;
  pinned: boolean;
  position: number;
  source_shot_id: string | null;
  source_board_id?: number | null;
  source_node_short_id: string | null;
  created_at: string;
}

function mapReferenceRow(row: ReferenceRowWire): ReferenceItem {
  // Coerce the kind string into the typed union — the backend already
  // validates against _ALLOWED_KINDS so any unknown value here would
  // mean a backend bug. Fall back to "image" defensively rather than
  // throwing, so a single bad row doesn't break the whole list render.
  const allowed: ReferenceItem["kind"][] = [
    "image",
    "character",
    "visual_asset",
    "storyboard_shot",
  ];
  const kind: ReferenceItem["kind"] = (allowed as string[]).includes(row.kind)
    ? (row.kind as ReferenceItem["kind"])
    : "image";
  return {
    id: row.id,
    mediaId: row.media_id,
    url: row.url,
    label: row.label,
    kind,
    aiBrief: row.ai_brief,
    aspectRatio: row.aspect_ratio,
    projectId: row.project_id ?? null,
    tags: Array.isArray(row.tags) ? row.tags : [],
    pinned: row.pinned,
    position: row.position,
    sourceShotId: row.source_shot_id,
    sourceBoardId: row.source_board_id ?? null,
    sourceNodeShortId: row.source_node_short_id,
    createdAt: row.created_at,
  };
}

export async function listReferences(params?: {
  q?: string;
  project_id?: string;
  source_board_id?: number;
  pinned_first?: boolean;
  limit?: number;
}): Promise<ReferenceItem[]> {
  const search = new URLSearchParams();
  if (params?.q) search.set("q", params.q);
  if (params?.project_id) search.set("project_id", params.project_id);
  // Not `if (…)` — board 0 is falsy but the ids start at 1, and an explicit
  // undefined check keeps it honest if that ever changes.
  if (params?.source_board_id !== undefined) {
    search.set("source_board_id", String(params.source_board_id));
  }
  if (params?.pinned_first !== undefined) {
    search.set("pinned_first", String(params.pinned_first));
  }
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  const rows = await api<ReferenceRowWire[]>(
    `/api/references${qs ? `?${qs}` : ""}`,
  );
  return rows.map(mapReferenceRow);
}

export async function createReference(
  input: ReferenceCreateInput,
): Promise<ReferenceItem> {
  const row = await api<ReferenceRowWire>("/api/references", {
    method: "POST",
    body: JSON.stringify(input),
  });
  return mapReferenceRow(row);
}

export async function patchReference(
  id: number,
  patch: ReferencePatchInput,
): Promise<ReferenceItem> {
  const row = await api<ReferenceRowWire>(`/api/references/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  return mapReferenceRow(row);
}

export async function deleteReference(id: number): Promise<void> {
  // Backend returns 204 No Content; api<T>() would choke on the empty
  // body, so we use fetch() directly and skip the JSON parse.
  const res = await fetch(`/api/references/${id}`, { method: "DELETE" });
  if (!res.ok) {
    throw new Error(await errorMessage(res));
  }
}


// ── Phase 3: Project / Scene / Shot / Bible ──────────────────────────────
// Mirrors the new REST surface in agent/flowboard/routes/projects.py,
// scenes.py, shots.py, bibles.py. UUIDs travel as strings end-to-end;
// numeric ids only exist on Asset/Node/Edge/Reference (those still use
// SQLModel int PKs).

export interface ProjectBible {
  art_style: string;
  color_palette: string[];
  line_style: string;
  lighting_conventions: string;
  negative_prompts: string[];
  style_anchor_asset_ids: number[];
}

export const EMPTY_PROJECT_BIBLE: ProjectBible = {
  art_style: "",
  color_palette: [],
  line_style: "",
  lighting_conventions: "",
  negative_prompts: [],
  style_anchor_asset_ids: [],
};

/** Phase 10: per-project role. The owner is a producer implicitly. */
export type ProjectRole = "admin" | "producer" | "lead" | "artist" | "viewer";

/** Phase 10: capability keys the backend reports in `ProjectDTO.can`.
 *  The UI reads these to hide (not just 403) what the caller can't do. */
export type ProjectCapability =
  | "series.create"
  | "series.update"
  | "series.delete"
  | "episode.create"
  | "episode.update"
  | "episode.delete"
  | "sequence.create"
  | "sequence.update"
  | "sequence.delete"
  | "canvas.write"
  | "canvas.read"
  | "project.decorate"
  | "member.manage"
  | "project.manage";

export interface ProjectDTO {
  id: string;
  name: string;
  project_bible: Partial<ProjectBible>;
  settings: Record<string, unknown>;
  created_at: string | null;
  /** Cover thumbnail media id (admin override else latest image); null if none. */
  thumb_media_id?: string | null;
  owner_user_id?: string | null;
  owner_name?: string | null;
  /** Everyone assigned (owner first), and their role. */
  assignee_ids?: string[];
  assignee_names?: string[];
  assignee_roles?: Record<string, ProjectRole>;
  /** Phase 10: this caller's role here + the flat can-I map the UI reads. */
  my_role?: ProjectRole | null;
  can?: Partial<Record<ProjectCapability, boolean>>;
  /** Phase 11.1: credit-budget rollup (list/detail only). */
  budget?: BudgetSummaryDTO;
}

// ── Phase 10: Series (Project → Series → Episode/Chapter → Sequence) ────────

export type UnitLabel = "Episode" | "Chapter";

export interface SeriesStats {
  episodes: number;
  by_status: Record<string, number>;
  completion_pct: number;
}

export interface SeriesDTO {
  id: string;
  project_id: string;
  name: string;
  code: string;
  unit_label: UnitLabel | string;
  order_index: number;
  /** Phase 10 CRM: Series_Master production metadata bag. */
  production?: Record<string, string | number>;
  /** Phase 11: the Series Producer — first reviewer in the approver chain. */
  producer_user_id?: string | null;
  producer_name?: string | null;
  /** Live per-status episode rollup (list/detail only). */
  stats?: SeriesStats;
  /** Phase 11.1: credit-budget rollup (list only). */
  budget?: BudgetSummaryDTO;
  created_at: string | null;
  /** Present on list/detail. */
  episode_count?: number;
}

export interface ProjectMemberDTO {
  user_id: string;
  name: string;
  role: ProjectRole;
  is_owner: boolean;
}

export interface ProjectImage {
  media_id: string;
  url: string;
}

export function listProjectImages(projectId: string): Promise<{ images: ProjectImage[] }> {
  return api<{ images: ProjectImage[] }>(`/api/projects/${projectId}/images`);
}

/** Set (media id) or clear (null) a project's cover thumbnail. Owner-scoped. */
export function setProjectCover(projectId: string, mediaId: string | null): Promise<ProjectDTO> {
  return api<ProjectDTO>(`/api/projects/${projectId}/cover`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ media_id: mediaId }),
  });
}

export interface ProjectDetailDTO extends ProjectDTO {
  scene_count: number;
  asset_count: number;
}

// Phase 8.3: a shot's SceneCanvas group metadata (lives in
// scene.canvas_state.shot_groups). Size is auto-fit by React Flow.
export interface ShotGroup {
  shot_id: string;
  position: { x: number; y: number };
  collapsed: boolean;
  label: string;
  order: number;
  // Phase 8.3b — manual frame size; when set, overrides the auto-fit.
  size?: { w: number; h: number };
}

export interface SceneCanvasState {
  shot_groups?: ShotGroup[];
}

export interface SceneDTO {
  id: string;
  project_id: string;
  /** Phase 10: the Series this Episode/Chapter belongs to (null on legacy rows). */
  series_id?: string | null;
  name: string;
  /** Phase 10: human code within the series — "EP007", "CH012". */
  code?: string;
  order_index: number;
  /** Phase 10 CRM: Episode_Tracker production metadata bag. */
  production?: Record<string, string | number>;
  /** Phase 11: the employee who owns this episode — the only person who may
   *  submit it, so nothing can be delivered until this is set. */
  assignee_user_id?: string | null;
  assignee_name?: string | null;
  deliverable_status?: "draft" | "submitted" | "approved" | "paid" | string;
  // Phase 8.3: Scene Bible removed; multi-shot layout lives here.
  canvas_state: SceneCanvasState;
  master_establishing_asset_id: number | null;
  /** Cover thumbnail media id (user/admin-set); null → gradient placeholder. */
  thumb_media_id?: string | null;
  created_at: string | null;
}

/** Set (media id) or clear (null) a scene's cover thumbnail. Owner-scoped. */
export function setSceneCover(sceneId: string, mediaId: string | null): Promise<SceneDTO> {
  return api<SceneDTO>(`/api/scenes/${sceneId}/cover`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ media_id: mediaId }),
  });
}

export interface SceneDetailDTO extends SceneDTO {
  shot_count: number;
}

export type ShotStatus =
  | "idle"
  | "running"
  | "awaiting_approval"
  | "done"
  | "error";

export interface ShotDTO {
  id: string;
  scene_id: string;
  /** Phase 10: human code within the episode/chapter — "SQ03". */
  code?: string;
  order_index: number;
  script_text: string;
  status: ShotStatus | string;
  current_node_id: number | null;
  final_video_asset_id: number | null;
  workflow_metadata: Record<string, unknown>;
  created_at: string | null;
}

// Phase 8.3: Scene Bible text removed; this now carries only the scene's
// master/establishing asset pointer (MasterShot reference flow).
export interface SceneEstablishing {
  master_establishing_asset_id: number | null;
  // Read-only convenience populated by GET — the Asset's uuid_media_id
  // (so the MasterShotNode can show the actual image without a second
  // roundtrip). PUT requests ignore this field.
  master_establishing_media_id?: string | null;
}

// ── Projects ─────────────────────────────────────────────────────────────

export function listProjects(): Promise<ProjectDTO[]> {
  return api<ProjectDTO[]>("/api/projects");
}

export function createProject(input: {
  name: string;
  project_bible?: Partial<ProjectBible>;
  settings?: Record<string, unknown>;
}): Promise<ProjectDTO> {
  return api<ProjectDTO>("/api/projects", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getProject(id: string): Promise<ProjectDetailDTO> {
  return api<ProjectDetailDTO>(`/api/projects/${id}`);
}

export function patchProject(
  id: string,
  patch: { name?: string; settings?: Record<string, unknown> },
): Promise<ProjectDTO> {
  return api<ProjectDTO>(`/api/projects/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteProject(id: string): Promise<{ deleted: string }> {
  return api<{ deleted: string }>(`/api/projects/${id}`, { method: "DELETE" });
}

export function getProjectCost(id: string): Promise<{ cost_usd: number }> {
  return api<{ cost_usd: number }>(`/api/projects/${id}/cost`);
}

export function getProjectBible(id: string): Promise<Partial<ProjectBible>> {
  return api<Partial<ProjectBible>>(`/api/projects/${id}/bible`);
}

export function putProjectBible(
  id: string,
  bible: ProjectBible,
): Promise<Partial<ProjectBible>> {
  return api<Partial<ProjectBible>>(`/api/projects/${id}/bible`, {
    method: "PUT",
    body: JSON.stringify(bible),
  });
}

// ── Scenes ───────────────────────────────────────────────────────────────

export function listScenes(
  projectId: string,
  seriesId?: string,
): Promise<SceneDTO[]> {
  const q = seriesId ? `?series_id=${seriesId}` : "";
  return api<SceneDTO[]>(`/api/projects/${projectId}/scenes${q}`);
}

export function createScene(
  projectId: string,
  input: { name: string; series_id?: string; code?: string; order_index?: number },
): Promise<SceneDTO> {
  return api<SceneDTO>(`/api/projects/${projectId}/scenes`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getScene(id: string): Promise<SceneDetailDTO> {
  return api<SceneDetailDTO>(`/api/scenes/${id}`);
}

export function patchScene(
  id: string,
  patch: {
    name?: string;
    series_id?: string;
    code?: string;
    order_index?: number;
    production?: Record<string, string | number | null>;
  },
): Promise<SceneDTO> {
  return api<SceneDTO>(`/api/scenes/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteScene(id: string): Promise<{ deleted: string }> {
  return api<{ deleted: string }>(`/api/scenes/${id}`, { method: "DELETE" });
}

// ── Phase 10: Series ───────────────────────────────────────────────────────

export function listSeries(projectId: string): Promise<SeriesDTO[]> {
  return api<SeriesDTO[]>(`/api/projects/${projectId}/series`);
}

export function createSeries(
  projectId: string,
  input: {
    name: string;
    code?: string;
    unit_label?: UnitLabel;
    order_index?: number;
    production?: Record<string, string | number | null>;
  },
): Promise<SeriesDTO> {
  return api<SeriesDTO>(`/api/projects/${projectId}/series`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** One series — used by the episode page for its breadcrumb, and by the series
 *  page itself. */
export function getSeries(seriesId: string): Promise<SeriesDTO> {
  return api<SeriesDTO>(`/api/series/${seriesId}`);
}

export function patchSeries(
  id: string,
  patch: {
    name?: string;
    code?: string;
    unit_label?: UnitLabel;
    order_index?: number;
    production?: Record<string, string | number | null>;
  },
): Promise<SeriesDTO> {
  return api<SeriesDTO>(`/api/series/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteSeries(id: string): Promise<{ deleted: string }> {
  return api<{ deleted: string }>(`/api/series/${id}`, { method: "DELETE" });
}

/** Episodes/Chapters under a series (same shape as listScenes). */
export function listSeriesEpisodes(seriesId: string): Promise<SceneDTO[]> {
  return api<SceneDTO[]>(`/api/series/${seriesId}/episodes`);
}

/** Distinct crew names across all episodes — the CRM crew-dropdown pool. */
export function getCrewNames(): Promise<{ names: string[] }> {
  return api<{ names: string[] }>(`/api/production/crew-names`);
}

/** Bulk-plan a series: ensure `episodes` Episodes, each with
 *  `sequences_per_episode` Sequences (idempotent; only creates what's missing). */
export function generateSeriesStructure(
  seriesId: string,
  input: { episodes: number; sequences_per_episode: number },
): Promise<{
  episodes_created: number;
  sequences_created: number;
  total_episodes: number;
  sequences_per_episode: number;
}> {
  return api(`/api/series/${seriesId}/generate-structure`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ── Phase 10: project members + roles ───────────────────────────────────────

export function listProjectMembers(
  projectId: string,
): Promise<{ members: ProjectMemberDTO[]; roles: ProjectRole[] }> {
  return api<{ members: ProjectMemberDTO[]; roles: ProjectRole[] }>(
    `/api/projects/${projectId}/members`,
  );
}

export function setProjectMembers(
  projectId: string,
  members: { user_id: string; role: ProjectRole }[],
): Promise<{ members: ProjectMemberDTO[]; roles: ProjectRole[] }> {
  return api<{ members: ProjectMemberDTO[]; roles: ProjectRole[] }>(
    `/api/projects/${projectId}/members`,
    { method: "PUT", body: JSON.stringify({ members }) },
  );
}

/** Lean id+name list of users a producer may add to this project. */
export function listAssignableUsers(
  projectId: string,
): Promise<{ user_id: string; name: string }[]> {
  return api<{ user_id: string; name: string }[]>(
    `/api/projects/${projectId}/assignable-users`,
  );
}

export function getSceneEstablishing(id: string): Promise<SceneEstablishing> {
  return api<SceneEstablishing>(`/api/scenes/${id}/bible`);
}

export function putSceneEstablishing(
  id: string,
  body: { master_establishing_asset_id: number | null },
): Promise<SceneEstablishing> {
  return api<SceneEstablishing>(`/api/scenes/${id}/bible`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// ── Phase 8.3: multi-shot SceneCanvas ──────────────────────────────────────

export interface SceneCanvasDTO {
  scene_id: string;
  project_id: string;
  shots: {
    id: string;
    order_index: number;
    script_text: string;
    status: string;
  }[];
  nodes: {
    id: number;
    shot_id: string;
    short_id: string;
    type: string;
    x: number;
    y: number;
    data: Record<string, unknown>;
    status: string;
  }[];
  edges: {
    id: number;
    shot_id: string;
    source_id: number;
    target_id: number;
    kind: string;
    source_variant_idx: number | null;
  }[];
  shot_groups: ShotGroup[];
}

export function getSceneCanvas(sceneId: string): Promise<SceneCanvasDTO> {
  return api<SceneCanvasDTO>(`/api/scenes/${sceneId}/canvas`);
}

export function autoMigrateScene(
  sceneId: string,
): Promise<{ scene_id: string; shot_groups: ShotGroup[]; migrated: boolean }> {
  return api(`/api/scenes/${sceneId}/auto-migrate`, { method: "POST" });
}

export function patchShotGroup(
  shotId: string,
  patch: Partial<Omit<ShotGroup, "shot_id">>,
): Promise<ShotGroup> {
  return api<ShotGroup>(`/api/shots/${shotId}/group`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

// Phase 7 stub — returns 501 until composition lands.
export async function composeScene(id: string): Promise<{ ok: true } | { error: string }> {
  const res = await fetch(`/api/scenes/${id}/compose`, { method: "POST" });
  if (res.status === 501) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    return { error: body.detail ?? "scene composition not implemented yet (Phase 7)" };
  }
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res));
  }
  return { ok: true };
}

// ── Shots ────────────────────────────────────────────────────────────────

export function listShots(sceneId: string): Promise<ShotDTO[]> {
  return api<ShotDTO[]>(`/api/scenes/${sceneId}/shots`);
}

export function createShot(
  sceneId: string,
  input: { order_index?: number; script_text?: string; code?: string } = {},
): Promise<ShotDTO> {
  return api<ShotDTO>(`/api/scenes/${sceneId}/shots`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getShot(id: string): Promise<ShotDTO> {
  return api<ShotDTO>(`/api/shots/${id}`);
}

export function patchShot(
  id: string,
  patch: {
    order_index?: number;
    script_text?: string;
    code?: string;
    status?: ShotStatus;
    workflow_metadata?: Record<string, unknown>;
  },
): Promise<ShotDTO> {
  return api<ShotDTO>(`/api/shots/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteShot(id: string): Promise<{ deleted: string }> {
  return api<{ deleted: string }>(`/api/shots/${id}`, { method: "DELETE" });
}

export interface ShotWorkflowResponse {
  nodes: Array<{
    id: number;
    shot_id: string;
    short_id: string;
    type: NodeType;
    x: number;
    y: number;
    w: number;
    h: number;
    data: Record<string, unknown>;
    status: NodeStatus;
    created_at?: string;
  }>;
  edges: Array<{
    id: number;
    shot_id: string;
    source_id: number;
    target_id: number;
    kind: string;
    source_variant_idx: number | null;
  }>;
}

export function getShotWorkflow(id: string): Promise<ShotWorkflowResponse> {
  return api<ShotWorkflowResponse>(`/api/shots/${id}/workflow`);
}

export function putShotWorkflow(
  id: string,
  body: { nodes: unknown[]; edges: unknown[] },
): Promise<ShotWorkflowResponse> {
  return api<ShotWorkflowResponse>(`/api/shots/${id}/workflow`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function runShot(id: string): Promise<ShotDTO> {
  return api<ShotDTO>(`/api/shots/${id}/run`, { method: "POST" });
}

export function cancelShot(id: string): Promise<ShotDTO> {
  return api<ShotDTO>(`/api/shots/${id}/cancel`, { method: "POST" });
}

// ── Video model registry (Phase 5) ─────────────────────────────────────

export interface VideoModelCapability {
  supports_multi_ref: boolean;
  supports_last_frame: boolean;
  supports_audio_toggle: boolean;
  max_refs: number;
  aspect_ratios: string[];
  resolutions: string[];
  durations: number[];
  // Person-driven (KYC) inputs — portrait→video / lip-sync / video-ref.
  // Only the Avis Seedance 2.0 model; the gen dialog shows the KYC toggle.
  supports_kyc?: boolean;
}

export interface VideoModelDTO {
  model_id: string;
  provider: string;
  display_name: string;
  upstream_model_id: string | null;
  capabilities: VideoModelCapability;
}

export interface VideoModelsResponse {
  default_model_id: string;
  models: VideoModelDTO[];
}

// ── Seed Audio 1.0 (BytePlus) — text → full audio scene (voice+music+SFX) ──
export interface SeedAudioResult {
  media_id: string;
  mime: string;
  duration: number | null;
  size: number;
}

export interface SeedAudioParams {
  prompt: string;
  format?: string;          // wav | mp3 | pcm | ogg_opus
  sample_rate?: number;
  speech_rate?: number;     // -50..100 (0 = normal)
  loudness_rate?: number;
  pitch_rate?: number;      // -12..12
  references?: string[];    // ≤3 audio media_ids / public URLs → @audio1..3
  image_ref?: string;       // 1 image media_id / URL (mutually exclusive w/ audio)
  node_id?: number;
}

export function seedAudioAvailable(): Promise<{ available: boolean }> {
  return api<{ available: boolean }>("/api/audio/seed/available");
}

export function generateSeedAudio(body: SeedAudioParams): Promise<SeedAudioResult> {
  return api<SeedAudioResult>("/api/audio/generate", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listVideoModels(): Promise<VideoModelsResponse> {
  return api<VideoModelsResponse>("/api/video/models");
}

/**
 * Record that the user actually downloaded an output. Fire-and-forget — it must
 * never block or fail the download itself. The media route doubles as the
 * preview route, so this explicit ping is the only reliable "was it kept?"
 * signal; it drives the admin cost/waste stats.
 */
export function markDownloaded(mediaId: string, nodeId?: number): void {
  void fetch(`/api/media/${mediaId}/downloaded`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_id: Number.isFinite(nodeId) ? nodeId : null }),
  }).catch(() => {
    /* stats are best-effort */
  });
}

// ── Phase 11: deliverable submission + review ──────────────────────────────

export type DeliverableStatus = "draft" | "submitted" | "approved" | "paid";

export interface SubmissionDTO {
  id: string;
  scene_id: string;
  version: number;
  drive_url: string;
  drive_file_id: string | null;
  /** Preferred: our own proxy (app streams the file using the studio's Drive
   *  identity), so the file can stay Restricted and the reviewer needs no
   *  Google account. Plays in a plain <video>. */
  stream_url: string | null;
  /** Fallback: Drive's own embed, used when the app has no Drive identity. */
  preview_url: string | null;
  note: string | null;
  submitted_by: string | null;
  submitted_by_name: string | null;
  submitted_at: string | null;
  status: "submitted" | "approved" | "rejected";
  approver_user_id: string | null;
  approver_name: string | null;
  reviewed_by_name: string | null;
  reviewed_at: string | null;
  review_note: string | null;
}

export interface DeliverableEpisodeDTO {
  id: string;
  name: string;
  code: string;
  project_id: string;
  project_name: string | null;
  series_id: string | null;
  series_name: string | null;
  series_code: string | null;
  assignee_user_id: string | null;
  assignee_name: string | null;
  deliverable_status: DeliverableStatus | string;
  latest_submission: SubmissionDTO | null;
}

/** Submit the finished cut (a Google Drive link) for an episode. */
export function submitEpisode(
  sceneId: string,
  input: { drive_url: string; note?: string },
): Promise<SubmissionDTO> {
  return api<SubmissionDTO>(`/api/scenes/${sceneId}/submissions`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Full submission history for an episode (newest version first). */
export function listEpisodeSubmissions(
  sceneId: string,
): Promise<{ episode: DeliverableEpisodeDTO; submissions: SubmissionDTO[] }> {
  return api(`/api/scenes/${sceneId}/submissions`);
}

export function approveSubmission(id: string, note?: string): Promise<SubmissionDTO> {
  return api<SubmissionDTO>(`/api/submissions/${id}/approve`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

/** Send work back. A reason is required. */
export function rejectSubmission(id: string, note: string): Promise<SubmissionDTO> {
  return api<SubmissionDTO>(`/api/submissions/${id}/reject`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

/** Episodes assigned to the signed-in employee ("My work"). */
export function listMyEpisodes(): Promise<{ episodes: DeliverableEpisodeDTO[] }> {
  return api(`/api/my/episodes`);
}

/** Submissions waiting on the signed-in reviewer. */
export function listReviewQueue(): Promise<{
  items: { submission: SubmissionDTO; episode: DeliverableEpisodeDTO | null }[];
}> {
  return api(`/api/review/queue`);
}

/** PM assigns the employee who owns (and may submit) an episode. */
export function setEpisodeAssignee(
  sceneId: string,
  userId: string | null,
): Promise<DeliverableEpisodeDTO> {
  return api<DeliverableEpisodeDTO>(`/api/scenes/${sceneId}/assignee`, {
    method: "PATCH",
    body: JSON.stringify({ user_id: userId }),
  });
}

/** PM sets the Series Producer (first reviewer in the approver chain). */
export function setSeriesProducer(
  seriesId: string,
  userId: string | null,
): Promise<{ id: string; producer_user_id: string | null; producer_name: string | null }> {
  return api(`/api/series/${seriesId}/producer`, {
    method: "PATCH",
    body: JSON.stringify({ user_id: userId }),
  });
}

// ── Phase 11.1: Project/Series credit budgets ──────────────────────────────

/** Every tier of the hierarchy can carry a credit ceiling, and all of them
 *  apply — the innermost breached one is what blocks a generation.
 *  "scene" is an Episode and "shot" a Sequence (table names, kept so the URL
 *  matches the API). */
export type BudgetScope = "project" | "series" | "scene" | "shot";

export interface CreditGrantDTO {
  id: string;
  scope?: BudgetScope;
  scope_id?: string;
  amount_usd: number;
  reason: string;
  granted_by_name: string | null;
  requested_by?: string | null;
  requested_by_username?: string | null;
  requested_by_email?: string | null;
  created_at: string | null;
  /** A request only raises the ceiling once an admin approves it. */
  status: "pending" | "approved" | "rejected";
  decided_by_name: string | null;
  decided_at: string | null;
  decision_note: string | null;
  /** Where the money goes — resolved server-side so the inbox needs no lookups. */
  project_id?: string;
  project_name?: string;
  series_name?: string;
  series_code?: string;
  /** The scope's budget at the moment of asking. */
  budget?: {
    used_usd: number;
    effective_usd: number;
    remaining_usd: number | null;
    unlimited: boolean;
  };
}

export interface BudgetSummaryDTO {
  scope: BudgetScope;
  scope_id: string;
  /** What the BOD set. 0 → unlimited. */
  base_usd: number;
  granted_usd: number;
  effective_usd: number;
  spent_usd: number;
  reserved_usd: number;
  used_usd: number;
  /** null when unlimited. */
  remaining_usd: number | null;
  unlimited: boolean;
  used_pct: number | null;
  grants?: CreditGrantDTO[];
}

export function getBudget(scope: BudgetScope, id: string): Promise<BudgetSummaryDTO> {
  return api<BudgetSummaryDTO>(`/api/budgets/${scope}/${id}`);
}

/** BOD/admin sets the base ceiling. 0 clears it (unlimited). */
export function setBudget(
  scope: BudgetScope,
  id: string,
  amountUsd: number,
): Promise<BudgetSummaryDTO> {
  return api<BudgetSummaryDTO>(`/api/budgets/${scope}/${id}`, {
    method: "PUT",
    body: JSON.stringify({ amount_usd: amountUsd }),
  });
}

/** PM REQUESTS more credit. Lands pending — an admin has to approve it before
 *  the ceiling moves. A reason is required and kept in the log. */
export function requestCredit(
  scope: BudgetScope,
  id: string,
  input: { amount_usd: number; reason: string },
): Promise<BudgetSummaryDTO & { grant: CreditGrantDTO }> {
  return api(`/api/budgets/${scope}/${id}/grants`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Admin inbox: every credit request awaiting a verdict. */
export function listPendingCreditRequests(): Promise<{ requests: CreditGrantDTO[] }> {
  return api(`/api/budgets/requests/pending`);
}

/** Admin approves — this is what actually raises the ceiling. */
export function approveCreditRequest(
  id: string,
  note?: string,
): Promise<BudgetSummaryDTO & { grant: CreditGrantDTO }> {
  return api(`/api/budgets/requests/${id}/approve`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

/** Admin rejects — a note is required so the asker knows why. */
export function rejectCreditRequest(
  id: string,
  note: string,
): Promise<BudgetSummaryDTO & { grant: CreditGrantDTO }> {
  return api(`/api/budgets/requests/${id}/reject`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}


// ── Change history (audit trail per object) ─────────────────────────────────

export type HistoryObjectType = "project" | "series" | "scene" | "shot";

export interface HistoryEntryDTO {
  id: number;
  created_at: string | null;
  action: string;
  actor: string | null;
  target: string | null;
  detail: string | null;
  ip: string | null;
}

/** Everything recorded against one object, newest first. This is what answers
 *  "who reassigned Ep03, and when" — the question the spreadsheet never could. */
export function getObjectHistory(
  objectType: HistoryObjectType,
  objectId: string,
  limit = 200,
): Promise<{ object_type: string; object_id: string; entries: HistoryEntryDTO[] }> {
  return api(`/api/history/${objectType}/${objectId}?limit=${limit}`);
}

// ── KPI ────────────────────────────────────────────────────────────────────

export interface KpiPersonDTO {
  user_id: string | null;
  name: string | null;
  assigned: number;
  delivered: number;
  in_review: number;
  attempts: number;
  rejections: number;
  first_pass: number;
  first_pass_rate: number | null;
  avg_attempts: number | null;
  credits_usd: number;
  credits_per_delivered_usd: number | null;
  avg_review_days: number | null;
}

export interface SeriesKpiDTO {
  series_id: string;
  series_name: string;
  series_code: string;
  episodes: number;
  delivered: number;
  in_review: number;
  unassigned: number;
  completion_pct: number | null;
  budget: BudgetSummaryDTO;
  people: KpiPersonDTO[];
}

export interface KpiProjectRowDTO {
  project_id: string;
  project_name: string;
  episodes: number;
  delivered: number;
  in_review: number;
  unassigned: number;
  completion_pct: number | null;
  budget: BudgetSummaryDTO;
}

export interface KpiOverviewDTO {
  totals: {
    projects: number;
    episodes: number;
    delivered: number;
    in_review: number;
    unassigned: number;
    credits_usd: number;
    completion_pct: number | null;
  };
  projects: KpiProjectRowDTO[];
  people: KpiPersonDTO[];
}

/** The board tracker: every project at once. Admin/BOD only. */
export function getKpiOverview(): Promise<KpiOverviewDTO> {
  return api(`/api/kpi/overview`);
}

/** Admin/BOD only — this compares people's output, so it is not exposed to a
 *  peer, not even the producer running the project. */
export function getProjectKpi(
  projectId: string,
): Promise<{ project_id: string; people: KpiPersonDTO[] }> {
  return api(`/api/kpi/projects/${projectId}`);
}

export function getSeriesKpi(seriesId: string): Promise<SeriesKpiDTO> {
  return api(`/api/kpi/series/${seriesId}`);
}

// ── CSV export ─────────────────────────────────────────────────────────────

export type ExportKind = "episodes" | "submissions" | "spend";

export function exportUrl(projectId: string, kind: ExportKind): string {
  return `/api/export/projects/${projectId}/${kind}`;
}

/** Trigger a CSV download.
 *
 * Fetched as a blob rather than a plain `<a href>`: the token is attached by the
 * global /api fetch interceptor (see api/authFetch.ts), which a navigation would
 * bypass — the download would arrive unauthenticated and 401. */
export async function downloadExport(
  projectId: string,
  kind: ExportKind,
  filename?: string,
): Promise<void> {
  const res = await fetch(exportUrl(projectId, kind));
  if (!res.ok) {
    throw new Error(`export failed (${res.status})`);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || `${kind}-${projectId}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}


// ── Spend ledger (admin/BOD) ───────────────────────────────────────────────

export interface LedgerRowDTO {
  usage_id: number | null;
  when: string | null;
  user_id: string | null;
  user_name: string | null;
  project_id: string | null;
  project_name: string | null;
  series_id: string | null;
  series_code: string | null;
  scene_id: string | null;
  episode: string | null;
  shot_id: string | null;
  sequence: string | null;
  node_id: number | null;
  kind: string | null;
  model: string | null;
  resolution: string | null;
  duration_seconds: number | null;
  cost_usd: number;
  /** take N of M on the same shot slot — the retake story */
  take: number;
  takes_on_node: number;
  /** the newest take on a slot: the one the artist settled on */
  kept: boolean;
  downloaded: boolean;
  unattributed?: boolean;
}

export interface LedgerDTO {
  rows: LedgerRowDTO[];
  total_rows: number;
  offset: number;
  limit: number;
  totals: {
    generations: number;
    total_usd: number;
    kept_usd: number;
    retake_usd: number;
    /** Charges with no node — they can't be told apart into shipped vs re-rolled. */
    unclassified_usd: number;
    unclassified_count: number;
    /** Share of the CLASSIFIABLE spend that didn't ship, not of the grand total. */
    retake_pct: number;
    people: number;
    downloaded: number;
  };
}

export interface LedgerFilters {
  project_id?: string;
  series_id?: string;
  scene_id?: string;
  shot_id?: string;
  user_id?: string;
  model?: string;
  kept?: boolean;
  limit?: number;
  offset?: number;
}

/** Every billed generation, with who ran it and where it landed. Admin/BOD only. */
export function getSpendLedger(f: LedgerFilters = {}): Promise<LedgerDTO> {
  const q = new URLSearchParams();
  Object.entries(f).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  });
  return api(`/api/admin/stats/ledger?${q.toString()}`);
}

export function getLedgerFilterOptions(): Promise<{
  projects: { id: string; name: string }[];
  people: { id: string; name: string }[];
  models: string[];
}> {
  return api(`/api/admin/stats/ledger/filters`);
}


// ── Spend over time (admin/BOD) ────────────────────────────────────────────

export type SpendPeriod = "day" | "week" | "month" | "year";

export interface TimelinePersonDTO {
  user_id: string | null;
  name: string;
  generations: number;
  total_usd: number;
  delivered: number;
}

export interface TimelineBucketDTO {
  key: string;
  label: string;
  total_usd: number;
  kept_usd: number;
  retake_usd: number;
  generations: number;
  delivered: number;
  submitted: number;
  people: TimelinePersonDTO[];
}

export interface TimelineDTO {
  period: SpendPeriod;
  buckets: TimelineBucketDTO[];
  totals: {
    total_usd: number;
    generations: number;
    delivered: number;
    peak_usd: number;
  };
}

/** Credits burned per period, and who burned them. Admin/BOD only. */
export function getSpendTimeline(
  period: SpendPeriod = "day",
  buckets = 30,
): Promise<TimelineDTO> {
  return api(`/api/admin/stats/timeline?period=${period}&buckets=${buckets}`);
}

export interface UserCostDTO {
  user_id: string;
  username: string;
  display_name: string | null;
  budget_usd: number | null;
  spent_usd: number;
  kept_usd: number;
  wasted_usd: number;
  kept_clips: number;
  wasted_takes: number;
  takes: number;
  downloaded_clips: number;
  waste_pct: number;
}

/** Spend per person, all time. Admin/BOD only. */
export function getUserCosts(): Promise<UserCostDTO[]> {
  return api(`/api/admin/stats/users`);
}

// ── Flow Studio (/giantflow) ─────────────────────────────────────────────────
//
// The studio came over from the manga_extract repo as a standalone surface. It is
// not yet wired into Project → Series → Episode, so it keeps its own board list
// (`/api/flowstudio/boards`) and its images are References scoped by
// `source_board_id` rather than `project_id`. Every endpoint here is admin-only
// server-side, for the same reason the unscoped `/api/requests` and
// `/api/references` paths are: with nothing to scope a permission check against,
// the only safe caller is one who may see everything.

/** A Flow Studio project. `kind` is echoed by the server as a constant — the
 *  studio's UI came from a repo where one table served two surfaces. */
export interface FlowBoard {
  id: number;
  name: string;
  kind: string;
  created_at: string | null;
}

export function listFlowBoards(): Promise<FlowBoard[]> {
  return api<FlowBoard[]>("/api/flowstudio/boards");
}

export function createFlowBoard(name: string): Promise<FlowBoard> {
  return api<FlowBoard>("/api/flowstudio/boards", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function patchFlowBoard(id: number, name: string): Promise<FlowBoard> {
  return api<FlowBoard>(`/api/flowstudio/boards/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

export function deleteFlowBoard(
  id: number,
): Promise<{ deleted: number; references_deleted: number }> {
  return api<{ deleted: number; references_deleted: number }>(
    `/api/flowstudio/boards/${id}`,
    { method: "DELETE" },
  );
}

/** Cache an image for use as a reference/source. Local-only: the engines read the
 *  bytes off disk at dispatch time, so nothing is pushed anywhere on upload. */
export async function uploadFlowImage(
  file: File,
): Promise<{ media_id: string; mime: string; size: number }> {
  const form = new FormData();
  form.append("file", file);
  // No Content-Type and no auth header on purpose: the browser sets the multipart
  // boundary itself, and `api/authFetch.ts` patches global fetch to attach the
  // bearer token — same as every other upload in this file.
  const res = await fetch("/api/flowstudio/upload", { method: "POST", body: form });
  if (!res.ok) {
    throw new Error(await errorMessage(res));
  }
  return res.json() as Promise<{ media_id: string; mime: string; size: number }>;
}

export interface FlowUsageGemini {
  today: number;
  total: number;
  daily_quota: number;
  remaining_est: number;
}

export interface FlowUsageSeedream {
  today: number;
  total: number;
  usd_per_image: number;
  cost_today: number; // USD spent today
  cost_total: number; // USD spent all-time
}

export interface FlowUsage {
  today: number;
  total: number;
  daily_quota: number;
  remaining_est: number;
  resets_at?: string; // ISO — the server's next local midnight
  seconds_until_reset?: number;
  // Two engine families priced differently, so one number would mislead: Gemini
  // and Atrium are quota-based; Seedream bills per image, so it reports money.
  engines?: { gemini: FlowUsageGemini; seedream: FlowUsageSeedream };
}

export function getFlowUsage(): Promise<FlowUsage> {
  return api<FlowUsage>("/api/flowstudio/usage");
}

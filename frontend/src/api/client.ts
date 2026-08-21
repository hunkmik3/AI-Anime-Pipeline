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

/** Force a download (Content-Disposition: attachment) rather than an inline
 *  render, optionally naming the file. */
export function mediaDownloadUrl(mediaId: string, filename?: string): string {
  const clean = mediaId.replace(/^media\//, "");
  const params = new URLSearchParams({ download: "1" });
  if (filename?.trim()) params.set("filename", filename.trim());
  return `/media/${encodeURIComponent(clean)}?${params.toString()}`;
}

/** Downscaled thumbnail (cached JPEG) — for grids/pickers, so we don't ship
 *  multi-MB full-res images for tiny tiles. Falls back to the original on the
 *  server for non-images. Use mediaUrl for full-res and mediaDownloadUrl to
 *  download. */
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
  // Image model that produced this (e.g. "gemini-3.1-flash-image", or
  // "dola-seedream-5-0-pro"); null for uploads and rows saved before the field
  // existed. The RESOLVED model, which can differ from the requested one when
  // the backend substitutes a fallback.
  modelUsed: string | null;
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
  model_used?: string | null;
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
  model_used?: string | null;
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
    modelUsed: row.model_used ?? null,
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
export type ProjectRole = "admin" | "producer" | "artist" | "viewer";

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
  /** The employee who BUILDS the series: every episode under it is theirs to work
   *  in and to hand in, unless an episode names somebody else. Never the same
   *  field as the producer above — that one reviews what this one delivers. */
  assignee_user_id?: string | null;
  assignee_name?: string | null;
  /** Live per-status episode rollup (list/detail only). */
  stats?: SeriesStats;
  /** Archive lock — the series is view-only (no editing or generation) when true. */
  frozen?: boolean;
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
  /** Archived/view-only: the parent series is frozen — no editing or generation. */
  frozen?: boolean;
  name: string;
  /** Phase 10: human code within the series — "EP007", "CH012". */
  code?: string;
  order_index: number;
  /** Phase 10 CRM: Episode_Tracker production metadata bag.
   *
   *  `production.status` is resolved server-side: a PM's stored answer if there is
   *  one, otherwise read off the work in the episode. So it can say "Production"
   *  with nothing stored — see `status_auto`. */
  production?: Record<string, string | number>;
  /** True when `production.status` was worked out from the episode's work rather
   *  than chosen by a person. Picking a value in the dropdown stores it and it
   *  stops being derived. */
  status_auto?: boolean;
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
export function getSeries(projectId: string): Promise<SeriesDTO> {
  return api<SeriesDTO>(`/api/series/${projectId}`);
}

export function patchSeries(
  id: string,
  patch: {
    name?: string;
    code?: string;
    unit_label?: UnitLabel;
    order_index?: number;
    production?: Record<string, string | number | null>;
    /** Archive lock: true = view-only (archived), false = reopen (editable). */
    frozen?: boolean;
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
export function listSeriesEpisodes(projectId: string): Promise<SceneDTO[]> {
  return api<SceneDTO[]>(`/api/series/${projectId}/episodes`);
}

/** Distinct crew names across all episodes — the CRM crew-dropdown pool. */
export function getCrewNames(): Promise<{ names: string[] }> {
  return api<{ names: string[] }>(`/api/production/crew-names`);
}

/** Bulk-plan a series: ensure `episodes` Episodes, each with
 *  `sequences_per_episode` Sequences (idempotent; only creates what's missing). */
export function generateSeriesStructure(
  projectId: string,
  input: { episodes: number; sequences_per_episode: number },
): Promise<{
  episodes_created: number;
  sequences_created: number;
  total_episodes: number;
  sequences_per_episode: number;
}> {
  return api(`/api/series/${projectId}/generate-structure`, {
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
  // Only the Avis Seedance 2.0/2.5 models; the gen dialog shows the KYC toggle.
  supports_kyc?: boolean;
  // DanceSee /api/v1/b2b/* unmoderated path (Seedance 2.0/2.5, B2B account).
  supports_b2b_unmoderated?: boolean;
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

/**
 * A SERIES and where its delivery stands — the row of "My work" and "Review".
 *
 * The series is what gets handed in: one person takes it, hands in one finished
 * cut, and a PM reviews it once. This used to be per episode, so a twelve-episode
 * series was twelve identical cards each offering to deliver a twelfth of the job.
 */
export interface DeliverableSeriesDTO {
  id: string;
  name: string;
  code: string;
  project_id: string;
  project_name: string | null;
  assignee_user_id: string | null;
  assignee_name: string | null;
  producer_name: string | null;
  deliverable_status: DeliverableStatus | string;
  /** How much is behind the one link — what somebody recognises the series by. */
  episode_count: number;
  episodes: { id: string; code: string; name: string }[];
  latest_submission: SubmissionDTO | null;
  /** Every attempt, newest first. The current one is `submissions[0]`. */
  submissions: SubmissionDTO[];
}

/** Hand in the finished cut (a Google Drive link) for a series. */
export function submitSeries(
  seriesId: string,
  input: { drive_url: string; note?: string },
): Promise<SubmissionDTO> {
  return api<SubmissionDTO>(`/api/series/${seriesId}/submissions`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Full submission history for a series (newest version first). */
export function listSeriesSubmissions(
  seriesId: string,
): Promise<{ series: DeliverableSeriesDTO; submissions: SubmissionDTO[] }> {
  return api(`/api/series/${seriesId}/submissions`);
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

export interface MaterialSeriesDTO {
  id: string; name: string; code: string;
  project_name: string | null; role: string;
  episode_count: number; clip_count: number;
  deliverable_status: string;
  latest_edit_id: string | null;
  latest_edit_version: number | null;
}
export interface MaterialsDTO {
  series_id: string; name?: string; clip_count: number;
  episodes: {
    scene_id: string; code: string; name: string; sequence_count: number;
    sequences: {
      shot_id: string; code: string; take_count: number;
      clips: { media_id: string; take: number; filename: string;
               duration_seconds?: number | null; resolution?: string | null;
               aspect_ratio?: string | null }[];
    }[];
  }[];
}

export interface EditNoteDTO {
  id: number; submission_id: string;
  shot_id: string | null; shot_code: string | null;
  at_seconds: number; body: string;
  drawing_media_id: string | null;
  resolved: boolean; author_name: string | null; created_at: string | null;
}

/** Every note on one cut, in play order — the order they are worked through. */
export function listEditNotes(
  submissionId: string,
): Promise<{ series_id: string | null; notes: EditNoteDTO[] }> {
  return api(`/api/submissions/${submissionId}/notes`);
}

/** Leave a note on a frame. `shot_id` is the sequence the editor PICKED — the cut
 *  is assembled outside the app, so no arithmetic can work it out. */
export function addEditNote(
  submissionId: string,
  input: {
    at_seconds: number; body: string; shot_id?: string | null;
    /** The strokes over the frame, as a data URL. Sent WITH the note: a drawing
     *  without its note is an orphan nobody can interpret. */
    drawing_data_url?: string | null;
  },
): Promise<EditNoteDTO> {
  return api(`/api/submissions/${submissionId}/notes`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Mark a note dealt with — or put it back. Reversible on purpose. */
export function resolveEditNote(noteId: number, done = true): Promise<EditNoteDTO> {
  return api(`/api/notes/${noteId}/resolve?done=${done ? "true" : "false"}`, {
    method: "POST",
  });
}

/** Every editor note on any sequence of an episode — one call for the canvas. */
export function listEpisodeNotes(
  sceneId: string,
): Promise<{ notes: EditNoteDTO[]; open_count: number }> {
  return api(`/api/scenes/${sceneId}/notes`);
}

/** What the editor said about ONE sequence — the artist's half. */
export function listShotNotes(
  shotId: string,
): Promise<{ notes: EditNoteDTO[]; open_count: number }> {
  return api(`/api/shots/${shotId}/notes`);
}

/** Series this account may pull raw material from — the editor's own page. */
export function listMyMaterials(): Promise<{ series: MaterialSeriesDTO[] }> {
  return api(`/api/my/materials`);
}

/** One series' clips, grouped episode → sequence, in play order. */
export function listSeriesMaterials(seriesId: string): Promise<MaterialsDTO> {
  return api(`/api/series/${seriesId}/materials`);
}

/** A plain URL, not a fetch: the browser's own downloader handles a 2 GB file,
 *  a progress bar and a resume, none of which is worth rebuilding here. The
 *  `dl` token authenticates the navigation — an `<a href>` can't send the
 *  Bearer header — so mint one with materialsToken() right before the click. */
export function materialsZipUrl(seriesId: string, token?: string): string {
  const q = token ? `?dl=${encodeURIComponent(token)}` : "";
  return `/api/series/${seriesId}/materials.zip${q}`;
}

/** Short-lived, series-scoped download token for materialsZipUrl(). Fetched via
 *  `api` (which attaches the Bearer header) immediately before the download,
 *  since it expires in minutes and must not sit in a rendered href. */
export function materialsToken(seriesId: string): Promise<{ token: string }> {
  return api(`/api/series/${seriesId}/materials-token`);
}

/** The series the signed-in employee has to hand in ("My work"). */
export function listMySeries(): Promise<{ series: DeliverableSeriesDTO[] }> {
  return api(`/api/my/series`);
}

/** Submissions waiting on the signed-in reviewer. */
export function listReviewQueue(): Promise<{
  items: { submission: SubmissionDTO; series: DeliverableSeriesDTO | null }[];
}> {
  return api(`/api/review/queue`);
}

/** PM assigns the employee who owns (and may submit) an episode. */
export function setEpisodeAssignee(
  sceneId: string,
  userId: string | null,
): Promise<{ id: string; assignee_user_id: string | null; assignee_name: string | null }> {
  return api(`/api/scenes/${sceneId}/assignee`, {
    method: "PATCH",
    body: JSON.stringify({ user_id: userId }),
  });
}

/** PM sets the Series Producer (first reviewer in the approver chain). */
/** Hand a whole series to one employee (every episode under it becomes theirs). */
export function setSeriesAssignee(
  seriesId: string,
  userId: string | null,
): Promise<{ id: string; assignee_user_id: string | null; assignee_name: string | null }> {
  return api(`/api/series/${seriesId}/assignee`, {
    method: "PATCH",
    body: JSON.stringify({ user_id: userId }),
  });
}

export function setSeriesProducer(
  projectId: string,
  userId: string | null,
): Promise<{ id: string; producer_user_id: string | null; producer_name: string | null }> {
  return api(`/api/series/${projectId}/producer`, {
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

export function getSeriesKpi(projectId: string): Promise<SeriesKpiDTO> {
  return api(`/api/kpi/series/${projectId}`);
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

// ── Giantflow panel production ───────────────────────────────────────────────
//
// The comic-adaptation pipeline: a folder of cut panels comes in, an artist
// restyles each one, a PM reviews it. The PANEL is the unit of work — assigned,
// statused, noted and exported — so most of these are addressed by panel id, not
// by project. See docs/GIANTFLOW_REFACTOR.md.

export type PanelStatus =
  | "todo"
  | "in_progress"
  | "submitted"
  | "changes_requested"
  | "approved";

/** One instalment of a comic — the tier the work is divided on. */
export interface FlowChapter {
  id: number;
  series_id: number;
  name: string;
  created_by_name: string | null;
  due_date: string | null;
  thumb_media_id: string | null;
  has_cover: boolean;
  batch_count: number;
  /** What a new batch here will be called, minus the number — so the form shows
   *  the exact name instead of guessing at the convention. */
  batch_name_prefix: string;
  panel_count: number;
  approved_count: number;
  status_counts: Record<string, number>;
  created_at: string | null;
}

export function listChapters(seriesId: number): Promise<FlowChapter[]> {
  return api<FlowChapter[]>(`/api/flowstudio/series/${seriesId}/chapters`);
}

export function getChapter(chapterId: number): Promise<FlowChapter> {
  return api<FlowChapter>(`/api/flowstudio/chapters/${chapterId}`);
}

export function createChapter(
  seriesId: number,
  name: string,
  dueDate?: string | null,
): Promise<FlowChapter> {
  return api<FlowChapter>(`/api/flowstudio/series/${seriesId}/chapters`, {
    method: "POST",
    body: JSON.stringify({ name, due_date: dueDate ?? null }),
  });
}

export function updateChapter(
  chapterId: number,
  patch: {
    name?: string;
    cover_media_id?: string | null;
    set_cover?: boolean;
    due_date?: string | null;
    set_due?: boolean;
  },
): Promise<FlowChapter> {
  return api<FlowChapter>(`/api/flowstudio/chapters/${chapterId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteChapter(chapterId: number): Promise<{ ok: boolean }> {
  return api(`/api/flowstudio/chapters/${chapterId}`, { method: "DELETE" });
}

export function reorderChapters(seriesId: number, ids: number[]): Promise<{ ok: boolean }> {
  return api(`/api/flowstudio/series/${seriesId}/chapters/reorder`, {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
}

/** Every approved panel in one chapter, foldered by batch. */
export function exportChapter(chapterId: number) {
  return download(`/api/flowstudio/chapters/${chapterId}/export`);
}

/** The slate — the container every comic hangs off. */
export interface FlowProject {
  id: number;
  name: string;
  thumb_media_id: string | null;
  has_cover: boolean;
  series_count: number;
  panel_count: number;
  approved_count: number;
  created_at: string | null;
}

export function listFlowProjects(): Promise<FlowProject[]> {
  return api<FlowProject[]>("/api/flowstudio/projects");
}

export function createFlowProject(name: string): Promise<FlowProject> {
  return api<FlowProject>("/api/flowstudio/projects", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function updateFlowProject(
  id: number,
  patch: { name?: string; cover_media_id?: string | null; set_cover?: boolean },
): Promise<FlowProject> {
  return api<FlowProject>(`/api/flowstudio/projects/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteFlowProject(id: number): Promise<{ ok: boolean }> {
  return api(`/api/flowstudio/projects/${id}`, { method: "DELETE" });
}

export function reorderFlowProjects(ids: number[]): Promise<{ ok: boolean }> {
  return api("/api/flowstudio/projects/reorder", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
}

export interface PanelSeries {
  id: number;
  /** The slate it hangs off. */
  project_id: number;
  name: string;
  /** Hand-picked cover, else the comic's opening panel; null when neither. */
  thumb_media_id: string | null;
  /** True only when someone uploaded one — drives Change vs Thumbnail. */
  has_cover: boolean;
  /** Who set it up, when, and when it ships. */
  created_by_name: string | null;
  created_at: string | null;
  due_date: string | null;
  /** A comic ships an instalment at a time; each chapter is divided into a
   *  batch per artist. */
  chapter_count: number;
  batch_count: number;
  panel_count: number;
  approved_count: number;
}

/**
 * One artist's share of a comic: its own name, its own imported folder of panels,
 * one person. The batch — not the project — owns an import, because material
 * arrives already divided by who is doing it.
 */
export interface PanelBatch {
  id: number;
  project_id: number;
  chapter_id: number;
  name: string;
  assignee_user_id: string | null;
  assignee_name: string | null;
  panel_count: number;
  approved_count: number;
  /** Panels per status — the whole spread, not just how many are approved. */
  status_counts: Record<string, number>;
  open_notes: number;
  /** First panel's raw image, so a batch is recognisable without opening it. */
  thumb_media_id: string | null;
  created_at: string | null;
}

export interface PanelNote {
  id: number;
  body: string;
  resolved: boolean;
  author_name: string | null;
  created_at: string | null;
}

export interface PanelVersion {
  media_id: string;
  version: number;
  model_used: string | null;
  created_at: string | null;
}

export interface Panel {
  id: number;
  batch_id: number;
  batch_name: string;
  project_id: number;
  code: string;
  order_index: number;
  status: PanelStatus;
  assignee_user_id: string | null;
  assignee_name: string | null;
  /** First raw piece — what the grid shows beside the result. */
  raw_media_id: string | null;
  raw_count: number;
  /** Newest generated version, or null before anything is generated. */
  latest_media_id: string | null;
  /** What the panel delivers: the submitted pick, else the latest version.
   *  Grids and covers read THIS — never latest_media_id. */
  delivered_media_id: string | null;
  delivered_version: number;
  /** Null until someone submits; the version the artist actually chose. */
  final_media_id: string | null;
  version_count: number;
  unresolved_notes: number;
  updated_at: string | null;
  // Detail view only (GET /panels/:id).
  raw?: { media_id: string; version: number }[];
  versions?: PanelVersion[];
  notes?: PanelNote[];
  history?: PanelEvent[];
}

export function listPanelSeries(projectId?: number): Promise<PanelSeries[]> {
  const qs = projectId === undefined ? "" : `?project_id=${projectId}`;
  return api<PanelSeries[]>(`/api/flowstudio/series${qs}`);
}

export function createPanelSeries(projectId: number, name: string): Promise<PanelSeries> {
  return api<PanelSeries>("/api/flowstudio/series", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, name }),
  });
}

export function updatePanelSeries(
  id: number,
  patch: { name?: string; due_date?: string | null; set_due?: boolean },
): Promise<PanelSeries> {
  return api<PanelSeries>(`/api/flowstudio/series/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function renamePanelSeries(id: number, name: string): Promise<PanelSeries> {
  return api<PanelSeries>(`/api/flowstudio/series/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

/**
 * Persist a hand-arranged order.
 *
 * Ids not named keep their relative order after the ones that are, so a partial
 * list — a filtered view, a stale tab — reorders what it knows without
 * scattering the rest.
 */
export function reorderPanelSeries(
  projectId: number,
  ids: number[],
): Promise<{ reordered: number }> {
  return api<{ reordered: number }>(`/api/flowstudio/projects/${projectId}/series/reorder`, {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
}

export function reorderBatches(
  chapterId: number,
  ids: number[],
): Promise<{ reordered: number }> {
  return api<{ reordered: number }>(
    `/api/flowstudio/chapters/${chapterId}/batches/reorder`,
    { method: "POST", body: JSON.stringify({ ids }) },
  );
}

/** Point the project card at an image; null falls back to the first panel. */
export function setPanelSeriesCover(
  id: number,
  mediaId: string | null,
): Promise<PanelSeries> {
  return api<PanelSeries>(`/api/flowstudio/series/${id}/cover`, {
    method: "POST",
    body: JSON.stringify({ media_id: mediaId }),
  });
}

export function deletePanelSeries(id: number): Promise<{ deleted: number }> {
  return api<{ deleted: number }>(`/api/flowstudio/series/${id}`, {
    method: "DELETE",
  });
}

/** Add ONE panel to a batch by hand (e.g. an insert like "…_P035-2" — chapter
 *  order is by code, so it lands right after P035). */
export function createPanel(batchId: number, code: string, mediaId?: string): Promise<Panel> {
  return api<Panel>(`/api/flowstudio/batches/${batchId}/panels`, {
    method: "POST",
    body: JSON.stringify({ code, media_id: mediaId ?? null }),
  });
}

export function deletePanel(panelId: number): Promise<{ deleted: number }> {
  return api<{ deleted: number }>(`/api/flowstudio/panels/${panelId}`, { method: "DELETE" });
}

/** Mark a panel done with NO processing — its raw art passes straight through
 *  to Giantstudio (approved + delivered immediately). */
export function passThroughPanel(panelId: number): Promise<Panel> {
  return api<Panel>(`/api/flowstudio/panels/${panelId}/pass-through`, { method: "POST" });
}

export interface ImportResult {
  batch_id: number;
  panels: Panel[];
  imported_files: number;
  skipped: string[];
  skipped_count: number;
}

/**
 * Import a raw-material folder.
 *
 * Each file's path *inside the chosen folder* has to travel with it: that is what
 * says which panel it belongs to (`PANEL008/a.png` → PANEL008). A browser upload
 * otherwise arrives as flat basenames and the grouping is lost. `webkitRelativePath`
 * is where the browser puts it.
 *
 * Order is the order sent — the cutter sorted the folder deliberately, so we pass
 * their order through rather than sorting again.
 */
export async function importPanelFolder(
  batchId: number,
  files: File[],
  onProgress?: (sent: number, total: number) => void,
  // append=true tops up a batch that was already imported (adds new panels,
  // skips codes that already exist) instead of the default fresh-import which
  // refuses when the batch is non-empty.
  append = false,
): Promise<ImportResult> {
  // The path inside the chosen folder identifies the panel ("PANEL008/a.png" →
  // PANEL008); strip the top folder the user actually picked.
  const relOf = (f: File): string => {
    const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath;
    return rel && rel.includes("/") ? rel.slice(rel.indexOf("/") + 1) : f.name;
  };

  // Prefer direct-to-R2 when the server offers it: the browser PUTs each file
  // straight to storage, bypassing the 100MB Cloudflare limit on this hostname.
  let r2Direct = false;
  try {
    const cfg = await api<{ r2_direct?: boolean }>(`/api/flowstudio/upload-config`);
    r2Direct = !!cfg.r2_direct;
  } catch {
    /* fall back to multipart below */
  }
  if (r2Direct) return importPanelFolderViaR2(batchId, files, relOf, onProgress, append);

  // Fallback: one multipart POST (subject to the 100MB Cloudflare limit).
  const form = new FormData();
  for (const f of files) {
    form.append("files", f);
    form.append("paths", relOf(f));
  }
  form.append("append", append ? "true" : "false");
  onProgress?.(0, files.length);
  const res = await fetch(`/api/flowstudio/batches/${batchId}/import`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  onProgress?.(files.length, files.length);
  return res.json() as Promise<ImportResult>;
}

interface PresignSlot {
  filename: string;
  media_id?: string;
  put_url?: string;
  public_url?: string;
  mime?: string;
  skip?: boolean;
}

/**
 * Direct-to-R2 import: ask the server for one presigned PUT url per file, PUT
 * each file straight to R2 (parallel, no 100MB Cloudflare limit), then register
 * the uploaded set as panels. The auth token is never sent to R2 — authFetch
 * only attaches it to same-origin `/api` calls, and these PUTs are cross-origin.
 */
async function importPanelFolderViaR2(
  batchId: number,
  files: File[],
  relOf: (f: File) => string,
  onProgress?: (sent: number, total: number) => void,
  append = false,
): Promise<ImportResult> {
  onProgress?.(0, files.length);
  const { uploads } = await api<{ uploads: PresignSlot[] }>(
    `/api/flowstudio/batches/${batchId}/import-urls`,
    {
      method: "POST",
      body: JSON.stringify(
        files.map((f) => ({ filename: f.name, mime: f.type || "application/octet-stream" })),
      ),
    },
  );

  const slots = uploads
    .map((u, i) => ({ ...u, file: files[i], rel: relOf(files[i]) }))
    .filter((s) => !s.skip && s.put_url && s.media_id && s.file);

  const registered: { media_id: string; rel_path: string; public_url: string; mime: string }[] = [];
  let done = 0;
  const queue = slots.slice();
  const worker = async (): Promise<void> => {
    for (;;) {
      const s = queue.shift();
      if (!s) return;
      try {
        // Raw fetch (not `api`): cross-origin to R2, so no auth header and no
        // JSON content-type — just the file bytes to the presigned url.
        const put = await fetch(s.put_url as string, { method: "PUT", body: s.file });
        if (put.ok) {
          registered.push({
            media_id: s.media_id as string,
            rel_path: s.rel,
            public_url: s.public_url as string,
            mime: s.mime || s.file.type || "",
          });
        }
      } catch {
        /* a failed PUT (CORS / network) just doesn't get registered */
      }
      done += 1;
      onProgress?.(done, files.length);
    }
  };
  await Promise.all(Array.from({ length: Math.min(6, slots.length || 1) }, worker));

  if (registered.length === 0) {
    throw new Error(
      "No files reached storage — check the bucket's CORS policy allows PUT from this site.",
    );
  }
  return api<ImportResult>(
    `/api/flowstudio/batches/${batchId}/import-register${append ? "?append=true" : ""}`,
    {
      method: "POST",
      body: JSON.stringify(registered),
    },
  );
}

/** Who a panel can be handed to. Narrows to project members once giantflow has
 *  its own membership; today it is every active account. */
export function listPanelAssignees(): Promise<{ user_id: string; name: string }[]> {
  return api<{ user_id: string; name: string }[]>("/api/flowstudio/assignable-users");
}

export function listPanels(batchId: number): Promise<Panel[]> {
  return api<Panel[]>(`/api/flowstudio/batches/${batchId}/panels`);
}

export function listBatches(chapterId: number): Promise<PanelBatch[]> {
  return api<PanelBatch[]>(`/api/flowstudio/chapters/${chapterId}/batches`);
}

export function getBatch(batchId: number): Promise<PanelBatch> {
  return api<PanelBatch>(`/api/flowstudio/batches/${batchId}`);
}

export function createBatch(
  chapterId: number,
  name: string,
  assigneeUserId?: string | null,
): Promise<PanelBatch> {
  return api<PanelBatch>(`/api/flowstudio/chapters/${chapterId}/batches`, {
    method: "POST",
    body: JSON.stringify({ name, assignee_user_id: assigneeUserId ?? null }),
  });
}

/**
 * Create several batches in one commit.
 *
 * Dividing a comic among its artists is one decision, not six — doing it six
 * times is six chances to lose track of who already has something. Rows with a
 * blank name are dropped server-side, so a form with spare rows needn't police
 * itself.
 */
export function createBatches(
  chapterId: number,
  batches: { name?: string | null; assignee_user_id?: string | null }[],
): Promise<PanelBatch[]> {
  return api<PanelBatch[]>(`/api/flowstudio/chapters/${chapterId}/batches/bulk`, {
    method: "POST",
    body: JSON.stringify({ batches }),
  });
}

/** `setAssignee` distinguishes "take it off them" (null) from "don't touch it". */
export function updateBatch(
  batchId: number,
  patch: { name?: string; assignee_user_id?: string | null; set_assignee?: boolean },
): Promise<PanelBatch> {
  return api<PanelBatch>(`/api/flowstudio/batches/${batchId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteBatch(batchId: number): Promise<{ deleted: number }> {
  return api<{ deleted: number }>(`/api/flowstudio/batches/${batchId}`, {
    method: "DELETE",
  });
}

export function getPanel(panelId: number): Promise<Panel> {
  return api<Panel>(`/api/flowstudio/panels/${panelId}`);
}


/** Engine settings for a panel generation. The panel imposes nothing — these are
 *  the artist's choices, passed straight through to the same engine the studio
 *  composer uses. */
export interface PanelGenParams {
  prompt: string;
  provider?: string;
  image_model?: string;
  aspect_ratio?: string;
  image_size?: string;
  variant_count?: number;
  preserve_colors?: boolean;
  /** Extra references beyond the panel's own raw material, which is added
   *  server-side. */
  ref_media_ids?: string[];
  source_media_id?: string;
}

/**
 * Queue a generation for a panel.
 *
 * Goes through the panel rather than POST /api/requests so the approved-lock is
 * checked BEFORE the money is spent — attaching results afterwards would find out
 * too late. The panel's raw material is prepended to the references server-side.
 */
export function generateForPanel(
  panelId: number,
  params: PanelGenParams,
): Promise<{ request_id: number; panel_id: number; references: number }> {
  return api(`/api/flowstudio/panels/${panelId}/generate`, {
    method: "POST",
    body: JSON.stringify(params),
  });
}

/** File finished images against the panel as its next version(s). */
/** This panel's pictures as library references — the shape the studio grid and
 *  viewer act on (tag, pin, refine, attach-to-prompt). */
export async function listPanelAssets(panelId: number): Promise<ReferenceItem[]> {
  const rows = await api<ReferenceRowWire[]>(`/api/flowstudio/panels/${panelId}/assets`);
  return rows.map(mapReferenceRow);
}

/** Attach an uploaded image to this panel as reference material (NOT a version:
 *  a version is a result, this is input the artist brought along). */
export async function addPanelAsset(
  panelId: number,
  mediaId: string,
  label?: string,
): Promise<ReferenceItem> {
  const row = await api<ReferenceRowWire>(`/api/flowstudio/panels/${panelId}/assets`, {
    method: "POST",
    body: JSON.stringify({ media_id: mediaId, label: label ?? null }),
  });
  return mapReferenceRow(row);
}

export function addPanelVersions(
  panelId: number,
  mediaIds: string[],
  modelUsed?: string | null,
): Promise<Panel> {
  return api<Panel>(`/api/flowstudio/panels/${panelId}/versions`, {
    method: "POST",
    body: JSON.stringify({ media_ids: mediaIds, model_used: modelUsed ?? null }),
  });
}

/**
 * Export downloads.
 *
 * Fetched rather than linked: these endpoints are authorised, and an `<a href>`
 * cannot carry the Bearer header. The patched `window.fetch` adds it, so the
 * bytes arrive here and are handed to the browser as a blob. The cost is that a
 * large zip passes through memory once — acceptable next to the alternative,
 * which is putting a token in a URL that lands in history and server logs.
 *
 * The filename comes from the server's Content-Disposition, so what the studio
 * gets on disk is the cutter's own panel code.
 */
async function download(url: string): Promise<{ written: number; skipped: number }> {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      /* not json — keep the status */
    }
    throw new Error(detail);
  }
  const disp = res.headers.get("content-disposition") ?? "";
  const name = /filename="([^"]+)"/.exec(disp)?.[1] ?? "download";
  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoked on the next tick, not immediately: Safari cancels an in-flight
  // download if the object URL dies in the same frame as the click.
  setTimeout(() => URL.revokeObjectURL(href), 1000);
  return {
    written: Number(res.headers.get("x-export-written") ?? 1),
    skipped: Number(res.headers.get("x-export-skipped") ?? 0),
  };
}

/** The one image this panel delivers. */
export function downloadPanel(panelId: number) {
  return download(`/api/flowstudio/panels/${panelId}/download`);
}

/** Every approved panel in one artist's batch, as a zip. */
export function exportBatch(batchId: number) {
  return download(`/api/flowstudio/batches/${batchId}/export`);
}

/** Every approved panel in the comic, foldered by batch. */
export function exportSeries(seriesId: number) {
  return download(`/api/flowstudio/series/${seriesId}/export`);
}

/** What the signed-in account may do in giantflow. Advisory — every capability
 *  is enforced per request as well; this only stops the UI offering buttons that
 *  would 403. */
export interface GiantflowMe {
  user_id: string | null;
  system_role: string | null;
  best_role: string;
  /** Uncapped by any preview — what the switch itself reads. */
  true_role?: string;
  capabilities: Record<string, boolean>;
  /** Per comic, because authority is per comic. */
  projects: Record<string, string>;
}

export function giantflowMe(): Promise<GiantflowMe> {
  return api<GiantflowMe>("/api/flowstudio/me");
}

export interface FlowMember {
  user_id: string;
  name: string;
  role: string;
}

export function listFlowMembers(seriesId: number): Promise<FlowMember[]> {
  return api<FlowMember[]>(`/api/flowstudio/series/${seriesId}/members`);
}

export function setFlowMember(
  seriesId: number,
  userId: string,
  role: string,
): Promise<FlowMember> {
  return api<FlowMember>(`/api/flowstudio/series/${seriesId}/members`, {
    method: "PUT",
    body: JSON.stringify({ user_id: userId, role }),
  });
}

export function removeFlowMember(seriesId: number, userId: string): Promise<{ ok: boolean }> {
  return api(`/api/flowstudio/series/${seriesId}/members/${userId}`, { method: "DELETE" });
}

/** A panel as it appears in a review queue: the pairing, who made it, and any
 *  unresolved remarks. */
/** One thing that happened to a panel. Versions and remarks each carry a
 *  timestamp, but only this says which remark answered which version. */
export interface PanelEvent {
  id: number;
  kind: "submitted" | "approved" | "changes_requested" | "reopened" | "version_added";
  actor_name: string | null;
  media_id: string | null;
  body: string | null;
  created_at: string | null;
}

export interface QueuePanel extends Panel {
  series_name: string;
  history?: PanelEvent[];
}

/** Every panel, with its state — the management view. `status` takes several. */
export function allPanels(filters: {
  status?: string[];
  series_id?: number;
  assignee?: string;
  q?: string;
} = {}): Promise<QueuePanel[]> {
  const s = new URLSearchParams();
  if (filters.status?.length) s.set("status", filters.status.join(","));
  if (filters.series_id !== undefined) s.set("series_id", String(filters.series_id));
  if (filters.assignee) s.set("assignee", filters.assignee);
  if (filters.q?.trim()) s.set("q", filters.q.trim());
  const qs = s.toString();
  return api<QueuePanel[]>(`/api/flowstudio/panels${qs ? `?${qs}` : ""}`);
}

/** Everything handed in and waiting on a verdict, across every artist. */
export function reviewQueue(seriesId?: number): Promise<QueuePanel[]> {
  const qs = seriesId === undefined ? "" : `?project_id=${seriesId}`;
  return api<QueuePanel[]>(`/api/flowstudio/review-queue${qs}`);
}

/** The signed-in artist's own panels, grouped by what the PM said. */
export function myWork(): Promise<{
  changes_requested: QueuePanel[];
  submitted: QueuePanel[];
  approved: QueuePanel[];
}> {
  return api(`/api/flowstudio/my-work`);
}

/**
 * One line on the notifications tab — either a job or something that happened.
 *
 * The same shape for both because they render as the same card; what separates
 * them is which list they arrive in. A job has no `at` (a state has no
 * timestamp), a feed line always does.
 */
export interface Notice {
  id: string;
  kind:
    | "sent_back"
    | "not_started"
    | "in_progress"
    | "to_review"
    | "unassigned"
    | "due_soon"
    | "overdue"
    | "submitted"
    | "approved"
    | "changes_requested"
    | "reopened";
  title: string;
  body: string | null;
  at: string | null;
  actor_name: string | null;
  href: string | null;
  panel_id: number | null;
  code: string | null;
  where: string | null;
  /** The picture this is about — the delivered version, else the raw material. */
  thumb_media_id: string | null;
  count: number;
  /** Your own doing — shown in the history, never counted as unread. */
  mine: boolean;
}

export interface NoticeSummary {
  todo: Notice[];
  feed: Notice[];
  unread: number;
  seen_at: string | null;
  role: string;
}

/** What this account has to do, and what changed while they were away. */
export function listNotices(): Promise<NoticeSummary> {
  return api<NoticeSummary>(`/api/flowstudio/notices`);
}

/** Just the badge — polled on a timer, so it skips the feed's text. */
export function noticeCount(): Promise<{ unread: number; todo: number }> {
  return api(`/api/flowstudio/notices/count`);
}

/** Mark the feed read up to now. The to-do list has no read state by design. */
export function markNoticesRead(): Promise<{ unread: number }> {
  return api(`/api/flowstudio/notices/read`, { method: "POST" });
}

export function submitPanel(panelId: number, mediaId?: string | null): Promise<Panel> {
  return api<Panel>(`/api/flowstudio/panels/${panelId}/submit`, {
    method: "POST",
    body: JSON.stringify({ media_id: mediaId ?? null }),
  });
}

export function reviewPanel(
  panelId: number,
  approve: boolean,
  notes: string[] = [],
): Promise<Panel> {
  return api<Panel>(`/api/flowstudio/panels/${panelId}/review`, {
    method: "POST",
    body: JSON.stringify({ approve, notes }),
  });
}

export function reopenPanel(panelId: number): Promise<Panel> {
  return api<Panel>(`/api/flowstudio/panels/${panelId}/reopen`, { method: "POST" });
}

export function addPanelNote(panelId: number, body: string): Promise<Panel> {
  return api<Panel>(`/api/flowstudio/panels/${panelId}/notes`, {
    method: "POST",
    body: JSON.stringify({ body }),
  });
}

/** Tick or untick a remark — the Miro board's "Fixed". */
export function resolvePanelNote(noteId: number, resolved: boolean): Promise<Panel> {
  return api<Panel>(`/api/flowstudio/notes/${noteId}`, {
    method: "PATCH",
    body: JSON.stringify({ resolved }),
  });
}

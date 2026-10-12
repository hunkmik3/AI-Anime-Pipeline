/** Reskin Studio data: one source film's pipeline as /api/studio reports it. */
import { api } from "../api/client";
import { getToken } from "../api/authFetch";
import type { FilmStyle } from "../automation/filmStyles";

export type StationKey = "source" | "shotlist" | "materials" | "clips" | "review";
export type StationStatus = "pending" | "running" | "attention" | "done";
export type ClipStatus = "done" | "rendering" | "preparing" | "prepared" | "failed" | "held" | "reviewing" | "waiting";
export type Delivery = "on_camera" | "offscreen" | "voice_over";
export type MotionProfile = "standard" | "fast" | "pose" | "fast_pose";

export const MOTION_LABEL: Record<MotionProfile, string> = {
  standard: "Clay chuẩn", fast: "Clay nhanh", pose: "Chuẩn + khung xương & máy quay", fast_pose: "Nhanh + khung xương & máy quay",
};

/** The camera measured from the source background for one shot (4D blocking layer). */
export interface CameraShot { shot: number; label: string; pan_pct?: number; tilt_pct?: number; zoom_pct?: number; roll_deg?: number; shake_pct?: number }

const CAMERA_WORDS: Record<string, string> = {
  static: "tĩnh", "push in": "đẩy vào", "pull out": "kéo ra", "pan left": "lia trái", "pan right": "lia phải",
  "tilt up": "ngước lên", "tilt down": "cúi xuống", roll: "nghiêng", handheld: "cầm tay", unclear: "chưa đo được",
};
export const cameraLabel = (label: string) => label.split(" + ").map(w => CAMERA_WORDS[w] ?? w).join(", ");

export interface FilmSettings {
  pipeline?: "cinematic" | "reskin_v1";
  style?: FilmStyle;
  aspect_ratio?: string;
  resolution?: string;
  clip_seconds?: number;
  clip_max_shots?: number;
  timing_policy?: "full_take" | "source_duration";
  motion_profile?: MotionProfile;
  generate_seconds?: number;
  casting_request?: string;
  environment_request?: string;
  review_masters?: boolean;
  pause_for_review?: boolean;
  prepare_before_render?: boolean;
  catalog_parallel?: number;
  kyc?: boolean;
  unmoderated?: boolean;
}

export interface FilmSummary {
  id: string;
  name: string;
  project_id: string | null;
  status: string;
  stage: string | null;
  stage_label: string;
  error: string | null;
  settings: FilmSettings;
  updated_at: string | null;
  progress: { stage?: string; done?: number; total?: number };
}

export interface Station { key: StationKey; label: string; status: StationStatus; summary: string }

export interface Finding {
  clip?: string; shot?: string; key?: string; origin?: string; criterion?: string;
  message?: string; expected?: string; heard?: string; severity: "blocking" | "warning"; accepted: boolean;
}

export interface Line { who?: string; speaker_id?: string | null; line: string; delivery: Delivery; cont: boolean; timing: [number, number] | null }

export type Visibility = "visible" | "partial" | "offscreen" | "occluded";
export interface Presence { asset_id: string; visibility: Visibility; state?: string; position?: string; holder_id?: string | null }

export interface Shot {
  index: number; source_shot: number; start: number; end: number; source_start: number; source_end: number;
  frame: string | null; framing?: string; camera?: string; action: string[]; staging: string;
  characters: string[]; states: Record<string, string>; presence: Presence[]; lines: Line[]; heard: string; findings: Finding[];
}

export interface Take {
  job_id: string; render_url: string; cleaned_url: string | null; at: string | null; set_aside: boolean;
  quality: (Quality & { speech_end_s?: number | null }) | null;
}

export interface Quality { status: string | null; findings: string[]; heard: string; error: string }

export type AudioChoice = "cleaned" | "native" | "silent";

export interface Clip {
  key: string; label: string; source_start: number; source_end: number; seconds: number; estimate_usd: number;
  staging_image_model: string; blocking: Finding[]; finding_count: number;
  shot_count: number; line_count: number; dialogue_text: string; heard_differs: boolean;
  take_id: string | null; take_count: number; take_selected: boolean; audio_choice: AudioChoice;
  staging_url: string | null; staging_model_used: string | null;
  status: ClipStatus; reason: string; hint: string; render_url: string | null; cleaned_url: string | null; url: string | null;
  quality: Quality | null; has_prompt: boolean; unknown_jobs: { id: string; kind: string; slot: string; error: string }[];
  motion_url: string | null; motion_method: string | null; camera: CameraShot[]; motion_profile?: MotionProfile; prompt_by?: string;
  updated_at: string | null; approval: { url: string; by: string; at: string; note?: string } | null;
}

/** One clip in full: its shots, its findings and every take. */
export interface ClipDetail extends Clip { shots: Shot[]; findings: Finding[]; takes: Take[]; revision: number }

export interface MaterialSlot {
  slot: string; label: string; image: string | null; uploaded: boolean; used: boolean;
  status: string; error: string; hint: string; updated_at: string | null;
  versions?: { job_id: string; url: string; media_id: string; at: string | null; model: string | null }[];
}

export interface Material {
  node_id: string; kind: string; name: string; role: string; description: string; target_description: string;
  image_model: string; clips: string[]; required: boolean; slots: MaterialSlot[];
}

export interface Provider { name: string; status: "ok" | "failing" | "idle"; at: string | null; message: string; hint: string }
export interface InboxItem { station: StationKey; kind: string; message: string; clip?: string; shot?: number; job_id?: string; node_id?: string; slot?: string; hint?: string }
export interface StudioFilmFile { filename: string; duration_s: number; at: string; clips: string[]; missing: string[]; timing_policy: string;
  kept_full?: string[]; unchecked?: string[] }
export interface StudioExport { filename: string; at: string; clips: string[]; missing: string[]; bytes: number }
export interface HistoryEntry { id: string; at: string; by: string; kind: string; summary: string; clips: string[] }
export interface Asset { id: string; name: string; kind: string; states: { key: string; label: string }[] }
export interface Impact { clips: string[]; review: boolean; estimate_usd: number; notes?: string[]; moved?: { from: number; to: number } }
export interface Run { id: string; status: string; stage: string | null; created_at: string; keys: string[]; mode: string; errors: string[] }

export interface FilmDetail extends FilmSummary {
  gates: { pause_for_review: boolean; prepare_before_render: boolean; review_masters: boolean; board_approved: boolean };
  providers: Provider[];
  films: StudioFilmFile[];
  stations: Station[];
  clips: Clip[];
  materials: Material[];
  inbox: InboxItem[];
  revision: number | null;
  runs: Run[];
  cost: { clips_rendered: number; seconds: number; seedance_usd: number; images: number; writes: number; note: string;
    budget_usd: number | null; left_usd: number | null } | null;
  exports: StudioExport[];
  history: HistoryEntry[];
  assets: Asset[];
  board_settings?: { imageFallbackModel: string; budgetUsd: number | null; styleNote: string; styleImageUrl: string; motionProfile: string };
  review?: { status: string | null; in_progress: boolean; counts: Record<string, unknown> };
  shared_issues?: string[];
  aspect_ratio?: string;
  style?: string;
  image_model?: string;
  waiting_materials_run?: string | null;
  characters?: { id: string; name: string }[];
}

export const IMAGE_MODEL_LABELS: Record<string, string> = {
  "": "Theo board",
  "dola-seedream-5-0-pro": "Seedream 5 (Avis)",
  "gemini-3-pro-image": "Gemini 3 Pro (Atrium)",
  "gemini-3.1-flash-image": "Gemini 3.1 Flash (Atrium)",
  "gemini-2.5-flash-image": "Gemini 2.5 Flash (Atrium)",
};

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const patch = (body: unknown): RequestInit => ({ method: "PATCH", body: JSON.stringify(body) });
const base = (id: string) => `/api/studio/films/${id}`;

export const studioApi = {
  list: () => api<FilmSummary[]>("/api/studio/films"),
  detail: (id: string) => api<FilmDetail>(base(id)),
  continueFilm: (id: string) => api<FilmSummary>(`${base(id)}/continue`, { method: "POST" }),
  editShot: (id: string, shot: number, revision: number, body: {
    lines?: Line[]; action?: string[]; staging?: string; presence?: Presence[]; character_states?: Record<string, string>; dry_run?: boolean;
  }) => api<Change>(`${base(id)}/shots/${shot}`, patch({ expected_revision: revision, ...body })),
  moveCut: (id: string, shot: number, revision: number, at: number, dryRun = false) =>
    api<Change>(`${base(id)}/shots/${shot}/cut`, json({ expected_revision: revision, at, dry_run: dryRun })),
  editMaterial: (id: string, node: string, revision: number, body: { target_description?: string; image_model?: string; dry_run?: boolean }) =>
    api<Change>(`${base(id)}/materials/${encodeURIComponent(node)}`, patch({ expected_revision: revision, ...body })),
  regenerate: (id: string, node: string, slot: string, revision: number) =>
    api<unknown>(`${base(id)}/materials/${encodeURIComponent(node)}/${encodeURIComponent(slot)}/regenerate`, json({ expected_revision: revision })),
  chooseImage: (id: string, node: string, slot: string, revision: number, url: string, mediaId: string, dryRun = false) =>
    api<Change>(`${base(id)}/materials/${encodeURIComponent(node)}/${encodeURIComponent(slot)}/image`,
      json({ expected_revision: revision, url, media_id: mediaId, dry_run: dryRun })),
  editClip: (id: string, key: string, revision: number, body: {
    staging_image_model?: string; motion_profile?: string; manual_prompt?: string; end_state?: string; auto_prompt?: boolean;
    audio_choice?: AudioChoice; selected_take?: string; dry_run?: boolean;
  }) => api<Change>(`${base(id)}/clips/${key}`, patch({ expected_revision: revision, ...body })),
  clip: (id: string, key: string) => api<ClipDetail>(`${base(id)}/clips/${key}`),
  prompt: (id: string, key: string) => api<{ prompt: string; end_state: string; writer: string; warnings: string[]; at: string | null;
    manual: boolean; verified?: boolean }>(`${base(id)}/clips/${key}/prompt`),
  split: (id: string, key: string, revision: number, atShot: number, dryRun = false) =>
    api<Change>(`${base(id)}/clips/${key}/split`, json({ expected_revision: revision, at_shot: atShot, dry_run: dryRun })),
  mergeNext: (id: string, key: string, revision: number, dryRun = false) =>
    api<Change>(`${base(id)}/clips/${key}/merge-next`, json({ expected_revision: revision, dry_run: dryRun })),
  retake: (id: string, revision: number, keys: string[]) => api<unknown>(`${base(id)}/retake`, json({ expected_revision: revision, keys })),
  recheck: (id: string, key: string) => api<unknown>(`${base(id)}/clips/${key}/quality`, { method: "POST" }),
  controlRun: (id: string, run: string, action: "pause" | "resume") => api<unknown>(`${base(id)}/runs/${run}/${action}`, { method: "POST" }),
  undo: (id: string, entry: string, revision: number) => api<Change>(`${base(id)}/history/${entry}/undo`, json({ expected_revision: revision })),
  editSettings: (id: string, body: Record<string, unknown>) =>
    api<{ revision?: number | null; dry_run?: boolean; impact: Impact; settings?: FilmSettings }>(`${base(id)}/settings`, patch(body)),
  deleteFilm: (id: string) => api<{ ok: boolean }>(base(id), { method: "DELETE" }),
  exportClips: (id: string) => api<StudioExport>(`${base(id)}/export`, { method: "POST" }),
  exportUrl: (id: string, filename: string) => `${base(id)}/export/${filename}`,
  approve: (id: string, key: string, url: string, approved: boolean, note = "") =>
    api<unknown>(`${base(id)}/clips/${key}/approve`, json({ url, approved, note })),
  render: (id: string, revision: number, keys: string[], mode: "prepare" | "render") =>
    api<{ run: { id: string }; superseded: string[] }>(`${base(id)}/render`, json({ expected_revision: revision, keys, mode, retry_failed: true })),
  review: (id: string, revision: number) => api<unknown>(`${base(id)}/review`, json({ expected_revision: revision })),
  accept: (id: string, revision: number, keys: string[], note: string) =>
    api<unknown>(`${base(id)}/accept`, json({ expected_revision: revision, keys, note })),
  markAbsent: (id: string, job: string, note: string) => api<unknown>(`${base(id)}/jobs/${job}/absent`, json({ note })),
  assemble: (id: string) => api<StudioFilmFile>(`${base(id)}/assemble`, { method: "POST" }),
  approveMasters: (projectId: string, runId: string, revision: number) =>
    api<unknown>(`/api/automation/projects/${projectId}/primary-materials/continue`, json({ run_id: runId, expected_revision: revision })),
  filmUrl: (id: string, filename: string) => `${base(id)}/film/${filename}`,
  frameUrl: (videoId: string, frame: string) => `/api/automation/videos/${videoId}/frames/${frame}`,
  sourceUrl: (videoId: string) => `/api/automation/videos/${videoId}/source`,
};

/** Upload a picture for a material sheet; returns its stored URL and media id. */
export async function uploadMaterialImage(projectId: string, file: File): Promise<{ url: string; mediaId: string }> {
  const form = new FormData();
  form.append("file", file);
  const token = getToken();
  const res = await fetch(`/api/automation/projects/${projectId}/upload-image`, {
    method: "POST", body: form, headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `Tải ảnh lỗi (${res.status})`);
  const out = await res.json() as { url: string; reference_url?: string; media_id?: string };
  return { url: out.reference_url || out.url, mediaId: out.media_id ?? "" };
}

/** Upload a source video with the film settings; reports progress 0..1. */
export function uploadFilm(file: File, name: string, settings: FilmSettings, rules: object, language: string,
                           onProgress: (p: number) => void): Promise<{ id: string; automation_project_id: string | null }> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    form.append("name", name);
    form.append("rules", JSON.stringify(rules));
    form.append("detail", "standard");
    form.append("analysis_mode", "one_pass");
    if (language) form.append("language", language);
    form.append("auto_production", JSON.stringify(settings));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/automation/videos");
    const token = getToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
      else {
        let detail = xhr.statusText;
        try { detail = JSON.parse(xhr.responseText).detail ?? detail; } catch { /* plain text */ }
        reject(new Error(typeof detail === "string" ? detail : JSON.stringify(detail)));
      }
    };
    xhr.onerror = () => reject(new Error("Upload bị ngắt."));
    xhr.send(form);
  });
}

export type Change = { revision?: number; dry_run?: boolean; impact: Impact };

/** Ask first what a change reopens; true when the owner goes ahead. */
export async function confirmChange(dryRun: () => Promise<Change>, what: string): Promise<boolean> {
  const { impact } = await dryRun();
  if (!impact.clips.length && !impact.notes?.length) return true;
  const clips = impact.clips.length ? `${impact.clips.length} clip phải làm lại (${impact.clips.slice(0, 6).join(", ")}${impact.clips.length > 6 ? "…" : ""})` : "";
  const cost = impact.estimate_usd ? `, render lại khoảng $${impact.estimate_usd.toFixed(2)}` : "";
  const review = impact.review ? "; cần đối chiếu lại nguồn" : "";
  const notes = impact.notes?.length ? `\n${impact.notes.join("\n")}` : "";
  return window.confirm(`${what}\n${clips}${cost}${review}.${notes}\nTiếp tục?`);
}

export const fmtTime = (s: number) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
export const DELIVERY_LABEL: Record<Delivery, string> = { on_camera: "Trong khung", offscreen: "Ngoài khung", voice_over: "Voice-over" };
export const CLIP_STATUS_LABEL: Record<ClipStatus, string> = {
  done: "Xong", rendering: "Đang render", preparing: "Đang chuẩn bị", prepared: "Có prompt", failed: "Lỗi",
  held: "Bị giữ", reviewing: "Đang đối chiếu", waiting: "Chờ",
};

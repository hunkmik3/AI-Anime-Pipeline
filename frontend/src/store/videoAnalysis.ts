/**
 * Reference videos — the "video mẫu" entry into /automation.
 *
 * A video is uploaded, measured and described on the server (minutes), then
 * adapted into a target world. This store only mirrors that job: it uploads,
 * polls while something is running, and sends the few edits a person makes —
 * rules and glossary. It never holds the board; `importReferenceBoard` on the
 * automation store turns a finished video into nodes.
 */
import { create } from "zustand";

import { api } from "../api/client";
import type { AssetKind, ProductionAsset, SceneInventory, SourceVerification } from "../automation/contracts";

export type VideoStatus =
  | "queued"
  | "analysing"
  | "analysed"
  | "casting"
  | "designing"
  | "conforming"
  | "adapting"
  | "adapted"
  | "failed"
  | "interrupted";

/** One generated reference image kept on a cast entry. */
export interface Plate {
  prompt: string;
  url: string | null;
  reference_url: string | null;
  media_id: string | null;
  persisted: boolean;
  model: string;
  size: string;
  aspect_ratio: string;
  state_key?: string;
  generated_at: string;
}

export interface CharacterState {
  key: string;
  label: string;
  look: string;
  wardrobe: string;
  posture: string;
}

/** A person in the film, in target names, with the shots they appear in. */
export interface CastCharacter {
  source_asset_id?: string;
  key: string;
  name: string;
  source_name?: string;
  role?: string;
  summary?: string;
  identity_anchor?: string;
  looks_like?: string;
  states: CharacterState[];
  shots: number[];
  frames: string[];
  plate?: Plate;
}

export interface CastEnvironment {
  source_asset_id?: string;
  key: string;
  name: string;
  summary?: string;
  lighting?: string;
  mood?: string;
  lock?: string;
  settings?: string[];
  shots: number[];
  frames: string[];
  plate?: Plate;
}

export interface CastAsset {
  key: string;
  id?: string;
  kind: "prop" | "background_group";
  name: string;
  description?: string;
  summary?: string;
  shots: number[];
  frames: string[];
  plate?: Plate;
}

export interface Cast {
  title?: string;
  logline?: string;
  characters?: CastCharacter[];
  environments?: CastEnvironment[];
  assets?: CastAsset[];
  props?: CastAsset[];
  background_groups?: CastAsset[];
  production_assets?: ProductionAsset[];
  source_verification?: SourceVerification;
  shots?: Record<string, { character_keys: string[]; environment_key: string }>;
  usage?: Record<string, unknown>;
}

export interface VideoSummary {
  id: string;
  name: string;
  filename: string;
  automation_project_id: string | null;
  status: VideoStatus;
  progress: { stage?: string; done?: number; total?: number };
  error: string | null;
  duration: number | null;
  aspect_ratio: string | null;
  shot_count: number;
  /** "deep" = more keyframes per shot, smaller vision batches, readier escalation. */
  detail?: "standard" | "deep";
  character_count?: number;
  environment_count?: number;
  adaptation_version: number;
  updated_at: string;
}

/** What the vision pass says about one shot. Timecodes are never in here —
 *  those come from the cut list and live on the shot itself. */
export interface SourceAnalysis {
  shot_size?: string;
  camera_angle?: string;
  camera_movement?: string;
  setting?: string;
  subjects?: string[];
  blocking?: string;
  screen_direction?: string;
  action?: string;
  reaction?: string | null;
  expression?: string | null;
  vfx?: string | null;
  title_card?: string | null;
  subtitle?: string | null;
  confidence?: number;
  uncertain?: string[];
  _model?: string;
}

export interface AnalysedShot {
  shot: number;
  start: number;
  end: number;
  frames: string[];
  /** The whole spoken line that STARTS here — never a slice of a neighbour's. */
  dialogue: string;
  /** What speech recognition caught inside this shot, kept for timing. */
  dialogue_heard?: string;
  /** Set when this shot is the middle of a line that started in shot N. */
  dialogue_continues?: number | null;
  source: SourceAnalysis | null;
}

export interface AdaptedShot {
  shot: number;
  title?: string;
  lens_mm?: string;
  camera?: string;
  action?: string[];
  dialogue?: { who: string; line: string }[];
  performance?: string[];
  avoid?: string[];
  sfx?: string[];
  vfx?: string | null;
  edit_note?: string | null;
  adapted_description?: string;
}

export interface Finding {
  level: "error" | "warning";
  code: string;
  message: string;
  shot: number | null;
}

export interface Validation {
  ok: boolean;
  errors: number;
  warnings: number;
  findings: Finding[];
}

export type Glossary = Record<"characters" | "sects" | "locations" | "techniques", Record<string, string>>;

export interface Rules {
  visual_style: string;
  character_names: string;
  sect_names: string;
  location_names: string;
  technique_names: string;
  dialogue_mode: "literal" | "cinematic";
  dialogue_language: string;
  preserve_editing: boolean;
}

/** Every spoken line in order, each owned by one shot (see the backend's
 *  dialogue.py: a burned-in subtitle stays on screen across cuts). */
export interface DialogueLine {
  text: string;
  first_shot: number;
  last_shot: number;
  source: "subtitle" | "asr";
}

export interface VideoDetail extends VideoSummary {
  analysis: {
    scene_inventory?: SceneInventory;
    source_verification?: SourceVerification;
    video?: { duration: number; fps: number; width: number; height: number; aspect_ratio: string; has_audio: boolean };
    cuts?: { kept: number[]; candidates: number; rejected: { at: number; reason: string }[] };
    transcript?: { start: number; end: number; text: string }[];
    dialogue_track?: DialogueLine[];
    speech_error?: string | null;
    story_errors?: string[];
    shots?: AnalysedShot[];
    sequences?: { first_shot: number; last_shot: number; title: string; goal?: string; conflict?: string }[];
    entities?: Record<string, { source_name: string; aliases?: string[]; title?: string | null; visual?: string }[]>;
    validation?: Validation;
    usage?: Record<string, unknown>;
    timings_s?: Record<string, number>;
  };
  adaptation: {
    rules?: Rules;
    glossary?: Glossary;
    shots?: Record<string, AdaptedShot>;
    validation?: Validation;
    usage?: Record<string, unknown>;
  };
  cast: Cast;
}

/** Two looks the board's prompt builders know how to write. The text is what
 *  the adaptation model reads; the board picks its preset from it. */
export const STYLE_PRESETS: { key: "anime" | "realistic" | "cg3d"; label: string; text: string }[] = [
  {
    key: "anime",
    label: "Anime 2D",
    text:
      "Modern Japanese 2D anime. Clean thin line art, 2-3 solid cel-shading tones, minimal gradients. " +
      "Live-action energy becomes hand-drawn impact frames, smears, speed lines and controlled bloom.",
  },
  {
    key: "cg3d",
    label: "3D CGI",
    text:
      "Modern 3D CGI animated feature. Stylised-realistic character design, physically based " +
      "rendering, subsurface skin, simulated cloth and hair, soft global illumination and " +
      "cinematic depth of field. Effects are rendered volumetrics and simulation, not drawn.",
  },
  {
    key: "realistic",
    label: "Live-action điện ảnh",
    text:
      "Cinematic realistic live-action drama. Natural skin and fabric detail, motivated practical lighting, " +
      "shallow depth of field. Energy effects stay grounded and physical.",
  },
];

export const DEFAULT_RULES: Rules = {
  visual_style: STYLE_PRESETS[0].text,
  character_names: "Japanese names (family name first), e.g. Kagari Ren",
  sect_names: "English, e.g. Heavenly Mountains Sect",
  location_names: "English, e.g. Summit of Light",
  technique_names: "English, e.g. Immortal-Slaying Sword Formation",
  dialogue_mode: "literal",
  dialogue_language: "English",
  preserve_editing: true,
};

export const RUNNING: VideoStatus[] = ["queued", "analysing", "casting", "designing", "conforming", "adapting"];

export const STAGE_LABELS: Record<string, string> = {
  probe: "đọc thông tin video",
  cuts: "dò điểm cắt",
  keyframes: "trích keyframe",
  speech: "nghe thoại",
  vision: "xem từng shot",
  story: "chia sequence, tìm tên riêng",
  glossary: "lập bảng tên",
  adapt: "chuyển thể từng shot",
  cast: "lập hồ sơ nhân vật & bối cảnh",
  inventory: "Agent 1 · thống kê nhân vật, đám đông và đạo cụ",
  source_verify: "Agent 1 · đối chiếu video gốc",
  source_layers: "phân biệt nội dung cảnh và chữ trên màn hình",
  source_identity: "đối chiếu nhân vật và đồ vật",
  source_protocol_review: "kiểm lại kết luận đối chiếu",
  source_refine: "sửa mô tả và đối chiếu từng shot",
  source_context: "đối chiếu thêm ngữ cảnh các shot",
  design: "thiết kế tạo hình",
  conform: "gỡ trang phục bản gốc khỏi shot",
  validate: "kiểm tra",
};

/** Image engines this board can drive, with the ceiling each really delivers. */
export interface ImageModelOption {
  id: string;
  label: string;
  max: string;
}
export const IMAGE_MODEL_LABELS: Record<string, string> = {
  "gemini-3-pro-image": "Nano Banana Pro",
  "gemini-3.1-flash-image": "Nano Banana 2",
  "gemini-2.5-flash-image": "Nano Banana",
  "dola-seedream-5-0-pro": "Seedream 5.0 Pro",
};

export const frameUrl = (videoId: string, frame: string) =>
  `/api/automation/videos/${videoId}/frames/${frame.split("/").pop()}`;

interface VideoAnalysisStore {
  videos: VideoSummary[];
  openId: string | null;
  detail: VideoDetail | null;
  uploading: number | null; // 0..1 while the file is on its way up
  error?: string;
  /** Submission is pending; the current analysis stays visible. */
  refiningId: string | null;

  loadVideos(projectId: string | null): Promise<void>;
  upload(file: File, projectId: string | null, rules: Rules, detail?: "standard" | "deep"): Promise<void>;
  open(id: string | null): Promise<void>;
  refresh(): Promise<void>;
  adapt(rules: Rules, glossary?: Glossary, fresh?: boolean): Promise<void>;
  resume(): Promise<void>;
  verify(): Promise<void>;
  refine(): Promise<void>;
  /** Accept, after reading them, the findings Agent 1 left unresolved. */
  acceptVerification(note?: string): Promise<void>;
  saveGlossary(glossary: Glossary): Promise<void>;
  /** Read the cast and places off the analysis (one model call, ~1-2 min). */
  readCast(): Promise<void>;
  /** Generate one character sheet or environment plate and keep it. */
  generatePlate(
    kind: AssetKind,
    key: string,
    opts: { model: string; size: string; stateKey?: string },
  ): Promise<void>;
  /** Keys currently generating, so each card can show its own spinner. */
  generating: string[];
  remove(id: string): Promise<void>;
}

let pollTimer: ReturnType<typeof setTimeout> | undefined;

export const useVideoAnalysis = create<VideoAnalysisStore>()((set, get) => {
  // Poll only while something is running, and only as fast as a stage moves.
  const schedule = (projectId: string | null) => {
    clearTimeout(pollTimer);
    const busy =
      get().videos.some((v) => RUNNING.includes(v.status)) ||
      (get().detail && RUNNING.includes(get().detail!.status));
    if (!busy) return;
    pollTimer = setTimeout(async () => {
      await get().loadVideos(projectId);
      if (get().openId) await get().refresh();
    }, 2500);
  };

  let lastProject: string | null = null;

  return {
    videos: [],
    openId: null,
    detail: null,
    uploading: null,
    generating: [],
    refiningId: null,

    async loadVideos(projectId) {
      lastProject = projectId;
      try {
        const qs = projectId ? `?project_id=${projectId}` : "";
        set({ videos: await api<VideoSummary[]>(`/api/automation/videos${qs}`) });
      } catch (err) {
        set({ error: (err as Error).message });
      }
      schedule(projectId);
    },

    upload(file, projectId, rules, detail = "standard") {
      // XHR, not fetch: an upload of tens of megabytes needs a progress bar,
      // and fetch still cannot report upload progress.
      return new Promise<void>((resolve, reject) => {
        const form = new FormData();
        form.append("file", file);
        form.append("name", file.name.replace(/\.[^.]+$/, ""));
        if (projectId) form.append("project_id", projectId);
        form.append("rules", JSON.stringify(rules));
        form.append("detail", detail);
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/automation/videos");
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) set({ uploading: e.loaded / e.total });
        };
        xhr.onload = async () => {
          set({ uploading: null });
          if (xhr.status >= 200 && xhr.status < 300) {
            const created = JSON.parse(xhr.responseText) as VideoSummary;
            await get().loadVideos(projectId);
            await get().open(created.id);
            resolve();
          } else {
            let detail = xhr.statusText;
            try {
              detail = JSON.parse(xhr.responseText).detail ?? detail;
            } catch {
              /* plain-text error */
            }
            set({ error: String(detail) });
            reject(new Error(String(detail)));
          }
        };
        xhr.onerror = () => {
          set({ uploading: null, error: "Upload bị ngắt." });
          reject(new Error("Upload bị ngắt."));
        };
        set({ uploading: 0, error: undefined });
        xhr.send(form);
      });
    },

    async open(id) {
      set({ openId: id, detail: id && get().detail?.id === id ? get().detail : null });
      if (id) await get().refresh();
    },

    async refresh() {
      const id = get().openId;
      if (!id) return;
      try {
        const detail = await api<VideoDetail>(`/api/automation/videos/${id}`);
        if (get().openId === id) set({ detail });
      } catch (err) {
        set({ error: (err as Error).message });
      }
      schedule(lastProject);
    },

    async adapt(rules, glossary, fresh = false) {
      const id = get().openId;
      if (!id) return;
      set({ error: undefined });
      try {
        await api(`/api/automation/videos/${id}/adapt`, {
          method: "POST",
          body: JSON.stringify({ rules, glossary: glossary ?? null, fresh }),
        });
      } catch (err) {
        set({ error: (err as Error).message });
      }
      await get().refresh();
      await get().loadVideos(lastProject);
    },

    async resume() {
      const id = get().openId;
      if (!id) return;
      try {
        await api(`/api/automation/videos/${id}/analyze`, { method: "POST" });
      } catch (err) {
        set({ error: (err as Error).message });
      }
      await get().refresh();
    },

    async readCast() {
      const id = get().openId;
      if (!id) return;
      set({ error: undefined });
      try {
        await api(`/api/automation/videos/${id}/cast`, { method: "POST" });
      } catch (err) {
        set({ error: (err as Error).message });
      }
      await get().refresh();
    },

    async verify() {
      const id = get().openId;
      if (!id) return;
      set({ error: undefined });
      try {
        await api(`/api/automation/videos/${id}/verify`, { method: "POST" });
      } catch (err) {
        set({ error: (err as Error).message });
      }
      await get().refresh();
    },

    async refine() {
      const id = get().openId;
      if (!id || get().refiningId === id || (get().detail && RUNNING.includes(get().detail!.status))) return;
      // Do not clear analysis, cast or adaptation while the new result is built.
      set({ error: undefined, refiningId: id });
      try {
        await api(`/api/automation/videos/${id}/refine`, { method: "POST" });
      } catch (err) {
        set({ error: (err as Error).message });
      } finally {
        if (get().openId === id) await get().refresh();
        await get().loadVideos(lastProject);
        if (get().refiningId === id) set({ refiningId: null });
      }
    },

    async acceptVerification(note = "") {
      const id = get().openId;
      if (!id) return;
      set({ error: undefined });
      try {
        await api(`/api/automation/videos/${id}/verify/accept`, { method: "POST", body: JSON.stringify({ note }) });
      } catch (err) {
        set({ error: (err as Error).message });
      }
      await get().refresh();
    },

    async generatePlate(kind, key, opts) {
      const id = get().openId;
      if (!id) return;
      const token = `${kind}:${key}`;
      set({ generating: [...get().generating, token], error: undefined });
      try {
        await api(`/api/automation/videos/${id}/plate`, {
          method: "POST",
          body: JSON.stringify({
            kind,
            key,
            state_key: opts.stateKey ?? "",
            image_model: opts.model,
            image_size: opts.size,
          }),
        });
        await get().refresh();
      } catch (err) {
        set({ error: (err as Error).message });
      } finally {
        set({ generating: get().generating.filter((t) => t !== token) });
      }
    },

    async saveGlossary(glossary) {
      const id = get().openId;
      if (!id) return;
      await api(`/api/automation/videos/${id}/glossary`, {
        method: "PATCH",
        body: JSON.stringify({ glossary }),
      });
      await get().refresh();
    },

    async remove(id) {
      await api(`/api/automation/videos/${id}`, { method: "DELETE" });
      if (get().openId === id) set({ openId: null, detail: null });
      await get().loadVideos(lastProject);
    },
  };
});

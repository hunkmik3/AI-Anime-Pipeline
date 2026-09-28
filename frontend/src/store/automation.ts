/**
 * Drama-film automation — the /automation demo board.
 *
 * A premise goes into one node; the breakdown fans out into a character node
 * per person, an environment node per place, and one node per sequence.
 * The cast and environment nodes generate their reference plates; each
 * sequence is cut into shots and then becomes exactly one clip, wired to the
 * material that clip is made from.
 *
 * Deliberately NOT the shotWorkflow store. That one carries a shot/scene/
 * project identity this surface does not have, and models every node as a row.
 * A board here is one row with one JSON blob, saved through
 * `/api/automation/projects` — the server is the record, and it autosaves.
 * localStorage only remembers which board was open and the toolbar choices.
 */
import type { Edge, Node } from "@xyflow/react";
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type EdgeChange,
  type NodeChange,
} from "@xyflow/react";
import { create, type StateCreator } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import { api } from "../api/client";
import { contractFingerprint, isStrictBoard, sameFingerprint, sourceReadyForShots, type AssetKind, type AssetPresence, type ProductionAsset, type PromptCoverage, type ReferenceAsset, type SourceVerification } from "../automation/contracts";

export type AutoStatus = "idle" | "running" | "done" | "error";

export interface RuntimeJob {
  id: string; kind: "clip" | "plate" | "write" | "source" | "raccord" | "production_run" | "extract_frame" | "assemble" | "ingest" | "material_binding" | "atlas"; node_id: string; slot: string; status: string;
  provider_job_id: string; error: string; result: Record<string, any>;
}

export interface CharacterState {
  key: string;
  label: string;
  look: string;
  wardrobe: string;
  posture: string;
}

export interface Character {
  source_asset_id?: string;
  plate?: ImportedPlate;
  key: string;
  name: string;
  role: string;
  summary: string;
  identity_anchor: string;
  states: CharacterState[];
}

export interface Environment {
  source_asset_id?: string;
  plate?: ImportedPlate;
  key: string;
  name: string;
  summary: string;
  lighting: string;
  mood: string;
  lock: string;
}

interface ImportedPlate {
  prompt?: string;
  url?: string | null;
  reference_url?: string | null;
  media_id?: string | null;
  state_key?: string;
}

export interface Asset {
  key: string;
  id?: string;
  kind: "prop" | "background_group";
  name: string;
  description?: string;
  summary?: string;
  shots?: number[];
  frames?: string[];
  plate?: ImportedPlate;
}

export interface DialogueLine {
  who: string;
  line: string;
}

export interface ShotPackage {
  version: string; sequence_key: string; ready: boolean; policy: string;
  materials: Record<string, { asset_id: string; name: string; kind: string; reference_url: string; wardrobe: string }>;
  shots: { id: string; index: number; material_keys: string[]; keyframe_recommended: boolean; keyframe_reasons: string[]; action: unknown; dialogue: unknown; inherited_asset_ids: string[]; raccord?: { direction: string; scene_rule: string; mode: string; predecessor_shot_id?: string | null } }[];
  issues: { code: string; blocking: boolean; asset?: string; shot?: string; message?: string }[];
}

export interface Sequence {
  production_context?: Record<string, unknown>;
  shot_package?: ShotPackage;
  asset_keys?: string[];
  scene_present_asset_ids?: string[];
  key: string;
  label: string;
  title: string;
  duration_s: number;
  summary: string;
  beat: string;
  environment_key: string;
  character_keys: string[];
  /** "cut" when the sequence re-makes an edited stretch of a reference video:
   *  the clip cuts between its shots instead of playing as one take. */
  editing?: "cut";
  /** Where in the reference video this clip comes from, e.g. "00:12.30–00:25.10 · shots 012–019". */
  source_range?: string;
}

/** A shot as a director writes it, not as a summary.
 *
 *  The list fields are the point. An earlier version had `action`,
 *  `performance` and `sfx` as single strings, and the cuts came out curt —
 *  the video model filled the gaps itself and always chose the broadest
 *  reading. `avoid` matters most: naming the wrong take is what stops it.
 *
 *  Older boards still hold the string form, so everything that reads these
 *  goes through `asLines`. */
export interface Shot {
  shot_uid?: string;
  source_start?: number;
  source_end?: number;
  scene_id?: string;
  continuity_events?: { type?: string; asset_id: string; field?: string; to?: unknown; reason?: string }[];
  source_shots?: number[];
  source_evidence?: unknown[];
  asset_presence?: AssetPresence[];
  source_appearances?: unknown[];
  scene_present_asset_ids?: string[];
  environment_key?: string;
  character_states?: Record<string, string>;
  n: number;
  title?: string;
  duration_s: number;
  framing: string;
  /** A range like "70-100". Older boards hold a single number. */
  lens_mm: string | number;
  camera?: string;
  /** Where things sit in frame — foreground, midground, background, eye path. */
  framing_note?: string;
  /** The shot's own light: key direction, colour, what changes while it runs. */
  lighting?: string;
  action: string[] | string;
  dialogue: DialogueLine[];
  performance?: string[] | string;
  /** What the model must NOT do. The single most load-bearing field. */
  avoid?: string[] | string;
  sfx?: string[] | string;
  edit_note?: string;
  character_keys: string[];
  /** Reference-video shots only: the shot number and timecode it re-makes. */
  source_shot?: number;
  source_tc?: string;
}

/** Read a field that is a list now and was a plain string before. */
export function asLines(value: string[] | string | undefined): string[] {
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  return Array.isArray(value) ? value.map((v) => String(v).trim()).filter(Boolean) : [];
}

/** One generated plate, plus the public copy downstream plates reference. */
export interface Plate {
  prompt: string;
  status: AutoStatus;
  error?: string;
  image?: string;
  /** Public URL Atrium can fetch. Absent when R2 could not take the file. */
  referenceUrl?: string;
  /** Media row for this plate. Only a media_id can become a KYC identity
   *  asset — the Avis KYC endpoint takes a cached file, never a URL. */
  mediaId?: string;
}

export interface ScriptNodeData extends Record<string, unknown> {
  kind: "script";
}

export interface CharacterNodeData extends Record<string, unknown> {
  kind: "character";
  character: Character;
  /** The master face. Every state sheet passes this back in as Image 1. */
  identity: Plate;
  /** Keyed by state key. */
  states: Record<string, Plate>;
  activeState: string;
}

export interface EnvironmentNodeData extends Record<string, unknown> {
  kind: "environment";
  environment: Environment;
  plate: Plate;
}

export interface AssetNodeData extends Record<string, unknown> {
  kind: "asset";
  asset: Asset;
  plate: Plate;
}

/** One node per sequence, not one list for the film.
 *
 *  A sequence is the unit everything downstream works in: it is cut into shots
 *  on its own, and it becomes exactly one clip. Giving it a node lets the
 *  board draw what a clip actually consumes — this sequence, these cast
 *  sheets, this location — instead of one fat list wired to everything. */
export interface SequenceNodeData extends Record<string, unknown> {
  kind: "sequence";
  sequence: Sequence;
  shots: Shot[];
  cutStatus: AutoStatus;
  cutError?: string;
  /** What this sequence has to establish for the audience. */
  functionOf?: string[];
  /** Continuity locks for this sequence — screen direction, prop positions. */
  raccord?: string[];
  /** Where this sequence leaves everyone. The next cut opens from it, which
   *  is what stops the film reading as fourteen unrelated scenes. */
  exitState?: string;
}

/** One reference image bound to an `@imageN` slot. Position IS the binding —
 *  Seedance matches the Nth uploaded image to `@imageN` — so this list and the
 *  prompt that names the labels are always built together, never separately. */
export interface VideoRef {
  assetId?: string;
  /** Several distinct source assets may share one labeled reference atlas. */
  assetBindings?: { assetId: string; name: string; kind: AssetKind; description?: string }[];
  label: string;
  name: string;
  url: string;
  /** Present on cast plates; KYC needs it, plain references do not. */
  mediaId?: string;
  kind: AssetKind;
}

/** One clip per sequence. Seedance will not go under 4s and gets unstable past
 *  ~20s, which is the band our sequences already sit in; the shots inside
 *  become the timestamped slices of the prompt rather than separate clips. */
export interface VideoNodeData extends Record<string, unknown> {
  coverage?: PromptCoverage;
  contractDigest?: string;
  coverageToken?: string;
  inputFingerprint?: string;
  kind: "video";
  sequenceKey: string;
  label: string;
  title: string;
  durationS: number;
  prompt: string;
  refs: VideoRef[];
  /** The clip's first and last frame, generated as images.
   *
   *  With both present the clip is generated by keyframe interpolation: it
   *  starts on one picture we made and ends on another, which is the only way
   *  two consecutive clips match across a cut. The provider refuses to mix
   *  that with reference images, so in this mode the cast sheets are used to
   *  MAKE the frames instead of being sent to the video model. */
  startFrame?: Plate;
  endFrame?: Plate;
  shotFrames?: Record<string, Plate & { package_version?: string; shot_id?: string }>;
  /** Continue from the clip before this one instead of from references.
   *  The provider takes a VIDEO of real people where it refuses a still of
   *  them, so this is the live-action way to stop the cast drifting. */
  chainFromPrevious?: boolean;
  status: AutoStatus;
  error?: string;
  clipUrl?: string;
  /** False when the clip is only a short-lived provider link (no R2). */
  persisted?: boolean;
  warnings?: string[];
  /** Who wrote the prompt: the writer model's name, "template", or "manual". */
  promptBy?: string;
  /** Where this clip ends — positions, props, mood — as the writer described
   *  it. The next clip's prompt opens from it. */
  endState?: string;
}

/** The pre-split node: one list for the whole film, wired to everything.
 *  Deliberately OUTSIDE AutoNodeData — nothing may create one any more, and
 *  only `ensureGraph` still knows the shape, purely to take it apart. */
interface LegacyShotlistData {
  kind: "shotlist";
  sequences: Sequence[];
  shots: Record<string, Shot[]>;
}

function asLegacyShotlist(node: AutoNode | undefined): LegacyShotlistData | null {
  const data = node?.data as unknown as LegacyShotlistData | undefined;
  return data?.kind === "shotlist" ? data : null;
}

export type AutoNodeData =
  | ScriptNodeData
  | CharacterNodeData
  | EnvironmentNodeData
  | AssetNodeData
  | SequenceNodeData
  | VideoNodeData;

export type AutoNode = Node<AutoNodeData>;

/** A saved board, as the sidebar lists it — no graph, so listing is cheap. */
export interface ProjectSummary {
  revision?: number;
  id: string;
  name: string;
  title: string;
  logline: string;
  runtime_seconds: number | null;
  updated_at: string;
}

export type SaveState = "idle" | "saving" | "saved" | "error";

/** An environment plate is a set drawing, not a frame: wide, whatever ratio the
 *  film is cut in. Matches the backend's automation.ENVIRONMENT_ASPECT. */
export const ENVIRONMENT_ASPECT = "21:9";

/** The look every prompt on the board is written in. Premise boards are
 *  live-action; a reference video can be re-made as anime. */
export type BoardStyle = "realistic" | "anime" | "cg3d";

interface ReferenceBoardResponse extends BreakdownResponse {
  production_assets?: ProductionAsset[];
  source_verification?: SourceVerification;
  shots: Record<string, Shot[]>;
  style: BoardStyle;
  aspect_ratio: string | null;
}

export interface Capabilities {
  image_models: string[];
  default_image_model: string;
  /** model id → the largest resolution it really delivers ("2K" / "4K"). */
  image_model_max: Record<string, string>;
  image_sizes: string[];
  default_image_size: string;
  atrium_configured: boolean;
  /** Seedream runs on Avis, so one engine can be live while the other is not. */
  avis_configured: boolean;
  /** False when R2 is unset: plates still generate, but faces will not match. */
  reference_chain: boolean;
}

interface BreakdownResponse {
  assets?: Asset[];
  title: string;
  logline: string;
  runtime_seconds: number;
  characters: Character[];
  environments: Environment[];
  sequences: Sequence[];
}

interface PlateResponse {
  images: { url: string; reference_url: string | null; media_id: string | null; persisted: boolean }[];
}

const emptyPlate = (): Plate => ({ prompt: "", status: "idle" });
const importedPlate = (plate?: ImportedPlate): Plate => plate ? {
  prompt: plate.prompt ?? "",
  status: plate.reference_url || plate.url ? "done" : "idle",
  image: plate.reference_url ?? plate.url ?? undefined,
  referenceUrl: plate.reference_url ?? undefined,
  mediaId: plate.media_id ?? undefined,
} : emptyPlate();

// Five lanes left→right: premise, cast, places, sequences, clips. A sequence
// and its clip share a row so the pair reads across.
// Five lanes left→right: premise, cast, places, sequences, clips. The x gaps
// clear the widest node in the lane to its left (nodes are 330-400px); the
// heights below are what a node of that kind actually occupies once its
// picture, its prompt row and its tags are rendered — a fixed 300px row was
// laid out before sheets and keyframes existed and left them overlapping.
const LANE_X = { character: 520, environment: 1000, asset: 1000, sequence: 1480, video: 1960 };
const NODE_HEIGHT = { character: 780, environment: 700, sequence: 340, video: 560 };
const ROW_GAP = 48;

/** Stack every lane top to bottom so nothing overlaps.
 *
 *  A sequence and its clip share a row, because the pair reads across; the row
 *  is as tall as the taller of the two. Cast and places are stacked on their
 *  own, in the order the breakdown produced them. */
export function layoutBoard(nodes: AutoNode[]): AutoNode[] {
  const order = (kind: AutoNodeData["kind"]) =>
    nodes
      .filter((n) => n.data.kind === kind)
      .sort((a, b) => a.position.y - b.position.y || a.id.localeCompare(b.id))
      .map((n) => n.id);

  const y: Record<string, number> = {};
  let top = 0;
  for (const id of order("character")) {
    y[id] = top;
    top += NODE_HEIGHT.character + ROW_GAP;
  }
  top = 0;
  for (const id of [...order("environment"), ...order("asset")]) {
    y[id] = top;
    top += NODE_HEIGHT.environment + ROW_GAP;
  }
  top = 0;
  for (const id of order("sequence")) {
    y[id] = top;
    const clip = id.replace(/^seq:/, "vid:");
    y[clip] = top;
    top += Math.max(NODE_HEIGHT.sequence, NODE_HEIGHT.video) + ROW_GAP;
  }

  return nodes.map((n) => {
    if (n.data.kind === "script") return { ...n, position: { x: 0, y: 0 } };
    const lane = LANE_X[n.data.kind as keyof typeof LANE_X];
    if (lane === undefined || y[n.id] === undefined) return n;
    return { ...n, position: { x: lane, y: y[n.id] } };
  });
}

export const IMAGE_MODEL_LABELS: Record<string, string> = {
  "gemini-3-pro-image": "Nano Banana Pro",
  "gemini-3.1-flash-image": "Nano Banana 2",
  "gemini-2.5-flash-image": "Nano Banana",
  "dola-seedream-5-0-pro": "Seedream 5.0 Pro",
};

interface AutomationStore {
  productionAssets: ProductionAsset[];
  sourceVerification?: SourceVerification;
  script: string;
  runtimeSeconds: number | null;
  title: string;
  logline: string;
  breakdownStatus: AutoStatus;
  breakdownError?: string;

  // Kept beside the nodes because cutting a sequence needs the film's cast and
  // places, and digging them back out of node data would couple the shotlist
  // node to every character node on the board.
  characters: Character[];
  environments: Environment[];
  style: BoardStyle;

  nodes: AutoNode[];
  edges: Edge[];

  imageModel: string;
  /** 1K / 2K / 4K, capped per model by the backend. */
  imageSize: string;
  aspectRatio: string;
  /** Route clips through the B2B path, which does not refuse photoreal
   *  people. Off = the moderated endpoint, which rejects every cast sheet
   *  this pipeline makes. */
  unmoderated: boolean;
  /** Person-driven path: every reference becomes an Avis identity asset.
   *  This is what lets photoreal cast through — the ordinary reference path
   *  refuses them ("input image may contain real person"). */
  kyc: boolean;
  capabilities: Capabilities | null;

  projects: ProjectSummary[];
  projectRevision: number;
  jobs: RuntimeJob[];
  preserveSourceShots: boolean;
  setPreserveSourceShots: (value: boolean) => void;
  refreshJobs: () => Promise<void>;
  currentProjectId: string | null;
  saveState: SaveState;
  saveError?: string;


  loadProjects(): Promise<void>;
  createProject(name: string): Promise<void>;
  openProject(id: string): Promise<void>;
  renameProject(id: string, name: string): Promise<void>;
  deleteProject(id: string): Promise<void>;
  /** Write the open board to the server now. Autosave calls this; the toolbar
   *  can too, for someone who wants to be sure before closing the tab. */
  saveNow(): Promise<void>;

  setScript(text: string): void;
  setRuntimeSeconds(seconds: number | null): void;
  setImageModel(model: string): void;
  setImageSize(size: string): void;
  setAspectRatio(ratio: string): void;
  setUnmoderated(on: boolean): void;
  setKyc(on: boolean): void;

  loadCapabilities(): Promise<void>;
  runBreakdown(): Promise<void>;
  /** Replace the board with one built from an analysed reference video:
   *  cast, places, and clips whose shots are already cut. */
  importReferenceBoard(videoId: string): Promise<void>;
  /** Take a verified report for the same source inventory this board was cut
   *  from — a reviewer's acceptance. False (and nothing changes) otherwise. */
  adoptSourceVerification(report: SourceVerification | undefined): boolean;
  reset(): void;

  onNodesChange(changes: NodeChange<AutoNode>[]): void;
  onEdgesChange(changes: EdgeChange[]): void;
  onConnect(connection: Connection): void;

  patchNode(id: string, patch: Partial<AutoNodeData>): void;
  setActiveState(id: string, stateKey: string): void;
  editPrompt(id: string, slot: string, prompt: string): void;
  /** Fill any prompt still blank from the house template. */
  primePrompts(): Promise<void>;
  generate(id: string, slot: string): Promise<void>;
  cutSequence(sequenceKey: string): Promise<void>;
  cutAll(): Promise<void>;
  cuttingAll: boolean;

  /** Generate the first or last frame of a clip, from the cast sheets and the
   *  location plate — the two pictures the clip will be pinned to. */
  generateKeyframe(sequenceKey: string, which: "start" | "end"): Promise<void>;
  /** How long one clip may run when a reference video is brought onto a board. */
  clipSeconds: number;
  setClipSeconds(seconds: number): void;

  /** Re-assemble a sequence's Seedance prompt from whatever the board holds
   *  now — cast sheets, environment plate, and the shots as timestamp slices.
   *  Cheap (no model call), so it is safe to call again after any edit. */
  collectVideoRefs(sequenceKey: string): {
    refs: VideoRef[];
    characters: Record<string, unknown>[];
    environment: Record<string, unknown> | null;
    /** The STYLE paragraph of the first cast sheet's prompt — the medium the
     *  references were drawn in, which the clip prompt must describe. */
    styleNote: string;
    referenceAssets: ReferenceAsset[];
  };
  collectAssetDependencies(asset: Asset): ReferenceAsset[];
  promptContract(sequenceKey: string): Record<string, unknown>;
  ensureGraph(): void;
  /** Re-stack every lane so nothing overlaps. Positions are the user's to
   *  change, so this only happens when they ask for it. */
  relayout(): void;
  backfillMediaIds(urls: string[]): Promise<void>;
  /** Write a sequence's clip prompt. `writer` has the prompt writer (GPT)
   *  write it in the house standard, opening from the previous clip's end
   *  state; without it the template assembles it for free. */
  primeVideoPrompt(sequenceKey: string, opts?: { writer?: boolean }): Promise<void>;
  /** contractFingerprint(promptContract(key)), recomputed only when something
   *  the contract is built from has changed. Safe to call from a selector. */
  currentFingerprint(sequenceKey: string): string;
  generateClip(sequenceKey: string): Promise<void>;

  /** The whole board as a file, and back again. Autosave covers a reload;
   *  this covers a cleared browser, a different machine, and handing the
   *  board to someone else. */
  /** Every generated clip, zipped in running order. */
  downloadClips(): Promise<void>;
  /** How many clips exist right now — drives the button's enabled state. */
  clipCount(): number;
  /** Every generated sheet and plate, zipped under its subject's own name. */
  downloadPlates(): Promise<void>;
  /** How many sheets and plates exist right now. */
  plateCount(): number;
  exportBoard(): void;
  importBoard(json: string): void;
}

type GenerationSettings = Pick<AutomationStore,
  "aspectRatio" | "imageModel" | "imageSize" | "clipSeconds" | "unmoderated" | "kyc">;

/** Accept only toolbar settings this install can represent. Missing legacy
 * fields retain the existing defaults; a project with saved settings owns them. */
function boardSettings(board: Partial<GenerationSettings>, caps: Capabilities | null): Partial<GenerationSettings> {
  const settings: Partial<GenerationSettings> = {};
  if (board.aspectRatio === "16:9" || board.aspectRatio === "9:16") settings.aspectRatio = board.aspectRatio;
  const models = caps?.image_models ?? Object.keys(IMAGE_MODEL_LABELS);
  if (typeof board.imageModel === "string" && models.includes(board.imageModel)) settings.imageModel = board.imageModel;
  if (typeof board.imageSize === "string" && ["1K", "2K", "4K"].includes(board.imageSize)) settings.imageSize = board.imageSize;
  if (typeof board.clipSeconds === "number" && [10, 15, 20, 25, 30].includes(board.clipSeconds)) settings.clipSeconds = board.clipSeconds;
  if (typeof board.unmoderated === "boolean") settings.unmoderated = board.unmoderated;
  if (typeof board.kyc === "boolean") settings.kyc = board.kyc;
  return settings;
}

/** The board as it travels — to a file, and to the `board` JSON column. */
export interface BoardFile extends Partial<GenerationSettings> {
  preserveSourceShots?: boolean;
  productionAssets?: ProductionAsset[];
  sourceVerification?: SourceVerification;
  characters?: Character[];
  environments?: Environment[];
  style?: BoardStyle;
  nodes?: AutoNode[];
  edges?: Edge[];
}


/** The node pair a sequence owns — the sequence itself and its clip — plus
 *  the edges that say what the clip is made from.
 *
 *  The wiring is the point: an edge from each cast sheet and from the location
 *  the sequence plays in, so the board shows what a clip consumes instead of
 *  hanging every clip off one shared list. Same builder for a fresh breakdown
 *  and for backfilling an older board, so the two can never drift. */
function sequencePair(
  seq: Sequence,
  index: number,
  characters: Character[],
  shots: Shot[] = [],
): { nodes: AutoNode[]; edges: Edge[] } {
  const seqId = `seq:${seq.key}`;
  const vidId = `vid:${seq.key}`;
  const known = new Set(characters.map((c) => c.key));

  const edges: Edge[] = [
    { id: `e-script-${seqId}`, source: "script", target: seqId },
    { id: `e-${seqId}-${vidId}`, source: seqId, target: vidId },
  ];
  for (const key of seq.character_keys ?? []) {
    if (!known.has(key)) continue;
    edges.push({ id: `e-char:${key}-${vidId}`, source: `char:${key}`, target: vidId });
  }
  for (const key of seq.asset_keys ?? []) {
    edges.push({ id: `e-asset:${key}-${vidId}`, source: `asset:${key}`, target: vidId });
  }
  if (seq.environment_key) {
    edges.push({
      id: `e-env:${seq.environment_key}-${vidId}`,
      source: `env:${seq.environment_key}`,
      target: vidId,
    });
  }

  return {
    nodes: [
      {
        id: seqId,
        type: "autoSequence",
        position: { x: LANE_X.sequence, y: index * (NODE_HEIGHT.sequence + ROW_GAP) },
        data: { kind: "sequence", sequence: seq, shots, cutStatus: shots.length ? "done" : "idle" },
      },
      {
        id: vidId,
        type: "autoVideo",
        position: { x: LANE_X.video, y: index * (NODE_HEIGHT.video + ROW_GAP) },
        data: {
          kind: "video",
          sequenceKey: seq.key,
          label: seq.label,
          title: seq.title,
          durationS: seq.duration_s,
          prompt: "",
          refs: [],
          status: "idle",
        },
      },
    ],
    edges,
  };
}

/** Everything a clip's prompt contract is assembled from, by identity. React
 *  Flow keeps a node's `data` object across moves, so dragging leaves these
 *  unchanged and the (large) contract is not rebuilt for every video node on
 *  every frame. */
const fingerprintCache = new Map<string, { parts: unknown[]; value: string }>();

function contractParts(state: AutomationStore, sequenceKey: string): unknown[] {
  const parts: unknown[] = [state.productionAssets, state.sourceVerification, state.style,
    state.aspectRatio, previousVideo(state.nodes, sequenceKey)?.endState ?? ""];
  for (const n of state.nodes) {
    if (n.id === `seq:${sequenceKey}` || n.data.kind === "character"
      || n.data.kind === "environment" || n.data.kind === "asset") parts.push(n.data);
  }
  return parts;
}

/** The STYLE section of a sheet prompt: its heading line's own text, if any,
 *  and the lines after it up to the next capitalised heading. */
function styleSection(prompt: string): string {
  const lines = prompt.split("\n");
  const at = lines.findIndex((l) => /^\s*STYLE\s*:?/.test(l));
  if (at < 0) return "";
  const out = [lines[at].replace(/^\s*STYLE\s*:?\s*/, "")];
  for (const line of lines.slice(at + 1)) {
    if (/^[A-Z][A-Z0-9 /&'().—–-]*[A-Z0-9)]:?\s*$/.test(line.trim())) break;
    out.push(line);
  }
  return out.join(" ").replace(/\s+/g, " ").trim().slice(0, 900);
}

/** The video node of the clip before this one, in board order. */
function previousVideo(nodes: AutoNode[], sequenceKey: string): VideoNodeData | undefined {
  const ordered = nodes
    .filter((n) => n.data.kind === "sequence")
    .sort((a, b) => a.position.y - b.position.y)
    .map((n) => (n.data.kind === "sequence" ? n.data.sequence.key : ""));
  const at = ordered.indexOf(sequenceKey);
  if (at <= 0) return undefined;
  const prev = nodes.find((n) => n.id === `vid:${ordered[at - 1]}`);
  return prev?.data.kind === "video" ? prev.data : undefined;
}

/** Nodes and edges for a fresh board — shared by the premise breakdown and a
 *  reference video, so both lay out and wire identically. ``shots`` pre-fills
 *  sequences that arrive already cut. */
function boardFrom(
  out: Pick<BreakdownResponse, "characters" | "environments" | "sequences" | "assets">,
  shots: Record<string, Shot[]> = {},
): { nodes: AutoNode[]; edges: Edge[] } {
  const nodes: AutoNode[] = [
    { id: "script", type: "autoScript", position: { x: 0, y: 0 }, data: { kind: "script" } },
  ];
  const edges: Edge[] = [];

  out.characters.forEach((character, i) => {
    const id = `char:${character.key}`;
    const states: Record<string, Plate> = {};
    for (const state of character.states) states[state.key] = emptyPlate();
    if (character.plate?.state_key) states[character.plate.state_key] = importedPlate(character.plate);
    nodes.push({
      id,
      type: "autoCharacter",
      position: { x: LANE_X.character, y: i * (NODE_HEIGHT.character + ROW_GAP) },
      data: {
        kind: "character",
        character,
        identity: importedPlate(character.plate),
        states,
        activeState: character.plate?.state_key || character.states[0]?.key || "",
      },
    });
    edges.push({ id: `e-script-${id}`, source: "script", target: id });
  });

  out.environments.forEach((environment, i) => {
    const id = `env:${environment.key}`;
    nodes.push({
      id,
      type: "autoEnvironment",
      position: { x: LANE_X.environment, y: i * (NODE_HEIGHT.environment + ROW_GAP) },
      data: { kind: "environment", environment, plate: importedPlate(environment.plate) },
    });
    edges.push({ id: `e-script-${id}`, source: "script", target: id });
  });

  for (const [i, asset] of (out.assets ?? []).entries()) {
    const id = `asset:${asset.key}`;
    nodes.push({ id, type: "autoAsset", position: { x: LANE_X.asset, y: i * 748 },
      data: { kind: "asset", asset, plate: importedPlate(asset.plate) } });
    edges.push({ id: `e-script-${id}`, source: "script", target: id });
  }

  for (const [i, seq] of out.sequences.entries()) {
    const { nodes: pair, edges: links } = sequencePair(seq, i, out.characters, shots[seq.key] ?? []);
    nodes.push(...pair);
    edges.push(...links);
  }

  return { nodes: layoutBoard(nodes), edges };
}

const BLANK_SCRIPT_NODE: AutoNode = {
  id: "script",
  type: "autoScript",
  position: { x: 0, y: 0 },
  data: { kind: "script" },
};

// Loading a board writes the whole graph at once. Without this, that write
// looks exactly like a user edit and bounces straight back to the server —
// which at best wastes a round trip and at worst saves a half-applied board.
let autosavePaused = false;
// Ignore a model response if the user moved to another film while it ran.
let boardEpoch = 0;
function suspendAutosave(fn: () => void): void {
  autosavePaused = true;
  try {
    fn();
  } finally {
    autosavePaused = false;
  }
}

/** Drop pictures that are pixels; keep the ones that are links.
 *
 *  A published plate is an R2 URL — a hundred bytes, and the whole reason a
 *  board reopens with its artwork intact in another tab. A plate that could
 *  NOT be published falls back to a `data:` URL of several megabytes, and that
 *  must never reach the saved board: one of them turns every autosave into a
 *  multi-megabyte PATCH. Such a plate shows in this tab and dies with it. */
function stripInlineImages(nodes: AutoNode[]): AutoNode[] {
  const linked = (p: Plate): Plate =>
    p.image?.startsWith("data:") ? { ...p, image: undefined } : p;

  return nodes.map((n) => {
    if (n.data.kind === "character") {
      const states: Record<string, Plate> = {};
      for (const [k, p] of Object.entries(n.data.states)) states[k] = linked(p);
      return { ...n, data: { ...n.data, identity: linked(n.data.identity), states } };
    }
    if (n.data.kind === "environment" || n.data.kind === "asset") {
      return { ...n, data: { ...n.data, plate: linked(n.data.plate) } };
    }
    return n;
  });
}

/** Un-stick a board saved mid-generation.
 *
 *  A plate's "running" lives in the board JSON, so a generation cut short —
 *  the tab closed, the server restarted, the laptop slept — reopens as a node
 *  that says "đang gen…" forever, with its button disabled and no way back.
 *  Nothing in flight survives a reload, so on open every "running" is a
 *  generation that already died: turn it into a failure the user can retry. */
const INTERRUPTED = "Lần gen trước bị ngắt giữa chừng — bấm gen lại.";

function clearStuckRunning(nodes: AutoNode[]): AutoNode[] {
  const revive = (p: Plate): Plate =>
    p.status === "running" && !(p as Plate & { runtimeJobId?: string }).runtimeJobId ? { ...p, status: "error", error: INTERRUPTED } : p;

  return nodes.map((n) => {
    if (n.data.kind === "character") {
      const states: Record<string, Plate> = {};
      for (const [k, p] of Object.entries(n.data.states)) states[k] = revive(p);
      return { ...n, data: { ...n.data, identity: revive(n.data.identity), states } } as AutoNode;
    }
    if (n.data.kind === "environment" || n.data.kind === "asset") {
      return { ...n, data: { ...n.data, plate: revive(n.data.plate) } } as AutoNode;
    }
    if (n.data.kind === "video" && n.data.status === "running" && !(n.data as VideoNodeData & { runtimeJobId?: string }).runtimeJobId) {
      return { ...n, data: { ...n.data, status: "error", error: INTERRUPTED } } as AutoNode;
    }
    if (n.data.kind === "sequence" && n.data.cutStatus === "running") {
      return { ...n, data: { ...n.data, cutStatus: "error", cutError: INTERRUPTED } } as AutoNode;
    }
    return n;
  });
}

/** `slot` is "identity" on a character's master face, a state key on one of its
 *  sheets, and "plate" on an environment — one addressing scheme for all three
 *  so generate/edit do not each need a shape switch. */
function readPlate(data: AutoNodeData, slot: string): Plate | undefined {
  if (data.kind === "character") {
    return slot === "identity" ? data.identity : data.states[slot];
  }
  if (data.kind === "environment" || data.kind === "asset") return data.plate;
  return undefined;
}

function writePlate(data: AutoNodeData, slot: string, patch: Partial<Plate>): AutoNodeData {
  if (data.kind === "character") {
    if (slot === "identity") return { ...data, identity: { ...data.identity, ...patch } };
    const current = data.states[slot] ?? emptyPlate();
    return { ...data, states: { ...data.states, [slot]: { ...current, ...patch } } };
  }
  if (data.kind === "environment" || data.kind === "asset") return { ...data, plate: { ...data.plate, ...patch } };
  return data;
}

let saveQueue: Promise<void> = Promise.resolve();
class PendingJobError extends Error {}
const runningJob = (status: string) => ["queued", "preparing", "submitting", "running"].includes(status);

export function applyRuntimeJobs(nodes: AutoNode[], jobs: RuntimeJob[]): AutoNode[] {
  const output = nodes.map((n) => ({ ...n, data: { ...n.data } }));
  const latest = new Map<string, RuntimeJob>();
  for (const job of jobs) latest.set(`${job.node_id}:${job.kind}:${job.slot}`, job);
  for (const job of latest.values()) {
    const n = output.find((item) => item.id === job.node_id);
    if (!n) continue;
    if (job.kind === "write" && n.data.kind === "video") {
      const out=job.result;
      const sequenceKey = n.data.sequenceKey;
      const seq = output.find((item) => item.id === `seq:${sequenceKey}`);
      const sameShots = seq?.data.kind === "sequence" && JSON.stringify(seq.data.shots) === JSON.stringify(out.source_shots);
      if (job.status === "succeeded" && sameShots && [out.base_prompt,out.prompt].includes(n.data.prompt)) {
        n.data={...n.data,prompt:out.prompt,durationS:out.duration_seconds,endState:out.end_state,promptBy:out.writer,
          coverage:out.coverage,contractDigest:out.contract_digest,coverageToken:out.coverage_token,inputFingerprint:out.input_fingerprint};
      }
      continue;
    }
    if (job.kind === "source" || job.kind === "raccord") continue;
    const status: AutoStatus = runningJob(job.status) ? "running" : job.status === "succeeded" ? "done" : "error";
    const patch: Record<string, unknown> = { status, runtimeJobId: job.id, runtimeStatus: job.status, error: job.error || undefined };
    if (job.status === "succeeded") {
      if (job.kind === "clip") Object.assign(patch, { clipUrl: job.result.url, persisted: job.result.persisted, warnings: job.result.warnings, jobId: job.provider_job_id });
      else {
        const image = job.result.images?.[0];
        if (image) Object.assign(patch, { image: image.url, referenceUrl: image.reference_url, mediaId: image.media_id });
      }
    }
    if (job.kind === "clip" && n.data.kind === "video") n.data = { ...n.data, ...patch } as AutoNodeData;
    else if (job.kind === "plate" && n.data.kind === "video" && job.slot.startsWith("shotframe:"))
      n.data = { ...n.data, shotFrames: { ...n.data.shotFrames, [job.slot]: { ...emptyPlate(), ...n.data.shotFrames?.[job.slot], ...patch, ...job.result.shot_frame } } };
    else if (job.kind === "plate" && n.data.kind === "video" && ["startFrame", "endFrame"].includes(job.slot))
      n.data = { ...n.data, [job.slot]: { ...(n.data as any)[job.slot], ...patch } };
    else if (job.kind === "plate") n.data = writePlate(n.data, job.slot, patch as Partial<Plate>);
  }
  return output;
}

async function durableRequest<T>(path: string, init: RequestInit, nodeId: string, slot: string): Promise<T> {
  const state = useAutomation.getState();
  const projectId = state.currentProjectId;
  if (!projectId) return api<T>(path, init); // Legacy unsaved draft, no board to persist into.
  await state.saveNow();
  if (useAutomation.getState().saveState === "error") throw new Error(useAutomation.getState().saveError ?? "Save failed");
  const payload = JSON.parse(String(init.body));
  const fingerprint = contractFingerprint({ projectId, path, payload, nodeId, slot });
  const storageKey = "automation-pending:" + fingerprint;
  const requestKey = safeLocalStorage.getItem(storageKey) || `${fingerprint}:${crypto.randomUUID()}`;
  safeLocalStorage.setItem(storageKey, requestKey);
  let job: RuntimeJob;
  try {
    job = await api<RuntimeJob>(`/api/automation/projects/${projectId}/jobs`, { method: "POST", body: JSON.stringify({
      kind: path.endsWith("/clip") ? "clip" : path.endsWith("/write") ? "write" : "plate", input_fingerprint: path.endsWith("/write") ? contractFingerprint(payload) : "", node_id: nodeId, slot, payload, request_key: requestKey,
      expected_revision: useAutomation.getState().projectRevision,
    }) });
  } catch (error) {
    // Keep key after an ambiguous HTTP failure, so another click deduplicates.
    throw error;
  }
  const apply = () => {
    if (useAutomation.getState().currentProjectId !== projectId) return;
    suspendAutosave(() => useAutomation.setState({ nodes: applyRuntimeJobs(useAutomation.getState().nodes, [job]) }));
  };
  apply();
  let failures = 0;
  while (runningJob(job.status)) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    try { job = await api<RuntimeJob>(`/api/automation/projects/${projectId}/jobs/${job.id}`); failures = 0; apply(); }
    catch {
      if (++failures >= 5) throw new PendingJobError("Mất kết nối theo dõi; job vẫn được lưu trên server. Mở lại board để tiếp tục.");
    }
  }
  if (job.status === "unknown") throw new Error(job.error || "Cần đối soát job trước khi gen lại.");
  safeLocalStorage.removeItem(storageKey);
  if (job.status !== "succeeded") throw new Error(job.error || "Job failed");
  return job.result as T;
}

/** Called by writer/keyframe preparation, never a user approval gate. */
export async function ensureRaccord(sequenceKey = ""): Promise<void> {
  const state = useAutomation.getState();
  const projectId = state.currentProjectId;
  if (!projectId) return;
  await state.saveNow();
  if (useAutomation.getState().saveState === "error") throw new Error(useAutomation.getState().saveError ?? "Save failed");
  if (useAutomation.getState().currentProjectId !== projectId) throw new Error("Board changed while preparing scene");
  let jobs = await api<RuntimeJob[]>(`/api/automation/projects/${projectId}/raccord`, { method: "POST", body: JSON.stringify({
    sequence_key: sequenceKey, expected_revision: useAutomation.getState().projectRevision,
  }) });
  let failures = 0;
  while (jobs.some((j) => runningJob(j.status))) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    if (useAutomation.getState().currentProjectId !== projectId) throw new Error("Đã chuyển board; kế hoạch vẫn chạy trên server.");
    try {
      jobs = await Promise.all(jobs.map((j) => runningJob(j.status) ? api<RuntimeJob>(`/api/automation/projects/${projectId}/jobs/${j.id}`) : Promise.resolve(j)));
      failures = 0;
    } catch {
      if (++failures >= 5) throw new PendingJobError("Mất kết nối theo dõi raccord; server vẫn giữ công việc.");
    }
  }
  const failed = jobs.find((j) => j.status !== "succeeded");
  if (failed) throw new Error(failed.error || "Scene planning failed");
  await useAutomation.getState().refreshJobs();
}

const createBoard: StateCreator<AutomationStore> = (set, get) => ({
  script: "",
  runtimeSeconds: null,
  title: "",
  logline: "",
  breakdownStatus: "idle",
  characters: [],
  environments: [],
  productionAssets: [],
  sourceVerification: undefined,
  style: "realistic",
  nodes: [
    { id: "script", type: "autoScript", position: { x: 0, y: 0 }, data: { kind: "script" } },
  ],
  edges: [],
  imageModel: "gemini-3-pro-image",
  imageSize: "2K",
  clipSeconds: 20,
  aspectRatio: "16:9",
  unmoderated: true,
  kyc: false,
  capabilities: null,

  projects: [],
  projectRevision: 0,
  jobs: [],
  preserveSourceShots: true,
  setPreserveSourceShots: (value) => set({ preserveSourceShots: value }),
  async refreshJobs() {
    const projectId = get().currentProjectId;
    if (!projectId) return;
    const jobs = await api<RuntimeJob[]>(`/api/automation/projects/${projectId}/jobs`);
    if (projectId !== get().currentProjectId) return;
    suspendAutosave(() => set({ jobs, nodes: applyRuntimeJobs(get().nodes, jobs) }));
  },
  currentProjectId: null,
  saveState: "idle",
  cuttingAll: false,

  async loadProjects() {
    try {
      set({ projects: await api<ProjectSummary[]>("/api/automation/projects") });
    } catch {
      // The rail degrades to "no boards yet" rather than blocking the canvas.
    }
  },

  async createProject(name) {
    const created = await api<ProjectSummary & { board: BoardFile }>(
      "/api/automation/projects",
      { method: "POST", body: JSON.stringify({ name }) },
    );
    await get().loadProjects();
    // A new board is empty, so open it by resetting rather than by loading.
    suspendAutosave(() => {
      get().reset();
      set({ currentProjectId: created.id, projectRevision: created.revision ?? 0, jobs: [], saveState: "saved" });
    });
  },

  async openProject(id) {
    const epoch = ++boardEpoch;
    const p = await api<ProjectSummary & { script: string; board: BoardFile }>(
      `/api/automation/projects/${id}`,
    );
    if (epoch !== boardEpoch) return;
    suspendAutosave(() => {
      const board = p.board ?? {};
      set({
        currentProjectId: id,
        projectRevision: p.revision ?? 0,
        jobs: [],
        preserveSourceShots: board.preserveSourceShots ?? true,
        title: p.title ?? "",
        logline: p.logline ?? "",
        script: p.script ?? "",
        runtimeSeconds: p.runtime_seconds ?? null,
        productionAssets: board.productionAssets ?? [],
        sourceVerification: board.sourceVerification,
        characters: board.characters ?? [],
        environments: board.environments ?? [],
        style: board.style ?? "realistic",
        ...boardSettings(board, get().capabilities),
        nodes: board.nodes?.length ? clearStuckRunning(board.nodes) : [BLANK_SCRIPT_NODE],
        edges: board.edges ?? [],
        breakdownStatus: board.nodes?.length ? "done" : "idle",
        breakdownError: undefined,
        saveState: "saved",
      });
    });
    get().ensureGraph();
    void get().refreshJobs().catch(() => {});
    void get().primePrompts();
  },

  async renameProject(id, name) {
    const updated = await api<ProjectSummary>(`/api/automation/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    });
    if (get().currentProjectId === id) set({ projectRevision: updated.revision ?? get().projectRevision });
    await get().loadProjects();
  },

  async deleteProject(id) {
    await api(`/api/automation/projects/${id}`, { method: "DELETE" });
    if (get().currentProjectId === id) {
      suspendAutosave(() => {
        get().reset();
        set({ currentProjectId: null, saveState: "idle" });
      });
    }
    await get().loadProjects();
  },

  async saveNow() {
    const requestedProject = get().currentProjectId;
    const perform = async () => {
    const s = get();
    if (s.currentProjectId !== requestedProject) return;
    if (!s.currentProjectId) return;
    set({ saveState: "saving", saveError: undefined });
    try {
      const saved = await api<ProjectSummary>(`/api/automation/projects/${s.currentProjectId}`, {
        method: "PATCH",
        body: JSON.stringify({
          expected_revision: s.projectRevision,
          script: s.script,
          title: s.title,
          logline: s.logline,
          runtime_seconds: s.runtimeSeconds,
          // Images are stripped for the same reason autosave strips them: a
          // handful of 4K data URLs is tens of megabytes of JSON per save.
          board: {
            ...boardSettings(s, s.capabilities),
            preserveSourceShots: s.preserveSourceShots,
            productionAssets: s.productionAssets,
            sourceVerification: s.sourceVerification,
            characters: s.characters,
            environments: s.environments,
            style: s.style,
            nodes: stripInlineImages(s.nodes),
            edges: s.edges,
          },
        }),
      });
      if (get().currentProjectId !== requestedProject) return;
      set({ saveState: "saved", projectRevision: saved?.revision ?? s.projectRevision });
      void get().loadProjects(); // refresh the rail's title / timestamp
    } catch (err) {
      if (get().currentProjectId !== requestedProject) return;
      set({ saveState: "error", saveError: (err as Error).message });
    }
    };
    saveQueue = saveQueue.then(perform, perform);
    await saveQueue;
  },

  setScript: (text) => set({ script: text }),
  setRuntimeSeconds: (seconds) => set({ runtimeSeconds: seconds }),
  setImageModel: (model) => set({ imageModel: model }),
  setImageSize: (size) => set({ imageSize: size }),
  setClipSeconds: (seconds) => set({ clipSeconds: seconds }),
  setAspectRatio: (ratio) => set({ aspectRatio: ratio }),
  setUnmoderated: (on) => set({ unmoderated: on }),
  setKyc: (on) => set({ kyc: on }),

  async loadCapabilities() {
    try {
      const caps = await api<Capabilities>("/api/automation/capabilities");
      // Loading capabilities may finish after a project opens. Do not replace
      // its saved model with the installation default in that race.
      const model = get().imageModel;
      set({ capabilities: caps, imageModel: get().currentProjectId && caps.image_models.includes(model)
        ? model : caps.default_image_model });
    } catch {
      // The banner is an affordance, not the feature. A failed probe leaves it
      // hidden rather than blocking a board the user can still drive.
    }
  },

  async runBreakdown() {
    const { script, runtimeSeconds } = get();
    if (!script.trim() || get().breakdownStatus === "running") return;
    const epoch = ++boardEpoch;
    set({ breakdownStatus: "running", breakdownError: undefined });

    try {
      const out = await api<BreakdownResponse>("/api/automation/breakdown", {
        method: "POST",
        body: JSON.stringify({ script, runtime_seconds: runtimeSeconds }),
      });
      if (epoch !== boardEpoch) return;

      const { nodes, edges } = boardFrom(out);
      set({
        nodes,
        edges,
        title: out.title,
        logline: out.logline,
        characters: out.characters,
        environments: out.environments,
        runtimeSeconds: out.runtime_seconds || runtimeSeconds,
        productionAssets: [],
        sourceVerification: undefined,
        breakdownStatus: "done",
      });
      void get().primePrompts();
    } catch (err) {
      if (epoch !== boardEpoch) return;
      set({ breakdownStatus: "error", breakdownError: (err as Error).message });
    }
  },

  adoptSourceVerification(report) {
    const current = get().sourceVerification;
    // Accepting changes the verdict, never an observation, so the digest holds.
    // Another digest is another inventory: the shots on this board were cut
    // from the old one and need a fresh import, not a new label.
    if (report?.status !== "verified" || !current?.digest || report.digest !== current.digest) return false;
    set({ sourceVerification: report });
    return true;
  },

  async importReferenceBoard(videoId) {
    if (get().breakdownStatus === "running") return;
    const epoch = ++boardEpoch;
    set({ breakdownStatus: "running", breakdownError: undefined });
    try {
      const out = await api<ReferenceBoardResponse>(`/api/automation/videos/${videoId}/board`, {
        method: "POST",
        body: JSON.stringify({ clip_seconds: get().clipSeconds, preserve_source_shots: get().preserveSourceShots }),
      });
      if (epoch !== boardEpoch) return;
      const { nodes, edges } = boardFrom(out, out.shots);
      set({
        nodes,
        edges,
        title: out.title,
        logline: out.logline,
        characters: out.characters,
        environments: out.environments,
        productionAssets: out.production_assets ?? [],
        sourceVerification: out.source_verification,
        style: out.style,
        runtimeSeconds: out.runtime_seconds,
        // Place plates in the reference's own frame, so a vertical short re-makes vertical.
        aspectRatio: out.aspect_ratio === "9:16" || out.aspect_ratio === "16:9" ? out.aspect_ratio : get().aspectRatio,
        // An anime cast is not a real person; the identity path would only get in the way.
        kyc: out.style !== "realistic" ? false : get().kyc,
        breakdownStatus: "done",
      });
      void get().primePrompts();
    } catch (err) {
      if (epoch !== boardEpoch) return;
      set({ breakdownStatus: "error", breakdownError: (err as Error).message });
      throw err;
    }
  },

  reset: () => {
    boardEpoch += 1;
    set({
      preserveSourceShots: true,
      jobs: [],
      title: "",
      logline: "",
      characters: [],
      environments: [],
      productionAssets: [],
      sourceVerification: undefined,
      style: "realistic",
      breakdownStatus: "idle",
      breakdownError: undefined,
      nodes: [
        { id: "script", type: "autoScript", position: { x: 0, y: 0 }, data: { kind: "script" } },
      ],
      edges: [],
    });
  },

  onNodesChange: (changes) => set({ nodes: applyNodeChanges(changes, get().nodes) }),
  onEdgesChange: (changes) => set({ edges: applyEdgeChanges(changes, get().edges) }),
  onConnect: (connection) => set({ edges: addEdge(connection, get().edges) }),

  patchNode: (id, patch) =>
    set({
      nodes: get().nodes.map((n) =>
        n.id === id ? ({ ...n, data: { ...n.data, ...patch } } as AutoNode) : n,
      ),
    }),

  setActiveState: (id, stateKey) =>
    set({
      nodes: get().nodes.map((n) =>
        n.id === id && n.data.kind === "character"
          ? ({ ...n, data: { ...n.data, activeState: stateKey } } as AutoNode)
          : n,
      ),
    }),

  editPrompt: (id, slot, prompt) =>
    set({
      nodes: get().nodes.map((n) =>
        n.id === id ? ({ ...n, data: writePlate(n.data, slot, { prompt }) } as AutoNode) : n,
      ),
    }),

  async primePrompts() {
    const epoch = boardEpoch;
    const { nodes } = get();
    // Ask for every blank prompt at once. They are pure assembly on the
    // backend — no model call — so the round trips cost nothing but latency.
    const asks: Promise<void>[] = [];

    for (const node of nodes) {
      if (node.data.kind === "character") {
        const { character, identity, states } = node.data;
        if (!identity.prompt) {
          asks.push(
            api<{ prompt: string }>("/api/automation/prompt", {
              method: "POST",
              body: JSON.stringify({
                kind: "character",
                character,
                state: character.states[0] ?? {},
                has_reference: false,
                style: get().style,
              }),
            }).then(({ prompt }) => { if (epoch === boardEpoch) get().editPrompt(node.id, "identity", prompt); }),
          );
        }
        for (const state of character.states) {
          if (states[state.key]?.prompt) continue;
          asks.push(
            api<{ prompt: string }>("/api/automation/prompt", {
              method: "POST",
              body: JSON.stringify({
                kind: "character",
                character,
                state,
                has_reference: true,
                style: get().style,
              }),
            }).then(({ prompt }) => { if (epoch === boardEpoch) get().editPrompt(node.id, state.key, prompt); }),
          );
        }
      } else if (node.data.kind === "asset" && !node.data.plate.prompt) {
        const dependencies = get().collectAssetDependencies(node.data.asset);
        asks.push(api<{ prompt: string }>("/api/automation/prompt", {
          method: "POST", body: JSON.stringify({ kind: node.data.asset.kind, asset: node.data.asset,
            style: get().style, aspect_ratio: "16:9", dependency_references: dependencies,
            has_reference: dependencies.length > 0 }),
        }).then(({ prompt }) => { if (epoch === boardEpoch) get().editPrompt(node.id, "plate", prompt); }));
      } else if (node.data.kind === "environment" && !node.data.plate.prompt) {
        asks.push(
          api<{ prompt: string }>("/api/automation/prompt", {
            method: "POST",
            body: JSON.stringify({
              kind: "environment",
              environment: node.data.environment,
              aspect_ratio: ENVIRONMENT_ASPECT,
              style: get().style,
            }),
          }).then(({ prompt }) => { if (epoch === boardEpoch) get().editPrompt(node.id, "plate", prompt); }),
        );
      }
    }
    await Promise.allSettled(asks);
  },

  async cutSequence(sequenceKey) {
    const { characters, environments } = get();
    const id = `seq:${sequenceKey}`;
    const node = get().nodes.find((n) => n.id === id);
    if (!node || node.data.kind !== "sequence" || node.data.cutStatus === "running") return;
    const sequence = node.data.sequence;

    // Hand over the neighbours. The previous sequence's exit state is what the
    // cut opens from; without it every sequence starts cold on an establishing
    // wide and the film reads as a slideshow.
    const ordered = get()
      .nodes.filter((n) => n.data.kind === "sequence")
      .sort((a, b) => a.position.y - b.position.y);
    const at = ordered.findIndex((n) => n.id === id);
    const prev = at > 0 ? ordered[at - 1] : undefined;
    const next = at >= 0 ? ordered[at + 1] : undefined;
    const prevData = prev?.data.kind === "sequence" ? prev.data : undefined;
    const nextData = next?.data.kind === "sequence" ? next.data : undefined;

    get().patchNode(id, { cutStatus: "running", cutError: undefined } as Partial<AutoNodeData>);
    try {
      const out = await api<{
        shots: Shot[];
        function: string[];
        raccord: string[];
        exit_state: string;
      }>("/api/automation/shots", {
        method: "POST",
        body: JSON.stringify({
          sequence,
          characters,
          environments,
          previous_exit: prevData?.exitState ?? "",
          previous_label: prevData?.sequence.label ?? "",
          next_summary: nextData?.sequence.summary ?? "",
          same_location:
            prevData?.sequence.environment_key === sequence.environment_key && Boolean(prevData),
        }),
      });
      get().patchNode(id, {
        cutStatus: "done",
        shots: out.shots,
        functionOf: out.function,
        raccord: out.raccord,
        exitState: out.exit_state,
        cutError: undefined,
      } as Partial<AutoNodeData>);
    } catch (err) {
      get().patchNode(id, {
        cutStatus: "error",
        cutError: (err as Error).message,
      } as Partial<AutoNodeData>);
    }
  },

  /** Cut every sequence, in running order, so each one inherits the exit
   *  state of the one before it. Cutting them individually and out of order
   *  is what leaves the film disjointed, so this is the button that should
   *  normally be used. */
  async cutAll() {
    const ordered = get()
      .nodes.filter((n) => n.data.kind === "sequence")
      .sort((a, b) => a.position.y - b.position.y);
    set({ cuttingAll: true });
    try {
      for (const node of ordered) {
        if (node.data.kind !== "sequence") continue;
        await get().cutSequence(node.data.sequence.key);
        const after = get().nodes.find((n) => n.id === node.id);
        // Stop on the first failure: every cut after this one would inherit a
        // missing exit state, which is the very thing being fixed.
        if (after?.data.kind === "sequence" && after.data.cutStatus === "error") break;
      }
    } finally {
      set({ cuttingAll: false });
    }
  },

  /** Bring any board up to the current graph shape.
   *
   *  Two jobs, both idempotent. It SPLITS the old single "shotlist" node —
   *  boards made before sequences had nodes of their own carry one fat list —
   *  and it fills in any sequence/clip pair that is missing. Run on open and
   *  on import so an older board is repaired rather than half-working.
   *
   *  Legacy layout carried each sequence's shots in a map on that one node,
   *  so the split has to hand them down; losing them would mean re-cutting
   *  every sequence, which costs a model call each. */
  ensureGraph() {
    const { nodes, edges, characters } = get();
    const legacyNode = nodes.find((n) => asLegacyShotlist(n) !== null);
    const legacy = asLegacyShotlist(legacyNode);

    const sequences: Sequence[] =
      legacy?.sequences ??
      nodes.flatMap((n) => (n.data.kind === "sequence" ? [n.data.sequence] : []));
    if (!sequences.length) return;

    const legacyShots: Record<string, Shot[]> = legacy?.shots ?? {};

    let nextNodes = legacyNode ? nodes.filter((n) => n.id !== legacyNode.id) : [...nodes];
    let nextEdges = legacyNode
      ? edges.filter((e) => e.source !== legacyNode.id && e.target !== legacyNode.id)
      : [...edges];

    for (const [i, seq] of sequences.entries()) {
      const existing = nextNodes.find((n) => n.id === `seq:${seq.key}`);
      const shots =
        existing?.data.kind === "sequence" && existing.data.shots.length
          ? existing.data.shots
          : (legacyShots[seq.key] ?? []);
      const { nodes: pair, edges: links } = sequencePair(seq, i, characters, shots);

      for (const node of pair) {
        if (!nextNodes.some((n) => n.id === node.id)) nextNodes = [...nextNodes, node];
      }
      for (const edge of links) {
        // Only add edges whose endpoints are actually on the board — a
        // sequence can name a character the breakdown never produced.
        const ok = nextNodes.some((n) => n.id === edge.source) && nextNodes.some((n) => n.id === edge.target);
        if (ok && !nextEdges.some((e) => e.id === edge.id)) nextEdges = [...nextEdges, edge];
      }
    }
    set({ nodes: nextNodes, edges: nextEdges });
  },

  relayout() {
    set({ nodes: layoutBoard(get().nodes) });
  },

  /** Gather the reference images a sequence needs, in @imageN order.
   *
   *  Only PUBLISHED plates qualify: Seedance fetches references over HTTP, so
   *  a picture that never made it to R2 is not a reference at all. Cast comes
   *  first in the sequence's own order, then the location — and the labels are
   *  handed to the prompt builder so the two can never drift apart. */
  collectVideoRefs(sequenceKey) {
    const { nodes, productionAssets } = get();
    const seqNode = nodes.find((n) => n.id === `seq:${sequenceKey}`);
    if (!seqNode || seqNode.data.kind !== "sequence") {
      return { refs: [], characters: [], environment: null, styleNote: "", referenceAssets: [] };
    }
    const seq = seqNode.data.sequence;
    const shots = seqNode.data.shots;
    const needed = new Set([
      ...Object.values(seq.shot_package?.materials ?? {}).map((m) => m.asset_id),
      ...(seq.asset_keys ?? []), ...(seq.scene_present_asset_ids ?? []),
      ...shots.flatMap((shot) => [...(shot.scene_present_asset_ids ?? []), ...(shot.asset_presence ?? []).map((p) => p.asset_id)]),
    ]);
    // A container or a crowd plate may require other assets to be represented.
    let grew = true;
    while (grew) {
      grew = false;
      for (const asset of productionAssets) if (needed.has(asset.id)) {
        for (const id of [...(asset.depends_on_asset_ids ?? []), ...(asset.member_ids ?? [])]) {
          if (!needed.has(id)) { needed.add(id); grew = true; }
        }
      }
    }
    const refs: VideoRef[] = [];
    const referenceAssets: ReferenceAsset[] = [];
    const characters: Record<string, unknown>[] = [];
    let styleNote = "";
    const add = (id: string, name: string, kind: AssetKind, plate?: Plate, extra = true,
      targetDescription?: string): string | undefined => {
      if (!plate?.referenceUrl) return undefined;
      // Atlas cell positions and approved target designs belong to the board
      // asset. Keep source observations immutable and use them only as fallback.
      const description = targetDescription?.trim() || productionAssets.find((asset) => asset.id === id)?.description;
      const binding = { assetId: id, name, kind, description };
      const existing = refs.find((reference) => reference.url === plate.referenceUrl);
      if (refs.some((reference) => reference.assetBindings?.some((entry) => entry.assetId === id))) {
        throw new Error(`Tài sản ${id} bị gắn reference lặp lại.`);
      }
      if (existing && (existing.mediaId ?? "") !== (plate.mediaId ?? "")) {
        throw new Error(`Ảnh chung của ${existing.name} và ${name} có Media ID khác nhau; đồng bộ ảnh tham chiếu trước khi gen.`);
      }
      const label = existing?.label ?? `@image${refs.length + 1}`;
      if (existing) {
        existing.assetBindings!.push(binding);
        existing.name = existing.assetBindings!.map((entry) => entry.name).join(" + ");
      } else {
        refs.push({ label, name, kind, assetId: id, assetBindings: [binding], url: plate.referenceUrl, mediaId: plate.mediaId });
      }
      if (extra) referenceAssets.push({ id, name, kind, description, ref_label: label, ref_url: plate.referenceUrl, media_id: plate.mediaId });
      return label;
    };
    const characterKeys = new Set([...(seq.character_keys ?? []), ...shots.flatMap((shot) => shot.character_keys ?? [])]);
    for (const node of nodes) {
      if (node.data.kind !== "character") continue;
      const d = node.data;
      const id = d.character.source_asset_id ?? d.character.key;
      if (!characterKeys.has(d.character.key) && !needed.has(id)) continue;
      const stateKey = shots.map((shot) => shot.character_states?.[d.character.key]).find(Boolean) ?? d.activeState;
      const state = d.character.states.find((st) => st.key === stateKey);
      const selected = d.states[stateKey];
      const plate = selected?.referenceUrl ? selected : d.identity;
      const label = add(id, d.character.name, "character", plate, false);
      if (!styleNote) styleNote = styleSection(plate?.prompt ?? "");
      // Expected metadata survives even when its image has not been generated.
      characters.push({ ...d.character, source_asset_id: id,
        look: state?.look ?? "", wardrobe: state?.wardrobe ?? "", posture: state?.posture ?? "",
        ref_label: label, ref_url: plate?.referenceUrl, media_id: plate?.mediaId });
    }
    let environment: Record<string, unknown> | null = null;
    const environmentKeys = new Set([seq.environment_key, ...shots.map((shot) => shot.environment_key).filter(Boolean)]);
    for (const node of nodes) {
      if (node.data.kind === "environment") {
        const d = node.data;
        const id = d.environment.source_asset_id ?? d.environment.key;
        if (!environmentKeys.has(d.environment.key) && !needed.has(id)) continue;
        const primary = d.environment.key === seq.environment_key || (!seq.environment_key && !environment);
        const label = add(id, d.environment.name, "environment", d.plate, !primary, d.environment.summary);
        const entry = { ...d.environment, source_asset_id: id, ref_label: label, ref_url: d.plate.referenceUrl, media_id: d.plate.mediaId };
        if (primary) environment = entry;
      } else if (node.data.kind === "asset") {
        const { asset, plate } = node.data;
        if (needed.has(asset.id ?? asset.key) || needed.has(asset.key)) {
          add(asset.id ?? asset.key, asset.name, asset.kind, plate, true, asset.description || asset.summary);
        }
      }
    }
    return { refs, characters, environment, styleNote, referenceAssets };
  },

  collectAssetDependencies(asset) {
    const { productionAssets, nodes } = get();
    const registered = productionAssets.find((entry) => entry.id === (asset.id ?? asset.key));
    const ids = [...new Set([...(registered?.depends_on_asset_ids ?? []), ...(registered?.member_ids ?? [])])];
    return ids.map((id, index) => {
      const entry = productionAssets.find((candidate) => candidate.id === id);
      const node = nodes.find((candidate) => candidate.data.kind === "character"
        ? (candidate.data.character.source_asset_id ?? candidate.data.character.key) === id
        : candidate.data.kind === "environment" ? (candidate.data.environment.source_asset_id ?? candidate.data.environment.key) === id
          : candidate.data.kind === "asset" && (candidate.data.asset.id ?? candidate.data.asset.key) === id);
      const data = node?.data;
      const plate = data?.kind === "character"
        ? (data.states[data.activeState]?.referenceUrl ? data.states[data.activeState] : data.identity)
        : data?.kind === "environment" || data?.kind === "asset" ? data.plate : undefined;
      return { id, name: entry?.name ?? id, kind: entry?.kind ?? "prop", ref_label: `@image${index + 1}`,
        ref_url: plate?.referenceUrl ?? "", media_id: plate?.mediaId };
    });
  },

  promptContract(sequenceKey) {
    const state = get();
    const seqNode = state.nodes.find((n) => n.id === `seq:${sequenceKey}`);
    if (!seqNode || seqNode.data.kind !== "sequence") throw new Error("Không tìm thấy shotlist của clip.");
    const { characters, environment, styleNote, referenceAssets } = state.collectVideoRefs(sequenceKey);
    return {
      sequence: { ...seqNode.data.sequence, function: seqNode.data.functionOf ?? [], raccord: seqNode.data.raccord ?? [] },
      shots: seqNode.data.shots, characters, environment, style: state.style,
      aspect_ratio: state.aspectRatio, style_note: styleNote,
      previous_state: previousVideo(state.nodes, sequenceKey)?.endState ?? "",
      ...(isStrictBoard(state.productionAssets, state.sourceVerification) ? {
        production_assets: state.productionAssets, source_verification: state.sourceVerification,
        reference_assets: referenceAssets,
      } : {}),
    };
  },

  currentFingerprint(sequenceKey) {
    const state = get();
    const parts = contractParts(state, sequenceKey);
    const hit = fingerprintCache.get(sequenceKey);
    if (hit && hit.parts.length === parts.length && hit.parts.every((part, i) => part === parts[i])) return hit.value;
    const value = contractFingerprint(state.promptContract(sequenceKey));
    fingerprintCache.set(sequenceKey, { parts, value });
    return value;
  },

  /** Give published plates a media row so they can become identity assets.
   *  Matches on the plate's own reference URL, so it repairs whichever nodes
   *  hold that picture without the caller tracking which slot it came from. */
  async backfillMediaIds(urls) {
    const epoch = boardEpoch;
    const wanted = [...new Set(urls.filter(Boolean))];
    if (!wanted.length) return;
    const out = await api<{ media_ids: Record<string, string | null> }>(
      "/api/automation/ingest",
      { method: "POST", body: JSON.stringify({ urls: wanted }) },
    );
    if (epoch !== boardEpoch) return;
    const fix = (p: Plate): Plate => {
      const found = p.referenceUrl ? out.media_ids[p.referenceUrl] : null;
      return found && !p.mediaId ? { ...p, mediaId: found } : p;
    };
    set({
      nodes: get().nodes.map((n) => {
        if (n.data.kind === "character") {
          const states: Record<string, Plate> = {};
          for (const [k, p] of Object.entries(n.data.states)) states[k] = fix(p);
          return { ...n, data: { ...n.data, identity: fix(n.data.identity), states } } as AutoNode;
        }
        if (n.data.kind === "environment" || n.data.kind === "asset") {
          return { ...n, data: { ...n.data, plate: fix(n.data.plate) } } as AutoNode;
        }
        return n;
      }),
    });
  },

  async generateKeyframe(sequenceKey, which) {
    const epoch = boardEpoch;
    const id = `vid:${sequenceKey}`;
    const node = get().nodes.find((n) => n.id === id);
    const seqNode = get().nodes.find((n) => n.id === `seq:${sequenceKey}`);
    if (!node || node.data.kind !== "video") return;
    if (!seqNode || seqNode.data.kind !== "sequence") return;
    const shots = seqNode.data.shots;
    if (!shots.length) throw new Error("Cắt shot cho sequence này trước đã.");

    const slot = which === "start" ? "startFrame" : "endFrame";
    const previous = (node.data as VideoNodeData)[slot];
    get().patchNode(id, {
      [slot]: { ...(previous ?? emptyPlate()), status: "running", error: undefined },
    } as Partial<AutoNodeData>);

    try {
      const { refs, characters, environment } = get().collectVideoRefs(sequenceKey);
      if (!refs.length) {
        throw new Error("Chưa có sheet nhân vật / plate bối cảnh để dựng frame.");
      }
      // The frame is one moment of one shot: the clip's first shot for a start
      // frame, its last for an end frame.
      const shot = which === "start" ? shots[0] : shots[shots.length - 1];
      let prompt: string;
      let referenceUrls = refs.map((r) => r.url);
      if (get().currentProjectId) {
        await ensureRaccord(sequenceKey);
        if (epoch !== boardEpoch) return;
        const frame = await api<{prompt:string;reference_urls:string[]}>(`/api/automation/projects/${get().currentProjectId}/shot-keyframe?sequence_key=${encodeURIComponent(sequenceKey)}&shot_index=${which === "start" ? 0 : shots.length-1}&which=${which}`);
        prompt = frame.prompt;referenceUrls = frame.reference_urls;
      } else {
      const legacy = await api<{ prompt: string }>("/api/automation/prompt", {
        method: "POST",
        body: JSON.stringify({
          kind: "keyframe",
          shot,
          which,
          characters,
          environment,
          style: get().style,
          aspect_ratio: get().aspectRatio,
        }),
      });
        prompt = legacy.prompt;
      }
      if (epoch !== boardEpoch) return;
      const out = await durableRequest<PlateResponse>("/api/automation/plate", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          image_model: get().imageModel,
          image_size: get().imageSize,
          aspect_ratio: get().aspectRatio,
          reference_urls: referenceUrls,
        }),
      }, id, slot);
      if (epoch !== boardEpoch) return;
      const first = out.images[0];
      get().patchNode(id, {
        [slot]: {
          ...(get().nodes.find((n) => n.id === id)?.data as any)?.[slot],
          prompt,
          status: "done",
          image: first?.url,
          referenceUrl: first?.reference_url ?? undefined,
          mediaId: first?.media_id ?? undefined,
        },
      } as Partial<AutoNodeData>);
    } catch (err) {
      if (epoch !== boardEpoch) return;
      get().patchNode(id, {
        [slot]: { ...(previous ?? emptyPlate()), status: "error", error: (err as Error).message },
      } as Partial<AutoNodeData>);
      throw err;
    }
  },

  async primeVideoPrompt(sequenceKey, opts) {
    const epoch = boardEpoch;
    const { nodes } = get();
    const node = nodes.find((n) => n.id === `vid:${sequenceKey}`);
    const seqNode = nodes.find((n) => n.id === `seq:${sequenceKey}`);
    if (!node || node.data.kind !== "video") return;
    if (!seqNode || seqNode.data.kind !== "sequence") return;

    if (get().currentProjectId && opts?.writer) {
      await ensureRaccord(sequenceKey);
      if (epoch !== boardEpoch) return;
    }
    if (get().currentProjectId && (opts?.writer || seqNode.data.sequence.shot_package)) {
      await get().saveNow();
      if (get().saveState === "error") throw new Error(get().saveError ?? "Save failed");
      const pack = await api<ShotPackage>(`/api/automation/projects/${get().currentProjectId}/shot-packages?sequence_key=${encodeURIComponent(sequenceKey)}`);
      if (epoch !== boardEpoch) return;
      const seqNow = get().nodes.find((n) => n.id === seqNode.id);
      if (seqNow?.data.kind === "sequence") get().patchNode(seqNow.id, { sequence: { ...seqNow.data.sequence, shot_package: pack } } as Partial<AutoNodeData>);
      if (!pack.ready) throw new Error("Chưa đủ đầu vào shot: " + pack.issues.filter((i) => i.blocking).map((i) => `${i.code} ${i.asset ?? i.shot ?? ""}`).join("; "));
    }
    // References already exist independently of whether the writer succeeds.
    // Publish them before waiting, without replacing an existing prompt.
    let refs = get().collectVideoRefs(sequenceKey).refs;
    get().patchNode(node.id, { refs } as Partial<AutoNodeData>);
    if (get().kyc && isStrictBoard(get().productionAssets, get().sourceVerification)) {
      const missing = refs.filter((ref) => !ref.mediaId);
      if (missing.length) await get().backfillMediaIds(missing.map((ref) => ref.url));
      if (epoch !== boardEpoch) return;
      refs = get().collectVideoRefs(sequenceKey).refs;
      get().patchNode(node.id, { refs } as Partial<AutoNodeData>);
      if (refs.some((ref) => !ref.mediaId)) {
        const error = "Chưa nạp được mọi reference vào thư viện media. Thử lại trước khi viết prompt có KYC.";
        get().patchNode(node.id, { error } as Partial<AutoNodeData>);
        throw new Error(error);
      }
    }
    if (get().currentProjectId) {
      await get().saveNow();
      if (get().saveState === "error") throw new Error(get().saveError ?? "Save failed");
      const context = await api<Record<string, unknown>>(`/api/automation/projects/${get().currentProjectId}/production?sequence_key=${encodeURIComponent(sequenceKey)}`);
      if (epoch !== boardEpoch) return;
      const currentSeq = get().nodes.find((n) => n.id === `seq:${sequenceKey}`);
      if (currentSeq?.data.kind === "sequence") get().patchNode(currentSeq.id, { sequence: { ...currentSeq.data.sequence, production_context: context } } as Partial<AutoNodeData>);
    }
    const body = get().promptContract(sequenceKey);
    const inputFingerprint = contractFingerprint(body);
    if (opts?.writer) {
      const out = await durableRequest<{
        prompt: string;
        duration_seconds: number;
        end_state: string;
        writer: string;
        warnings: string[];
        coverage?: PromptCoverage;
        contract_digest?: string;
        coverage_token?: string;
      }>("/api/automation/video/write", {
        method: "POST",
        body: JSON.stringify(body),
      }, node.id, "prompt").catch((error: Error) => {
        if (epoch === boardEpoch) {
          // Sheets can finish or change while writing. Keep the available
          // references visible on failure; leave the prior prompt untouched.
          try { refs = get().collectVideoRefs(sequenceKey).refs; } catch { /* keep last valid bindings */ }
          get().patchNode(node.id, { refs, error: error.message } as Partial<AutoNodeData>);
        }
        throw error;
      });
      if (epoch !== boardEpoch) return;
      const shotsNow=get().nodes.find((n)=>n.id===seqNode.id);
      if (shotsNow?.data.kind!=="sequence" || JSON.stringify(shotsNow.data.shots)!==JSON.stringify(body.shots)) {
        throw new Error("Shotlist đã thay đổi; prompt mới được giữ trong lịch sử job để bạn xem lại.");
      }
      const draftNow=get().nodes.find((n)=>n.id===node.id);
      if (draftNow?.data.kind==="video" && draftNow.data.prompt!==node.data.prompt && draftNow.data.prompt!==out.prompt) {
        throw new Error("Prompt mới đã lưu trong lịch sử job; giữ bản bạn đang sửa trên board.");
      }
      get().patchNode(node.id, {
        prompt: out.prompt,
        refs,
        durationS: out.duration_seconds,
        endState: out.end_state || undefined,
        promptBy: out.writer,
        warnings: out.warnings,
        coverage: out.coverage,
        contractDigest: out.contract_digest,
        coverageToken: out.coverage_token,
        inputFingerprint,
        error: undefined,
      } as Partial<AutoNodeData>);
      return;
    }
    const out = await api<{ prompt: string; duration_seconds: number }>(
      "/api/automation/video/prompt",
      { method: "POST", body: JSON.stringify(body) },
    );
    if (epoch !== boardEpoch) return;
    get().patchNode(node.id, {
      prompt: out.prompt,
      refs,
      durationS: out.duration_seconds,
      promptBy: "template",
      coverage: undefined, contractDigest: undefined, coverageToken: undefined, inputFingerprint,
    } as Partial<AutoNodeData>);
  },

  async generateClip(sequenceKey) {
    const epoch = boardEpoch;
    const id = `vid:${sequenceKey}`;
    const node = get().nodes.find((n) => n.id === id);
    if (!node || node.data.kind !== "video" || node.data.status === "running") return;

    // Write first, unless a written prompt already matches the references it
    // would be sent with. A clip is the most expensive thing on this board and
    // a stale prompt is only noticed after paying for it — but a prompt the
    // writer already wrote (or one put here by hand) is the one the user read,
    // and writing it again would hand them a different one unasked.
    get().patchNode(id, { status: "running", error: undefined } as Partial<AutoNodeData>);
    try {
      const current = node.data;
      const strictBoard = isStrictBoard(get().productionAssets, get().sourceVerification);
      if (get().kyc && strictBoard) {
        const missing = get().collectVideoRefs(sequenceKey).refs.filter((ref) => !ref.mediaId);
        if (missing.length) await get().backfillMediaIds(missing.map((ref) => ref.url));
      }
      if (epoch !== boardEpoch) return;
      const { refs: wanted } = get().collectVideoRefs(sequenceKey);
      const written = Boolean(current.prompt) && current.promptBy !== undefined && current.promptBy !== "template";
      // A prompt written before fingerprints existed, or one placed here by
      // hand, has none. Outside the strict contract it is kept while the same
      // people are referenced in the same order — rewriting it would hand the
      // user a prompt they never read and then pay for it.
      const sameRefs = wanted.map((r) => r.name).join("|") === current.refs.map((r) => r.name).join("|");
      const upToDate = current.inputFingerprint
        ? sameFingerprint(current.inputFingerprint, get().currentFingerprint(sequenceKey))
        : !strictBoard && sameRefs;
      if (written && upToDate) {
        get().patchNode(id, { refs: wanted } as Partial<AutoNodeData>);
      } else {
        await get().primeVideoPrompt(sequenceKey, { writer: true });
      }
      if (epoch !== boardEpoch) return;
      const fresh = get().nodes.find((n) => n.id === id);
      if (!fresh || fresh.data.kind !== "video") return;
      const d = fresh.data;
      const promptContract = get().promptContract(sequenceKey);
      const strict = isStrictBoard(get().productionAssets, get().sourceVerification);
      if (strict && (d.chainFromPrevious || d.startFrame?.referenceUrl)) {
        throw new Error("Clip đã kiểm tra coverage cần chế độ ảnh reference. Tắt nối tiếp hoặc bỏ frame đầu/cuối trước khi gen.");
      }
      if (strict && (!sourceReadyForShots(promptContract.shots as Shot[], get().sourceVerification) || d.coverage?.status !== "verified" || !d.contractDigest || !d.coverageToken)) {
        throw new Error("Cần Agent 1 xác minh video gốc và Agent 2 xác nhận đủ nội dung trước khi gen.");
      }
      if (strict && !sameFingerprint(d.inputFingerprint, contractFingerprint(promptContract))) {
        throw new Error("Shotlist hoặc reference đã thay đổi trong lúc viết. Viết lại prompt trước khi gen.");
      }
      // The board and clip travel with every generation: the server reads
      // from the saved board whether a receipt is required.
      const checkedContract = {
        project_id: get().currentProjectId ?? undefined,
        sequence_key: sequenceKey,
        ...(strict ? { prompt_contract: promptContract, coverage: d.coverage,
          contract_digest: d.contractDigest, coverage_token: d.coverageToken } : {}),
      };
      if (!d.refs.length) {
        throw new Error(
          "Chưa có ref nào — gen sheet nhân vật và plate bối cảnh của sequence này trước.",
        );
      }
      // KYC turns EVERY reference into an identity asset, in the same order,
      // so @imageN keeps meaning what the prompt says. Any ref missing its
      // media row would silently shift the rest by one, so refuse instead.
      // Chaining: this clip continues the one before it. Checked first because
      // it is the only mode that carries a photoreal cast across a seam.
      if (d.chainFromPrevious) {
        const ordered = get()
          .nodes.filter((n) => n.data.kind === "sequence")
          .sort((a, b) => a.position.y - b.position.y)
          .map((n) => (n.data.kind === "sequence" ? n.data.sequence.key : ""));
        const at = ordered.indexOf(sequenceKey);
        const prev = at > 0 ? get().nodes.find((n) => n.id === `vid:${ordered[at - 1]}`) : undefined;
        const prevUrl = prev?.data.kind === "video" ? prev.data.clipUrl : undefined;
        if (!prevUrl) throw new Error("Clip trước chưa gen xong — chưa có gì để nối tiếp.");
        const out = await durableRequest<{
          url: string;
          persisted: boolean;
          job_id: string | null;
          warnings: string[];
        }>("/api/automation/video/clip", {
          method: "POST",
          body: JSON.stringify({
            ...checkedContract,
            prompt: d.prompt,
            reference_urls: [],
            duration_seconds: d.durationS,
            aspect_ratio: get().aspectRatio,
            resolution: "720p",
            unmoderated: get().unmoderated,
            kyc_media_ids: [],
            previous_clip_url: prevUrl,
            chain: "extend",
          }),
        }, id, "");
        if (epoch !== boardEpoch) return;
        get().patchNode(id, {
          status: "done",
          clipUrl: out.url,
          persisted: out.persisted,
          warnings: out.warnings,
          error: undefined,
        } as Partial<AutoNodeData>);
        return;
      }

      // Keyframes beat references: the clip is pinned to two pictures we made,
      // and the provider will not take both kinds of input on one call.
      const startFrame = d.startFrame?.referenceUrl;
      const endFrame = d.endFrame?.referenceUrl;
      if (startFrame) {
        const out = await durableRequest<{
          url: string;
          persisted: boolean;
          job_id: string | null;
          warnings: string[];
        }>("/api/automation/video/clip", {
          method: "POST",
          body: JSON.stringify({
            ...checkedContract,
            prompt: d.prompt,
            reference_urls: [],
            duration_seconds: d.durationS,
            aspect_ratio: get().aspectRatio,
            resolution: "720p",
            unmoderated: get().unmoderated,
            kyc_media_ids: [],
            first_frame_url: startFrame,
            last_frame_url: endFrame ?? "",
          }),
        }, id, "");
        if (epoch !== boardEpoch) return;
        get().patchNode(id, {
          status: "done",
          clipUrl: out.url,
          persisted: out.persisted,
          warnings: out.warnings,
          error: undefined,
        } as Partial<AutoNodeData>);
        return;
      }

      const kyc = get().kyc;
      if (kyc && d.refs.some((r) => !r.mediaId)) {
        // Plates made before the pipeline ingested anything have only a URL.
        // Fetch them back into the media store rather than asking for a paid
        // regeneration of artwork that is already correct.
        await get().backfillMediaIds(d.refs.filter((r) => !r.mediaId).map((r) => r.url));
        get().patchNode(id, { refs: get().collectVideoRefs(sequenceKey).refs } as Partial<AutoNodeData>);
      }
      const after = get().nodes.find((n) => n.id === id);
      if (!after || after.data.kind !== "video") return;
      const refs = after.data.refs;
      const kycMediaIds = kyc ? (refs.map((r) => r.mediaId).filter(Boolean) as string[]) : [];
      if (kyc && kycMediaIds.length !== refs.length) {
        throw new Error(
          "Không nạp được media cho mọi ref, nên thứ tự @imageN sẽ lệch. Thử gen lại plate thiếu.",
        );
      }

      const out = await durableRequest<{
        url: string;
        persisted: boolean;
        job_id: string | null;
        warnings: string[];
      }>("/api/automation/video/clip", {
        method: "POST",
        body: JSON.stringify({
          ...checkedContract,
          prompt: after.data.prompt,
          // Strict receipts validate URL order even when the provider uses
          // cached identity media IDs as the actual transport.
          reference_urls: kyc && !strict ? [] : refs.map((r) => r.url),
          duration_seconds: after.data.durationS,
          aspect_ratio: get().aspectRatio,
          resolution: "720p",
          unmoderated: get().unmoderated,
          kyc_media_ids: kycMediaIds,
        }),
      }, id, "");
      if (epoch !== boardEpoch) return;
      get().patchNode(id, {
        status: "done",
        clipUrl: out.url,
        persisted: out.persisted,
        warnings: out.warnings,
        error: undefined,
      } as Partial<AutoNodeData>);
    } catch (err) {
      if (epoch !== boardEpoch) return;
      get().patchNode(id, {
        status: err instanceof PendingJobError ? "running" : "error",
        error: (err as Error).message,
      } as Partial<AutoNodeData>);
    }
  },

  async generate(id, slot) {
    const epoch = boardEpoch;
    const node = get().nodes.find((n) => n.id === id);
    if (!node) return;
    const plate = readPlate(node.data, slot);
    if (!plate?.prompt.trim() || plate.status === "running") return;

    // A state sheet inherits the master face. Without that reference it is a
    // fresh person wearing the right clothes, so refuse rather than burn a
    // generation on a face that will not match.
    const isIdentity = slot === "identity";
    const isStateSheet = node.data.kind === "character" && !isIdentity;
    const referenceUrl =
      isStateSheet && node.data.kind === "character" ? node.data.identity.referenceUrl : undefined;
    const dependencies = node.data.kind === "asset" ? get().collectAssetDependencies(node.data.asset) : [];
    const missing = dependencies.filter((dependency) => !dependency.ref_url);
    if (missing.length) {
      get().patchNode(id, writePlate(node.data, slot, { status: "error",
        error: `Gen reference thành phần trước: ${missing.map((dependency) => dependency.name).join(", ")}.` }));
      return;
    }

    if (isStateSheet && !referenceUrl) {
      get().patchNode(id, {
        ...writePlate(node.data, slot, {
          status: "error",
          error: "Generate the identity portrait first — this sheet inherits its face.",
        }),
      });
      return;
    }

    get().patchNode(id, { ...writePlate(node.data, slot, { status: "running", error: undefined }) });

    try {
      const { imageModel } = get();
      const out = await durableRequest<PlateResponse>("/api/automation/plate", {
        method: "POST",
        body: JSON.stringify({
          prompt: plate.prompt,
          image_model: imageModel,
          image_size: get().imageSize,
          // Neither of these follows the film's own ratio. A turnaround sheet
          // is a studio document that wants width for four full-body views; an
          // environment plate is a set drawing, read across at 21:9.
          aspect_ratio: node.data.kind === "environment" ? ENVIRONMENT_ASPECT : "16:9",
          reference_urls: dependencies.length ? dependencies.map((dependency) => dependency.ref_url) : referenceUrl ? [referenceUrl] : [],
        }),
      }, id, slot);
      if (epoch !== boardEpoch) return;
      const first = out.images[0];
      const latest = get().nodes.find((n) => n.id === id);
      if (!latest) return;
      get().patchNode(id, {
        ...writePlate(latest.data, slot, {
          status: "done",
          image: first?.url,
          referenceUrl: first?.reference_url ?? undefined,
          mediaId: first?.media_id ?? undefined,
          error: undefined,
        }),
      });
    } catch (err) {
      if (epoch !== boardEpoch) return;
      const latest = get().nodes.find((n) => n.id === id);
      if (!latest) return;
      get().patchNode(id, {
        ...writePlate(latest.data, slot, { status: err instanceof PendingJobError ? "running" : "error", error: (err as Error).message }),
      });
    }
  },

  clipCount() {
    return get().nodes.filter((n) => n.data.kind === "video" && n.data.clipUrl).length;
  },

  plateCount() {
    return get().nodes.filter(
      (n) =>
        (n.data.kind === "character" && n.data.identity.image) ||
        ((n.data.kind === "environment" || n.data.kind === "asset") && n.data.plate.image),
    ).length;
  },

  async downloadPlates() {
    // Cast first, then locations, each lane in the order it is laid out — the
    // zip reads like the board rather than like a hash map.
    const byLane = (kind: AutoNode["data"]["kind"]) =>
      get()
        .nodes.filter((n) => n.data.kind === kind)
        .sort((a, b) => a.position.y - b.position.y);

    const plates = [
      ...byLane("character").flatMap((n) =>
        n.data.kind === "character" && n.data.identity.image
          ? [{ url: n.data.identity.image, name: n.data.character.name, kind: "character" }]
          : [],
      ),
      ...byLane("environment").flatMap((n) =>
        n.data.kind === "environment" && n.data.plate.image
          ? [{ url: n.data.plate.image, name: n.data.environment.name, kind: "environment" }]
          : [],
      ),
      ...byLane("asset").flatMap((n) => n.data.kind === "asset" && n.data.plate.image
        ? [{ url: n.data.plate.image, name: n.data.asset.name, kind: n.data.asset.kind }] : []),
    ];
    if (!plates.length) return;

    const name = `${(get().title || "tao-hinh").replace(/[^\w-]+/g, "-").toLowerCase()}-tao-hinh.zip`;
    const res = await fetch("/api/automation/export/plates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plates, filename: name }),
    });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },

  async downloadClips() {
    const { nodes, title } = get();
    // Running order comes from the sequence lane's own layout, not from node
    // insertion order — a board that has been rearranged should still export
    // in the order the film plays.
    const order = nodes
      .filter((n) => n.data.kind === "sequence")
      .sort((a, b) => a.position.y - b.position.y)
      .map((n) => (n.data.kind === "sequence" ? n.data.sequence.key : ""));

    const clips = order
      .map((key) => nodes.find((n) => n.id === `vid:${key}`))
      .filter((n): n is AutoNode => Boolean(n))
      .flatMap((n) =>
        n.data.kind === "video" && n.data.clipUrl
          ? [{ url: n.data.clipUrl, label: n.data.label, title: n.data.title }]
          : [],
      );
    if (!clips.length) return;

    const name = `${(title || "clips").replace(/[^\w-]+/g, "-").toLowerCase()}.zip`;
    const res = await fetch("/api/automation/export/clips", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clips, filename: name }),
    });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },

  exportBoard() {
    const s = get();
    const blob = new Blob(
      [
        JSON.stringify(
          {
            version: 1,
            preserveSourceShots: s.preserveSourceShots,
            ...boardSettings(s, s.capabilities),
            title: s.title,
            logline: s.logline,
            script: s.script,
            runtimeSeconds: s.runtimeSeconds,
            productionAssets: s.productionAssets,
            sourceVerification: s.sourceVerification,
            characters: s.characters,
            environments: s.environments,
            style: s.style,
            // Images included here on purpose: a file has no quota, and an
            // export is the copy you keep.
            nodes: stripInlineImages(s.nodes),
            edges: s.edges,
          },
          null,
          2,
        ),
      ],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(s.title || "board").replace(/[^\w-]+/g, "-").toLowerCase()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  },

  importBoard(json) {
    const d = JSON.parse(json) as Partial<AutomationStore> & { nodes?: AutoNode[] };
    if (!Array.isArray(d.nodes) || !d.nodes.length) {
      throw new Error("File này không có node nào — không phải bản xuất của board.");
    }
    boardEpoch += 1;
    set({
      preserveSourceShots: d.preserveSourceShots ?? true,
      title: d.title ?? "",
      logline: d.logline ?? "",
      script: d.script ?? "",
      runtimeSeconds: d.runtimeSeconds ?? null,
      productionAssets: d.productionAssets ?? [],
      sourceVerification: d.sourceVerification,
      characters: d.characters ?? [],
      environments: d.environments ?? [],
      style: d.style ?? "realistic",
      ...boardSettings(d, get().capabilities),
      nodes: clearStuckRunning(d.nodes),
      edges: d.edges ?? [],
      breakdownStatus: "done",
      breakdownError: undefined,
    });
    get().ensureGraph();
    // Fills only prompts that came in blank, so an export that already has
    // hand-edited prompts keeps them.
    void get().primePrompts();
  },
});

/** localStorage, but a full quota is a shrug rather than a thrown promise.
 *
 *  Persisting the whole board here used to blow the ~5MB quota on a real film,
 *  and because zustand writes on EVERY `set`, the rejection landed in the
 *  middle of unrelated work — cutting a sequence failed with a storage error.
 *  Nothing kept here is load-bearing any more (see `partialize`), so a failed
 *  write must never reach the caller. */
const safeLocalStorage = {
  getItem: (name: string) => {
    try {
      return localStorage.getItem(name);
    } catch {
      return null;
    }
  },
  setItem: (name: string, value: string) => {
    try {
      localStorage.setItem(name, value);
    } catch {
      /* quota or a locked-down browser — the server holds the board */
    }
  },
  removeItem: (name: string) => {
    try {
      localStorage.removeItem(name);
    } catch {
      /* nothing to do */
    }
  },
};

export const useAutomation = create<AutomationStore>()(
  persist(createBoard, {
    name: "flowboard.automation.board",
    // v2 drops the whole-board mirror. Bumping the version makes zustand throw
    // away the v1 blob still sitting in people's browsers, which is what was
    // over quota in the first place.
    version: 2,
    storage: createJSONStorage(() => safeLocalStorage),
    migrate: () => ({}) as Partial<AutomationStore>,
    // The board itself lives on the server now and autosaves there. Keeping a
    // second copy here bought nothing and cost the quota — a film's worth of
    // sequences, prompts and cut shots does not fit. What is left is the small
    // preferences used before a board opens. Saved board settings take
    // precedence when that project loads; older boards retain these defaults.
    partialize: (s) =>
      ({
        currentProjectId: s.currentProjectId,
        imageModel: s.imageModel,
        imageSize: s.imageSize,
        clipSeconds: s.clipSeconds,
        aspectRatio: s.aspectRatio,
        unmoderated: s.unmoderated,
        kyc: s.kyc,
      }) as unknown as AutomationStore,
  }),
);

// Reopen whatever board was last open. The board used to come back from
// localStorage; now it comes back from the server, which is the only copy that
// can hold a whole film.
const restoring = useAutomation.getState().currentProjectId;
if (restoring) {
  void useAutomation.getState().openProject(restoring).catch(() => {
    // Deleted, or belongs to someone else now — start on an empty board
    // rather than stranding the page on a project id that resolves to nothing.
    useAutomation.setState({ currentProjectId: null });
  });
}

// ─────────────────────────────── autosave ────────────────────────────────
//
// localStorage above survives a reload; this survives everything else. It only
// runs once a board is open — before that there is nowhere to put the work,
// which is why the page pushes you to create one first.
//
// Debounced rather than per-keystroke: the premise textarea would otherwise
// PATCH on every character typed.
const AUTOSAVE_DELAY_MS = 1200;
let autosaveTimer: ReturnType<typeof setTimeout> | undefined;

useAutomation.subscribe((state, prev) => {
  if (autosavePaused || !state.currentProjectId) return;
  const changed =
    state.nodes !== prev.nodes ||
    state.edges !== prev.edges ||
    state.script !== prev.script ||
    state.title !== prev.title ||
    state.logline !== prev.logline ||
    state.runtimeSeconds !== prev.runtimeSeconds ||
    state.characters !== prev.characters ||
    state.environments !== prev.environments ||
    state.style !== prev.style ||
    state.productionAssets !== prev.productionAssets ||
    state.sourceVerification !== prev.sourceVerification ||
    state.preserveSourceShots !== prev.preserveSourceShots ||
    state.aspectRatio !== prev.aspectRatio ||
    state.imageModel !== prev.imageModel ||
    state.imageSize !== prev.imageSize ||
    state.clipSeconds !== prev.clipSeconds ||
    state.unmoderated !== prev.unmoderated ||
    state.kyc !== prev.kyc;
  if (!changed) return;

  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => void useAutomation.getState().saveNow(), AUTOSAVE_DELAY_MS);
});

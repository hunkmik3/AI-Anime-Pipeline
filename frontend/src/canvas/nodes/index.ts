import { ApprovalGateNode } from "./ApprovalGateNode";
import { AudioRefNode } from "./AudioRefNode";
import { BibleRefNode } from "./BibleRefNode";
import { CharacterNode } from "./CharacterNode";
import { ImageNode } from "./ImageNode";
import { MasterShotNode } from "./MasterShotNode";
import { NoteNode } from "./NoteNode";
import { PromptNode } from "./PromptNode";
import { ScriptNode } from "./ScriptNode";
import { StoryboardNode } from "./StoryboardNode";
import { VideoNode } from "./VideoNode";
import { VisualAssetNode } from "./VisualAssetNode";

export const nodeTypes = {
  character: CharacterNode,
  image: ImageNode,
  video: VideoNode,
  prompt: PromptNode,
  note: NoteNode,
  visual_asset: VisualAssetNode,
  storyboard: StoryboardNode,
  script: ScriptNode,
  bible_ref: BibleRefNode,
  master_shot: MasterShotNode,
  approval_gate: ApprovalGateNode,
  audio_ref: AudioRefNode,
};

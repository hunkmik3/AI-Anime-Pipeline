# Historical format — not for new generation

Superseded on 2026-10-01 by ../CLIP_PROMPT_STANDARD.md. Retained only for legacy regression tests.

# Clip prompt standard — Seedance 2.5

How a clip prompt is written. This is the standard every clip prompt — hand-written
or produced by the automation — is judged against.

Distilled from the owner's ten X-Ray prompts, kept verbatim in
[`clip-prompts/xray/`](clip-prompts/xray/) (clip-01 … clip-10). Approved by the owner
on 2026-09-24 after the first three were generated: *"khá tốt về mọi mặt"*.

The only change from the owner's files: `grey blazer` → `school blazer`. Theo's
approved sheet is a charcoal-navy blazer, and a prompt must never contradict the
image it sends. The originals stay in the owner's own folder.

---

## What it delivered

Generated at 480p, 9:16, from the prompt files unchanged apart from the blazer
fix. Speech was measured locally with faster-whisper; cuts by frame difference.

| Clip | Length | Shots written → cut | Lines spoken | Note |
|---|---:|---|---|---|
| 01 | 23 s | 11 → ~11 | 3 / 3 | object see-through gag, clean |
| 02 | 26 s | 11 → 11 | 7 / 7 | one line said **twice** (see *Slack*) |
| 03 | 28 s | 8 → 8 + extra reaction cuts | 7 / 7 | "Rothwell", "Gullwing" heard as "Rothberry", "Gold Bay" |

---

## The skeleton, in this order

```
CLIP NN — TITLE
DURATION: N seconds. SHOT COUNT: M.

[CREATIVES DESCRIPTION]

@image1 — NAME. <who they are in one line>. Preserve identity, face, hairstyle,
  age and proportions, wardrobe. <what they hold / how they carry it through this clip>
@image2 — …
@imageK — LOCATION. Architectural, material and daylight reference. Create the
  required new camera compositions within one coherent <place>.
<anyone without a reference: described once, kept distinct, told whether they speak>

<STYLE paragraph: medium, rendering, acting register, the age rule, what it is not>

DURATION: N seconds. SHOT COUNT: M.
Opening state: <positions, eyelines, which hand holds which prop, open or closed —
  exactly where the previous clip ended>

[ONE-SENTENCE SUMMARY]

<who does what, and what changes by the end>

[SPECIFIC TIMELINE]

[SHOT 1 — 00:00–00:04]
<size + angle + subject>. <one to four short physical sentences>.
NAME: "line"
<delivery: tone; how to pronounce numbers and names; where the sentence goes next>
<who stays silent and does not lip-sync; the prop's state when the shot ends>
SFX: <a few sounds, optional>

[SHOT 2 — 00:04–00:07]
…

[OVERALL SUPPLEMENT]

<eyeline axis> <performance arc> <prop chain A → B → C> <which sentence spans which
shots, heard once> <who never speaks> <the state handed to the next clip>
<audio bans> <content rules>
```

---

## The rules

### References
- Every `@imageN` is declared once, **in the order the images are sent**. Seedance
  binds by position, so the prompt's numbering is the contract: whoever generates
  must send the images in exactly that order.
- A character line names the role and what must be preserved. Colours and garments
  come only from the approved design — or are left to the image. Never from the
  reference video, never guessed.
- The location is a reference for architecture, material and light. Every shot is
  a new composition inside one coherent geography.
- People without a reference are described once and kept distinct ("one additional
  unnamed bodyguard; the second guard has no dialogue").
- The style paragraph states the age rule: the teenagers stay teenagers, the adults
  keep their reference ages.

### Continuity between clips
- **Opening state** picks up exactly where the previous clip ended: who stands on
  which side, eyelines, which hand holds which prop, open or closed.
- The supplement **ends by handing a state to the next clip** ("End with the box
  ready in Theo's hand for the next clip").
- A prop follows one explicit path, written once as a chain: *Theo's left hand →
  left inner blazer pocket in Shot 2 → same pocket through the handshake → Theo's
  left hand again in Shot 8.*

### Dialogue
- English, word for word from the script. Each line appears once, inside the shot
  it is heard in.
- Speaker labels are **cast names** — `GRANT`, `DANA` — never descriptions
  (`BODYGUARD`, `SHORT-HAIRED WOMAN IN THE DARK BLAZER`).
- Voices not in frame are marked: `NAME, OFF SCREEN:`, `NAME, VOICEOVER:`,
  `NAME, VOICEOVER, CONTINUING:`. The listener "does not lip-sync" them.
- A sentence split across a cut says where it cuts and that it carries on: *"Cut
  after 'a,' carrying her voice across the cut"*, *"continue the sentence without
  a restart or unnatural pause"*.
- Numbers and unusual words get a pronunciation: *"Pronounce 4.0 as 'four point
  oh.'"*, *"Pronounce 'four hundred million.'"*
- Say who is silent: *"Only the blonde student speaks. The other two react
  silently."*
- The supplement repeats the spans: *"Grant's announcement spans Shots 6–8, each
  spoken once without restarting."*

### Timeline
- Whole-second timecodes, contiguous from `00:00`; the last shot ends on the
  `DURATION` value, and the clip is generated at exactly that length.
- A shot is as long as its lines need (clip 03 runs 28 s against a 19 s reference
  stretch). Seedance speaks English at about 2.2 words a second.
- No shot under one second.

### Slack becomes repetition
Clip 02, shot 4: five seconds for two short lines. The model said *"I can't believe
she's in our school"* twice to fill it. A shot longer than its lines needs gets an
action for the remaining time, or is shortened — never left empty.

### Writing a shot
- Size, angle and subject first; then short physical sentences a model can perform;
  then the line and how it is delivered; then who is silent and where the prop is.
- Negatives in plain words, inside the shot they belong to: *"Theo remains silent
  and does not lip-sync her line."*

### Content with a school-age cast
A beat in the reference that sexualises a minor is **adapted, not reproduced**, and
the adaptation keeps the beat's job in the story:

| Clip | Reference | Written as |
|---|---|---|
| 01 | clothes turned see-through | see-through **objects**: pencil case, locker, bag, book — *"What color is the pen in my pencil case?"* |
| 06 | X-ray body | holographic overlay of skull, mouth and throat only, ending above the collar |
| 10 | lips close-up, thigh grab, neck kiss | brief closed-mouth kiss in a two-shot, an ordinary hand touch |

The supplement says it outright: *"No undressing, clothing transparency, sexualized
framing, body scanning, nudity or gore."*

### Audio
`No music, subtitles, captions, title cards or extra intelligible dialogue.`
Background voices stay indistinct.

---

## How the automation writes to this standard

Since 2026-09-24 the clip prompt is written by a model, not assembled by a
template: `POST /api/automation/video/write` →
`agent/flowboard/services/prompt_writer.py` (`gpt-6-astra`, fallback
`claude-opus-4-5`). It reads this file and two of the ten prompts
(clip-02, clip-03) as examples, the clip's shots and lines from the board, each
reference's look from its design brief, and the **end state** the previous clip's
prompt handed on.

Code fixes what the model may not change, and checks it after:

- the `@imageN` tags, in the order the board sends the images;
- whole-second shot times, never below what a shot's line takes to say, ending
  on the clip length;
- every line word for word, in its own shot;
- no undressing in the timeline when the cast is school-age — a beat that needs
  it is handed over marked `rewrite_required`, to be adapted as clip 01 was.

A prompt that fails is sent back once with the problems named; if it fails again
the template's prompt is used and the node says why. The board's **viết prompt**
button and **Gen** both use the writer; Gen reuses a written (or hand-placed,
`promptBy: "manual"`) prompt as long as the same people are referenced in the
same order.

Image prompts follow the same split: the writer rewrites the descriptive
sections of a sheet or plate prompt from its design brief, and the technical
sections — format, the 60/40 layout, pose, rendering, exclusions — stay the
house text that made the approved sheets.

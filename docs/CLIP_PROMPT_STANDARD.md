# Clip prompt standard — cinematic-v1

The owner approved this as the common video-prompt standard on 2026-10-01. Use it
for **all new video prompts**, written manually or through Automation, for any
project, shotlist, cast, setting, genre, style or aspect ratio. It supersedes the
older six-section format, which is archived in `cinematic-prompts/legacy-standard.md`.

The structure is shared; all story content comes from the current project. This
is not a preset for a particular film, a campus, adult 3D characters or 1:1 video.
Image sheets keep their separately approved image-sheet layout.

## Inputs

Use the ordered shotlist, exact dialogue, character profiles and approved designs,
environment descriptions, principal props, background groups, actual reference
images when supplied, and opening/previous end state plus scene context. Read the
images rather than assuming their appearance from filenames. Profiles without
optional images remain usable as text; do not invent image tags. Missing required
references or genuinely contradictory facts must be reported.

## Output structure

```text
Create a **<editorial duration>-second <orientation> <ratio> <project style> sequence** <setting>.

## REFERENCE CONTROL
- **@image1 = <EXACT ASSET NAME> — <REFERENCE ROLE>.**
  <Identity, wardrobe, materials, construction or architecture to preserve.>
- **@image2 = ...**
  <Interpret atlas cells and distinguish separate people from confirmed turnarounds.>

## STYLE
<Project medium, proportions, materials, motivated lighting and performance register.>
**Dramatic intention:** <What changes emotionally in this clip.>
**Opening state:** <Positions, eyelines, props, hands, contents and states carried in.>

# SHOT 1 | MM:SS.mmm–MM:SS.mmm
**Duration:** <seconds>
**Framing:** <size, angle, subject, useful crop>
**Camera:** <specified movement and focus>
<Physical action from start through transition to end, with relevant scene members
and props. Describe what remains off camera or occluded when continuity needs it.>

## DIALOGUE
<NAME> — <SPOKEN ON CAMERA / OFF SCREEN / VOICE-OVER / CONTINUING>:
“<Exact supplied line>”
**Delivery / audio:** <Pace, intention, visible speaker mouth and sentence bridges.>
**End state:** <Physical state retained after this shot.>

# SHOT 2 | ...
<Repeat for each supplied shot. Omit DIALOGUE for silent shots.>

## AUDIO
<Room tone and motivated SFX. Explicitly lock the supplied dialogue language;
for English lines: English dialogue only, no translation or dubbing.>
NO BACKGROUND MUSIC. NO BGM. NO SCORE.
No subtitles, captions, title cards or extra intelligible dialogue.

## CONTINUITY / NEGATIVE CONSTRAINTS
<Specific clothing/identity locks, crowd persistence, prop custody and scale,
scene geography, eyelines, sentence continuation and final carry-forward state.>
```

## Directing rules

- Every new video prompt explicitly includes `NO BACKGROUND MUSIC. NO BGM. NO SCORE.`
  in AUDIO, across all projects and visual styles. Keep supplied dialogue, room tone
  and motivated sound effects only; do not recreate the original video's music.
  The owner reaffirmed this default on 2026-10-07. A prompt requests this behavior;
  it does not certify that the generated audio is music-free.
- Preserve shot order and supplied actions, camera facts, dialogue and speaker labels.
  Use the board's timing policy. With source preservation enabled, keep fractional
  boundaries exactly; provider padding is a silent hold outside the editorial cut.
- Declare every supplied image tag once and in binding order. A location sheet's
  empty presentation does not imply an empty filmed scene. Reference poses do not
  determine blocking; confirmed alternate prop views do not create extra objects.
- A cut is not a scene change. Retain scene participants, crowds and objects until
  an established entrance/exit, location change or time break changes their presence.
  Show only the subset admitted by each composition rather than forcing everyone
  into close-ups. Offscreen and absent are different states.
- Keep clothing, accessories, prop size relative to hands, holders, occupied hands,
  lid states and contents consistent. Explain visible transfers; never teleport or
  silently drop an object to accommodate an action.
- Use supplied profiles and actual images to preserve each project's visual style.
  Do not default to the approved example's age, ethnicity, costume, place or medium.
- Dialogue stays verbatim in its supplied language. Put each line in its shot,
  with the correct speaker and delivery. An offscreen speaker can still have a
  shoulder or hand visible; that does not require lip-sync or a change of camera.
- Compatible choices for an unspecified free hand, framing, or small visible
  transition can be logged as production staging. They must preserve known facts,
  plot, cuts, exits and dialogue, and must not be described as source observations.
- Do not fill gaps using a different film or example. If an uncertainty requires
  no staging decision, leave it unspecified. If facts contradict, report the conflict.
- Write performable action and concrete constraints. Do not promise artifact-free
  rendering merely because the prompt describes continuity.

## Automation contract

The automatic one-pass film route mechanically serializes locked camera/framing,
ordered action beats and exact dialogue/speakers into the same cinematic structure,
including clip-local utterance windows. It uses the approved target projection when
an adaptation is present. GPT still directs performance/staging from the supplied facts;
the ordinary per-clip checks remain. Its source-readiness method is explicitly
`one_pass_production/observed`, not independent source verification. See
`ONE_PASS_SOURCE_ANALYSIS.md` for upload-to-film execution and bounded local repairs.

The cinematic structure is shared, but video direction is medium-specific.
`film_motion.py` supplies a separate production standard to the writer and reviewer:
live action uses photographic materials and natural continuous acting; modern
Japanese 2D uses stable drawn construction, cel tones and deliberate twos/holds with
selective ones; American 2D uses expressive theatrical posing and controlled
deformation; 3D uses stable modeled volumes, grounded weight and follow-through.
The chosen design, age, wardrobe, source cameras, timing and dialogue stay fixed.
Animation cadence in a prompt is a request, not an FPS API control or a guarantee.
Material sheet masters are independent of these motion rules. Existing approved
prompts are preserved; new production writing tasks carry the motion-rule version.

The writer and independent reviewer use GPT through Avis (`gpt-6-luna` by default).
The writer returns `prompt`, `end_state`, `staging_decisions`, and `source_issues`.
Code validates structure, timing, reference order and exact dialogue. The separate
review checks shot coverage and continuity; repairs are bounded. All app routes,
including the older `/video/prompt` alias and production batch jobs, use this engine.
Disabling the writer stops new prompt writing; it never substitutes a template.
Existing approved prompts are not bulk rewritten by this policy.

Implementation and configuration: [cinematic-prompts/README.md](cinematic-prompts/README.md).
The approved [example](cinematic-prompts/approved-example.txt) is an offline
regression fixture, not story context sent to the model.

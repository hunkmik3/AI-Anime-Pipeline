# 3D Donghua điện ảnh — reusable production preset

The approved V7 character/environment masters and the two approved style references
are bundled in `agent/flowboard/assets/styles/donghua_premium/`. They are runtime
assets, not links into one user's Downloads folder. Style key: `donghua_premium`.
The original masters are retained verbatim on disk; the builder substitutes each
project's profiles and requested state, makes proportions age-faithful, and fixes
the approved output layout. No example cast, wardrobe, building or story is canon.

## Using the app

In Automation's **Video mẫu** input, choose **3D Donghua điện ảnh**, leave
**Tự chạy đến video hoàn chỉnh** enabled, select the output ratio/resolution and
upload the source. Each automatic upload creates its own project. Defaults are
1:1, 480p, 20-second clip grouping, KYC and Người thật/B2B enabled, four images and
four clips scheduled in parallel where dependencies permit. The upload button
states that production uses paid generation calls.

The source worker runs analysis, source verification, verbatim shot preparation,
cast/environment/prop/crowd profiles, design briefs and board creation. It then
hands off to the durable production controller for missing materials, continuity
planning, the Avis GPT cinematic writer/reviewer, Seedance clips and final assembly.
The browser can close while the server remains running. Existing output with
matching inputs is reused. A resumed handoff cannot start a duplicate run.

New empty boards also have a style selector in the toolbar. Once material or
video output exists, create a new project to change the look. Existing projects,
approved prompts and paid media are not bulk rewritten by installing this preset.

## Rendering and references

- **Character:** Seedream 5.0 Pro through Avis, 2K, 16:9, gray studio sheet;
  three full-body front/strict-side/back views on the left 60%, four head studies
  in a 2×2 grid on the right 40%. Exactly one requested outfit/timeline state.
- **Environment:** same renderer/preset family, single 16:9 production plate;
  architecture, era, time of day and geography come from the current profile.
- **Crowds/props:** project-specific ensemble/prop sheets with identity and
  content dependencies. Photographs and groups reuse their named subject refs.
- The bundled reference is always image 1 for sheet generation. Actual subject
  identity, costume anchors and dependency images start at image 2. A missing
  reference fails before the image request; remaining slots are not shifted.
- The style image defines rendering quality only. It does not import a woman,
  silver gown, villa or nighttime into other projects. An empty environment
  plate does not imply an empty crowd in the resulting scene.
- Video prompts follow `CLIP_PROMPT_STANDARD.md`, including crowd persistence,
  hand/prop custody, ongoing background actions and final state. The video ratio
  is independent of the 16:9 sheet ratio. Exact supplied dialogue/language stays
  unchanged; new uploads use `dialogueLanguage: source`.

The image prompt builders use the approved masters directly, without passing
them through the older image-section rewriter. The video writer remains GPT
through Avis. Preset version hashes cover the masters, references and shared look;
new production requests bind that version. Restart the server after changing
bundled preset files.

## API and recovery

`GET /api/automation/styles` exposes preset metadata and reference URLs.
`POST /api/automation/videos` accepts the usual multipart video plus an
`auto_production` JSON form field, for example:

```json
{"style":"donghua_premium","aspect_ratio":"1:1","resolution":"480p","clip_seconds":20,"kyc":true,"unmoderated":true}
```

Source summaries include `auto_production.stage`, output project ID, run ID,
run status and final output. The source panel links to the board and finished film.
Before the handoff, **Tiếp tục sản xuất sau khi xử lý** retries unfinished source
work; once a run exists, resume it in **Sản xuất**. Cached dialogue which was
translated or altered is marked stale on retry; unaffected shots are retained.

Automatic operation does not invent missing evidence or silently accept unresolved
source findings. Those failures stop before paid production, with their reason.
Provider errors and ambiguous submissions also stop; they are not blindly resent.
The current pipeline does not automatically watch and regenerate imperfect output
videos. It cannot guarantee identical style or artifact-free movement on every
generation. This implementation has a mocked full production integration test;
installing it does not itself launch another paid film render.

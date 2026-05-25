# Phase 7 — Identity Drift Test (Seedance 2.0 r2v)

> **Purpose**: a repeatable, manual end-to-end protocol that measures
> **character identity drift** across a 5-shot scene generated with
> Seedance 2.0 reference-to-video (r2v). The result decides whether
> **Phase 8** (multi-shot-per-generation) is viable — if single-shot r2v
> already drifts badly, packing multiple shots into one generation will
> drift worse.
>
> **Status**: scaffolding (Phase 7.6). Run once the BytePlus key has
> Seedance 2.0 activated. This doc is the test plan + scoring template;
> fill the RESULTS section after a run.

---

## 0. Pre-conditions

- [ ] BytePlus key has `dreamina-seedance-2-0-260128` activated (probe with
      a 1-ref submit; a 404 on the model id means it's not enabled).
- [ ] R2 configured (`~/.flowboard/secrets.json` `r2` block) — references
      must be hoisted to public URLs. See `docs/r2_setup.md`.
- [ ] Two **character reference sheets** on hand (full-body + face), from
      EP 1, saved to the project asset library:
  - **Kenji** — full character sheet (`@imageKenji`)
  - **Ren** — full character sheet (`@imageRen`)
- [ ] Pricing rate set so cost rolls up (`config.json` per-model rate),
      else cost reads `$0.00` (acceptable for this test; tokens still log).

## 1. Setup

**Project Bible** (style anchor):
```
Art style: cinematic 2D anime, cel-shaded, clean line art.
Palette: warm tungsten interior, desaturated shadows.
Line style: medium-weight, consistent.
Lighting: motivated practical lights (desk lamp, window).
Negative: 3D render, photoreal, blur, extra fingers, off-model faces.
```

**Scene Bible** (spatial anchor — office interior):
```
Interior, mid-size corporate office, late afternoon. Kenji's desk is
camera-left by the window; Ren's desk camera-right. Glass partition behind.
Spatial constraint: window is always camera-left; door is camera-right.
```

**References** (the r2v anchors):

| Ref | Asset | Role hint (UI-only) |
|---|---|---|
| `@image1` | Kenji character sheet | character |
| `@image2` | Ren character sheet | character |

> Role hints are persisted on the VideoNode but **not** sent to the API
> (the API accepts only `reference_image`). They exist so Phase 6 prompt
> synthesis can later compose semantic `@imageN` description lines.

## 2. The 5 shots

All shots: Seedance 2.0, r2v mode, `--rt 16:9 --rs 1080p`, `duration 5`,
both character refs attached (order Kenji=@image1, Ren=@image2) + the
Scene Bible injected. Vary only the motion prompt + framing.

| # | Shot | Framing | Motion prompt (EN, post-synthesis) |
|---|---|---|---|
| 1 | Establishing | Wide | "Wide establishing of the office, late afternoon light from camera-left window. @image1 Kenji at desk left, @image2 Ren at desk right. Slow push-in." |
| 2 | Two-shot | Medium | "Medium two-shot. @image1 Kenji and @image2 Ren face each other across the aisle, mid-conversation. Subtle idle motion." |
| 3 | CU Kenji | Close-up | "Close-up of @image1 Kenji, speaking, slight head turn toward camera. Window light camera-left." |
| 4 | CU Ren | Close-up | "Close-up of @image2 Ren, listening then reacting, eyes widen. Same lighting." |
| 5 | Wide reaction | Wide | "Wide reaction shot, both @image1 Kenji and @image2 Ren standing, Ren steps back. Camera holds." |

## 3. Build procedure (per shot, in Flowboard UI)

1. New Shot in the office Scene.
2. On the canvas: add a **BibleRef** node (scene) + a **Video** node.
3. Open the Video node settings → select model **Dreamina Seedance 2.0**.
4. In **References (multi-ref)**: add Kenji (`@image1`) then Ren (`@image2`)
   — confirm the order badges read `@image1`, `@image2`. Set role hints to
   `character` for both.
5. Set duration 5, aspect 16:9, resolution 1080p.
6. Enter the shot's motion prompt; Generate.
7. Wait for the clip; download / note the `media_id`.

> Expectation: 2 refs → **r2v mode** (≥2 refs). The submit body should
> carry **two `reference_image` blocks and no `first_frame`**, with
> `@image1 @image2` auto-injected into the prompt text (unless the
> synthesized prompt already contains them). Verify in the worker logs /
> `Request.result.raw`.

## 4. Evaluation — identity drift, shot 1 → shot 5

For each of the 5 clips, score each character on a **1–5** scale
(5 = on-model, indistinguishable from the reference sheet; 1 = a
different person). Score the **first frame** and the **last frame**
separately to also catch within-clip drift.

### Scoring template

| Shot | Char | Face (1-5) | Hair (1-5) | Outfit (1-5) | Within-clip drift (first→last) | Notes |
|---|---|---|---|---|---|---|
| 1 | Kenji |  |  |  |  |  |
| 1 | Ren |  |  |  |  |  |
| 2 | Kenji |  |  |  |  |  |
| 2 | Ren |  |  |  |  |  |
| 3 | Kenji |  |  |  |  |  |
| 4 | Ren |  |  |  |  |  |
| 5 | Kenji |  |  |  |  |  |
| 5 | Ren |  |  |  |  |  |

### Aggregate criteria

- **Cross-shot identity stability** — average Face score across shots per
  character. Target for Path B (70-80% of manual Dreamina UI): **≥ 3.5/5**.
- **Outfit consistency** — outfit score should not fall >1 point between
  the establishing shot (1) and the final shot (5).
- **Spatial consistency** — does the window stay camera-left across shots?
  (Scene Bible compliance — pass/fail per shot.)
- **Within-clip drift** — flag any clip where the character visibly morphs
  between first and last frame (a Phase 8 blocker if common).

### Verdict gate for Phase 8

| Outcome | Decision |
|---|---|
| Avg Face ≥ 3.5 AND ≤1 within-clip morph across 5 clips | **Phase 8 GO** — try multi-shot-per-gen. |
| Avg Face 2.5–3.5 | **Investigate** — try ref ordering, sheet quality, prompt @imageN descriptions before Phase 8. |
| Avg Face < 2.5 OR frequent within-clip morph | **Phase 8 NO-GO** — single-shot r2v isn't stable enough; revisit refs/model. |

## 5. RESULTS (fill after a run)

- Date run:
- Model id / upstream:
- Tokens per clip (5s): _expected ≈108,900_
- Aggregate Face (Kenji / Ren):
- Aggregate Outfit:
- Spatial Bible compliance (x/5):
- Within-clip morphs observed:
- **Verdict**:
- Raw clips: `storage/media/<media_id>.mp4` (gitignored)

"""Medium-specific directing standards; separate from image-sheet masters.

Cadence is a directing request, not a provider FPS control or a render guarantee.
Changing it must never regenerate material sheets or rewrite approved prompts.
"""
import hashlib
import json
from copy import deepcopy

COMMON = (
    "Use the supplied shot timings, cameras, dialogue language, identities, age, wardrobe, "
    "set geography and prop custody unchanged. Style controls representation and performance, "
    "not story or casting. Stage contacts with clear approach, contact, weight transfer and "
    "release; preserve occupied hands and rigid prop construction. Background participants "
    "remain present and continue motivated action where visible. Do not turn a reference sheet "
    "into frozen actors. Avoid changing the camera or adding a cut to demonstrate the style."
)
STANDARDS = {
    "live_action": {
        "label": "Live-action feature film",
        "rendering": "Photographic human faces and locations; natural skin texture and hair, real fabric and metal responses, motivated light and restrained depth of field. No CGI face, waxy smoothing or cel shading.",
        "performance": "Natural live-action timing with subtle eye focus, breathing, blinks and speech-driven expression. Realistic weight, foot contact, hand anatomy and cloth inertia. No cartoon squash/stretch, rubber limbs or game-idle acting.",
        "cadence": "Natural cinematic motion and motion blur; continuous human movement. Do not apply anime holds, stepped drawings or random stutter.",
    },
    "anime_jp": {
        "label": "Modern Japanese 2D anime feature film",
        "rendering": "Precise drawn contours, stable line weight and hair shapes, clean fills with two or three cel-shadow tones; painted backgrounds, coherent perspective and restrained compositing. No sculpted CGI face, photographic pores or glossy 3D shading.",
        "performance": "Strong readable key poses and purposeful holds, controlled in-betweens, expression-specific mouth drawings synchronized to supplied speech. Maintain facial construction, anatomy, garment shapes and prop volume across cuts. Secondary hair, clothing and crowd action remain motivated rather than frozen.",
        "cadence": "Request character animation mainly on twos, occasional deliberate holds on threes, selective ones for fast action and clear hand contacts. The feel of a 24 fps anime timeline is a cadence reference, not a requirement to quantize or retime the supplied fractional shot boundaries; the provider determines encoded FPS. Camera moves may remain smooth independently. No random stutter, optical-flow face warping or uniformly interpolated ultra-smooth animation.",
    },
    "cartoon_us": {
        "label": "American theatrical 2D animation",
        "rendering": "Confident flowing drawn contours, stable shapes, clean fills and economical cel shadows; coordinated painted backgrounds. No photoreal microtexture, CGI surfaces or stock-vector cutout appearance.",
        "performance": "Expressive pose-to-pose acting, clear anticipation, rhythmic timing, overlapping action and follow-through. Controlled squash/stretch only as appropriate to emotion and material; rigid props stay rigid. Preserve identity, age and volumes after each deformation. Avoid puppet-like motion and persistent off-model faces.",
        "cadence": "Use twos where appropriate and ones for fast or fluid passages, with brief purposeful smears only during rapid movement. Camera and drawing cadence may differ. No arbitrary jitter or motion interpolation that destroys linework.",
    },
    "animation_3d": {
        "label": "Cinematic 3D animation",
        "rendering": "Keep the selected 3D art direction: modeled volumes, coherent groomed hair, distinct material responses, restrained subsurface scattering, motivated bounce light and grounded shadows. Do not convert stylized or donghua faces to live action or flat cel drawings.",
        "performance": "Fluid pose-to-pose animation with anticipation, balance, convincing weight, contact, follow-through and responsive eyes. Hair and cloth follow the actor's movement and physical forces. Match expression amplitude to the selected design; keep donghua drama restrained. Preserve age and design proportions, never automatically enlarge eyes or make adults childlike.",
        "cadence": "Continuous feature-animation motion. Controlled squash/stretch only when the chosen design calls for it, never in hard props. Avoid floaty footsteps, frozen background actors, game-idle loops, texture crawling and unintended exposure flicker.",
    },
}
STYLE_MEDIUM = {
    "realistic": "live_action", "live_action_feature": "live_action",
    "anime": "anime_jp", "anime_jp_modern": "anime_jp", "cartoon_us_2d": "cartoon_us",
    "cg3d": "animation_3d", "donghua_premium": "animation_3d",
}


def standard(style):
    medium = STYLE_MEDIUM.get(style)
    if medium is None:
        return {}  # Unknown/custom styles must not silently become live action.
    return {"medium": medium, **deepcopy(STANDARDS[medium]), "continuity": COMMON,
            "scope": "Prompt direction only; actual encoded FPS and animation cadence require output inspection. Do not change the supplied source timing."}


def text(style):
    value = standard(style)
    return "\n".join(value[k] for k in ("rendering", "performance", "cadence", "continuity")) if value else ""


def version(style):
    return "film-motion-v1-" + hashlib.sha256(json.dumps(standard(style), sort_keys=True).encode()).hexdigest()[:16]

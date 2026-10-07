import approvedCatalog from "./approved-film-styles.json";

export const PROFILE_STYLE_KEYS = ["live_action_feature", "anime_jp_modern", "cartoon_us_2d"] as const;
export type FilmStyle = "realistic" | "anime" | "cg3d" | "donghua_premium" | typeof PROFILE_STYLE_KEYS[number];

// Mirrors the approved runtime preset.json assets; the backend regression test
// checks names and full style prose so the selector and generators stay in sync.
export const APPROVED_FILM_STYLES = approvedCatalog.map(p => ({ ...p, key: p.key as FilmStyle }));
export function isFilmStylePreset(style: string): boolean {
  return style === "donghua_premium" || PROFILE_STYLE_KEYS.some(key => key === style);
}

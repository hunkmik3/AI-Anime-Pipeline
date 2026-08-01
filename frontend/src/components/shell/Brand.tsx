import { Link } from "react-router-dom";

/**
 * The app's name mark.
 *
 * One component because there were three copies — the top bar, the sidebar (shown
 * on the canvas, where there is no top bar) and the admin console — each with its
 * own logo size, weight and spacing, so the mark visibly changed size depending on
 * which screen you were on. Two of them also hardcoded `v1.0.2` while package.json
 * had moved on to 1.2.12.
 */

/** Injected by Vite at build time from package.json — see vite.config.ts. */
const VERSION: string =
  typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : "";

export function Brand({
  to = "/projects",
  subtitle,
}: {
  to?: string;
  /** A line under the name, e.g. "Admin console". Omit for the plain mark. */
  subtitle?: string;
}) {
  return (
    <Link to={to} className={`brand${subtitle ? " brand--stacked" : ""}`}>
      <img src="/favicon.png" alt="" width={24} height={24} />
      <span className="brand__text">
        <span className="brand__name">
          Giant Studio
          {VERSION ? <em className="brand__ver">v{VERSION}</em> : null}
        </span>
        {subtitle ? <small className="brand__sub">{subtitle}</small> : null}
      </span>
    </Link>
  );
}

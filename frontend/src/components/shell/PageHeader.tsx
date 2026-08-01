import type { ReactNode } from "react";

/**
 * The block every page opens with: what this page is, and what you can do here.
 *
 * Pages used to each start differently — some had a title, some didn't, actions
 * sat top-left on one screen and bottom-right on the next. With nothing in a
 * predictable place, you had to re-read each page to find the button.
 *
 * `crumb` is for a page nested under another (a project inside Manage); `tabs`
 * renders below the title so switching views doesn't move the title around.
 */
export function PageHeader({
  title,
  subtitle,
  crumb,
  actions,
  tabs,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  crumb?: ReactNode;
  actions?: ReactNode;
  tabs?: ReactNode;
}) {
  return (
    <div className="pagehead">
      {crumb ? <div className="pagehead__crumb">{crumb}</div> : null}
      <div className="pagehead__row">
        <div className="pagehead__titles">
          <h1 className="pagehead__title">{title}</h1>
          {subtitle ? <p className="pagehead__sub">{subtitle}</p> : null}
        </div>
        {actions ? <div className="pagehead__actions">{actions}</div> : null}
      </div>
      {tabs ? <div className="pagehead__tabs">{tabs}</div> : null}
    </div>
  );
}

/**
 * Tab strip for switching views within one page. Kept here rather than in each
 * page so every tabbed screen looks and behaves the same.
 */
export function PageTabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: readonly { key: T; label: string }[];
  active: T;
  onChange: (key: T) => void;
}) {
  return (
    <div className="pagetabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.key}
          role="tab"
          aria-selected={active === t.key}
          className={`pagetabs__tab${active === t.key ? " is-active" : ""}`}
          onClick={() => onChange(t.key)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

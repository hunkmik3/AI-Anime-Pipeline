/**
 * Self-contained interactive charts for the admin console (no chart library).
 * Every chart ships a hover layer by default — the dataviz baseline for an
 * HTML/SVG chart. Colours are the validated dark-mode marks: kept #1f9e57,
 * wasted #e8542b, emerald #1f9e57 for single-series magnitude. Text always
 * wears ink tokens, never the series colour.
 */
import { useState, type ReactNode } from "react";

const usd = (v: number) => `$${v.toFixed(2)}`;

/* ── shared cursor-following tooltip ─────────────────────────────────────── */
interface Tip {
  x: number;
  y: number;
  flip: boolean; // anchor to the right of x (grow leftward) near the right edge
  node: ReactNode;
}
/** Build a Tip from a mouse event relative to a container box, flipping the
 *  anchor when the cursor is past 60% width so it never overflows the card. */
function mkTip(e: { clientX: number; clientY: number }, box: DOMRect, node: ReactNode): Tip {
  const relX = e.clientX - box.left;
  const flip = relX > box.width * 0.6;
  return {
    x: flip ? relX - 12 : relX + 12,
    y: e.clientY - box.top + 14,
    flip,
    node,
  };
}
function Tooltip({ tip }: { tip: Tip | null }) {
  if (!tip) return null;
  return (
    <div
      className="chartt"
      style={{ left: tip.x, top: tip.y, transform: tip.flip ? "translateX(-100%)" : undefined }}
      role="status"
    >
      {tip.node}
    </div>
  );
}

/* ── donut: composition of a whole (e.g. kept vs wasted) ─────────────────── */
export interface Slice {
  label: string;
  value: number;
  color: string;
}
export function Donut({
  data,
  centerTop,
  size = 168,
}: {
  data: Slice[];
  centerTop?: string;
  size?: number;
}) {
  const [hi, setHi] = useState<number | null>(null);
  const [tip, setTip] = useState<Tip | null>(null);
  const total = data.reduce((s, d) => s + d.value, 0) || 1;
  const R = size / 2;
  const stroke = size * 0.16;
  const r = R - stroke / 2 - 2;
  const C = 2 * Math.PI * r;
  let offset = 0;

  const active = hi != null ? data[hi] : null;
  const centerVal = active ? active.value : total;
  const centerLbl = active ? active.label : centerTop ?? "Total";

  return (
    <div
      className="chart"
      onMouseLeave={() => {
        setHi(null);
        setTip(null);
      }}
    >
      <div className="donut">
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img">
          {/* track */}
          <circle cx={R} cy={R} r={r} fill="none" stroke="rgba(145,158,171,0.14)" strokeWidth={stroke} />
          {data.map((d, i) => {
            const frac = d.value / total;
            const len = frac * C;
            const seg = (
              <circle
                key={i}
                cx={R}
                cy={R}
                r={r}
                fill="none"
                stroke={d.color}
                strokeWidth={hi === i ? stroke + 4 : stroke}
                strokeDasharray={`${Math.max(0, len - 2)} ${C - Math.max(0, len - 2)}`}
                strokeDashoffset={-offset}
                strokeLinecap="butt"
                transform={`rotate(-90 ${R} ${R})`}
                style={{ transition: "stroke-width 120ms ease", cursor: "pointer", opacity: hi == null || hi === i ? 1 : 0.5 }}
                onMouseEnter={() => setHi(i)}
                onMouseMove={(e) => {
                  const box = e.currentTarget.ownerSVGElement!.getBoundingClientRect();
                  setTip(
                    mkTip(e, box, (
                      <>
                        <b style={{ color: d.color }}>{d.label}</b>
                        <span>
                          {usd(d.value)} · {((frac * 100) || 0).toFixed(1)}%
                        </span>
                      </>
                    )),
                  );
                }}
              />
            );
            offset += len;
            return seg;
          })}
        </svg>
        <div className="donut__center">
          <span className="donut__val">{usd(centerVal)}</span>
          <span className="donut__lbl">{centerLbl}</span>
        </div>
        <Tooltip tip={tip} />
      </div>
      <ul className="chart-legend">
        {data.map((d, i) => (
          <li
            key={i}
            className={hi != null && hi !== i ? "is-dim" : undefined}
            onMouseEnter={() => setHi(i)}
            onMouseLeave={() => setHi(null)}
          >
            <span className="chart-legend__dot" style={{ background: d.color }} />
            <span className="chart-legend__lbl">{d.label}</span>
            <span className="chart-legend__val">
              {usd(d.value)} · {(((d.value / total) * 100) || 0).toFixed(0)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ── horizontal bars: magnitude across categories (single hue) ───────────── */
export interface Bar {
  label: string;
  value: number;
  meta?: string;
}
export function HBars({
  data,
  color = "#1f9e57",
  unit = "usd",
}: {
  data: Bar[];
  color?: string;
  unit?: "usd" | "count";
}) {
  const [hi, setHi] = useState<number | null>(null);
  const [tip, setTip] = useState<Tip | null>(null);
  const max = Math.max(1, ...data.map((d) => d.value));
  const fmt = (v: number) => (unit === "usd" ? usd(v) : String(v));
  if (!data.length) return <div className="chart-empty">No data yet.</div>;
  return (
    <div className="chart hbars" onMouseLeave={() => { setHi(null); setTip(null); }}>
      {data.map((d, i) => (
        <div
          key={i}
          className={`hbar${hi === i ? " is-hi" : ""}`}
          onMouseEnter={() => setHi(i)}
          onMouseMove={(e) => {
            const box = (e.currentTarget.parentElement as HTMLElement).getBoundingClientRect();
            setTip(
              mkTip(e, box, (
                <>
                  <b>{d.label}</b>
                  <span>{fmt(d.value)}{d.meta ? ` · ${d.meta}` : ""}</span>
                </>
              )),
            );
          }}
        >
          <span className="hbar__label" title={d.label}>{d.label}</span>
          <span className="hbar__track">
            <span
              className="hbar__fill"
              style={{ width: `${(d.value / max) * 100}%`, background: color }}
            />
          </span>
          <span className="hbar__val">
            {fmt(d.value)}
            {d.meta ? <small> · {d.meta}</small> : null}
          </span>
        </div>
      ))}
      <Tooltip tip={tip} />
    </div>
  );
}

/* ── stacked horizontal bars: two-part composition per row ───────────────── */
export interface StackRow {
  label: string;
  a: number; // first segment (e.g. wasted)
  b: number; // second segment (e.g. kept)
}
export function StackedBars({
  rows,
  keys,
}: {
  rows: StackRow[];
  keys: { a: { label: string; color: string }; b: { label: string; color: string } };
}) {
  const [tip, setTip] = useState<Tip | null>(null);
  const max = Math.max(1, ...rows.map((r) => r.a + r.b));
  if (!rows.length) return <div className="chart-empty">No data yet.</div>;
  const seg = (
    e: React.MouseEvent,
    label: string,
    color: string,
    value: number,
    total: number,
  ) => {
    const box = (e.currentTarget.closest(".chart") as HTMLElement).getBoundingClientRect();
    setTip(
      mkTip(e, box, (
        <>
          <b style={{ color }}>{label}</b>
          <span>
            {usd(value)} · {total > 0 ? ((value / total) * 100).toFixed(0) : 0}%
          </span>
        </>
      )),
    );
  };
  return (
    <div className="chart stacked" onMouseLeave={() => setTip(null)}>
      <ul className="chart-legend chart-legend--top">
        <li>
          <span className="chart-legend__dot" style={{ background: keys.a.color }} />
          <span className="chart-legend__lbl">{keys.a.label}</span>
        </li>
        <li>
          <span className="chart-legend__dot" style={{ background: keys.b.color }} />
          <span className="chart-legend__lbl">{keys.b.label}</span>
        </li>
      </ul>
      {rows.map((r, i) => {
        const t = r.a + r.b;
        return (
          <div key={i} className="sbar">
            <span className="sbar__label" title={r.label}>{r.label}</span>
            <span className="sbar__track" style={{ width: `${(t / max) * 100}%` }}>
              {r.a > 0 && (
                <span
                  className="sbar__seg"
                  style={{ flexGrow: r.a, background: keys.a.color }}
                  onMouseMove={(e) => seg(e, keys.a.label, keys.a.color, r.a, t)}
                />
              )}
              {r.b > 0 && (
                <span
                  className="sbar__seg"
                  style={{ flexGrow: r.b, background: keys.b.color }}
                  onMouseMove={(e) => seg(e, keys.b.label, keys.b.color, r.b, t)}
                />
              )}
            </span>
            <span className="sbar__val">{usd(t)}</span>
          </div>
        );
      })}
      <Tooltip tip={tip} />
    </div>
  );
}

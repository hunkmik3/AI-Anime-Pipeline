import { Fragment } from "react";

/**
 * Renders a long identifier so it wraps at natural boundaries (`_`, `-`, `#`)
 * instead of being split mid-word. Browsers don't treat underscores as break
 * opportunities, so `MOGU_26014_P1VIO_SchoolViolencePilot#1` is otherwise one
 * giant "word" that gets chopped anywhere. We insert a <wbr> after each
 * delimiter — a zero-width break hint that adds no visible/copyable character —
 * so the break lands between tokens (…_P1VIO_ ↵ SchoolViolencePilot#1), keeping
 * each word intact. A word that still can't fit falls back to CSS break-word.
 */
export function BreakableName({ text }: { text: string }) {
  // Split but keep the delimiters, so we can drop a <wbr> right after each one.
  const parts = text.split(/([_\-#/.])/);
  return (
    <>
      {parts.map((p, i) =>
        /^[_\-#/.]$/.test(p) ? (
          <Fragment key={i}>
            {p}
            <wbr />
          </Fragment>
        ) : (
          <Fragment key={i}>{p}</Fragment>
        ),
      )}
    </>
  );
}

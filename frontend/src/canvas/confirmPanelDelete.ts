import type { Node } from "@xyflow/react";

/**
 * Ask before throwing away the panel a sequence was handed.
 *
 * A panel that has crossed from Giantflow arrives once. The handover records
 * which sequence a panel became, so a second approval finds it and does
 * nothing — that is what stops a re-approval stacking a second copy on the
 * canvas, and it is also why a deleted one never comes back on its own. Nobody
 * finds that out at a good moment.
 *
 * So the deletion is confirmed, and the message says the part that matters: not
 * "are you sure", which everybody clicks through, but what they will have to do
 * afterwards.
 *
 * Only nodes carrying `sourcePanelId` are guarded. Everything the artist made
 * themselves deletes with the Delete key as before — a canvas that asked twice
 * about every node would train people to dismiss it, and then this one goes
 * through too.
 */

/** The mark the handover leaves. A node without it was made here. */
export function isPanelNode(n: { data?: unknown }): boolean {
  const d = n.data as Record<string, unknown> | undefined;
  return Boolean(d && d.sourcePanelId);
}

function label(n: { data?: unknown }): string {
  const d = n.data as Record<string, unknown> | undefined;
  return typeof d?.title === "string" && d.title ? d.title : "panel";
}

/**
 * `true` to let the delete proceed. Wired to ReactFlow's `onBeforeDelete`,
 * which vetoes BEFORE the node leaves the screen — `onNodesDelete` runs after
 * the fact, so confirming there would mean putting a removed node back.
 */
export function confirmPanelDelete(deleted: { nodes: Node[] }): boolean {
  const panels = deleted.nodes.filter(isPanelNode);
  if (panels.length === 0) return true;

  const names = panels.map(label);
  const what =
    panels.length === 1
      ? `ảnh panel “${names[0]}”`
      : `${panels.length} ảnh panel (${names.slice(0, 3).join(", ")}${
          names.length > 3 ? "…" : ""
        })`;

  return window.confirm(
    `Xoá ${what}?\n\n` +
      "Đây là ảnh gốc chuyển sang từ Giantflow. Nó chỉ được giao MỘT LẦN — " +
      "xoá rồi thì duyệt lại panel cũng không đưa nó về nữa.\n\n" +
      "Muốn dùng lại, bạn phải tự thêm ảnh vào: kéo từ thư viện, hoặc mở panel " +
      "bên Giantflow để lấy.",
  );
}

import { create } from "zustand";

import { listMySeries, listReviewQueue } from "../api/client";

/**
 * How much work is waiting for the signed-in user, for the badges in the nav.
 *
 * Before this, "My work" and "Review" were links buried in the account dropdown
 * with no indication that anything needed doing — a reviewer had to remember to
 * go and look, and a rejected episode could sit for days because the artist was
 * never told it came back. A number on the nav is the whole point: the app tells
 * you, instead of waiting to be asked.
 *
 * Both counts are deliberately "needs *me* to act", not "exists". An episode
 * already submitted and awaiting review is not the artist's problem, so counting
 * it would train people to ignore the badge.
 */

export interface InboxState {
  /** Series handed to me that I still have to deliver (draft or sent back). */
  myWork: number;
  /** Submissions waiting on my verdict. */
  toReview: number;
  loaded: boolean;
  refresh: () => Promise<void>;
}

export const useInboxStore = create<InboxState>((set) => ({
  myWork: 0,
  toReview: 0,
  loaded: false,

  refresh: async () => {
    // Each side is independent: a viewer with no review rights still gets their
    // own count, so one failing call must not blank the other badge.
    const [work, review] = await Promise.allSettled([
      listMySeries(),
      listReviewQueue(),
    ]);

    const patch: Partial<InboxState> = { loaded: true };
    if (work.status === "fulfilled") {
      patch.myWork = work.value.series.filter((s) => {
        const status = s.deliverable_status || "draft";
        // Approved and in-review need nothing from the assignee.
        return status === "draft" || status === "rejected";
      }).length;
    }
    if (review.status === "fulfilled") {
      patch.toReview = review.value.items.length;
    }
    set(patch);
  },
}));

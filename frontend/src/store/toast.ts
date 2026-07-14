import { create } from "zustand";

/**
 * App-wide transient toast (success / error), independent of the domain
 * stores. The Toaster renders it at top priority. Auto-dismiss is handled
 * by the Toaster's timer.
 */
export type ToastKind = "success" | "error";

interface ToastState {
  message: string | null;
  kind: ToastKind;
  show: (message: string, kind?: ToastKind) => void;
  clear: () => void;
}

export const useToastStore = create<ToastState>((set) => ({
  message: null,
  kind: "success",
  show: (message, kind = "success") => set({ message, kind }),
  clear: () => set({ message: null }),
}));

/** Imperative helper for non-component call sites. */
export const toast = (message: string, kind: ToastKind = "success") =>
  useToastStore.getState().show(message, kind);

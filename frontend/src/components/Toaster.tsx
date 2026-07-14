import { useEffect, useRef } from "react";
import { useShotWorkflowStore } from "../store/shotWorkflow";
import { useChatStore } from "../store/chat";
import { useGenerationStore } from "../store/generation";
import { usePipelineStore } from "../store/pipeline";
import { useToastStore } from "../store/toast";

export function Toaster() {
  const boardError = useShotWorkflowStore((s) => s.error);
  const chatError = useChatStore((s) => s.error);
  const genError = useGenerationStore((s) => s.error);
  const pipelineError = usePipelineStore((s) => s.error);
  const clearBoardError = useShotWorkflowStore((s) => s.clearError);
  const clearChatError = useChatStore((s) => s.clearError);
  const clearGenError = useGenerationStore((s) => s.clearError);
  const clearPipelineError = usePipelineStore((s) => s.clearError);

  // Phase 8.4 — non-error info/success notice (e.g. "Frame extracted").
  const notice = useGenerationStore((s) => s.notice);
  const clearNotice = useGenerationStore((s) => s.clearNotice);
  // App-wide explicit toast (admin actions, etc.).
  const toastMsg = useToastStore((s) => s.message);
  const toastKind = useToastStore((s) => s.kind);
  const clearToast = useToastStore((s) => s.clear);

  const storeError = chatError ?? pipelineError ?? genError ?? boardError;
  const clearStoreError =
    chatError !== null
      ? clearChatError
      : pipelineError !== null
      ? clearPipelineError
      : genError !== null
      ? clearGenError
      : clearBoardError;

  // Priority: explicit toast > store error > info notice.
  let message: string | null;
  let isError: boolean;
  let dismiss: () => void;
  if (toastMsg) {
    message = toastMsg;
    isError = toastKind === "error";
    dismiss = clearToast;
  } else if (storeError) {
    message = storeError;
    isError = true;
    dismiss = clearStoreError;
  } else {
    message = notice;
    isError = false;
    dismiss = clearNotice;
  }

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!message) return;

    if (timerRef.current !== null) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      dismiss();
      timerRef.current = null;
    }, 5000);

    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [message, dismiss]);

  if (!message) return null;

  return (
    <div
      className={`toaster${isError ? "" : " toaster--notice"}`}
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
    >
      <div className="toaster__body">
        <span className="toaster__icon" aria-hidden="true">{isError ? "!" : "✓"}</span>
        <span className="toaster__msg">{message}</span>
        <button
          className="toaster__close"
          onClick={dismiss}
          aria-label={isError ? "Dismiss error" : "Dismiss"}
        >
          ×
        </button>
      </div>
    </div>
  );
}

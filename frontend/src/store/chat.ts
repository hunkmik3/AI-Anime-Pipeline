import { create } from "zustand";
import {
  listChatMessages,
  sendChatMessage,
  type ChatMessageDTO,
  type PlanDTO,
} from "../api/client";

interface ChatState {
  // Phase 3: chat module is dead code (ChatSidebar disabled in App.tsx).
  // ``boardId`` renamed semantically to the project UUID for the new chat
  // route, but the surface is preserved to avoid downstream churn.
  boardId: string | null;
  messages: ChatMessageDTO[];
  plans: Record<number, PlanDTO>;
  loading: boolean;
  pending: boolean;
  error: string | null;

  loadChat(projectId: string): Promise<void>;
  sendMessage(message: string, mentions: string[]): Promise<void>;
  clearError(): void;
}

// Monotonic counter for optimistic temp IDs; two sends in the same millisecond
// used to collide on `-Date.now()`.
let _tempSeq = 0;

export const useChatStore = create<ChatState>((set, get) => ({
  boardId: null,
  messages: [],
  plans: {},
  loading: false,
  pending: false,
  error: null,

  async loadChat(projectId: string) {
    set({ boardId: projectId, loading: true, error: null });
    try {
      const messages = await listChatMessages(projectId);
      set({ messages, loading: false });
    } catch (err) {
      set({
        loading: false,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  },

  async sendMessage(message: string, mentions: string[]) {
    const { boardId, messages } = get();
    if (boardId === null) return;

    const tempId = -(++_tempSeq);
    const optimisticMsg: ChatMessageDTO = {
      id: tempId,
      project_id: boardId,
      role: "user",
      content: message,
      mentions,
      created_at: new Date().toISOString(),
    };

    set({ messages: [...messages, optimisticMsg], pending: true });

    try {
      const response = await sendChatMessage(boardId, message, mentions);
      set((s) => ({
        messages: [
          ...s.messages.filter((m) => m.id !== tempId),
          response.user,
          response.assistant,
        ],
        plans: response.plan
          ? { ...s.plans, [response.assistant.id]: response.plan }
          : s.plans,
        pending: false,
      }));
    } catch (err) {
      set((s) => ({
        messages: s.messages.filter((m) => m.id !== tempId),
        pending: false,
        error: err instanceof Error ? err.message : String(err),
      }));
    }
  },

  clearError() {
    set({ error: null });
  },
}));

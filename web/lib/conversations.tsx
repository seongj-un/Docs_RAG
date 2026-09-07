"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  conversations as api,
  describeError,
  type Conversation,
  type ErrorCopy,
} from "@/lib/api";

type ConversationsValue = {
  conversations: Conversation[];
  loading: boolean;
  error: ErrorCopy | null;
  refresh: () => Promise<void>;
  create: (scopeDocumentId: string | null) => Promise<Conversation>;
  remove: (id: string) => Promise<void>;
};

const ConversationsContext = createContext<ConversationsValue | null>(null);

export function ConversationsProvider({ children }: { children: ReactNode }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ErrorCopy | null>(null);

  const refresh = useCallback(async () => {
    try {
      setConversations(await api.listConversations());
      setError(null);
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // 마운트 시 서버에서 가져온다 — 효과가 있어야 할 자리.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
  }, [refresh]);

  const create = useCallback(async (scopeDocumentId: string | null) => {
    const created = await api.createConversation(scopeDocumentId);
    setConversations((current) => [created, ...current]);
    return created;
  }, []);

  const remove = useCallback(async (id: string) => {
    await api.deleteConversation(id);
    setConversations((current) => current.filter((item) => item.id !== id));
  }, []);

  const value = useMemo(
    () => ({ conversations, loading, error, refresh, create, remove }),
    [conversations, loading, error, refresh, create, remove],
  );

  return (
    <ConversationsContext.Provider value={value}>
      {children}
    </ConversationsContext.Provider>
  );
}

export function useConversations(): ConversationsValue {
  const value = useContext(ConversationsContext);
  if (value === null) {
    throw new Error(
      "useConversations must be used inside ConversationsProvider",
    );
  }
  return value;
}

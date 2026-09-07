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
  describeError,
  documents as api,
  type Document,
  type ErrorCopy,
} from "@/lib/api";

/** 색인이 끝날 때까지 다시 물어보는 간격. */
const POLL_MS = 2000;

function isBusy(document: Document): boolean {
  return document.status === "pending" || document.status === "processing";
}

type DocumentsValue = {
  documents: Document[];
  loading: boolean;
  error: ErrorCopy | null;
  refresh: () => Promise<void>;
  upload: (file: File) => Promise<void>;
  remove: (id: string) => Promise<void>;
  byId: (id: string) => Document | undefined;
};

const DocumentsContext = createContext<DocumentsValue | null>(null);

/* 사이드바와 문서 화면이 같은 목록을 본다. 각자 불러오면 폴링 루프가 둘이
 * 되고, 한쪽에서 지운 문서가 다른 쪽에 남는다. */
export function DocumentsProvider({ children }: { children: ReactNode }) {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ErrorCopy | null>(null);

  const refresh = useCallback(async () => {
    try {
      setDocuments(await api.listDocuments());
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

  /* 읽는 중인 문서가 하나라도 있을 때만 폴링한다. busy는 불리언이라
   * 상태가 실제로 뒤집힐 때만 타이머를 다시 건다 — documents 배열에
   * 의존하면 폴링할 때마다 타이머를 새로 만든다. */
  const busy = documents.some(isBusy);
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [busy, refresh]);

  const upload = useCallback(
    async (file: File) => {
      const created = await api.uploadDocument(file);
      /* 202로 돌아오므로 목록에 아직 없다. 폴링이 잡아가기 전에 먼저
       * 끼워 넣어야 "올렸는데 아무 일도 없다"로 보이지 않는다. */
      setDocuments((current) => [
        {
          id: created.id,
          filename: created.filename,
          status: created.status,
          num_pages: null,
          error: null,
          created_at: new Date().toISOString(),
        },
        ...current,
      ]);
    },
    [],
  );

  const remove = useCallback(async (id: string) => {
    await api.deleteDocument(id);
    setDocuments((current) => current.filter((item) => item.id !== id));
  }, []);

  const byId = useCallback(
    (id: string) => documents.find((item) => item.id === id),
    [documents],
  );

  const value = useMemo(
    () => ({ documents, loading, error, refresh, upload, remove, byId }),
    [documents, loading, error, refresh, upload, remove, byId],
  );

  return (
    <DocumentsContext.Provider value={value}>
      {children}
    </DocumentsContext.Provider>
  );
}

export function useDocuments(): DocumentsValue {
  const value = useContext(DocumentsContext);
  if (value === null) {
    throw new Error("useDocuments must be used inside DocumentsProvider");
  }
  return value;
}

"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

import { auth, type User } from "@/lib/api";

/* 인증 상태는 쿠키에만 있고 자바스크립트는 그걸 읽을 수 없다(HttpOnly).
 * 그래서 "로그인했는지"는 /auth/me를 한 번 물어봐야만 알 수 있다. */
type SessionValue = {
  user: User | null;
  /** 아직 /auth/me 응답을 못 받은 상태. 이때 화면을 그리면 깜빡인다. */
  loading: boolean;
  setUser: (user: User | null) => void;
  /** 서버 상태를 다시 읽는다. 인증을 마친 뒤 배너를 내리는 데 쓴다. */
  refresh: () => Promise<void>;
  signOut: () => Promise<void>;
};

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    auth
      .me()
      .then((value) => {
        if (!cancelled) setUser(value);
      })
      .catch(() => {
        // 401이면 로그인하지 않은 것. 그 외 실패도 여기서는 같게 다룬다.
        if (!cancelled) setUser(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      setUser(await auth.me());
    } catch {
      /* 401이면 로그인하지 않은 것. 그 외 실패도 여기서는 같게 다룬다. */
      setUser(null);
    }
  }, []);

  const signOut = useCallback(async () => {
    try {
      await auth.logout();
    } finally {
      // 서버가 실패해도 클라이언트는 로그아웃 상태로 만든다.
      setUser(null);
    }
  }, []);

  return (
    <SessionContext.Provider value={{ user, loading, setUser, refresh, signOut }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error("useSession must be used inside SessionProvider");
  }
  return value;
}

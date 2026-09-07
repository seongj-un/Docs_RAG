"use client";

import { useRouter } from "next/navigation";
import { Suspense, useEffect, type ReactNode } from "react";

import { ConversationsProvider } from "@/lib/conversations";
import { DocumentsProvider } from "@/lib/documents";
import { useSession } from "@/lib/session";

/* 로그인하지 않으면 접근할 수 없는 화면들(M5 완료기준). 인증 상태는
 * HttpOnly 쿠키에만 있어서 서버 컴포넌트에서 미리 알 수 없다 — /auth/me
 * 응답을 받을 때까지는 아무것도 그리지 않는다. 반쯤 그렸다가 로그인
 * 화면으로 튕기는 것보다 잠깐 빈 화면이 낫다. */
export default function AppLayout({ children }: { children: ReactNode }) {
  const { user, loading } = useSession();
  const router = useRouter();

  useEffect(() => {
    if (!loading && user === null) {
      router.replace("/login");
    }
  }, [loading, user, router]);

  if (loading || user === null) return null;

  /* AppShell이 useSearchParams를 쓴다. Next 16은 그 위에 Suspense 경계를
   * 요구하고, 모든 화면이 셸을 거치므로 여기 한 번만 둔다. */
  return (
    <DocumentsProvider>
      <ConversationsProvider>
        <Suspense fallback={null}>{children}</Suspense>
      </ConversationsProvider>
    </DocumentsProvider>
  );
}

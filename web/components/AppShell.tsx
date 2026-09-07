"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/Button";
import { StatusMark } from "@/components/ui/StatusMark";
import { useDocuments } from "@/lib/documents";
import { useSession } from "@/lib/session";
import styles from "./AppShell.module.css";

const COLLAPSE_KEY = "sidebar-collapsed";

type AppShellProps = {
  children: ReactNode;
  /** 상단 바에 남기는 현재 위치. 화면이 스스로 제목을 갖는다. */
  crumb?: string;
  /** 사이드바 대화 이력 자리. P4에서 채워진다. */
  conversations?: ReactNode;
  /** 새 대화 버튼. 채팅 화면에서만 의미가 있다. */
  onNewConversation?: () => void;
};

export function AppShell({
  children,
  crumb,
  conversations,
  onNewConversation,
}: AppShellProps) {
  const [collapsed, setCollapsed] = useState(false);
  const { documents, loading } = useDocuments();
  const { user, signOut } = useSession();
  const pathname = usePathname();
  const router = useRouter();

  useEffect(() => {
    try {
      setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "true");
    } catch {
      /* 저장소가 막혀 있으면 펼친 상태로 시작한다. */
    }
  }, []);

  function toggleSidebar() {
    setCollapsed((current) => {
      const next = !current;
      try {
        localStorage.setItem(COLLAPSE_KEY, String(next));
      } catch {
        /* 이번 세션에만 적용된다. */
      }
      return next;
    });
  }

  async function onSignOut() {
    await signOut();
    router.replace("/login");
  }

  return (
    <div className={styles.shell}>
      <aside
        className={styles.sidebar}
        data-collapsed={collapsed}
        /* 접힌 사이드바는 화면에서 사라진 것과 같다 — 보조기술과 탭 순서에서도
         * 빠져야 한다. 그러지 않으면 안 보이는 링크로 포커스가 들어간다. */
        inert={collapsed ? true : undefined}
        aria-label="문서와 대화"
      >
        <div className={styles.sidebarInner}>
          {onNewConversation && (
            <Button onClick={onNewConversation}>새 대화</Button>
          )}

          <div className={styles.sidebarScroll}>
            {conversations}

            <section className={styles.section}>
              <div className={styles.sectionHead}>
                <span>문서</span>
                <Link className={styles.crumb} href="/documents">
                  관리
                </Link>
              </div>
              {loading && <p className={styles.empty}>불러오는 중…</p>}
              {!loading && documents.length === 0 && (
                <p className={styles.empty}>아직 올린 문서가 없습니다</p>
              )}
              {documents.map((document) => (
                <Link
                  key={document.id}
                  href={`/documents/${document.id}`}
                  className={styles.item}
                  data-active={pathname === `/documents/${document.id}`}
                >
                  <StatusMark
                    status={document.status}
                    error={document.error}
                    showLabel={false}
                  />
                  <span className={styles.itemLabel}>{document.filename}</span>
                </Link>
              ))}
            </section>
          </div>

          <div className={styles.sidebarFoot}>
            <Link
              href="/settings"
              className={styles.item}
              data-active={pathname === "/settings"}
            >
              <span className={styles.itemLabel}>설정</span>
            </Link>
            <button type="button" className={styles.item} onClick={onSignOut}>
              <span className={styles.itemLabel}>
                {user?.email ?? "로그아웃"}
              </span>
            </button>
          </div>
        </div>
      </aside>

      <div className={styles.main}>
        <div className={styles.topbar}>
          <button
            type="button"
            className={styles.toggle}
            onClick={toggleSidebar}
            aria-expanded={!collapsed}
            aria-label={collapsed ? "사이드바 펼치기" : "사이드바 접기"}
          >
            {collapsed ? "☰" : "◧"}
          </button>
          {crumb && <span className={styles.crumb}>{crumb}</span>}
        </div>
        <div className={styles.content}>{children}</div>
      </div>
    </div>
  );
}

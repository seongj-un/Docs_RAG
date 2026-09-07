"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import type { Conversation } from "@/lib/api";

import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { StatusMark } from "@/components/ui/StatusMark";
import { useConversations } from "@/lib/conversations";
import { useDocuments } from "@/lib/documents";
import { useSession } from "@/lib/session";
import styles from "./AppShell.module.css";

const COLLAPSE_KEY = "sidebar-collapsed";
/* AppShell.module.css의 중단점과 같아야 한다 — 여기서 덮개로 바뀐다. */
const NARROW = "(max-width: 720px)";

type AppShellProps = {
  children: ReactNode;
  /** 상단 바에 남기는 현재 위치. 화면이 스스로 제목을 갖는다. */
  crumb?: string;
};

/* 사이드바는 화면마다 다르지 않다 — 대화 이력도 문서 목록도 어디서나 같은
 * 자리에 있다. 그래서 목록을 화면에서 주입받지 않고 셸이 직접 그린다.
 * 대화는 /?c=<id>로 연다. 상태가 아니라 주소라서 새로고침해도 남고,
 * 설정이나 문서 화면에서 눌러도 채팅으로 옮겨간다. */
export function AppShell({ children, crumb }: AppShellProps) {
  const [collapsed, setCollapsed] = useState(false);
  const { documents, loading } = useDocuments();
  const {
    conversations,
    loading: conversationsLoading,
    remove: removeConversation,
  } = useConversations();
  const [pendingDelete, setPendingDelete] = useState<Conversation | null>(null);
  const [deleting, setDeleting] = useState(false);
  const { user, signOut } = useSession();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const router = useRouter();
  const openConversationId = searchParams.get("c");

  useEffect(() => {
    /* 좁은 화면에서 사이드바는 본문을 덮는다. 저장된 값은 넓은 화면에서의
     * 취향이므로 여기서는 따르지 않는다 — 폰에서 열자마자 대화가 가려진다. */
    if (window.matchMedia(NARROW).matches) {
      setCollapsed(true);
      return;
    }
    try {
      setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "true");
    } catch {
      /* 저장소가 막혀 있으면 펼친 상태로 시작한다. */
    }
  }, []);

  /* 덮개일 때는 항목을 고르면 닫는다. 안 그러면 고른 화면이 계속 가려진다. */
  function closeIfOverlay() {
    if (window.matchMedia(NARROW).matches) setCollapsed(true);
  }

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
      {/* 탭 순서의 맨 앞. 사이드바를 통째로 건너뛰어 대화로 간다. */}
      <a className={styles.skip} href="#main">
        본문으로 건너뛰기
      </a>

      <aside
        className={styles.sidebar}
        data-collapsed={collapsed}
        /* 접힌 사이드바는 화면에서 사라진 것과 같다 — 보조기술과 탭 순서에서도
         * 빠져야 한다. 그러지 않으면 안 보이는 링크로 포커스가 들어간다. */
        inert={collapsed ? true : undefined}
        aria-label="문서와 대화"
      >
        <div className={styles.sidebarInner}>
          <Button
            onClick={() => {
              router.push("/");
              closeIfOverlay();
            }}
          >
            새 대화
          </Button>

          <div className={styles.sidebarScroll}>
            <section className={styles.section}>
              <div className={styles.sectionHead}>
                <span>대화</span>
              </div>
              {conversationsLoading && (
                <p className={styles.empty}>불러오는 중…</p>
              )}
              {!conversationsLoading && conversations.length === 0 && (
                <p className={styles.empty}>아직 나눈 대화가 없습니다</p>
              )}
              {conversations.map((conversation) => (
                <div key={conversation.id} className={styles.itemRow}>
                  <Link
                    href={`/?c=${conversation.id}`}
                    className={styles.item}
                    data-active={conversation.id === openConversationId}
                    onClick={closeIfOverlay}
                  >
                    <span className={styles.itemLabel}>
                      {conversation.title ?? "제목 없는 대화"}
                    </span>
                  </Link>
                  <button
                    type="button"
                    className={styles.itemDelete}
                    onClick={() => setPendingDelete(conversation)}
                    aria-label={`${conversation.title ?? "제목 없는 대화"} 지우기`}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </section>

            <section className={styles.section}>
              <div className={styles.sectionHead}>
                <span>문서</span>
                <Link
                  className={styles.crumb}
                  href="/documents"
                  onClick={closeIfOverlay}
                >
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
                  onClick={closeIfOverlay}
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
              onClick={closeIfOverlay}
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

      {!collapsed && (
        <button
          type="button"
          className={styles.scrim}
          onClick={() => setCollapsed(true)}
          aria-label="사이드바 닫기"
          tabIndex={-1}
        />
      )}

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
        {/* tabIndex={-1}이라야 앵커로 건너뛴 뒤 포커스가 실제로 여기 앉는다 —
            없으면 스크롤만 되고 다음 Tab이 문서 처음으로 돌아간다. */}
        <div id="main" className={styles.content} tabIndex={-1}>
          {children}
        </div>
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title={`${pendingDelete?.title ?? "제목 없는 대화"} 지우기`}
        consequence="이 대화의 질문과 답변이 모두 사라집니다. 올린 문서는 그대로 남습니다."
        confirmLabel="지우기"
        busy={deleting}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await removeConversation(pendingDelete.id);
      /* 열려 있던 대화를 지웠으면 빈 화면에 남겨두지 않는다. */
      if (pendingDelete.id === openConversationId) router.push("/");
      setPendingDelete(null);
    } finally {
      setDeleting(false);
    }
  }
}

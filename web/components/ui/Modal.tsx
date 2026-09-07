"use client";

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import styles from "./Modal.module.css";

/* 포커스를 받을 수 있는 것들. :not([disabled])와 tabindex="-1" 제외가 없으면
 * 트랩이 비활성 버튼이나 프로그램 전용 포커스 대상에 갇힌다. */
const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

type ModalProps = {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** 헤더 오른쪽 여분 자리. 근거가 여럿일 때의 전환 컨트롤 등. */
  headerExtra?: ReactNode;
};

export function Modal({
  open,
  title,
  onClose,
  children,
  headerExtra,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  /* 닫을 때 원래 있던 자리로 포커스를 돌려놓는다. 이게 없으면 각주를 눌러
   * 연 모달을 닫았을 때 포커스가 문서 처음으로 튄다. */
  const restoreTo = useRef<HTMLElement | null>(null);
  const titleId = useId();

  const focusables = useCallback((): HTMLElement[] => {
    const panel = panelRef.current;
    if (!panel) return [];
    return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
  }, []);

  useEffect(() => {
    if (!open) return;

    restoreTo.current = document.activeElement as HTMLElement | null;

    /* 열리면 첫 포커스 대상으로 들어간다. 다만 지금 보여주는 것이 첫
     * 요소가 아닐 수 있다 — 각주 2번을 눌렀는데 포커스가 "근거 1번"에
     * 가면 보는 것과 듣는 것이 어긋난다. 그래서 내용을 아는 쪽이
     * data-autofocus로 지목할 수 있게 두고, 없으면 첫 요소로 간다.
     * 둘 다 없으면 패널 자체가 받는다(tabIndex={-1}이라 프로그램 포커스만). */
    const marked = panelRef.current?.querySelector<HTMLElement>(
      "[data-autofocus]",
    );
    const target = marked ?? focusables()[0] ?? panelRef.current;
    target?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;

      const items = focusables();
      if (items.length === 0) {
        event.preventDefault();
        return;
      }

      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;

      /* 양 끝에서 감아 돌린다. 포커스가 패널 밖으로 나가면(브라우저 UI에서
       * 돌아온 경우 등) 첫 항목으로 되돌린다. */
      if (event.shiftKey && (active === first || !panelRef.current?.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      restoreTo.current?.focus();
    };
  }, [open, onClose, focusables]);

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <div
      className={styles.backdrop}
      /* 배경을 눌러 닫되, 패널 안에서 시작한 드래그가 배경에서 끝나는 경우는
       * 닫지 않는다 — 그래서 target이 정확히 배경일 때만 본다. */
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className={styles.header}>
          <h2 id={titleId} className={styles.title}>
            {title}
          </h2>
          <div className="flex items-center gap-2">
            {headerExtra}
            <button
              type="button"
              className={styles.close}
              onClick={onClose}
              aria-label="닫기"
            >
              ✕
            </button>
          </div>
        </div>
        <div className={styles.content}>{children}</div>
      </div>
    </div>,
    document.body,
  );
}

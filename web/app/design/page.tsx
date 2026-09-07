"use client";

import { useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Footnote } from "@/components/ui/Footnote";
import { Modal } from "@/components/ui/Modal";
import { StatusMark } from "@/components/ui/StatusMark";
import { useTheme, type Theme } from "@/lib/theme";

/* 디자인 시스템 프리뷰. 화면을 짜기 전에 프리미티브를 한자리에서 보고
 * 라이트/다크가 같은 원리로 뒤집히는지 확인하는 용도다. */
const THEMES: Theme[] = ["system", "light", "dark"];
const THEME_LABEL: Record<Theme, string> = {
  system: "시스템",
  light: "라이트",
  dark: "다크",
};

export default function DesignPreview() {
  const { theme, setTheme } = useTheme();
  const [modalOpen, setModalOpen] = useState(false);

  return (
    <main
      className="mx-auto flex flex-col gap-9 px-6 py-12"
      style={{ maxWidth: "var(--width-screen)" }}
    >
      <header className="flex items-center justify-between">
        <h1>디자인 시스템</h1>
        <div className="flex gap-1.5">
          {THEMES.map((option) => (
            <Button
              key={option}
              variant={theme === option ? "primary" : "default"}
              onClick={() => setTheme(option)}
            >
              {THEME_LABEL[option]}
            </Button>
          ))}
        </div>
      </header>

      <section className="flex flex-col gap-3">
        <h3>각주</h3>
        <p>
          문서는 2024년에 도입되었습니다
          <Footnote number={1} onSelect={() => setModalOpen(true)} /> 적용 범위는
          전 부서입니다
          <Footnote number={2} onSelect={() => setModalOpen(true)} />
        </p>
      </section>

      <section className="flex flex-col gap-3">
        <h3>상태 표시</h3>
        <div className="flex gap-7">
          <StatusMark status="ready" />
          <StatusMark status="processing" />
          <StatusMark status="failed" />
        </div>
      </section>

      <section className="flex flex-col gap-3">
        <h3>버튼</h3>
        <div className="flex gap-2">
          <Button variant="primary">질문하기</Button>
          <Button>취소</Button>
          <Button variant="quiet">새 대화</Button>
          <Button disabled>사용 불가</Button>
        </div>
      </section>

      <section className="flex flex-col gap-3">
        <h3>알림 블록</h3>
        <Alert
          title="문서에서 찾을 수 없습니다"
          actions={<Button>전체 문서에서 다시 찾기</Button>}
        >
          지금 보고 있는 문서 안에는 이 질문에 답할 내용이 없습니다.
        </Alert>
      </section>

      <section className="flex flex-col gap-3">
        <h3>모달</h3>
        <div>
          <Button onClick={() => setModalOpen(true)}>근거 열어보기</Button>
        </div>
      </section>

      <Modal
        open={modalOpen}
        title="근거.pdf · 7쪽"
        onClose={() => setModalOpen(false)}
      >
        <p style={{ color: "var(--body)" }}>
          여기에 청크 원문이 들어간다. 근거 문장은 배경색 대신 1px --edge 밑줄로
          표시하고, 인용 블록은 왼쪽 세로선으로 묶는다.
        </p>
      </Modal>
    </main>
  );
}

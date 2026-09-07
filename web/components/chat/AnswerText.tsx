"use client";

import { Fragment } from "react";

import { Footnote } from "@/components/ui/Footnote";
import type { Annotated } from "@/lib/footnotes";
import styles from "./Thread.module.css";

type AnswerTextProps = {
  /* annotate()는 호출자가 한 번만 돌린다 — 모달을 열려면 각주 목록이
   * 여기 밖에서도 필요해서, 결과를 넘겨받는다. */
  annotated: Annotated;
  /** 스트리밍 중이면 커서를 붙인다. */
  streaming?: boolean;
  onSelectFootnote: (number: number) => void;
};

export function AnswerText({
  annotated,
  streaming = false,
  onSelectFootnote,
}: AnswerTextProps) {
  return (
    <>
      <div className={styles.answer}>
        {annotated.segments.map((segment, index) =>
          segment.kind === "text" ? (
            <Fragment key={index}>{segment.text}</Fragment>
          ) : (
            <Footnote
              key={index}
              number={segment.number}
              onSelect={onSelectFootnote}
            />
          ),
        )}
        {streaming && <span className={styles.caret} aria-hidden />}
      </div>

      {annotated.orphans.length > 0 && (
        <p className={styles.orphans}>
          <span>본문에 표시되지 않은 근거</span>
          {annotated.orphans.map((footnote) => (
            <Footnote
              key={footnote.number}
              number={footnote.number}
              onSelect={onSelectFootnote}
            />
          ))}
        </p>
      )}
    </>
  );
}

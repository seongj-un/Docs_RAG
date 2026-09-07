"use client";

import styles from "./Footnote.module.css";

type FootnoteProps = {
  /** 답변 안에서의 각주 번호. 1부터 센다. */
  number: number;
  onSelect: (number: number) => void;
};

/** 답변 문장 끝에 붙는 위첨자 번호. 누르면 근거 모달이 열린다. */
export function Footnote({ number, onSelect }: FootnoteProps) {
  return (
    <button
      type="button"
      className={styles.footnote}
      onClick={() => onSelect(number)}
      aria-label={`근거 ${number}번 보기`}
    >
      {number}
    </button>
  );
}

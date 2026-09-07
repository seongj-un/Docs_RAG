"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/Button";
import { useDocuments } from "@/lib/documents";
import styles from "./Composer.module.css";

/** 질의 범위. null은 올린 문서 전체를 뜻한다. */
export type Scope = string | null;

type ComposerProps = {
  scope: Scope;
  onScopeChange: (scope: Scope) => void;
  onSubmit: (question: string) => void;
  disabled?: boolean;
};

export function Composer({
  scope,
  onScopeChange,
  onSubmit,
  disabled = false,
}: ComposerProps) {
  const { documents } = useDocuments();
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  /* 다 읽지 못한 문서는 고를 수 없다 — 고르면 답이 나올 수 없는 범위가 된다. */
  const askable = documents.filter((document) => document.status === "ready");

  /* 범위로 잡아둔 문서가 지워지면 조용히 전체로 돌린다. 없는 문서를 가리킨
   * 채로 물으면 백엔드가 404를 준다. */
  useEffect(() => {
    if (scope !== null && !askable.some((item) => item.id === scope)) {
      onScopeChange(null);
    }
  }, [scope, askable, onScopeChange]);

  function send() {
    const question = value.trim();
    if (!question || disabled) return;
    onSubmit(question);
    setValue("");
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    /* 줄바꿈은 Shift+Enter. 조합 중인 한글이 Enter로 끊기지 않도록
     * isComposing을 본다 — 없으면 "안녕하" 상태에서 전송된다. */
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  }

  function resize() {
    const node = inputRef.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${node.scrollHeight}px`;
  }

  return (
    <div className={styles.composer}>
      <div className={styles.inner}>
        <div className={styles.box}>
          <textarea
            ref={inputRef}
            className={styles.input}
            value={value}
            rows={1}
            placeholder={
              askable.length === 0
                ? "먼저 문서를 올려 주세요"
                : "문서에 대해 물어보세요"
            }
            disabled={disabled || askable.length === 0}
            onChange={(event) => {
              setValue(event.target.value);
              resize();
            }}
            onKeyDown={onKeyDown}
            aria-label="질문"
          />
          <Button
            variant="primary"
            onClick={send}
            disabled={disabled || value.trim() === ""}
          >
            묻기
          </Button>
        </div>

        <div className={styles.controls}>
          <label htmlFor="scope">찾을 범위</label>
          <select
            id="scope"
            className={styles.scope}
            value={scope ?? ""}
            onChange={(event) => onScopeChange(event.target.value || null)}
          >
            <option value="">올린 문서 전체</option>
            {askable.map((document) => (
              <option key={document.id} value={document.id}>
                {document.filename}
              </option>
            ))}
          </select>
          <span className={styles.hint}>Enter로 묻기 · Shift+Enter 줄바꿈</span>
        </div>
      </div>
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Modal } from "@/components/ui/Modal";
import { chunks as chunkApi, describeError, type ErrorCopy } from "@/lib/api";
import { useDocuments } from "@/lib/documents";
import { pageLabel, type Footnote } from "@/lib/footnotes";
import styles from "./CitationModal.module.css";

type CitationModalProps = {
  /** 이 답변의 각주 전부. 모달 안에서 서로 전환한다. */
  footnotes: Footnote[];
  /** 열려 있는 각주 번호. null이면 닫힘. */
  activeNumber: number | null;
  onSelect: (number: number) => void;
  onClose: () => void;
};

export function CitationModal({
  footnotes,
  activeNumber,
  onSelect,
  onClose,
}: CitationModalProps) {
  const { byId } = useDocuments();
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<ErrorCopy | null>(null);

  const active = footnotes.find((item) => item.number === activeNumber);
  const chunkId = active?.citation.chunk_id ?? null;

  /* 청크 원문을 그때그때 불러온다. Citation.snippet은 240자에서 잘려 있어서
   * "근거를 정확히 보여준다"고 할 수 없다. */
  useEffect(() => {
    if (chunkId === null) return;

    let cancelled = false;
    setContent(null);
    setError(null);

    chunkApi
      .getChunk(chunkId)
      .then((chunk) => {
        if (!cancelled) setContent(chunk.content);
      })
      .catch((cause) => {
        if (!cancelled) setError(describeError(cause));
      });

    return () => {
      cancelled = true;
    };
  }, [chunkId]);

  if (!active) return null;

  const document = byId(active.citation.document_id);
  const name = document?.filename ?? "지워진 문서";

  return (
    <Modal
      open
      title="근거"
      onClose={onClose}
      headerExtra={
        footnotes.length > 1 ? (
          <div className={styles.switcher}>
            {footnotes.map((footnote) => (
              <button
                key={footnote.number}
                type="button"
                className={styles.tab}
                data-active={footnote.number === active.number}
                onClick={() => onSelect(footnote.number)}
                aria-label={`근거 ${footnote.number}번`}
              >
                {footnote.number}
              </button>
            ))}
          </div>
        ) : null
      }
    >
      <p className={styles.source}>
        <span className={styles.sourceName}>{name}</span>
        {" · "}
        {pageLabel(active.citation)}
      </p>

      {error && <Alert title={error.title}>{error.hint}</Alert>}

      {!error && content === null && (
        <p className={styles.loading}>원문을 불러오는 중…</p>
      )}

      {!error && content !== null && (
        <blockquote className={styles.quote}>{content}</blockquote>
      )}
    </Modal>
  );
}

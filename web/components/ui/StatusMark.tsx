import styles from "./StatusMark.module.css";

/** 백엔드의 documents.status. pending은 사용자 눈에 processing과 같다. */
export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

/* 카피 규칙: 시스템 용어를 쓰지 않는다. ready/processing/failed 대신
 * 사용자가 무엇을 할 수 있는지로 말한다. */
const LABEL: Record<DocumentStatus, string> = {
  pending: "읽는 중",
  processing: "읽는 중",
  ready: "질문 가능",
  failed: "글자 없음",
};

const SHAPE: Record<DocumentStatus, string> = {
  pending: styles.processing,
  processing: styles.processing,
  ready: styles.ready,
  failed: styles.failed,
};

type StatusMarkProps = {
  status: DocumentStatus;
  /** 목록처럼 좁은 자리에서는 형태만 남기고 글자를 뺀다. */
  showLabel?: boolean;
};

export function StatusMark({ status, showLabel = true }: StatusMarkProps) {
  const label = LABEL[status];
  return (
    <span className={styles.row}>
      <span
        className={`${styles.mark} ${SHAPE[status]}`}
        role="img"
        aria-label={showLabel ? undefined : label}
        aria-hidden={showLabel}
      />
      {showLabel && <span className={styles.label}>{label}</span>}
    </span>
  );
}

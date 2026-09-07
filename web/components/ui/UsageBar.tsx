import styles from "./UsageBar.module.css";

/** 한도의 이만큼을 쓰면 임박으로 본다. */
const NEAR = 0.8;

type UsageBarProps = {
  label: string;
  used: number;
  limit: number;
  /** "82쪽 남았습니다"의 단위. */
  unit: string;
  /** 언제 다시 채워지는지. 남은 양만 말하고 끝내지 않는다. */
  refill: string;
};

export function UsageBar({ label, used, limit, unit, refill }: UsageBarProps) {
  const capped = Math.min(used, limit);
  const ratio = limit > 0 ? capped / limit : 0;
  const near = ratio >= NEAR;
  const remaining = Math.max(limit - used, 0);

  return (
    <div className={styles.usage}>
      <div className={styles.head}>
        <span className={styles.label}>{label}</span>
        <span className={styles.count}>
          {used.toLocaleString()} / {limit.toLocaleString()}
          {unit}
        </span>
      </div>

      <div
        className={styles.track}
        role="progressbar"
        aria-label={label}
        aria-valuenow={capped}
        aria-valuemin={0}
        aria-valuemax={limit}
      >
        <div
          className={styles.fill}
          data-near={near}
          style={{ width: `${Math.round(ratio * 100)}%` }}
        />
      </div>

      <p className={styles.note} data-near={near}>
        {remaining === 0
          ? `다 썼습니다. ${refill}`
          : `${remaining.toLocaleString()}${unit} 남았습니다. ${refill}`}
      </p>
    </div>
  );
}

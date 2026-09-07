import styles from "./Steps.module.css";

export type StepState = "done" | "active" | "upcoming" | "failed";

export type Step = {
  label: string;
  state: StepState;
  note?: string;
};

const MARK: Record<StepState, string> = {
  done: styles.done,
  active: styles.active,
  upcoming: styles.upcoming,
  failed: styles.failed,
};

export function Steps({ steps }: { steps: Step[] }) {
  return (
    <ol className={styles.list}>
      {steps.map((step, index) => (
        <li key={step.label} className={styles.step}>
          <div className={styles.rail}>
            <span className={`${styles.mark} ${MARK[step.state]}`} aria-hidden>
              {step.state === "done" ? "✓" : ""}
            </span>
            {index < steps.length - 1 && <span className={styles.line} />}
          </div>
          <div className={styles.body}>
            <div
              className={styles.label}
              data-dim={step.state === "upcoming"}
            >
              {step.label}
            </div>
            {step.note && <div className={styles.note}>{step.note}</div>}
          </div>
        </li>
      ))}
    </ol>
  );
}

import type { ReactNode } from "react";

import styles from "./Alert.module.css";

type AlertProps = {
  title: string;
  children?: ReactNode;
  /** 다음 행동. 실패·거부는 원인만 말하고 끝내지 않는다. */
  actions?: ReactNode;
  /** 스크린리더에 즉시 읽힐 내용이면 assertive. */
  urgent?: boolean;
};

export function Alert({ title, children, actions, urgent }: AlertProps) {
  return (
    <div
      className={styles.alert}
      role={urgent ? "alert" : "status"}
      aria-live={urgent ? "assertive" : "polite"}
    >
      <p className={styles.title}>{title}</p>
      {children && <div className={styles.body}>{children}</div>}
      {actions && <div className={styles.actions}>{actions}</div>}
    </div>
  );
}

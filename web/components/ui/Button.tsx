import type { ButtonHTMLAttributes } from "react";

import styles from "./Button.module.css";

/* 위험 동작(계정 지우기 등)에 별도 변종이 없는 것은 의도다. 색을 쓰지 않는
 * 디자인에서 '위험한 버튼'은 형태로 표현되지 않는다 — 확인 모달 한 단계와
 * 문구가 그 자리를 대신한다. ConfirmDialog 참조. */
type Variant = "default" | "primary" | "quiet";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
};

export function Button({
  variant = "default",
  className = "",
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`${styles.button} ${styles[variant]} ${className}`}
      {...rest}
    />
  );
}

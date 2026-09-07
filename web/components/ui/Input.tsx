import type { InputHTMLAttributes } from "react";
import { useId } from "react";

import styles from "./Input.module.css";

type InputProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
};

export function Input({ label, id, className = "", ...rest }: InputProps) {
  const generated = useId();
  const inputId = id ?? generated;

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={inputId}>
        {label}
      </label>
      <input id={inputId} className={`${styles.input} ${className}`} {...rest} />
    </div>
  );
}

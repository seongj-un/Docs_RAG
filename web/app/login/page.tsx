"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { auth, describeError, type ErrorCopy } from "@/lib/api";
import { useSession } from "@/lib/session";
import styles from "./login.module.css";

type Mode = "login" | "signup";

const COPY: Record<Mode, { title: string; subtitle: string; submit: string }> = {
  login: {
    title: "다시 오셨네요",
    subtitle: "올려둔 문서에 이어서 질문할 수 있습니다.",
    submit: "로그인",
  },
  signup: {
    title: "문서 Q&A 시작하기",
    subtitle: "문서를 올리면 그 안에서 근거를 찾아 답합니다.",
    submit: "가입하고 시작하기",
  },
};

export default function LoginPage() {
  const router = useRouter();
  const { setUser } = useSession();

  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<ErrorCopy | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const copy = COPY[mode];

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const user =
        mode === "login"
          ? await auth.login(email, password)
          : await auth.signup(email, password);
      setUser(user);
      router.replace("/");
    } catch (cause) {
      setError(describeError(cause));
      setSubmitting(false);
    }
  }

  function switchMode() {
    setMode(mode === "login" ? "signup" : "login");
    setError(null);
  }

  return (
    <main className={styles.screen}>
      <div className={styles.card}>
        <div className={styles.heading}>
          <h1>{copy.title}</h1>
          <p className={styles.subtitle}>{copy.subtitle}</p>
        </div>

        {error && (
          <Alert title={error.title} urgent>
            {error.hint}
          </Alert>
        )}

        <form className={styles.form} onSubmit={onSubmit}>
          <Input
            label="이메일"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            autoComplete="email"
            required
            disabled={submitting}
          />
          <Input
            label="비밀번호"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete={
              mode === "login" ? "current-password" : "new-password"
            }
            /* 백엔드가 8자 이상을 요구한다. 폼에서 먼저 막아 왕복을 아낀다. */
            minLength={mode === "signup" ? 8 : undefined}
            required
            disabled={submitting}
          />
          <Button type="submit" variant="primary" disabled={submitting}>
            {submitting ? "잠시만요…" : copy.submit}
          </Button>
        </form>

        <p className={styles.footer}>
          {mode === "login" ? "처음이신가요?" : "이미 계정이 있나요?"}
          <button type="button" className={styles.switch} onClick={switchMode}>
            {mode === "login" ? "가입하기" : "로그인"}
          </button>
        </p>
      </div>
    </main>
  );
}

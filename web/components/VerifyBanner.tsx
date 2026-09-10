"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { auth, describeError } from "@/lib/api";
import { useSession } from "@/lib/session";
import styles from "./VerifyBanner.module.css";

/* 발송은 응답 밖(BackgroundTasks)에서 일어나서 실패해도 사용자에게 도달할
 * 길이 없다. 그래서 이 배너의 "다시 보내기"가 유일한 복구 경로다 —
 * 미인증인 동안에는 어느 화면에서든 닿을 수 있어야 한다. */
export function VerifyBanner() {
  const { user } = useSession();
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [error, setError] = useState<string | null>(null);

  if (!user || user.email_verified) return null;

  async function onResend() {
    setState("sending");
    setError(null);
    try {
      await auth.resendVerification();
      setState("sent");
    } catch (cause) {
      const copy = describeError(cause);
      setError(`${copy.title} ${copy.hint}`);
      setState("idle");
    }
  }

  return (
    <div className={styles.banner} role="status">
      <span className={styles.message}>
        {state === "sent"
          ? `${user.email} 로 링크를 다시 보냈습니다. 메일함을 확인해 주세요.`
          : "이메일을 확인해 주세요. 확인 전에는 질문과 업로드가 몇 번으로 제한됩니다."}
      </span>
      {state !== "sent" && (
        <Button onClick={() => void onResend()} disabled={state === "sending"}>
          {state === "sending" ? "보내는 중…" : "다시 보내기"}
        </Button>
      )}
      {error !== null && <span className={styles.error}>{error}</span>}
    </div>
  );
}

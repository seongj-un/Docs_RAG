"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { auth, describeError, type ErrorCopy } from "@/lib/api";
import { useSession } from "@/lib/session";
import styles from "./verify.module.css";

/* (app) 그룹 밖에 있다. 메일 링크는 로그인하지 않은 다른 브라우저에서
 * 열리는 것이 정상 경로인데, 그룹 안이면 로그인 리다이렉트에 걸려 주소의
 * 토큰이 유실된다. */

type State =
  | { kind: "checking" }
  | { kind: "done" }
  | { kind: "failed"; copy: ErrorCopy };

/* 토큰 없음은 렌더링 시점에 이미 알 수 있는 값이라 이펙트를 거칠 필요가
 * 없다 — 이펙트 본문에서 동기적으로 setState하면 react-hooks/set-state-in-effect가
 * 잡아내는 불필요한 연쇄 렌더가 생긴다. */
function initialState(token: string | null): State {
  if (token === null || token === "") {
    return {
      kind: "failed",
      copy: {
        title: "링크가 올바르지 않습니다",
        hint: "메일의 링크를 그대로 열어 주세요. 주소가 잘렸을 수 있습니다.",
      },
    };
  }
  return { kind: "checking" };
}

function VerifyInner() {
  const token = useSearchParams().get("token");
  const { refresh } = useSession();
  const [state, setState] = useState<State>(() => initialState(token));
  /* React 18의 개발용 이중 실행에서 토큰을 두 번 쓰면, 두 번째가
   * "이미 인증됨"으로 실패해 성공 화면이 실패 화면으로 뒤집힌다. */
  const started = useRef(false);

  useEffect(() => {
    if (started.current || token === null || token === "") return;
    started.current = true;

    auth
      .verify(token)
      .then(async () => {
        await refresh();
        setState({ kind: "done" });
      })
      .catch((cause) => {
        setState({ kind: "failed", copy: describeError(cause) });
      });
  }, [token, refresh]);

  if (state.kind === "checking") {
    return <p className={styles.subtitle}>확인하고 있습니다…</p>;
  }

  if (state.kind === "done") {
    return (
      <div className={styles.heading}>
        <h1>이메일 확인이 끝났습니다</h1>
        <p className={styles.subtitle}>
          이제 한도 없이 질문하고 문서를 올릴 수 있습니다.
        </p>
        <Link className={styles.link} href="/">
          돌아가기
        </Link>
      </div>
    );
  }

  return (
    <div className={styles.heading}>
      <h1>{state.copy.title}</h1>
      <p className={styles.subtitle}>{state.copy.hint}</p>
      <ResendOrSignIn />
    </div>
  );
}

/** 재발송은 세션이 있어야 한다. 없으면 로그인부터 안내한다. */
function ResendOrSignIn() {
  const { user, loading } = useSession();
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (loading) return null;
  if (user === null) {
    return (
      <Link className={styles.link} href="/login">
        로그인하고 다시 받기
      </Link>
    );
  }
  if (user.email_verified) {
    return (
      <Link className={styles.link} href="/">
        돌아가기
      </Link>
    );
  }
  if (sent) {
    return <p className={styles.subtitle}>링크를 다시 보냈습니다. 메일함을 확인해 주세요.</p>;
  }

  async function onResend() {
    setError(null);
    try {
      await auth.resendVerification();
      setSent(true);
    } catch (cause) {
      const copy = describeError(cause);
      setError(`${copy.title} ${copy.hint}`);
    }
  }

  return (
    <div className={styles.actions}>
      <Button onClick={() => void onResend()}>새 링크 받기</Button>
      {error !== null && <p className={styles.error}>{error}</p>}
    </div>
  );
}

export default function VerifyPage() {
  /* useSearchParams 는 Suspense 경계를 요구한다. */
  return (
    <main className={styles.screen}>
      <div className={styles.card}>
        <Suspense fallback={<p className={styles.subtitle}>확인하고 있습니다…</p>}>
          <VerifyInner />
        </Suspense>
      </div>
    </main>
  );
}

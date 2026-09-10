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
          {/* 인증해도 무제한이 되지 않는다 — 맛보기 쿼터(lib/api/types.ts)가
           * 평소 한도로 바뀔 뿐이다. settings 페이지의 사용량 바를 하드코딩
           * 없이 고친 것과 같은 이유로, 여기서도 숫자 없이 사실만 말한다. */}
          이제 맛보기 쿼터가 풀려 평소 한도로 질문하고 문서를 올릴 수 있습니다.
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
      {/* VerifyBanner.tsx와 같은 이유의 role="status" — 실패 사유가 이펙트
       * 완료 후에 나타나는 비동기 갱신이라, 이 표시가 없으면 스크린 리더
       * 사용자는 "확인하고 있습니다…"에서 뭐가 바뀌었는지 알 길이 없다. */}
      <p className={styles.subtitle} role="status">
        {state.copy.hint}
      </p>
      <ResendOrSignIn />
    </div>
  );
}

/** 재발송은 세션이 있어야 한다. 없으면 로그인부터 안내한다. */
function ResendOrSignIn() {
  const { user, loading } = useSession();
  /* VerifyBanner.tsx와 같은 3상태 머신. sending 동안 버튼을 잠그지 않으면
   * 더블클릭이나 Enter 연타로 재발송이 두 번 나간다 — 서버가 분당 1회로
   * 막아주니 결과는 크지 않지만(두 번째 호출이 에러로 튈 뿐), 애초에 막을
   * 수 있는 걸 화면에 흘릴 이유가 없다. */
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
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
  if (state === "sent") {
    return (
      <div className={styles.actions}>
        <p className={styles.subtitle}>
          링크를 다시 보냈습니다. 메일함을 확인해 주세요.
        </p>
        {/* 성공/미로그인/이미인증과 같은 다른 종료 분기들처럼, 여기도
         * 막다른 화면으로 남기지 않는다. */}
        <Link className={styles.link} href="/">
          돌아가기
        </Link>
      </div>
    );
  }

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
    <div className={styles.actions}>
      <Button onClick={() => void onResend()} disabled={state === "sending"}>
        {state === "sending" ? "보내는 중…" : "새 링크 받기"}
      </Button>
      {/* VerifyBanner.tsx의 error span과 같은 이유 — role="status"가 없으면
       * 재발송 실패를 스크린 리더 사용자에게 알릴 길이 없다. */}
      {error !== null && (
        <p className={styles.error} role="status">
          {error}
        </p>
      )}
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

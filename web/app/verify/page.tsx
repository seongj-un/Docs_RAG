"use client";

import Link from "next/link";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { Button } from "@/components/ui/Button";
import { auth, describeError, type ErrorCopy } from "@/lib/api";
import { useSession } from "@/lib/session";
import {
  readVerifyToken,
  scrubQueryToken,
  tokenState,
  type VerifyToken,
} from "@/lib/verifyLink";
import styles from "./verify.module.css";

/* (app) 그룹 밖에 있다. 메일 링크는 로그인하지 않은 다른 브라우저에서
 * 열리는 것이 정상 경로인데, 그룹 안이면 로그인 리다이렉트에 걸려 주소의
 * 토큰이 유실된다. */

type State =
  | { kind: "checking" }
  | { kind: "done" }
  | { kind: "failed"; copy: ErrorCopy };

/* 주소에 토큰이 아예 없을 때. describeError 가 만드는 것과 같은 모양이라
 * 실패 화면을 하나로 유지한다.
 *
 * 토큰이 프래그먼트로 옮겨간 뒤에도 이 문구가 맞다 — 오히려 더 맞다.
 * 토큰이 사라지는 실제 경로가 "주소가 잘렸다"이기 때문이다: 사용자가
 * 주소의 앞부분만 복사했거나, 메일 게이트웨이가 링크를 재작성하면서
 * `#...` 를 떨어뜨렸거나. 원인만 말하고 끝내지 않는다는 카피 규칙은 아래
 * ResendOrSignIn 이 채운다. */
const MISSING_TOKEN: ErrorCopy = {
  title: "링크가 올바르지 않습니다",
  hint: "메일의 링크를 그대로 열어 주세요. 주소가 잘렸을 수 있습니다.",
};

/* 프래그먼트는 서버에 존재하지 않는다 — 브라우저가 `#` 뒤를 보내지 않는
 * 것이 이 형식을 고른 이유 그 자체다(app/services/verification.py 의
 * build_link). 그래서 토큰은 하이드레이션이 끝난 뒤에야 읽힌다.
 *
 * 그 "렌더 중에는 모르고 마운트 뒤에 안다"를 useEffect + setState 로 쓰면
 * 두 가지가 걸린다: react-hooks/set-state-in-effect 가 잡는 불필요한 연쇄
 * 렌더(이전 구현의 주석이 지적하던 바로 그것)와, 첫 렌더와 하이드레이션이
 * 어긋나는 문제다. useSyncExternalStore 는 둘 다 없다 — 하이드레이션에는
 * getServerSnapshot(=undefined, "아직 모른다")을 쓰고 그 직후 한 번
 * getSnapshot 으로 넘어간다. 토큰 없음 판정을 마운트 이후로 미루면서도
 * setState 는 한 번도 하지 않는다. */
function subscribeToAddress(onChange: () => void): () => void {
  window.addEventListener("hashchange", onChange);
  return () => window.removeEventListener("hashchange", onChange);
}

function tokenInAddress(): string | null {
  return readVerifyToken(window.location);
}

function noTokenYet(): undefined {
  return undefined;
}

function useVerifyToken(): VerifyToken {
  return useSyncExternalStore<VerifyToken>(
    subscribeToAddress,
    tokenInAddress,
    noTokenYet,
  );
}

function Checking() {
  return <p className={styles.subtitle}>확인하고 있습니다…</p>;
}

function Failed({ copy }: { copy: ErrorCopy }) {
  return (
    <div className={styles.heading}>
      <h1>{copy.title}</h1>
      {/* VerifyBanner.tsx와 같은 이유의 role="status" — 실패 사유가 첫
       * 렌더가 아니라 그 뒤에 나타나는 비동기 갱신이라, 이 표시가 없으면
       * 스크린 리더 사용자는 "확인하고 있습니다…"에서 뭐가 바뀌었는지 알
       * 길이 없다. 토큰 없음까지 마운트 이후 판정으로 바뀐 지금은 이 화면의
       * 모든 경로가 그렇다. */}
      <p className={styles.subtitle} role="status">
        {copy.hint}
      </p>
      <ResendOrSignIn />
    </div>
  );
}

function VerifyInner() {
  const token = useVerifyToken();
  const { refresh } = useSession();
  const [state, setState] = useState<State>({ kind: "checking" });
  /* React 18의 개발용 이중 실행에서 토큰을 두 번 쓰면, 두 번째가
   * "이미 인증됨"으로 실패해 성공 화면이 실패 화면으로 뒤집힌다. */
  const started = useRef(false);

  /* 레거시 `?token=` 으로 들어왔다면 주소를 프래그먼트 형태로 바꿔 히스토리와
   * 주소창에서 토큰을 걷어낸다. 서버에는 이미 갔으니 그쪽은 되돌릴 수 없지만,
   * 브라우저에 영구히 남는 것과 사용자가 주소창을 그대로 복사해 붙여넣는
   * 것은 여기서 끝난다. 지우지 않고 옮기는 이유는 scrubQueryToken 참고. */
  useEffect(() => {
    const scrubbed = scrubQueryToken(window.location.href);
    if (scrubbed !== null) window.history.replaceState(null, "", scrubbed);
  }, []);

  useEffect(() => {
    if (started.current || typeof token !== "string") return;
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

  /* 주소를 아직 못 읽은 동안은 중립 화면을 그린다. 여기서 MISSING_TOKEN 을
   * 그리면 정상 링크에서도 "링크가 올바르지 않습니다"가 한 프레임 번쩍인다. */
  const address = tokenState(token);
  if (address === "unknown") return <Checking />;
  if (address === "missing") return <Failed copy={MISSING_TOKEN} />;

  if (state.kind === "checking") return <Checking />;

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

  return <Failed copy={state.copy} />;
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
  /* Suspense 경계가 없다. 있던 이유는 useSearchParams 가 그걸 요구해서였는데,
   * 토큰이 프래그먼트로 옮겨가면서 이 페이지는 쿼리스트링을 읽지 않는다 —
   * 주소는 useSyncExternalStore 로 클라이언트에서만 읽고, 그건 서스펜드하지
   * 않는다. */
  return (
    <main className={styles.screen}>
      <div className={styles.card}>
        <VerifyInner />
      </div>
    </main>
  );
}

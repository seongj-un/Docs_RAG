/* 이메일 인증 링크의 토큰을 주소에서 읽어내는 자리.
 *
 * 토큰은 쿼리스트링이 아니라 URL 프래그먼트(`/verify#token=…`)로 온다.
 * 프래그먼트는 브라우저가 서버로 보내지 않아서 프록시 액세스 로그에도,
 * Referer 에도, CDN 로그에도 애초에 도달하지 않는다. 이유의 원본은
 * app/services/verification.py 의 build_link docstring 이다.
 *
 * 이 판단들을 컴포넌트가 아니라 순수 함수로 빼둔 이유는 footnotes.ts 와
 * 같다 — 이 저장소에는 컴포넌트를 렌더해서 검사할 도구(jsdom,
 * testing-library)가 없다. 분기를 전부 여기 모아두면 vitest 로 고정할 수
 * 있고, page.tsx 에는 고정되지 않은 분기가 남지 않는다.
 */

/** 주소에서 읽은 토큰. **세 값을 구분하는 것이 이 모듈의 존재 이유다.**
 *
 * - `undefined` — 아직 모른다. 서버 렌더와 하이드레이션에는 프래그먼트가
 *   존재하지 않는다(서버는 `#` 뒤를 받지조차 못한다).
 * - `null` — 주소를 읽었고, 토큰이 없다.
 * - 문자열 — 토큰이 있다.
 */
export type VerifyToken = string | null | undefined;

/** 주소를 읽기 전인지 · 없는지 · 있는지. `tokenState` 의 결과다. */
export type TokenState = "unknown" | "missing" | "present";

/** 주소에서 토큰을 꺼낸다. 없으면 `null`.
 *
 * 프래그먼트를 먼저 보고, 없을 때만 쿼리스트링을 본다. 쿼리를 계속 받는
 * 것은 하위 호환 때문이다 — 형식을 바꾸기 전에 발송된 메일의 `?token=`
 * 링크가 TTL(VERIFY_TOKEN_TTL_HOURS) 동안 살아 있다. 여기서 안 받으면 그
 * 링크들은 전부 "링크가 올바르지 않습니다"로 끝난다. 반대로 받아주는 쪽의
 * 비용은 없다: 유출은 **우리가 무엇을 보내느냐**(build_link)에서 생기고,
 * 이 함수가 쿼리 토큰을 볼 때쯤이면 그 요청은 이미 지나간 뒤다. 그래도
 * 히스토리에 남는 것은 scrubQueryToken 이 지운다.
 *
 * 프래그먼트가 쿼리를 이기는 순서인 이유는 scrubQueryToken 때문이다 —
 * 레거시 링크를 프래그먼트 형태로 바꿔치기한 직후에도 읽히는 토큰이 같아야
 * 화면이 뒤집히지 않는다.
 *
 * URLSearchParams 는 `+` 를 공백으로 읽는다. 백엔드의 토큰은
 * `secrets.token_urlsafe`(=`[A-Za-z0-9_-]`)라 `+` 가 나오지 않고, 그래도
 * 섞이면 build_link 의 `quote()` 가 퍼센트 인코딩해 보내므로 안전하다.
 */
export function readVerifyToken(url: { hash: string; search: string }): string | null {
  // URLSearchParams 는 앞의 "?" 는 떼주지만 "#" 은 떼주지 않는다.
  const fromFragment = new URLSearchParams(url.hash.replace(/^#/, "")).get("token");
  if (fromFragment !== null && fromFragment !== "") return fromFragment;

  const fromQuery = new URLSearchParams(url.search).get("token");
  return fromQuery === null || fromQuery === "" ? null : fromQuery;
}

/** 화면을 고르는 유일한 분기.
 *
 * **`"unknown"` 을 `"missing"` 으로 접으면 안 된다.** 접는 순간 정상 링크로
 * 들어온 사용자도 첫 프레임에 "링크가 올바르지 않습니다"를 보게 된다 —
 * 프래그먼트는 하이드레이션이 끝난 뒤에야 읽히기 때문이다. 판정을 렌더
 * 시점에 동기적으로 하던 이전 구현(쿼리스트링은 서버도 볼 수 있었다)이 더
 * 이상 성립하지 않는 지점이 정확히 여기다.
 */
export function tokenState(token: VerifyToken): TokenState {
  if (token === undefined) return "unknown";
  if (token === null || token === "") return "missing";
  return "present";
}

/** 레거시 `?token=` 으로 들어온 주소를 프래그먼트 형태로 바꾼 주소.
 *  바꿀 것이 없으면 `null`.
 *
 * history.replaceState 에 물려 브라우저 히스토리와 주소창에서 토큰을
 * 걷어내는 용도다. 서버 쪽 유출은 이미 일어난 뒤라 되돌릴 수 없지만,
 * 히스토리에 영구히 남는 것과 사용자가 주소창을 복사해 어딘가 붙여넣는
 * 것은 여기서 막을 수 있다.
 *
 * 쿼리에서 **지우기만** 하지 않고 프래그먼트로 **옮기는** 것이 요점이다.
 * 지우기만 하면 바꾼 직후 읽히는 토큰이 `null` 이 되어 멀쩡히 검증 중인
 * 화면이 "링크가 올바르지 않습니다"로 뒤집힌다. 옮기면 readVerifyToken 이
 * 돌려주는 값이 그대로라 아무것도 흔들리지 않는다.
 */
export function scrubQueryToken(href: string): string | null {
  const url = new URL(href);
  if (url.searchParams.get("token") === null) return null;

  // 둘 다 있으면 읽을 때와 같은 우선순위(프래그먼트 우선)를 지킨다.
  const token = readVerifyToken(url);
  url.searchParams.delete("token");
  url.hash = token === null ? "" : `token=${encodeURIComponent(token)}`;
  return url.toString();
}

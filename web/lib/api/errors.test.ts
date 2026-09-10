import { describe, expect, it } from "vitest";

import { ApiError, BY_DETAIL, BY_STATUS, describeError } from "./errors";

/* 목록을 베껴두지 않고 실제 표를 순회한다. 사본은 조용히 좁아지고 검사는
 * 줄어든 채로 통과한다 — 실제로 두 건이 빠져 있었다.
 *
 * "이 표가 백엔드와 일치하는가"는 여기서 알 수 없다(백엔드 소스를 읽을 수
 * 없으므로). 그건 tests/test_error_details.py 가 양방향으로 대조한다.
 * 이 파일이 보는 것은 describeError 의 동작과 문구의 품질이다. */
const BACKEND_DETAILS = Object.keys(BY_DETAIL);

describe("describeError", () => {
  it("429 하나를 detail로 갈라 서로 다른 행동을 안내한다", () => {
    /* 이 갈래가 이 모듈이 존재하는 이유다. 상태 코드만 보면 둘 다 429라
     * "잠시 뒤에 다시" 하나로 뭉개지는데, 쿼터는 기다린다고 풀리지 않는다. */
    const rate = describeError(
      new ApiError(429, "query rate limit exceeded"),
    );
    const quota = describeError(
      new ApiError(429, "daily query quota exceeded"),
    );

    expect(rate.title).toBe("질문이 너무 빠릅니다");
    expect(quota.title).toBe("오늘 쓸 수 있는 질문을 다 썼습니다");
    expect(rate).not.toEqual(quota);
  });

  it("업로드 쪽 429도 두 갈래를 구분한다", () => {
    const rate = describeError(new ApiError(429, "upload rate limit exceeded"));
    const quota = describeError(
      new ApiError(429, "monthly upload page quota exceeded"),
    );

    expect(rate).not.toEqual(quota);
    expect(quota.hint).toContain("다음 달");
  });

  it("detail이 상태 코드보다 우선한다", () => {
    /* 같은 detail을 엉뚱한 상태 코드에 실어 보내도 문구는 detail을 따른다 —
     * 상태 코드는 거칠고 detail이 구체적이기 때문. */
    const odd = describeError(new ApiError(500, "daily query quota exceeded"));

    expect(odd.title).toBe("오늘 쓸 수 있는 질문을 다 썼습니다");
  });

  it("모르는 detail이면 상태 코드로 떨어진다", () => {
    const copy = describeError(new ApiError(404, "something new"));

    expect(copy.title).toBe("찾을 수 없습니다");
  });

  it("상태 0은 응답 자체가 없었다는 뜻이다", () => {
    /* 서버가 안 떴거나 CORS에 막힌 경우. "잠시 뒤에 다시"가 아니라
     * 백엔드를 확인하라고 말해야 한다. */
    const copy = describeError(new ApiError(0, "서버에 연결하지 못했습니다"));

    expect(copy.title).toBe("서버에 연결하지 못했습니다");
    expect(copy.hint).toContain("백엔드");
  });

  it("모르는 상태 코드는 일반 문구로 떨어진다", () => {
    const copy = describeError(new ApiError(418, "i am a teapot"));

    expect(copy.title).toBe("문제가 생겼습니다");
  });

  it("ApiError가 아닌 것도 안전하게 다룬다", () => {
    /* 코드 버그로 TypeError가 올라와도 화면이 죽으면 안 된다. */
    expect(describeError(new TypeError("boom")).title).toBe("문제가 생겼습니다");
    expect(describeError("문자열").title).toBe("문제가 생겼습니다");
    expect(describeError(null).title).toBe("문제가 생겼습니다");
    expect(describeError(undefined).title).toBe("문제가 생겼습니다");
  });

  it("표에 있는 detail은 하나도 폴백으로 떨어지지 않는다", () => {
    /* 표에 적어두고도 조회에서 놓치는 일이 없게 한다. 하나라도 폴백으로
     * 떨어지면 카피 규칙(원인 + 해결 방법)이 깨지고, 사용자는 무엇을 해야
     * 할지 모르게 된다. */
    const fallback = describeError(new ApiError(999, "unmapped"));

    expect(BACKEND_DETAILS.length).toBeGreaterThan(0);
    for (const detail of BACKEND_DETAILS) {
      const copy = describeError(new ApiError(400, detail));
      expect(copy, detail).not.toEqual(fallback);
    }
  });

  it("모든 문구가 원인과 해결 방법을 함께 준다", () => {
    /* 카피 규칙: "에러 발생" 대신 (원인) + (해결 방법). */
    const cases = [
      ...BACKEND_DETAILS.map((d) => new ApiError(400, d)),
      ...Object.keys(BY_STATUS).map((s) => new ApiError(Number(s), "x")),
      new ApiError(999, "x"), // 어디에도 없는 것 = 폴백
    ];

    for (const error of cases) {
      const copy = describeError(error);
      expect(copy.title.length, String(error.status)).toBeGreaterThan(0);
      expect(copy.hint.length, String(error.status)).toBeGreaterThan(0);
      // 시스템 용어를 그대로 노출하지 않는다.
      expect(copy.title).not.toMatch(/[a-z]{4,}/);
    }
  });

  it("인증이 필요한 403은 기다리라고 하지 않는다", () => {
    /* 쿼터와 달리 시간이 지나도 풀리지 않는다. 사용자가 메일의 링크를
     * 눌러야 한다 — "잠시 뒤에 다시"는 틀린 조언이다. */
    const copy = describeError(
      new ApiError(403, "email verification required"),
    );

    expect(copy.title).toBe("이메일 확인이 필요합니다");
    expect(copy.hint).not.toContain("잠시 뒤");
    expect(copy.hint).toContain("메일");
  });

  it("만료된 링크와 이미 쓴 링크를 다르게 안내한다", () => {
    /* 둘 다 "링크가 안 된다"지만 다음 행동이 다르다: 하나는 새 링크를
     * 받아야 하고, 다른 하나는 아무것도 할 필요가 없다. */
    const expired = describeError(new ApiError(400, "invalid or expired token"));
    const already = describeError(new ApiError(409, "email already verified"));

    expect(expired).not.toEqual(already);
    expect(expired.hint).toContain("새 링크");
  });

  it("재발송 제한은 스팸함을 함께 안내한다", () => {
    const copy = describeError(
      new ApiError(429, "verification email rate limit exceeded"),
    );

    expect(copy.title).toBe("메일을 방금 보냈습니다");
    expect(copy.hint).toContain("스팸함");
  });
});

describe("ApiError", () => {
  it("status와 detail을 보존한다", () => {
    const error = new ApiError(429, "daily query quota exceeded");

    expect(error.status).toBe(429);
    expect(error.detail).toBe("daily query quota exceeded");
    expect(error.message).toBe("daily query quota exceeded");
    expect(error).toBeInstanceOf(Error);
  });
});

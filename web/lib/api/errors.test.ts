import { describe, expect, it } from "vitest";

import { ApiError, describeError } from "./errors";

/* 백엔드가 실제로 보내는 detail 문자열들. 여기 적힌 것과 백엔드가
 * 어긋나면 문구가 조용히 일반 폴백으로 떨어지고, 그 폴백은 틀린 조언을
 * 준다("잠시 뒤에 다시 시도해 주세요"). 백엔드 쪽 대조는
 * tests/test_error_details.py가 맡는다. */
const BACKEND_DETAILS = [
  "query rate limit exceeded",
  "daily query quota exceeded",
  "upload rate limit exceeded",
  "monthly upload page quota exceeded",
  "email already registered",
  "invalid email or password",
  "search unavailable",
];

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

  it("백엔드가 보내는 detail은 전부 전용 문구를 가진다", () => {
    /* 하나라도 일반 폴백으로 떨어지면 카피 규칙(원인 + 해결 방법)이
     * 깨진다 — 사용자는 무엇을 해야 할지 모르게 된다. */
    const fallback = describeError(new ApiError(999, "unmapped"));

    for (const detail of BACKEND_DETAILS) {
      const copy = describeError(new ApiError(400, detail));
      expect(copy, detail).not.toEqual(fallback);
    }
  });

  it("모든 문구가 원인과 해결 방법을 함께 준다", () => {
    /* 카피 규칙: "에러 발생" 대신 (원인) + (해결 방법). */
    const cases = [
      ...BACKEND_DETAILS.map((d) => new ApiError(400, d)),
      new ApiError(0, "x"),
      new ApiError(401, "x"),
      new ApiError(404, "x"),
      new ApiError(413, "x"),
      new ApiError(415, "x"),
      new ApiError(429, "x"),
      new ApiError(500, "x"),
      new ApiError(999, "x"),
    ];

    for (const error of cases) {
      const copy = describeError(error);
      expect(copy.title.length, String(error.status)).toBeGreaterThan(0);
      expect(copy.hint.length, String(error.status)).toBeGreaterThan(0);
      // 시스템 용어를 그대로 노출하지 않는다.
      expect(copy.title).not.toMatch(/[a-z]{4,}/);
    }
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

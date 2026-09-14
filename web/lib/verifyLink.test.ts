import { describe, expect, it } from "vitest";

import { readVerifyToken, scrubQueryToken, tokenState } from "./verifyLink";

/** location 처럼 생긴 것. 이 모듈이 필요로 하는 두 필드만 있으면 된다. */
function at(href: string): { hash: string; search: string } {
  return new URL(href, "https://docs-rag.example.com");
}

describe("readVerifyToken", () => {
  it("프래그먼트에서 토큰을 읽는다", () => {
    expect(readVerifyToken(at("/verify#token=abc123"))).toBe("abc123");
  });

  it("퍼센트 인코딩을 푼다", () => {
    expect(readVerifyToken(at("/verify#token=a%2Bb%2Fc"))).toBe("a+b/c");
  });

  it("프래그먼트에 다른 값이 섞여 있어도 token 만 꺼낸다", () => {
    expect(readVerifyToken(at("/verify#from=mail&token=abc123"))).toBe("abc123");
  });

  it("토큰이 없으면 null", () => {
    expect(readVerifyToken(at("/verify"))).toBeNull();
    expect(readVerifyToken(at("/verify#"))).toBeNull();
    expect(readVerifyToken(at("/verify#token="))).toBeNull();
  });

  /* 형식을 바꾸기 전에 발송된 메일이 TTL 동안 살아 있다. 여기서 안 받으면
   * 그 링크들이 전부 죽는다. */
  it("레거시 ?token= 링크도 계속 받는다", () => {
    expect(readVerifyToken(at("/verify?token=old123"))).toBe("old123");
  });

  it("둘 다 있으면 프래그먼트가 이긴다", () => {
    expect(readVerifyToken(at("/verify?token=query#token=frag"))).toBe("frag");
  });
});

describe("tokenState", () => {
  /* 이 테스트가 이 변경의 핵심이다. 프래그먼트는 서버 렌더와 하이드레이션에
   * 존재하지 않아서, 정상 링크로 들어와도 첫 렌더의 토큰은 undefined 다.
   * 그 "아직 모른다"를 "없다"로 접으면 멀쩡한 링크에서도 "링크가 올바르지
   * 않습니다"가 한 프레임 번쩍인다. */
  it("아직 모르는 것(undefined)은 없는 것(null)과 다르다", () => {
    expect(tokenState(undefined)).toBe("unknown");
    expect(tokenState(undefined)).not.toBe("missing");
  });

  it("없으면 missing, 있으면 present", () => {
    expect(tokenState(null)).toBe("missing");
    expect(tokenState("")).toBe("missing");
    expect(tokenState("abc123")).toBe("present");
  });
});

describe("scrubQueryToken", () => {
  it("쿼리 토큰을 프래그먼트로 옮긴다", () => {
    const scrubbed = scrubQueryToken("https://docs-rag.example.com/verify?token=old123");

    expect(scrubbed).toBe("https://docs-rag.example.com/verify#token=old123");
  });

  /* 지우기만 하고 옮기지 않으면, 바꾼 직후 읽히는 토큰이 null 이 되어
   * 검증 중이던 화면이 "링크가 올바르지 않습니다"로 뒤집힌다. */
  it("바꾼 주소에서 읽은 토큰이 바꾸기 전과 같다", () => {
    const before = "https://docs-rag.example.com/verify?token=old123";
    const after = scrubQueryToken(before);

    expect(after).not.toBeNull();
    expect(readVerifyToken(at(after as string))).toBe(readVerifyToken(at(before)));
  });

  it("다른 쿼리 파라미터는 건드리지 않는다", () => {
    expect(scrubQueryToken("https://docs-rag.example.com/verify?utm=mail&token=old")).toBe(
      "https://docs-rag.example.com/verify?utm=mail#token=old",
    );
  });

  it("프래그먼트로 들어온 정상 링크는 바꿀 것이 없다", () => {
    expect(scrubQueryToken("https://docs-rag.example.com/verify#token=abc")).toBeNull();
    expect(scrubQueryToken("https://docs-rag.example.com/verify")).toBeNull();
  });

  it("둘 다 있으면 쿼리 쪽만 걷어내고 프래그먼트를 남긴다", () => {
    expect(
      scrubQueryToken("https://docs-rag.example.com/verify?token=query#token=frag"),
    ).toBe("https://docs-rag.example.com/verify#token=frag");
  });
});

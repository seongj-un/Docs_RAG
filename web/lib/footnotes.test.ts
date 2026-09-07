import { describe, expect, it } from "vitest";

import type { Citation } from "@/lib/api";
import { annotate, pageLabel } from "./footnotes";

function citation(
  chunkId: string,
  pageFrom: number | null,
  pageTo: number | null = null,
): Citation {
  return {
    chunk_id: chunkId,
    document_id: "doc-1",
    page_from: pageFrom,
    page_to: pageTo ?? pageFrom,
    snippet: `${chunkId} 스니펫`,
  };
}

describe("annotate", () => {
  it("[p.N]을 각주로 바꾸고 앞뒤 글자를 보존한다", () => {
    const result = annotate("연차는 15일이다 [p.7]. 끝.", [citation("a", 7)]);

    expect(result.segments).toEqual([
      { kind: "text", text: "연차는 15일이다 " },
      { kind: "footnote", number: 1, citation: citation("a", 7) },
      { kind: "text", text: ". 끝." },
    ]);
    expect(result.footnotes).toHaveLength(1);
    expect(result.orphans).toHaveLength(0);
  });

  it("같은 청크를 두 번 인용하면 같은 번호를 준다", () => {
    const result = annotate("가 [p.3] 나 [p.3]", [citation("a", 3)]);

    const numbers = result.segments
      .filter((segment) => segment.kind === "footnote")
      .map((segment) => segment.number);

    expect(numbers).toEqual([1, 1]);
    expect(result.footnotes).toHaveLength(1);
  });

  it("번호는 본문에 나온 순서를 따른다", () => {
    const result = annotate("가 [p.9] 나 [p.2]", [
      citation("a", 2),
      citation("b", 9),
    ]);

    expect(result.footnotes.map((f) => [f.number, f.citation.chunk_id])).toEqual(
      [
        [1, "b"],
        [2, "a"],
      ],
    );
  });

  it("범위 청크는 그 안의 어느 쪽으로 인용해도 잡힌다", () => {
    const ranged = citation("a", 4, 6);
    const result = annotate("가 [p.5]", [ranged]);

    expect(result.footnotes[0].citation.chunk_id).toBe("a");
  });

  it("[p.4-6] 같은 범위 표기도 읽는다", () => {
    const result = annotate("가 [p.4-6]", [citation("a", 4, 6)]);

    expect(result.footnotes).toHaveLength(1);
  });

  it("근거 없는 인용은 글자로 남긴다 — 빈 모달을 여는 것보다 낫다", () => {
    const result = annotate("가 [p.99] 나", [citation("a", 3)]);

    expect(result.segments).toEqual([
      { kind: "text", text: "가 [p.99] 나" },
    ]);
    expect(result.footnotes).toHaveLength(0);
    /* 쓰이지 못한 근거는 orphan으로 넘어간다. */
    expect(result.orphans.map((f) => f.number)).toEqual([1]);
  });

  it("모델이 표기를 빠뜨려도 근거를 잃지 않는다", () => {
    const result = annotate("연차는 15일이다.", [
      citation("a", 3),
      citation("b", 8),
    ]);

    expect(result.footnotes).toHaveLength(0);
    expect(result.orphans.map((f) => f.number)).toEqual([1, 2]);
  });

  it("본문에 쓰인 것과 남은 것의 번호가 겹치지 않는다", () => {
    const result = annotate("가 [p.3]", [citation("a", 3), citation("b", 8)]);

    expect(result.footnotes.map((f) => f.number)).toEqual([1]);
    expect(result.orphans.map((f) => f.number)).toEqual([2]);
  });

  it("쪽 정보가 없는 근거는 어떤 인용에도 걸리지 않는다", () => {
    const result = annotate("가 [p.1]", [citation("a", null)]);

    expect(result.footnotes).toHaveLength(0);
    expect(result.orphans).toHaveLength(1);
  });

  it("빈 답변도 터지지 않는다", () => {
    expect(annotate("", []).segments).toEqual([]);
  });
});

describe("pageLabel", () => {
  it("한 쪽이면 한 쪽으로 적는다", () => {
    expect(pageLabel(citation("a", 7))).toBe("7쪽");
  });

  it("여러 쪽이면 범위로 적는다", () => {
    expect(pageLabel(citation("a", 4, 6))).toBe("4–6쪽");
  });

  it("쪽 정보가 없으면 없다고 적는다", () => {
    expect(pageLabel(citation("a", null))).toBe("쪽 정보 없음");
  });
});

import type { Citation } from "@/lib/api";

/* 각주 매핑.
 *
 * 모델은 답변 본문에 [p.7] 같은 표기를 남기고, citations[]는 그와 별개인
 * 배열로 온다. 둘을 잇는 유일한 끈은 페이지 번호다 — 그래서 이 매핑은
 * 역매칭이고, 실패할 수 있다.
 *
 * 실패하는 경우가 실제로 둘 있다:
 * 1. 모델이 컨텍스트에 없는 페이지를 인용한다 → 각주를 만들지 않고 [p.N]
 *    글자를 그대로 둔다. 눌렀는데 빈 모달이 열리는 것보다 낫다.
 * 2. 모델이 표기를 아예 빠뜨린다 → 근거는 있는데 볼 방법이 없어진다.
 *    그래서 본문에서 못 쓴 citation을 orphans로 따로 넘긴다.
 */

const CITATION_PATTERN = /\[p\.\s*(\d+)(?:\s*[-~]\s*(\d+))?\]/g;

export type Segment =
  | { kind: "text"; text: string }
  | { kind: "footnote"; number: number; citation: Citation };

export type Footnote = {
  number: number;
  citation: Citation;
};

export type Annotated = {
  segments: Segment[];
  /** 본문에 실제로 붙은 각주. 모달 안 전환은 이 순서를 따른다. */
  footnotes: Footnote[];
  /** 본문이 가리키지 못한 근거. 답변 아래에 따로 내보인다. */
  orphans: Footnote[];
};

function covers(citation: Citation, page: number): boolean {
  const from = citation.page_from;
  if (from === null) return false;
  const to = citation.page_to ?? from;
  return page >= from && page <= to;
}

export function annotate(answer: string, citations: Citation[]): Annotated {
  const segments: Segment[] = [];
  const footnotes: Footnote[] = [];
  /* 같은 청크를 두 번 인용하면 같은 번호를 준다 — 논문 각주와 같은 규칙. */
  const numberByChunk = new Map<string, number>();

  let cursor = 0;
  CITATION_PATTERN.lastIndex = 0;

  for (
    let match = CITATION_PATTERN.exec(answer);
    match !== null;
    match = CITATION_PATTERN.exec(answer)
  ) {
    const page = Number(match[1]);
    const citation = citations.find((item) => covers(item, page));

    if (!citation) {
      /* 근거를 못 찾았으면 표기를 글자로 남긴다. 여기서 잘라내면
       * 모델이 잘못 인용했다는 사실까지 감춰진다. */
      continue;
    }

    if (match.index > cursor) {
      segments.push({ kind: "text", text: answer.slice(cursor, match.index) });
    }

    let number = numberByChunk.get(citation.chunk_id);
    if (number === undefined) {
      number = numberByChunk.size + 1;
      numberByChunk.set(citation.chunk_id, number);
      footnotes.push({ number, citation });
    }

    segments.push({ kind: "footnote", number, citation });
    cursor = match.index + match[0].length;
  }

  if (cursor < answer.length) {
    segments.push({ kind: "text", text: answer.slice(cursor) });
  }

  const orphans = citations
    .filter((citation) => !numberByChunk.has(citation.chunk_id))
    .map((citation, index) => ({
      number: numberByChunk.size + index + 1,
      citation,
    }));

  return { segments, footnotes, orphans };
}

/** 근거 하나를 사람이 읽는 위치 표기로. */
export function pageLabel(citation: Citation): string {
  const from = citation.page_from;
  if (from === null) return "쪽 정보 없음";
  const to = citation.page_to ?? from;
  return from === to ? `${from}쪽` : `${from}–${to}쪽`;
}

import { describe, expect, it } from "vitest";

import { readSse, type SseFrame } from "./sse";

/** 주어진 조각들을 그 경계 그대로 흘려보내는 가짜 응답.
 *
 * 조각 경계가 곧 네트워크 청크 경계다 — 프레임 중간에서 끊기는 상황을
 * 재현하는 것이 이 테스트의 핵심이라, 조각을 합쳐서 넘기면 안 된다. */
function responseOf(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  return {
    body: {
      getReader: () => ({
        read: async () =>
          index < chunks.length
            ? { done: false, value: encoder.encode(chunks[index++]) }
            : { done: true, value: undefined },
        releaseLock: () => undefined,
      }),
    },
  } as unknown as Response;
}

async function collect(response: Response): Promise<SseFrame[]> {
  const frames: SseFrame[] = [];
  for await (const frame of readSse(response)) frames.push(frame);
  return frames;
}

describe("readSse", () => {
  it("event와 data를 짝지어 읽는다", async () => {
    const frames = await collect(
      responseOf(
        'event: meta\ndata: {"chunks":3}\n\n',
        'event: done\ndata: {"refused":false}\n\n',
      ),
    );

    expect(frames).toEqual([
      { event: "meta", data: { chunks: 3 } },
      { event: "done", data: { refused: false } },
    ]);
  });

  it("프레임이 청크 중간에서 끊겨도 조립한다", async () => {
    /* 실제로 벌어지는 일이다. 완성된 것만 잘라내고 나머지를 버퍼에
     * 남기지 않으면 여기서 토큰이 사라진다. */
    const frames = await collect(
      responseOf('event: tok', 'en\ndata: {"text":"연차는 ', '15일"}\n', "\n"),
    );

    expect(frames).toEqual([{ event: "token", data: { text: "연차는 15일" } }]);
  });

  it("한 청크에 여러 프레임이 들어와도 다 뽑는다", async () => {
    const frames = await collect(
      responseOf(
        'event: token\ndata: {"text":"가"}\n\nevent: token\ndata: {"text":"나"}\n\n',
      ),
    );

    expect(frames.map((f) => (f.data as { text: string }).text)).toEqual([
      "가",
      "나",
    ]);
  });

  it("CRLF 경계도 읽는다", async () => {
    const frames = await collect(
      responseOf('event: meta\r\ndata: {"cached":true}\r\n\r\n'),
    );

    expect(frames).toEqual([{ event: "meta", data: { cached: true } }]);
  });

  it("마지막 프레임에 빈 줄이 없어도 잃지 않는다", async () => {
    const frames = await collect(responseOf('event: done\ndata: {"ok":1}'));

    expect(frames).toEqual([{ event: "done", data: { ok: 1 } }]);
  });

  it("data가 여러 줄이면 줄바꿈으로 이어 붙인다", async () => {
    const frames = await collect(responseOf('data: {"a":1,\ndata: "b":2}\n\n'));

    expect(frames).toEqual([{ event: "message", data: { a: 1, b: 2 } }]);
  });

  it("event가 없으면 message로 본다", async () => {
    const frames = await collect(responseOf('data: {"x":1}\n\n'));

    expect(frames[0].event).toBe("message");
  });

  it("주석 줄(:)은 무시한다", async () => {
    const frames = await collect(
      responseOf(': 하트비트\nevent: token\ndata: {"text":"가"}\n\n'),
    );

    expect(frames).toEqual([{ event: "token", data: { text: "가" } }]);
  });

  it("data 없는 프레임은 내보내지 않는다", async () => {
    /* 하트비트만 오는 경우. 이걸 프레임으로 내면 호출자가 빈 사건을 본다. */
    const frames = await collect(
      responseOf(": keep-alive\n\nevent: token\ndata: {\"text\":\"가\"}\n\n"),
    );

    expect(frames).toHaveLength(1);
  });

  it("콜론 뒤 공백 하나는 값이 아니다", async () => {
    const spaced = await collect(responseOf('data: {"text":" 앞뒤 "}\n\n'));
    const tight = await collect(responseOf('data:{"text":" 앞뒤 "}\n\n'));

    /* 명세상 "data: x"와 "data:x"는 같은 값 x다. 공백을 두 번 벗기면
     * 값 안의 공백까지 먹는다. */
    expect(spaced).toEqual(tight);
    expect((spaced[0].data as { text: string }).text).toBe(" 앞뒤 ");
  });

  it("JSON이 아니면 문자열 그대로 넘긴다", async () => {
    const frames = await collect(responseOf("event: note\ndata: 그냥 글자\n\n"));

    expect(frames).toEqual([{ event: "note", data: "그냥 글자" }]);
  });

  it("본문이 없으면 아무것도 내보내지 않는다", async () => {
    const frames = await collect({ body: null } as unknown as Response);

    expect(frames).toEqual([]);
  });

  it("멀티바이트 문자가 청크 경계에서 쪼개져도 깨지지 않는다", async () => {
    /* "연"은 UTF-8로 3바이트다. 디코더를 stream 모드로 두지 않으면
     * 여기서 U+FFFD가 된다. */
    const bytes = new TextEncoder().encode('data: {"text":"연차"}\n\n');
    let index = 0;
    const pieces = [bytes.slice(0, 16), bytes.slice(16)];
    const response = {
      body: {
        getReader: () => ({
          read: async () =>
            index < pieces.length
              ? { done: false, value: pieces[index++] }
              : { done: true, value: undefined },
          releaseLock: () => undefined,
        }),
      },
    } as unknown as Response;

    const frames = await collect(response);
    expect((frames[0].data as { text: string }).text).toBe("연차");
  });
});

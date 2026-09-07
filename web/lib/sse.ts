/* SSE 수신.
 *
 * EventSource를 쓸 수 없다. 질의는 POST에 본문이 있고 세션 쿠키가 필요한데
 * EventSource는 GET 전용이고 본문을 실을 수 없다. 그래서 fetch의
 * ReadableStream을 직접 읽고 프레임을 손으로 자른다.
 */

export type SseFrame = {
  event: string;
  data: unknown;
};

/** 프레임 하나를 파싱한다. data가 없는 프레임(하트비트 등)은 null. */
function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of raw.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // 주석·빈 줄

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    // 명세상 콜론 뒤 공백 하나는 값이 아니다.
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }

  if (dataLines.length === 0) return null;

  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    // 백엔드는 항상 JSON을 보낸다. 아니면 문자열 그대로 넘긴다.
    return { event, data: dataLines.join("\n") };
  }
}

/** 응답 본문을 SSE 프레임 스트림으로 읽는다. */
export async function* readSse(
  response: Response,
): AsyncGenerator<SseFrame, void, undefined> {
  if (!response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      /* 프레임 경계는 빈 줄이다. 청크가 프레임 중간에서 끊길 수 있으므로
       * 완성된 것만 잘라내고 나머지는 버퍼에 남긴다. */
      let boundary = findBoundary(buffer);
      while (boundary !== null) {
        const frame = parseFrame(buffer.slice(0, boundary.index));
        buffer = buffer.slice(boundary.index + boundary.length);
        if (frame) yield frame;
        boundary = findBoundary(buffer);
      }
    }

    /* 마지막 프레임이 빈 줄 없이 끝났을 수 있다. */
    const tail = parseFrame(buffer);
    if (tail) yield tail;
  } finally {
    reader.releaseLock();
  }
}

function findBoundary(buffer: string): { index: number; length: number } | null {
  const lf = buffer.indexOf("\n\n");
  const crlf = buffer.indexOf("\r\n\r\n");
  if (lf === -1 && crlf === -1) return null;
  if (crlf !== -1 && (lf === -1 || crlf < lf)) {
    return { index: crlf, length: 4 };
  }
  return { index: lf, length: 2 };
}

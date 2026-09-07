import type { ApiErrorShape } from "./errors";
import { ApiError } from "./errors";

/* 백엔드는 별도 오리진에 있고 세션 쿠키로 인증한다. 그래서 모든 요청이
 * credentials: "include"여야 하고, 백엔드는 allow_credentials=True에
 * 정확한 오리진 목록을 둔다 (app/config.py의 cors_origins). */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

type RequestOptions = {
  method?: string;
  /** JSON 본문. FormData는 body로 직접 넘긴다. */
  json?: unknown;
  body?: BodyInit;
  signal?: AbortSignal;
};

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", json, body, signal } = options;

  const headers: Record<string, string> = {};
  /* FormData에는 Content-Type을 붙이지 않는다 — 브라우저가 multipart
   * 경계 문자열을 포함해 직접 만들어야 한다. */
  if (json !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      credentials: "include",
      body: json !== undefined ? JSON.stringify(json) : body,
      signal,
    });
  } catch (cause) {
    /* 네트워크 자체가 실패한 경우. 서버가 안 떠 있거나 CORS에 막힌 것이라
     * 상태 코드가 없다 — 0으로 구분한다. */
    if (signal?.aborted) throw cause;
    throw new ApiError(0, "서버에 연결하지 못했습니다");
  }

  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response));
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as ApiErrorShape;
    if (typeof body.detail === "string") return body.detail;
  } catch {
    /* 본문이 JSON이 아니면 상태 텍스트로 만족한다. */
  }
  return response.statusText;
}

/** SSE처럼 응답 본문을 직접 읽어야 하는 요청. */
export async function requestStream(
  path: string,
  options: RequestOptions = {},
): Promise<Response> {
  const { method = "POST", json, signal } = options;
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      Accept: "text/event-stream",
    },
    credentials: "include",
    body: json !== undefined ? JSON.stringify(json) : undefined,
    signal,
  });

  /* 스트림이 시작되기 *전에* 걸린 실패(401 등)는 아직 상태 코드로 온다.
   * 시작된 뒤의 실패는 event: error로 오므로 여기서 잡히지 않는다. */
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response));
  }
  return response;
}

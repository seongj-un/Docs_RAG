import { readSse } from "../sse";
import { request, requestStream } from "./client";
import type { Citation, Conversation, ConversationDetail } from "./types";

export function listConversations(): Promise<Conversation[]> {
  return request<Conversation[]>("/conversations");
}

export function createConversation(
  scopeDocumentId: string | null = null,
): Promise<Conversation> {
  return request<Conversation>("/conversations", {
    method: "POST",
    json: { scope_document_id: scopeDocumentId },
  });
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/conversations/${id}`);
}

export function deleteConversation(id: string): Promise<void> {
  return request<void>(`/conversations/${id}`, { method: "DELETE" });
}

/* 스트림에서 오는 사건들. 백엔드 app/routers/conversations.py의 프로토콜과
 * 1:1이다: meta -> token* -> done, 또는 언제든 error. */
export type QueryEvent =
  | { type: "meta"; scope: string | null; cached: boolean; chunks: number }
  | { type: "token"; text: string }
  | {
      type: "done";
      answer: string;
      refused: boolean;
      citations: Citation[];
      cached: boolean;
    }
  | { type: "error"; status: number; detail: string };

/** 한 턴을 질의하고 사건을 순서대로 내보낸다.
 *
 * 스트리밍이 시작되면 HTTP 상태는 이미 200으로 굳는다. 그래서 429(쿼터)나
 * 500은 예외가 아니라 `error` 사건으로 온다 — 호출자는 상태 코드가 아니라
 * 이 사건을 보고 판단해야 한다.
 */
export async function* streamQuery(
  conversationId: string,
  question: string,
  options: { documentId?: string | null; signal?: AbortSignal } = {},
): AsyncGenerator<QueryEvent, void, undefined> {
  const response = await requestStream(
    `/conversations/${conversationId}/query`,
    {
      json: {
        question,
        document_id: options.documentId ?? null,
      },
      signal: options.signal,
    },
  );

  for await (const frame of readSse(response)) {
    const data = frame.data as Record<string, unknown>;
    switch (frame.event) {
      case "meta":
        yield {
          type: "meta",
          scope: (data.scope as string | null) ?? null,
          cached: Boolean(data.cached),
          chunks: Number(data.chunks ?? 0),
        };
        break;
      case "token":
        yield { type: "token", text: String(data.text ?? "") };
        break;
      case "done":
        yield {
          type: "done",
          answer: String(data.answer ?? ""),
          refused: Boolean(data.refused),
          citations: (data.citations as Citation[]) ?? [],
          cached: Boolean(data.cached),
        };
        break;
      case "error":
        yield {
          type: "error",
          status: Number(data.status ?? 500),
          detail: String(data.detail ?? ""),
        };
        break;
      default:
        break; // 모르는 사건은 무시한다 — 프로토콜이 늘어나도 깨지지 않게.
    }
  }
}

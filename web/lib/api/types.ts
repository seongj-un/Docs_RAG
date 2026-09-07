/* 백엔드 app/schemas.py를 그대로 옮긴 것. 필드 이름은 스네이크 케이스를
 * 유지한다 — 카멜로 바꾸면 API 응답과 화면 코드 사이에 번역 계층이 하나
 * 더 생기고, 그 계층은 스키마가 바뀔 때마다 조용히 어긋난다. */

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export type User = {
  id: string;
  email: string;
  created_at: string | null;
};

export type Document = {
  id: string;
  filename: string;
  num_pages: number | null;
  status: DocumentStatus;
  error: string | null;
  created_at: string | null;
};

export type DocumentCreated = {
  id: string;
  filename: string;
  status: DocumentStatus;
};

export type Citation = {
  chunk_id: string;
  document_id: string;
  page_from: number | null;
  page_to: number | null;
  /** 240자에서 잘린 미리보기. 모달은 이걸 쓰지 않고 /chunks/{id}를 부른다. */
  snippet: string;
};

export type Chunk = {
  id: string;
  document_id: string;
  chunk_index: number;
  page_from: number | null;
  page_to: number | null;
  content: string;
};

export type Conversation = {
  id: string;
  title: string | null;
  scope_document_id: string | null;
  created_at: string | null;
};

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[] | null;
  refused: boolean;
  created_at: string | null;
};

export type ConversationDetail = Conversation & {
  messages: Message[];
};

export type Usage = {
  queries_today: number;
  queries_per_day: number;
  pages_this_month: number;
  pages_per_month: number;
};

import { request } from "./client";
import type { Document, DocumentCreated } from "./types";

export function listDocuments(): Promise<Document[]> {
  return request<Document[]>("/documents");
}

export function getDocument(id: string): Promise<Document> {
  return request<Document>(`/documents/${id}`);
}

export function deleteDocument(id: string): Promise<void> {
  return request<void>(`/documents/${id}`, { method: "DELETE" });
}

/** 업로드는 202로 즉시 돌아온다. 색인은 뒤에서 돌므로 상태를 폴링해야 한다. */
export function uploadDocument(file: File): Promise<DocumentCreated> {
  const form = new FormData();
  form.append("file", file);
  return request<DocumentCreated>("/documents", { method: "POST", body: form });
}

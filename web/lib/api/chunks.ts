import { request } from "./client";
import type { Chunk } from "./types";

/** 근거 모달용 청크 원문. Citation.snippet은 240자에서 잘려 있다. */
export function getChunk(id: string): Promise<Chunk> {
  return request<Chunk>(`/chunks/${id}`);
}

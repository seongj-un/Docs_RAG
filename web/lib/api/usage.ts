import { request } from "./client";
import type { Usage } from "./types";

export function getUsage(): Promise<Usage> {
  return request<Usage>("/usage");
}

import { request } from "./client";
import type { User } from "./types";

export function signup(email: string, password: string): Promise<User> {
  return request<User>("/auth/signup", {
    method: "POST",
    json: { email, password },
  });
}

export function login(email: string, password: string): Promise<User> {
  return request<User>("/auth/login", {
    method: "POST",
    json: { email, password },
  });
}

export function logout(): Promise<void> {
  return request<void>("/auth/logout", { method: "POST" });
}

/** 현재 세션의 사용자. 세션이 없으면 401로 던진다. */
export function me(): Promise<User> {
  return request<User>("/auth/me");
}

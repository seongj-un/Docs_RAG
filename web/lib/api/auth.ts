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

/** 메일 링크의 토큰을 검증한다. 세션이 없어도 된다 — 다른 브라우저에서
 *  열리는 것이 정상 경로다. */
export function verify(token: string): Promise<User> {
  return request<User>("/auth/verify", {
    method: "POST",
    json: { token },
  });
}

/** 인증 메일 재발송. 현재 세션의 계정으로만 보낸다. */
export function resendVerification(): Promise<void> {
  return request<void>("/auth/resend-verification", { method: "POST" });
}

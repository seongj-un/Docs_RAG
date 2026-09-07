/* 문서 색인 실패의 갈래.
 *
 * 노션 카피 표는 failed를 곧 "글자 없음"으로 놓았지만, 실제로 백엔드는
 * 두 가지를 같은 status로 기록한다: 뽑아낼 글자가 없는 PDF와, 임베딩
 * 서버에 닿지 못한 경우다. 둘을 뭉뚱그리면 멀쩡한 문서를 다시 스캔하라고
 * 잘못 안내하게 된다 — 사용자가 할 일이 서로 다르므로 나눠 말한다.
 *
 * 판별 근거는 ingest.index_document가 던지는 ValueError의 문구다
 * (app/services/ingest.py: raise ValueError("no extractable text in PDF")).
 */
export type FailureKind = "no-text" | "server";

export function failureKind(error: string | null | undefined): FailureKind {
  return error?.includes("no extractable text") ? "no-text" : "server";
}

export const FAILURE_LABEL: Record<FailureKind, string> = {
  "no-text": "글자 없음",
  server: "읽지 못함",
};

export const FAILURE_TITLE: Record<FailureKind, string> = {
  "no-text": "글자를 찾지 못했습니다",
  server: "문서를 정리하지 못했습니다",
};

export const FAILURE_NOTE: Record<FailureKind, string> = {
  "no-text":
    "이 PDF에는 뽑아낼 수 있는 글자가 없습니다. 스캔한 이미지로만 된 문서일 수 있습니다.",
  server:
    "문서를 정리하는 서버에 닿지 못했습니다. 문서 잘못이 아니니, 잠시 뒤에 지우고 다시 올려 주세요.",
};

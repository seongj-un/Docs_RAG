export type ApiErrorShape = { detail?: string };

export class ApiError extends Error {
  /** 0은 HTTP 응답 자체가 없었다는 뜻 — 서버가 죽었거나 CORS에 막혔다. */
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export type ErrorCopy = {
  /** 무엇이 잘못됐는지. */
  title: string;
  /** 다음에 무엇을 하면 되는지. 카피 규칙: 원인만 말하고 끝내지 않는다. */
  hint: string;
};

/* 백엔드 detail은 영어 식별자다 ("daily query quota exceeded"). 사용자에게는
 * 그대로 보여주지 않는다 — 카피 규칙상 시스템 용어를 쓰지 않고, 429처럼 한
 * 상태 코드가 서로 다른 두 가지를 뜻할 때는 detail로만 구분되기 때문이다. */
const BY_DETAIL: Record<string, ErrorCopy> = {
  "query rate limit exceeded": {
    title: "질문이 너무 빠릅니다",
    hint: "잠시 뒤에 다시 물어봐 주세요.",
  },
  "daily query quota exceeded": {
    title: "오늘 쓸 수 있는 질문을 다 썼습니다",
    hint: "내일 다시 채워집니다.",
  },
  "upload rate limit exceeded": {
    title: "문서를 너무 빠르게 올리고 있습니다",
    hint: "잠시 뒤에 다시 올려 주세요.",
  },
  "monthly upload page quota exceeded": {
    title: "이번 달 올릴 수 있는 쪽수를 다 썼습니다",
    hint: "다음 달 1일에 다시 채워집니다.",
  },
  "email already registered": {
    title: "이미 가입된 이메일입니다",
    hint: "그 이메일로 로그인해 주세요.",
  },
  "invalid email or password": {
    title: "이메일이나 비밀번호가 맞지 않습니다",
    hint: "다시 확인해 주세요.",
  },
};

const BY_STATUS: Record<number, ErrorCopy> = {
  0: {
    title: "서버에 연결하지 못했습니다",
    hint: "백엔드가 실행 중인지 확인한 뒤 다시 시도해 주세요.",
  },
  401: {
    title: "로그인이 필요합니다",
    hint: "다시 로그인해 주세요.",
  },
  404: {
    title: "찾을 수 없습니다",
    hint: "이미 지워졌을 수 있습니다. 목록을 새로 불러와 주세요.",
  },
  413: {
    title: "파일이 너무 큽니다",
    hint: "50MB, 500쪽 이하 문서만 올릴 수 있습니다.",
  },
  415: {
    title: "PDF만 올릴 수 있습니다",
    hint: "다른 형식은 아직 읽지 못합니다.",
  },
  429: {
    title: "요청이 너무 많습니다",
    hint: "잠시 뒤에 다시 시도해 주세요.",
  },
  500: {
    title: "서버에서 문제가 생겼습니다",
    hint: "잠시 뒤에 다시 시도해 주세요.",
  },
};

const FALLBACK: ErrorCopy = {
  title: "문제가 생겼습니다",
  hint: "잠시 뒤에 다시 시도해 주세요.",
};

/** 상태 코드와 detail을 사용자 언어의 (원인 + 해결 방법)으로 바꾼다. */
export function describeError(error: unknown): ErrorCopy {
  if (!(error instanceof ApiError)) return FALLBACK;
  return BY_DETAIL[error.detail] ?? BY_STATUS[error.status] ?? FALLBACK;
}

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
/* 테스트가 이 표 자체를 순회한다. 목록을 따로 베껴 두면 그 사본이
 * 조용히 좁아지고, 검사는 줄어든 채로 계속 통과한다 — 실제로 그렇게
 * 두 건이 빠져 있었다. 대조 상대는 tests/test_error_details.py 다. */
export const BY_DETAIL: Record<string, ErrorCopy> = {
  "query rate limit exceeded": {
    title: "질문이 너무 빠릅니다",
    hint: "잠시 뒤에 다시 물어봐 주세요.",
  },
  "daily query quota exceeded": {
    title: "오늘 쓸 수 있는 질문을 다 썼습니다",
    hint: "내일 다시 채워집니다.",
  },
  /* 로그인·가입 시도 제한(429). 비밀번호를 틀린 것이 아니라 너무 자주
   * 시도한 것이므로, "다시 확인해 주세요"가 아니라 기다리라고 말해야 한다. */
  "auth rate limit exceeded": {
    title: "시도가 너무 잦습니다",
    hint: "잠시 뒤에 다시 로그인해 주세요.",
  },
  "upload rate limit exceeded": {
    title: "문서를 너무 빠르게 올리고 있습니다",
    hint: "잠시 뒤에 다시 올려 주세요.",
  },
  "model quota exceeded": {
    title: "지금은 답변을 만들 수 없습니다",
    hint: "사용량이 한도에 닿았습니다. 잠시 뒤에, 그래도 안 되면 내일 다시 시도해 주세요.",
  },
  "model unavailable": {
    title: "지금은 답변을 만들 수 없습니다",
    hint: "AI 쪽이 잠시 붐빕니다. 잠시 뒤에 다시 물어봐 주세요.",
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
  /* 이메일 인증(403). 쿼터의 429와 달리 기다려서 풀리지 않는다 —
   * 사용자가 메일의 링크를 눌러야 한다. */
  "email verification required": {
    title: "이메일 확인이 필요합니다",
    hint: "가입할 때 보낸 메일의 링크를 눌러 주세요. 안 왔다면 다시 보낼 수 있습니다.",
  },
  /* 유효 시간을 숫자로 적지 않는다. 실제 값은 VERIFY_TOKEN_TTL_HOURS 이고
   * 백엔드는 그걸 읽는데, 여기 24를 박아두면 설정을 바꾸는 순간 화면만
   * 옛 숫자를 말한다 — 이 기능에서 이미 한 번 고친 종류의 거짓말이다.
   * 프론트가 그 값을 받을 창구는 없으므로, 숫자를 빼는 쪽이 맞다. */
  "invalid or expired token": {
    title: "링크가 만료됐습니다",
    hint: "받으신 링크는 일정 시간이 지나면 닫힙니다. 새 링크를 받아 주세요.",
  },
  /* 실패가 아니라 이미 끝났다는 뜻이다. 사용자가 할 일이 없다. */
  "email already verified": {
    title: "이미 확인된 이메일입니다",
    hint: "그대로 사용하시면 됩니다.",
  },
  "verification email rate limit exceeded": {
    title: "메일을 방금 보냈습니다",
    hint: "1분 뒤에 다시 요청해 주세요. 스팸함도 확인해 보세요.",
  },
  /* 임베딩·리랭커 서버가 죽은 경우(503). 백엔드가 Retry-After를 주지
   * 않는 것과 같은 이유로 "잠시 뒤에 다시"라고 하지 않는다 — 쿼터와 달리
   * 대개 누가 서버를 다시 켜야 풀린다. 기다리라고만 하면 틀린 조언이다. */
  "search unavailable": {
    title: "지금은 답을 찾아드릴 수 없습니다",
    hint: "문서를 찾아주는 서버가 응답하지 않습니다. 올린 문서와 지난 대화는 그대로 있습니다.",
  },
};

export const BY_STATUS: Record<number, ErrorCopy> = {
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
  503: {
    title: "잠시 이용할 수 없습니다",
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

"""Application configuration loaded from environment / .env.

All M1 tunables (chunking, retrieval, providers) live here so services stay
free of magic numbers and the whole pipeline can be reconfigured without code
changes.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/docs_rag"

    # Logging. 루트 로거 레벨이고, 실제 설정은 app/logging.py 가 한다.
    # 기본을 WARNING 으로 두는 이유: 이 값이 생기기 전까지 앱의 루트 레벨을
    # 실제로 정한 것은 alembic.ini 의 [logger_root] 였고 그 값이 WARNING
    # 이었다. 앱이 로깅을 넘겨받으면서 기본 레벨까지 같이 바꾸면, 이번
    # 변경이 실제로 뭘 고쳤는지가 쏟아지는 INFO 에 묻힌다. 앱 자신의 로그는
    # WARNING/exception 아홉 군데뿐이라 올려서 얻을 것도 지금은 없다.
    # 조사할 일이 생기면 LOG_LEVEL=INFO 로 한 판 돌려도 된다. SQL 이 쏟아질
    # 걱정은 안 해도 된다 — SQLAlchemy 가 import 시점에 자기 "sqlalchemy"
    # 로거를 WARNING 으로 못박으므로(sqlalchemy/log.py) 엔진 로거는 루트
    # 레벨을 물려받지 않는다. 실측으로 확인했다. SQL 을 정말로 보려면
    # app/db.py 의 엔진에 echo=True 를 주는 쪽이고, 그건 명시적 선택이다
    # (바인딩 값에 질문·답변이 실린다는 점은 그때 감수하는 것이다).
    log_level: str = "WARNING"

    # Embedding (BGE-M3 via TEI)
    embed_provider: str = "local"
    embed_model: str = "BAAI/bge-m3"
    embed_dim: int = 1024
    # BGE-M3 lexical (sparse) weights live over the tokenizer vocab (XLM-R = 250002).
    embed_sparse_dim: int = 250002
    tei_url: str = "http://localhost:8080"

    # --- M2: hybrid search + reranking ---
    # When true, /query uses dense+sparse RRF fusion then cross-encoder rerank.
    # When false, falls back to M1 dense-only cosine search.
    hybrid_enabled: bool = True
    rerank_url: str = "http://localhost:8081"  # bge-reranker-v2-m3 (TEI)
    rrf_k: int = 60          # RRF constant
    # Candidates each retriever (dense, sparse) contributes before RRF fusion
    # and reranking. `python -m eval.candk_sweep` on eval/corpora/wide.py (260
    # pages, built specifically so this cutoff binds) found R@1 held at 1.000
    # all the way from 50 down to 5, while reranking time fell 1226ms ->
    # 135ms (9.1x) — candidate count is what reranking time is most sensitive
    # to. Shipped 20, not 5: 5 leaves only ~1.7x headroom over the worst rank
    # the gold chunk fell to in that sweep, 20 leaves ~6.7x and still roughly
    # halves reranking time versus the old default of 50. Headroom shrinks as
    # a corpus grows, and the measurement corpus was smaller than a
    # production one would be — so the conservative number is what ships.
    #
    # ⚠️ 위 근거는 **실제 문서 크기에서 검증된 적이 없다.** 저 측정에 쓴
    # wide 는 청크가 53자, hard 는 73자다. 실제 문서(이 DB 의
    # 인공지능기본법)는 중앙 1,295자다. 리랭킹 비용은 후보 개수가 아니라
    # 입력 총길이에 지배되므로 둘은 다른 이야기다 — 2026-09-11 실측:
    #
    #   같은 CAND_K=20 에서   53자 청크 →   474ms
    #                      1,295자 청크 → 9,409ms
    #   1,295자 기준  K=10 → 6,760ms · K=5 → 1,175ms · K=3 → 696ms
    #
    # 그래서 eval/corpora/deep.py 를 만들어(wide 와 같은 조항·질의, 쪽만
    # 1,250자로 채움) 다시 쟀다. **결과가 뒤집혔다** — 2026-09-11:
    #
    #   코퍼스      K=100   K=50   K=30   K=20   K=10    K=5
    #   wide(53자)  1.000  1.000  1.000  1.000  1.000  1.000   ← 어디서 줄여도 공짜
    #   deep(1250자) 0.947  0.947  0.895  0.868  0.816  0.789   ← 줄일수록 손해
    #
    # 짧은 청크에서는 CAND_K 를 5까지 줄여도 R@1 이 1.000 이었다. 실제
    # 길이에서는 단조 감소한다. 지금 기본값 20 은 이미 R@1 0.868 — 천장
    # (후보적중)도 같은 0.868 이라, 리랭커가 떨어뜨리는 게 아니라 **검색이
    # 정답을 후보에 못 올린다.** 쪽이 길어지면 사실이 희석돼 dense 순위가
    # 나빠지기 때문이다(deep K=20 에서 dense 적중 0.737).
    #
    # 무릎은 40 이다(30~50 을 5 단위로 다시 훑음):
    #
    #   K=50  R@1 0.947  11.5초      K=35  R@1 0.895  7.7초
    #   K=45  R@1 0.947   9.9초      K=30  R@1 0.895  6.5초
    #   K=40  R@1 0.947   8.8초  ← 0.947 을 지키는 가장 싼 값
    #
    # 40 아래로 내려가는 순간 semantic 질의의 천장이 1.000 → 0.929 로
    # 꺾인다(dense 는 30 에서도 1.000 인데 sparse 가 0.714 라, 융합 후
    # 후보에 못 드는 것이 생긴다). exact 는 sparse 가 30 까지 1.000 을
    # 지켜 멀쩡하다 — 두 유형이 서로 다른 채널에 기대고 있어서, 깊이를
    # 줄이면 semantic 쪽이 먼저 무너진다.
    #
    # 즉 이 설정은 더 이상 "공짜 속도"가 아니라 정확도-지연 교환이다:
    #   K=20 → 4.4초 / R@1 0.868      K=40 → 8.8초 / R@1 0.947
    # **40 을 택했다** — 8점을 4.4초에 산다. 정답이 1위에 안 오는 질의가
    # 여덟 중 하나에서 스무 중 하나로 줄어드는 쪽이, 답을 4초 빨리 주고
    # 틀리는 쪽보다 낫다고 판단했다(2026-09-11, 제품 결정).
    #
    # 이 교환 자체를 없애려면 리랭킹이 싸져야 한다. ONNX int8 이 CPU 만으로
    # GPU 속도를 냈다(8.7초 vs 9.4초, scripts/bench_rerank_alternatives.py)
    # — 다만 그 품질 측정은 짧은 청크 코퍼스 기준이라, 실제 길이에서
    # 확인되기 전까지는 후보이지 해답이 아니다.
    cand_k: int = 40         # candidates per retriever (dense top-N, sparse top-N)
    # Context size after reranking. Was 8; the M4 golden-set evaluation measured
    # context_precision 0.246 at that size (7 of 8 chunks typically irrelevant)
    # while R@3 was 1.000 — the needed page was always within the top 3, so the
    # extra five were pure noise.
    rerank_top: int = 3
    # Grounding floor for the reranked path. Separate from MIN_SCORE because the
    # scores are not the same quantity: MIN_SCORE gates cosine similarity, this
    # gates the cross-encoder's sigmoid. Reusing 0.2 for both (the M2 decision)
    # deterministically refused 21% of answerable questions — the reranker scores
    # correct-but-reworded matches low (e.g. "강아지" vs "반려동물" -> 0.038),
    # while genuinely unanswerable questions score ~0.000-0.002. 0.005 sits in
    # that gap. It is a cheap pre-filter, not the refusal decision: borderline
    # cases still reach the LLM, which is instructed to refuse ungrounded asks.
    rerank_min_score: float = 0.005
    # Characters of each candidate sent to the cross-encoder. Reranking cost is
    # linear in input length up to the model's own cut-off, so this is the
    # largest single lever on query latency. 0 disables truncation.
    # Only the scoring input is shortened — stored content, the generation
    # context, and citation snippets are untouched.
    rerank_max_chars: int = 0

    # Generation LLM (Gemini)
    llm_provider: str = "gemini"
    # ⚠️ 무료 티어에서는 이 모델을 대화형으로 쓸 수 없다 — 실측으로 한 문장
    # 응답에 34~40초, 그리고 하루 20회에서 429. 같은 요청이 flash-lite 로는
    # 0.9초다. 무료 키로 돌린다면 LLM_MODEL 을 gemini-3.1-flash-lite 로 두고,
    # 유료 티어에서 이 기본값으로 돌아올 것.
    llm_model: str = "gemini-3.6-flash"
    # Evaluation runs on a separate, lighter model. The flagship Flash tier
    # allows only 20 requests/day free, which a 36-question sweep exhausts
    # before it finishes; Flash-Lite has room for repeated before/after runs.
    # Production generation is unaffected by this setting.
    eval_llm_model: str = "gemini-3.1-flash-lite"
    gemini_api_key: str = ""

    # Chunking (token targets, approximated by words in M1)
    chunk_size: int = 700
    chunk_overlap: int = 100
    # --- M7 W5: 청킹 단위와 헤딩 경로 접두사 ---
    # 둘 다 **기존 동작이 기본값**이다. 프로덕션 인덱싱 경로는 이 두 줄을
    # 더해도 M1 때와 같은 청크를 만든다 — 실험을 붙이면서 재인덱싱 없이
    # 검색 품질이 조용히 달라지는 것이 이 저장소에서 가장 비싼 사고다.
    #
    # "section" 은 고정 토큰 창 대신 헤딩 경계에서 자른다. 가설(Notion W5):
    # 명세 문서는 섹션이 의미 단위라 섹션 청킹이 유리하다 — 특히 표가 창
    # 한가운데에서 잘리면 머리행과 데이터행이 서로 다른 청크로 갈라져 어느
    # 쪽도 "student_id 가 무엇인가"에 답하지 못한다.
    #
    # 2026-09-14 실측 (`python -m eval.harness ab --dataset
    # eval/datasets/spec_golden.jsonl --k 10`, 명세 코퍼스 30쪽·채점 27문항).
    # 누적 사다리라 각 칸은 바로 위 칸에서 **하나만** 더 켠 것이다:
    #
    #   칸                  청킹             R@1    R@5   R@10   P@10    MRR  재색인s  지연ms
    #   A0-baseline         fixed          0.444  0.722  0.759  0.093  0.629    16.9      54
    #   A1-section          section        0.481  0.759  0.796  0.093  0.663    13.6      52
    #   A2-heading-prefix   section+prefix 0.500  0.796  0.796  0.093  0.690    13.7      53
    #   A3-hybrid           section+prefix 0.537  0.778  0.870  0.104  0.751  (재사용)     70
    #   A4-rerank           section+prefix 0.704  1.000  1.000  0.126  0.901  (재사용)   9457
    #
    # 섹션 청킹은 이 코퍼스에서 **공짜였다** — 지표가 오르고 재색인이 오히려
    # 빨라졌으며(16.9 → 13.6초) 질의 지연은 같다. 청크 개수도 60 으로 동일해서,
    # 이득이 "청크가 작아져서"가 아니라 경계가 옳아서임을 알 수 있다.
    #
    # 헤딩 접두사는 **R@10 을 전혀 안 움직였는데 R@5 +0.037 · MRR +0.027 이다.**
    # R@10 대역은 이미 포화라 거기서는 안 보였을 뿐이다. R@10 만 봤다면 "효과
    # 없음"으로 기각했을 것이고 그게 틀린 결론이다 — 커버리지가 아니라 순위를
    # 고치는 손잡이다.
    #
    # ⚠️ **사다리가 단조가 아니다.** A3-hybrid 칸에서 R@5 가 떨어진다
    # (0.796 → 0.778). 같은 칸에서 R@1 +0.037 · R@10 +0.074 · MRR +0.061 로
    # 나머지는 전부 오른다 — 하이브리드가 정답을 더 깊이 끌어오면서 4~5위
    # 대역을 흐트러뜨린다. sparse 의 literal 오답이 RRF 에서 상위 가중치를 받기
    # 때문이고, M2 의 D18 실험에서 이미 본 모양이다(그때는 semantic R@1 이
    # 1.00 → 0.75 로 퇴행했다). 두 마일스톤 뒤 다른 코퍼스에서 독립적으로
    # 재현됐고, 그때처럼 다음 칸의 리랭커가 전 대역을 1.000 으로 복구한다.
    # "리랭커는 선택이 아니라 필수"라는 M2 의 결론이 여기서도 유지된다.
    #
    # ⚠️ 그런데도 기본값은 fixed 다. 두 가지 이유이고 둘 다 측정과 무관하다:
    # (1) 이 숫자는 합성 코퍼스 하나·채점 27문항에서 나왔다. 이 저장소는 코퍼스
    #     하나로 일반화했다가 두 번 뒤집혔다(RERANK_MAX_CHARS, CAND_K — 위
    #     cand_k 주석 참고). 실제 업무 문서에서 재확인되기 전까지는 후보다.
    # (2) 청킹을 바꾸면 **이미 색인된 모든 문서를 다시 색인해야** 이득이 생긴다.
    #     설정 한 줄이 아니라 마이그레이션에 가까운 작업이라, 근거가 코퍼스
    #     하나일 때 기본값으로 밀 것이 아니다.
    chunk_strategy: str = "fixed"  # fixed | section
    # 청크 앞(임베딩 입력에만)에 "명세 > 학생 > 목록 조회" 를 붙인다. 저장되는
    # content 는 건드리지 않는다 — content 가 원문의 축자 부분문자열이라는
    # 계약 위에 인용 스니펫과 eval/gold.py 의 스팬 역매칭이 서 있다.
    chunk_heading_prefix: bool = False

    # Retrieval
    top_k: int = 8
    min_score: float = 0.2

    # Storage (M1: local disk)
    storage_dir: str = "./storage"

    # Operational statistics endpoint. Empty disables it entirely — the route
    # then 404s rather than 401s, so an unconfigured deployment does not
    # advertise that an admin surface exists at all.
    admin_token: str = ""

    # Token spend accounting for scripts/cost_report.py. Rates default to 0 —
    # this project does not guess what the provider charges, because a made-up
    # number in a cost alert is worse than no alert. Fill them in from the
    # provider's pricing page, per one million tokens.
    cost_per_mtok_in: float = 0.0
    cost_per_mtok_out: float = 0.0
    cost_currency: str = "USD"
    # Spend over the report window that should trip an alert. 0 disables.
    cost_ceiling: float = 0.0

    # --- M3: auth & tenant isolation ---
    session_cookie_name: str = "session_id"
    session_ttl_days: int = 14
    # MUST be true in production — a session cookie without Secure can be sent
    # over plain HTTP. Left false so local http://localhost development works.
    # 기본을 True 로 둔다. 잊었을 때 안전한 쪽으로 실패해야 한다 — 반대로
    # 두면 공개 배포에서 세션 쿠키가 평문 HTTP 로도 나가는데 아무 증상이 없다.
    # 이 값이 True 여도 브라우저는 http://localhost 를 신뢰 가능한 출처로
    # 취급하므로 로컬 개발은 그대로 된다. 평문 HTTP 로 외부에 노출하는
    # 예외적인 경우에만 false 로 내린다.
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "lax"
    # M5 frontend runs on its own origin and must send the session cookie, so
    # credentials are allowed — which forbids a "*" origin. List them exactly.
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # --- M3 Phase 2: abuse & cost defense ---
    # Rate limits are per-process token buckets (see services/ratelimit.py):
    # correct for a single instance, multiplied by N behind N workers.
    rate_limit_query_per_min: int = 20
    rate_limit_upload_per_min: int = 5
    # 로그인·가입 시도. 업로드·질의에만 제한이 있고 인증에는 없어서, 8자
    # 비밀번호에 무제한으로 시도할 수 있었다.
    rate_limit_auth_per_min: int = 10
    # Quotas are counted in the database, so they hold across processes.
    quota_queries_per_day: int = 200
    quota_upload_pages_per_month: int = 1000
    # usage.acquire_quota_lock 이 잠금을 쥔 김에 이 사용자의 이 kind 에 대해
    # settled_at 이 없는(예약 중) 채로 이 시간보다 오래된 usage_events 행을
    # 지운다 — reserve() 를 심은 프로세스가 commit_reservation/
    # release_reservation 에 이르기 전에 죽었다는 뜻이라, 그 슬롯을 영원히
    # 갉아먹게 둘 이유가 없다(미인증 계정의 맛보기 한도는 창이 없어 굴러
    # 넘어가지 않으므로 특히 그렇다). 진행 중인 정상 요청보다 짧게 잡으면
    # 그 요청의 예약을 다음 동시 요청이 스윕해 지워버려 쿼터가 새는 쪽으로
    # 사고가 난다 — 값은 반드시 "가장 긴 정상 요청"보다 넉넉히 커야 한다.
    # embed()/rerank() 는 각각 HTTP 클라이언트 타임아웃이 120초(하드코딩,
    # rerank.py/embeddings.py)이고 한 요청 안에서 순서대로 둘 다 겪을 수
    # 있다 — 240초. 여기에 LLM 호출 실측치(34~40초)를 더하면 최악의 "그래도
    # 정상적으로 끝나는" 요청이 300초 안팎이 된다. 600초는 그 두 배에 가까운
    # 여유이면서도, 진짜 죽은 예약이 다음 사용까지 10분 넘게 계정을 잠그지는
    # 않는다.
    reservation_ttl_seconds: int = 600
    # Upload guards, checked before any embedding spend.
    max_upload_mb: int = 50
    max_upload_pages: int = 500
    # Semantic cache: reuse a past answer when a new question is near-identical.
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.95

    # --- 이메일 인증 ---
    # console 은 링크를 로그로만 찍는다. RESEND_API_KEY 가 비어 있으면
    # resend 로 설정돼 있어도 console 로 내려온다 — 키 없이 clone 해도
    # 앱이 뜨고 테스트가 돌아야 하기 때문이다.
    mail_provider: str = "console"
    resend_api_key: str = ""
    # 도메인이 없으면 Resend 는 이 주소로만 보낼 수 있고, 수신도 계정
    # 소유자 본인에게만 전달된다. 공개 배포 시 no-reply@<도메인> 으로 바꾼다.
    mail_from: str = "onboarding@resend.dev"
    # 인증 링크가 가리키는 프론트엔드 오리진. 백엔드가 아니다 — 메일 링크는
    # /verify 페이지로 가고 그 페이지가 POST 를 친다.
    app_base_url: str = "http://localhost:3000"
    verify_token_ttl_hours: int = 24
    # 미인증 계정의 맛보기 한도. 기존 쿼터와 달리 **계정 수명 전체 누적**이다.
    # "하루 5회"로 두면 미인증 계정이 매일 5회씩 영원히 쓸 수 있어, 막으려던
    # 재가입 어뷰즈가 그대로 통과한다. 0 이면 게이트를 끈다.
    unverified_quota_queries: int = 5
    unverified_quota_documents: int = 1
    # 문서 개수만 세면 500쪽짜리 PDF 한 장으로 "문서 1개" 한도를 다 채우면서
    # 그 안에서 500쪽 분량의 BGE-M3 임베딩을 태울 수 있다 — 비용은 문서
    # 수가 아니라 쪽수에 비례하는데 문서 게이트는 쪽수를 전혀 보지 않는다.
    # 그래서 별도 쪽수 상한을 둔다. 0 이면 게이트를 끈다.
    unverified_quota_pages: int = 50
    # 인증 메일 재발송. 자기 메일함만 채우는 행위지만 Resend 무료 한도가
    # 하루 100통이라 태울 수 있다.
    rate_limit_verify_resend_per_min: int = 1

    # --- M7 W2: MCP 서버 ---
    # 별도 프로세스가 아니라 같은 FastAPI 앱에 마운트된다. 검색 파이프라인과
    # 세션 테이블을 그대로 쓰는 것이 목적이므로, 프로세스를 나누면 DB 연결과
    # 설정을 두 벌 유지하게 되고 "두 소비자가 갈라지지 않게" 하려던
    # services/pipeline.py 의 존재 이유가 약해진다.
    #
    # 끌 수 있어야 한다. 실사용(완료 기준: 하루 붙여 보기) 중에 문제가 나면
    # 배포를 되돌리는 것이 아니라 환경변수 하나로 내려야 한다. False 면
    # 마운트 자체를 하지 않으므로 경로가 404 다 — admin_token 이 비었을 때
    # 관리 라우트를 404 로 두는 것과 같은 판단이다(존재를 광고하지 않는다).
    mcp_enabled: bool = True
    # Streamable HTTP 엔드포인트 경로. 이 값은 **서브앱 안에서의** 경로이고,
    # 서브앱은 루트("/")에 맨 마지막으로 마운트된다 — 왜 그렇게 하는지는
    # app/main.py 의 주석에 있다. Caddy 뒤에서는 handle_path /api/* 가
    # 접두사를 떼므로 공개 URL 이 https://<도메인>/api/mcp 가 된다.
    mcp_path: str = "/mcp"
    # DNS 리바인딩 보호(Host/Origin 검사). SDK 는 transport_security 를 주지
    # 않으면 host 기본값("127.0.0.1")을 보고 로컬호스트만 허용하고, host 에
    # "0.0.0.0" 을 주면 보호를 **조용히** 꺼버린다. 그래서 우리는 항상
    # 명시적으로 만들어 넘긴다 — "설정을 안 했더니 꺼져 있더라"가 구조적으로
    # 불가능해야 한다.
    #
    # 비워 두면 로컬호스트만 허용한다. 로컬 개발은 그대로 되고, 공개 배포는
    # 421 Invalid Host header 로 **시끄럽게** 실패한다. 반대로(비었을 때 전부
    # 허용) 두면 배포에서 아무 증상 없이 보호가 꺼진 채로 돌아간다.
    # session_cookie_secure 와 같은 판단이다 — 잊었을 때 안전한 쪽으로.
    #
    # Caddy 는 원래 Host 를 그대로 넘기므로 배포에서는 공개 도메인을 넣는다:
    #   MCP_ALLOWED_HOSTS=["docs.example.com"]
    # 끝에 ":*" 를 붙이면 포트가 무엇이든 허용한다(SDK 의 와일드카드 문법).
    mcp_allowed_hosts: list[str] = []
    # Origin 은 없을 수도 있고(같은 출처 요청·비브라우저 클라이언트), 없으면
    # SDK 가 통과시킨다. Claude/Cursor 는 브라우저가 아니라 Origin 을 보내지
    # 않으므로 보통 비워 둔 채로 동작한다 — 브라우저에서 직접 붙일 때만 채운다.
    mcp_allowed_origins: list[str] = []
    # tools/list 응답에 실어 보낼 캐시 힌트(wire 이름 ttlMs). 우리 툴 목록은
    # 배포할 때만 바뀐다. 60초면 한 에이전트 세션 안에서 반복되는 tools/list
    # 를 사실상 없애면서도, 배포 뒤 낡은 목록을 들고 있는 시간을 1분으로
    # 묶는다. 0 이면 캐시하지 말라는 뜻이다.
    mcp_tools_cache_ttl_ms: int = 60_000
    # RFC 9728 보호 자원 메타데이터 라우트가 광고할 이 서버의 공개 URL.
    # 라우트가 실제로 켜지는지는 이 값이 아니라 mcp_auth_mode 가 정한다 —
    # 근거는 아래 mcp_auth_mode 주석. 켤 때는 이 서버의 공개 URL 을 넣는다:
    #   MCP_RESOURCE_SERVER_URL=https://docs.example.com/api/mcp
    mcp_resource_server_url: str = ""
    # AuthSettings.issuer_url 은 쓰지 않아도 필수·non-nullable 이다. session
    # 모드에서 우리는 토큰을 발급하지 않고 기존 세션 행을 재사용하므로 이 값은
    # 아무 데도 실려 나가지 않는다(resource_server_url 이 None 이라 메타데이터
    # 라우트가 없다). 유효한 URL 이기만 하면 되고, .invalid 는 절대 해석되지
    # 않는 예약 TLD(RFC 2606)라 실수로 누가 이 주소를 찔러볼 수도 없다.
    # oauth/both 모드에서는 이 값을 쓰지 않고 mcp_oauth_issuer 가 대신한다.
    mcp_issuer_url: str = "https://mcp.docs-rag.invalid/"

    # --- M7 W7: OAuth 리소스 서버 ---
    # **이 서버는 토큰을 발급하지 않는다.** 인가 서버를 직접 만드는 것은 W7 이
    # 명시적으로 범위 밖에 둔 일이라, 여기 있는 것은 전부 "남이 발급한 토큰을
    # 어떻게 검증할 것인가"뿐이다.
    #
    # 검증기가 둘이다. 어느 것을 쓸지 이 값이 정한다:
    #
    #   session  세션 행 베어러만 받는다. M7 W2 가 만든, 지금 유일하게 실제로
    #            동작하는 경로다(Authorization: Bearer <session_id>).
    #   oauth    외부 IdP 가 발급한 JWT 만 받는다. 세션 베어러는 거절된다.
    #   both     JWT 를 먼저 시도하고, 아니면 세션으로 해석한다. IdP 를 붙이는
    #            동안의 이행 구간용이다.
    #
    # **기본이 session 인 이유.** 두 방향의 사고를 견줬다. (가) oauth 를 기본
    # 으로 두면, IdP 설정이 비어 있는 클론은 아무 토큰도 통과시키지 못해 MCP 가
    # 그냥 죽는다 — 시끄럽지만 지금 붙어 있는 클라이언트를 전부 끊는다.
    # (나) session 을 기본으로 두면 오늘 동작하는 것이 계속 동작한다. 위험한
    # 방향은 "IdP 토큰을 검증하고 있다고 믿는데 사실은 아무거나 받는" 상태인데,
    # 그건 이 기본값으로는 생길 수 없다: oauth/both 를 **켰는데** 설정이 모자라면
    # 앱이 기동 자체를 거부한다(app/mcp/oauth.py 의 validate_settings). 즉 조용히
    # 약해지는 경로가 없으므로 기본은 "오늘 되는 것"으로 둔다.
    #
    # RFC 9728 메타데이터와 401 의 WWW-Authenticate resource_metadata= 도 이
    # 값에 매달아 뒀다. session 모드에서는 가리킬 인가 서버가 없으므로 켜지
    # 않는다 — 없는 인가 서버를 광고하면 클라이언트는 쓸 수 있는 세션 베어러
    # 대신 실패할 OAuth 디스커버리로 끌려간다.
    mcp_auth_mode: str = "session"
    # 토큰을 발급한 인가 서버. iss 클레임과 정확히 같은 문자열이어야 한다
    # (RFC 8414 는 문자열 비교를 요구한다 — 끝의 / 하나가 검증을 깨뜨린다).
    mcp_oauth_issuer: str = ""
    # 서명 키를 가져올 JWKS 문서. 로컬 검증이라 요청마다 IdP 를 때리지 않는다.
    mcp_oauth_jwks_url: str = ""
    # aud 클레임이 이 값과 같아야 한다 = "우리 서버용으로 발급된 토큰인가".
    # 비워 두면 mcp_resource_server_url 을 쓴다. RFC 8707 에서 리소스 지시자는
    # 곧 리소스 서버의 URL 이므로 둘이 같은 것이 정상이고, 따로 둘 수 있게
    # 해 둔 것은 aud 를 다른 값으로 쓰는 IdP 가 있기 때문이다.
    mcp_oauth_audience: str = ""
    # 허용 서명 알고리즘. **목록으로 못박는 것이 요점이다** — 토큰의 alg 헤더를
    # 그대로 믿으면 alg=none 이나 HS256 혼동 공격이 열린다.
    mcp_oauth_algorithms: list[str] = ["RS256"]

    # --- M7 W7: 관측성(OpenTelemetry) ---
    # 기본이 False 이고, False 는 "싸다"가 아니라 **no-op** 이다. TracerProvider
    # 를 아예 설치하지 않으므로 opentelemetry-api 의 프록시가 무효 스팬을
    # 돌려주고, 우리 헬퍼들은 전부 거기서 빠져나간다(app/services/otel.py).
    # 수집기가 없는 클론에서 앱이 정상 동작해야 한다는 것이 W7 의 요구다.
    otel_enabled: bool = False
    otel_service_name: str = "docs-rag"
    # console | otlp. otlp 는 opentelemetry-exporter-otlp-proto-http 를 따로
    # 설치해야 한다 — requirements.txt 에 넣지 않은 이유는 protobuf/requests 를
    # 끌고 오는데 계측을 켜는 배포에서만 필요하기 때문이다. 없으면 기동 시
    # 에러 로그를 남기고 계측 없이 계속 간다(관측이 관측 대상을 내리지 않는다).
    otel_exporter: str = "console"

    # --- M7 W6: L2 judge (로컬 생성 모델) ---
    # 평가 전용이다. 애플리케이션 경로는 이 값들을 하나도 읽지 않는다 —
    # eval/judge_local.py 만 읽는다. 그럼에도 여기 두는 이유는 .env 하나로
    # 환경이 서술돼야 하기 때문이고, eval_llm_model 이 이미 같은 자리에 있다.
    #
    # **judge 가 로컬인 이유는 두 가지가 겹쳐서다.** W6 은 피평가 모델과 다른
    # 계열을 쓰라고 하고(생성은 Gemini), M7 개요의 미결정 항목은 "judge용 외부
    # LLM 호출에 문서 청크를 넣어도 되는지"를 아직 못 정했다. 로컬 judge 는
    # 앞을 만족하면서 뒤를 없앤다 — 청크가 이 맥을 떠나지 않는다. BGE-M3 를
    # 로컬로 돌리는 근거와 같은 문장이다.
    judge_provider: str = "ollama"
    judge_base_url: str = "http://127.0.0.1:11434"
    # Qwen 계열을 쓴다. 같은 맥에 gemma3 가 이미 받아져 있지만 Gemma 는 Gemini
    # 와 같은 구글 계보라, W6 이 피하라고 한 자기채점에 가장 가까운 선택이 된다.
    # 4B(Q4_K_M, 디스크 2.5GB · num_ctx 8192 에서 상주 3.9GB)인 것은 16GB 에
    # BGE-M3 + 리랭커 6.9GB 가 이미 상주하기 때문이다. 실측 2026-09-14:
    # 로드 2.2초, 로드 포함 첫 판정 9.7초, 이후 판정 3.5~8.4초(중앙값 5.4초).
    # ⚠️ 작은 모델은 사람과의 일치도가 낮고 우리는 아직 그것을 재지 않았다 —
    # 자세한 것은 eval/judge_local.py 의 모듈 docstring.
    judge_model: str = "qwen3:4b"
    # 첫 호출은 가중치 로드를 함께 치른다(실측 2.2초). 긴 컨텍스트 문항이
    # CPU 로 떨어지는 경우까지 감안한 상한이다.
    judge_timeout_s: float = 180.0
    # 컨텍스트 3청크 + 루브릭 + 답변이 들어간다. 넘치면 ollama 가 조용히 앞을
    # 잘라내는데, 잘려 나가는 것은 프롬프트 앞쪽 = 루브릭이다. 루브릭 없는
    # 판정은 이 설계의 전제를 통째로 잃은 판정이라 넉넉히 잡는다.
    judge_num_ctx: int = 8192
    # 스윕 사이에 모델을 메모리에 남겨 둘 시간. 0 으로 두면 문항마다 2.2초씩
    # 로드를 다시 치른다.
    judge_keep_alive: str = "5m"


settings = Settings()

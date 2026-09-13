# 배포 설정 리포트 (M6 — 실제 호스트로 넘어가기)

작업 기준 커밋: `6b617d2` (main). 이 리포트를 쓰기 시작한 시점의 HEAD다 —
`app/config.py`를 다른 에이전트가 동시에 건드리고 있어(`6b617d2 tune(retrieval):
CAND_K 50 -> 20`) 작업 중 여러 번 최신화됐다. 아래 내용은 그 이후 상태 기준.

범위: 설정과 문서만. 아무것도 배포·기동·provision 하지 않았다. `app/`·`web/`
애플리케이션 코드와 `app/config.py`·`.env.example`은 건드리지 않았다(경계
지시대로 — 후자 둘은 다른 에이전트 소유).

---

## 요약

- `docker-compose.yml`, `Caddyfile`, `README.md`를 고쳤다. 커밋 SHA는 이
  리포트 끝의 "커밋" 참고.
- 가장 중요한 발견: **GPU 프로파일(`--profile gpu`)이 리랭킹을 전혀
  가속하지 못하고 있었다.** 임베딩용 TEI 컨테이너 하나만 있고, 리랭커는
  기본으로 항상 뜨는 CPU `models` 컨테이너를 계속 썼다 — GPU 호스트에서도
  실제 병목(리랭킹)은 그대로 CPU였다는 뜻이다. `tei-rerank` 서비스를
  추가하고 `EMBED_MODEL_URL`/`RERANK_MODEL_URL`로 임베딩·리랭킹 목적지를
  독립적으로 지정할 수 있게 고쳤다.
- 공개 호스트 관점에서 2건을 더 고쳤다: `tei` 포트가 0.0.0.0에 열려
  있었던 것(db와 같은 패턴으로 루프백 바인딩), 모든 서비스에 재시작
  정책이 없었던 것(`restart: unless-stopped` 추가). Caddy에 접근 로그를
  한 줄 추가했다.
- 설정으로 못 고치는 것 3가지를 찾았다 — 가로 확장 시 레이트리밋 무력화,
  업로드 원본 백업 부재, 리랭크 타임아웃(120초) 하드코딩. 아래 상세.
- `MAIL_PROVIDER`/`APP_BASE_URL`은 이미 이전 라운드(`fd7e3d9`)에서 문서에
  들어가 있었다 — 이번엔 전체 설정을 한자리에 모으고, mailer의 경고와
  "같은 대우"가 필요해 보이는 설정 2개를 발견해 제안으로만 남겼다(코드
  작성 안 함, 아래 4절).
- `docker compose config`(기본·`--profile gpu` 둘 다) 정상 렌더링 확인.
- 테스트: 이전 168 passed / 1 failed → 이후 169 passed / 0 failed (아래
  6절 — 실패했던 1건은 내 변경과 무관).

---

## 1. 배포 경로 — 무엇이 어디서 도는가

README에 "실제 호스트에 배포하기" 절을 새로 넣었다(`## 운영` 아래, "백업과
복구 리허설" 앞). 핵심만 요약하면:

| | 맥(로컬, 지금 기본값) | 리눅스 + GPU | 리눅스, GPU 없음 |
| --- | --- | --- | --- |
| db·app·web·proxy | 컨테이너 | 컨테이너 | 컨테이너 |
| 임베딩(BGE-M3) | **호스트** MPS (`scripts/local_model_server.py`) | 컨테이너 `tei` | 컨테이너 `models`(CPU) |
| 리랭킹(bge-reranker-v2-m3) | **호스트** MPS(같은 프로세스) | 컨테이너 `tei-rerank`(신설) | 컨테이너 `models`(CPU) — 정직하게 위험함, 아래 |

### 발견: GPU 프로파일이 리랭킹을 가속하지 않고 있었다

`docker-compose.yml`의 `tei` 서비스(`--profile gpu`)는 `BAAI/bge-m3`
임베딩 모델만 서빙한다. 그런데 `app` 서비스의 `TEI_URL`/`RERANK_URL`은
둘 다 하나의 `MODEL_URL` 변수로 정해지는 구조였다:

```yaml
TEI_URL: ${MODEL_URL:-http://models:8081}
RERANK_URL: ${MODEL_URL:-http://models:8081}
```

즉 `--profile gpu`를 켜고 `MODEL_URL=http://tei:80`으로 두 값을 다
`tei`에 몰아주면, 리랭킹 요청이 임베딩 전용 TEI 인스턴스로 가서 실패하고
(`/rerank` 엔드포인트가 그 모델에 없음), 반대로 `MODEL_URL`을 안 건드리면
리랭킹은 안내 없이 계속 기본으로 뜨는 `models`(CPU) 컨테이너에서 돈다 —
어느 쪽이든 **GPU 호스트에서도 리랭킹은 가속되지 않는다.** README가
"NVIDIA 호스트라면 `docker compose --profile gpu up`으로 TEI를 대신
쓴다"라고 썼던 것은 이 지점에서 정확하지 않았다.

원인: TEI는 인스턴스 하나당 모델 하나만 서빙한다. 임베딩(BGE-M3)과
리랭킹(bge-reranker-v2-m3)은 서로 다른 모델이라 TEI 컨테이너가 하나로는
둘 다 못 한다(호스트의 `scripts/local_model_server.py`는 반대로 프로세스
하나에서 두 모델을 다 올리는데, TEI는 그 구성을 흉내 낼 수 없다). 이건
새 문제가 아니라 GPU 프로파일이 처음 생긴 커밋(`fc7d6d4`, `40aa06f` 보안
라운드보다 이전)부터 있던 구멍이다 — 보안 라운드가 기본 경로(CPU/db)를
다뤘고 `--profile gpu`는 대상이 아니었을 것이다.

**고침:** `tei-rerank` 서비스를 추가했다(같은 이미지,
`--model-id BAAI/bge-reranker-v2-m3`). `app`의 환경변수를 다음과 같이
바꿔 임베딩·리랭킹 목적지를 독립적으로 줄 수 있게 했다(기존 `MODEL_URL`
동작은 그대로 유지 — 맥에서 호스트 서버 하나가 둘 다 맡는 경우는 안
바뀐다):

```yaml
TEI_URL: ${EMBED_MODEL_URL:-${MODEL_URL:-http://models:8081}}
RERANK_URL: ${RERANK_MODEL_URL:-${MODEL_URL:-http://models:8081}}
```

GPU 호스트에서 쓰는 법:
```bash
EMBED_MODEL_URL=http://tei:80 RERANK_MODEL_URL=http://tei-rerank:80 \
  docker compose --profile gpu up -d
```
`docker compose config`로 중첩 보간(`${A:-${B:-default}}`)이 실제로
해석되는지, 이 값들이 우선순위대로 먹히는지 둘 다 확인했다(6절).

### GPU 없는 리눅스 — 정직한 결론

**이 구성은 GPU 없는 리눅스에서 실제 크기 문서를 안정적으로 리랭킹한다는
보장을 못 한다.** `models` 컨테이너는 뜨고 응답하지만:

- 184초(청크 50개, CAND_K 50 기준 — compose 주석의 실측치)라는 숫자는
  이 저장소가 테스트한 **맥의 컨테이너 CPU** 기준이다. 클라우드 범용
  vCPU가 그보다 빠르다는 보장은 없다. 184초는 바닥값이지 최악값이 아니다.
- 클라이언트 타임아웃(120초)이 `rerank.py`·`embeddings.py`에
  `httpx.Timeout(120.0)`로 **하드코딩**돼 있다. 설정(`.env`/Settings)으로
  못 바꾼다 — 느린 호스트에서 완화책으로 늘리고 싶어도 코드를 고쳐야
  한다.
- 유일한 손잡이는 `CAND_K`를 낮추는 것(리랭킹 비용이 후보 수에 선형)뿐이고,
  낮추면 R@1 천장도 낮아진다. `RERANK_MAX_CHARS`로 입력을 자르는 것은
  대안이 아니다 — 이미 측정으로 품질이 깨진다고 문서화돼 있다.

이건 설정으로 해결할 수 있는 문제가 아니라서 README에도 "정직한 결론"으로
그대로 적었다. GPU가 없는데 실 사용을 계획한다면, 코퍼스가 작다는 확신이
있지 않은 한 이 경로는 권하지 않는다.

---

## 2. 공개 호스트 위협 모델 — compose · Caddyfile

`40aa06f`가 이미 db 포트 루프백 바인딩·기본 비밀번호 경고·세션 쿠키
Secure·인증 레이트리밋 네 가지를 고쳤다. 그 위에 이번에 발견/수정한 것과,
찾았지만 설정만으로는 못 고치는 것을 나눠 적는다.

### 고친 것

1. **`tei` 포트가 0.0.0.0에 열려 있었다.** `ports: ["8080:80"]` — db가
   `40aa06f`에서 고쳐진 것과 똑같은 모양의 구멍이다(다만 `tei`는 그 커밋
   *이전*에 생겼고, 그 라운드는 기본 경로를 다뤘지 `--profile gpu`는
   범위 밖이었을 것이다). 앱은 compose 네트워크로 이 서비스에 닿으므로
   host publish는 순전히 호스트에서 직접 찔러볼 때의 편의다. db와 같은
   패턴으로 `127.0.0.1:${TEI_PORT:-8080}:80`으로 좁혔다. 새로 추가한
   `tei-rerank`도 처음부터 루프백으로(`127.0.0.1:${TEI_RERANK_PORT:-8082}:80`).

2. **재시작 정책이 어느 서비스에도 없었다.** 호스트가 재부팅되거나
   컨테이너가 죽으면 아무것도 자동으로 안 돌아온다 — 로컬 개발에서는
   증상이 없지만 공개 호스트에서는 그대로 다운타임이다. 7개 서비스
   전부(`db`·`models`·`tei`·`tei-rerank`·`app`·`web`·`proxy`)에
   `restart: unless-stopped`를 추가했다. 이 정책은 `docker compose down`
   같은 명시적 중지에는 반응하지 않으므로 로컬 워크플로(`stack.sh
   down`/`up`, 백업 절차의 `docker compose stop app` → `start app`)와
   충돌하지 않는다 — 죽거나 재부팅됐을 때만 반응한다.

3. **Caddy에 접근 로그가 없었다.** 사고 대응 때 "누가·언제·무엇을"이
   전혀 안 남아 있었다. 인자 없는 `log` 지시어 한 줄을 사이트 블록에
   추가했다 — 기본 출력(stderr)이 `docker compose logs proxy`로 그대로
   보인다. 파일·로테이션 설정은 안 건드렸다(호스트의 로그 드라이버
   설정에 맡기는 게 맞다고 판단).

### 확인했고 안전하다고 판단해 안 건드린 것

- `.env`·`backups/`·`storage/`가 전부 `.gitignore`에 있고, `git log --all`
  로 과거에도 커밋된 적이 없음을 확인했다(실제 비밀값·업로드 원본이 저장소
  히스토리에 없다).
- `.dockerignore`/`web/.dockerignore`가 `.env*`를 이미지 빌드 컨텍스트에서
  제외한다. `web/Dockerfile`은 멀티스테이지라 `builder` 단계가 무엇을
  복사하든 최종 `runner` 이미지엔 `standalone`/`static`/`public`만 남는다.
- `ADMIN_TOKEN` 기본값(빈 값 → 404)과 `SESSION_COOKIE_SECURE` 기본값
  (`true`)은 이미 안전한 기본값이다 — README 새 절에 "바꾸지 말 것"으로
  명시했다.
- `app` 서비스의 `FORWARDED_ALLOW_IPS: "*"`는 이 서비스가 compose
  네트워크에만 열려 있다는 전제로만 안전하다 — 기존 주석이 이미 "포트를
  직접 publish하면 먼저 좁힐 것"이라고 경고하고 있어 추가로 건드리지
  않았다.
- Caddy 설정(`caddy validate`)은 별도 컨테이너 실행이 필요해서 돌리지
  않았다 — "서버를 띄우지 말 것" 경계를 문자 그대로 지켰다. `log` 지시어는
  Caddy 2 표준 문법으로, 인자 없이 쓰면 사이트 블록 전체에 적용되고
  기본 출력은 stderr다(수동 검토로 확인). 배포 전에 `caddy validate`나
  실제 기동으로 한 번 더 확인하길 권한다.

### 보고만 하는 것 — 설정만으로는 못 고침

1. **가로 확장 시 레이트리밋·쿼터가 조용히 무력화된다.**
   `RATE_LIMIT_*`은 프로세스 메모리 토큰버킷이다
   (`app/services/ratelimit.py` 주석에 이미 명시). `uvicorn --workers N`이나
   `docker compose up --scale app=N`으로 늘리면 버킷도 N개가 되어 한도가
   그만큼 느슨해지는데 에러도 로그도 없다. 지금 `Dockerfile`의 CMD는
   worker 1개 고정이라 기본값 그대로는 문제가 없지만, 트래픽 대응으로
   늘리는 순간 발생한다. 고치려면 버킷을 프로세스 밖(Redis 등)으로 옮기는
   코드 변경이 필요하다 — 설정 파일 몇 줄로 되는 일이 아니라서 여기서는
   README에 경고만 남기고 구현하지 않았다.

2. **업로드 원본(`uploads` 볼륨)에 백업이 없다.** `scripts/db_backup.sh`는
   Postgres(`pgdata`)만 백업한다. `uploads`가 사라지면 검색·답변은 DB에
   남은 청크로 계속되지만 원본 PDF는 영영 사라지고, 재청킹·재색인이
   불가능해진다. README 마일스톤의 "M6 남은 것"(로컬 볼륨 → 오브젝트
   스토리지)이 바로 이 문제고, 아직 착수 전이다. 이번 작업 범위에서
   백업 스크립트를 새로 만드는 것은 "설정"이 아니라 "구현"이라고 판단해
   손대지 않았다 — 필요하면 별도 작업으로 분리하는 걸 권한다.

3. **리랭크/임베딩 타임아웃(120초)이 코드에 박혀 있다.** 위 1절에서 다룬
   대로 `.env`로 조정할 방법이 지금은 없다. GPU 없는 호스트에서 완화책으로
   쓰려면(타임아웃을 늘려서 느린 응답을 받아주는 대신 느리게라도 성공시키는
   절충) 코드 변경이 필요하다.

---

## 3. 설정 가시성 점검

`MAIL_PROVIDER`·`APP_BASE_URL`은 이미 `fd7e3d9`에서 README에 들어갔고
(빠른 시작 표, 설정 표, "이메일 인증" 절 세 군데), 지금도 그대로 있다 —
이번에 새로 넣을 필요는 없었다. 대신 두 가지를 했다:

1. README에 "배포 전 반드시 바꿔야 하는 값" 표를 새로 만들어, 이 두 값을
   포함해 실제 배포에서 손대야 하는 값 전체(`POSTGRES_PASSWORD`,
   `GEMINI_API_KEY`, `SITE_ADDRESS`, `HTTP_PORT`/`HTTPS_PORT`,
   `MAIL_PROVIDER`, `APP_BASE_URL`, `MAIL_FROM`,
   `EMBED_MODEL_URL`/`RERANK_MODEL_URL`, `ADMIN_TOKEN`,
   `SESSION_COOKIE_SECURE`)을 한자리에서 "기본값 그대로 두면 벌어지는 일"
   형식으로 통일했다. 이 중 `POSTGRES_PASSWORD`는 지금까지 compose
   주석과 `.env.example` 주석에만 있었고 README 표에는 없었다 — 이번에
   처음 README 표에 들어갔다.
2. `app/services/mailer.py`의 `warn_if_base_url_looks_local()`과 "같은
   대우"가 필요해 보이는 설정을 찾았다(4절 — 코드는 작성하지 않고 제안만).

### 검토했지만 낮은 우선순위로 판단한 것

- **`GEMINI_API_KEY` 미설정**은 `app/services/llm.py`의 `_client()`가
  `RuntimeError("GEMINI_API_KEY is not set")`을 던진다(지연 초기화 —
  import 시점엔 키가 없어도 앱이 뜨고 테스트가 돈다, 의도적). 이건 mailer
  케이스와 성격이 다르다: mailer는 **성공한 것처럼 보이면서 조용히
  실패**하지만, 이건 **첫 실제 질의에서 바로, 크게 실패**한다(500, 로그에
  예외 스택). "아무도 못 알아채는 함정"은 아니라서 같은 처방(부팅 시
  경고)이 필수는 아니라고 봤다. 다만 지금은 첫 요청이 와야 발견되니,
  부팅 시점(`lifespan`)에 한 번 더 검사해 "실패를 더 앞으로 당기는" 것은
  운영 경험상 나쁘지 않은 개선이라고 본다 — 우선순위는 낮게 적어 둔다.
- `.env.example`에 `RERANK_TOP`·`RERANK_MIN_SCORE`·`RERANK_MAX_CHARS`·
  `RRF_K`·`HYBRID_ENABLED`가 없다(README·`app/config.py`엔 있다). 배포를
  막는 문제는 아니다 — 전부 안전한 기본값이 코드에 있다 — 하지만 발견성
  문제로 남겨 둔다. (`CAND_K`는 다른 에이전트가 이번에 이미
  `.env.example`에 추가했다.)

---

## 4. `app/config.py` / `.env.example`에 제안하는 것 (코드 작성 안 함)

아래 둘 다 기존 설정 필드만 참조한다 — 새 `Settings` 필드나
`.env.example` 항목이 필요하지는 않다. `app/services/mailer.py`의
`warn_if_base_url_looks_local()`과 나란히, `app.main`의 lifespan에서
호출하는 두 번째(혹은 확장된) 함수 정도로 그릴 수 있을 것 같다.

**제안 A — DB 기본 자격증명이 그대로인 채 배포된 경우.**
```python
if "postgres:postgres@" in settings.database_url:
    logger.warning("DATABASE_URL이 기본 자격증명이다 — 공개 호스트라면 바꿀 것")
```
DB 포트가 루프백에 묶여 있어 위험도는 이미 낮아졌지만(2절), 호스트에
다른 경로가 하나라도 열리면 그다음 문이 이거다. 값 하나 비교라 비용이
싸다.

**제안 B — `MAIL_FROM`이 기본값인 채 "배포로 보이는" 상태에서 발송을 켠
경우.**
```python
if (
    settings.mail_provider == "resend"
    and settings.mail_from == "onboarding@resend.dev"
    and not _looks_local(settings.app_base_url)
):
    logger.warning(
        "MAIL_FROM이 onboarding@resend.dev 그대로다 — 도메인 인증 전엔 "
        "계정 소유자 본인 외에는 메일이 안 간다."
    )
```
이건 `APP_BASE_URL` 죽은 링크 문제와 **다른 실패 모양**이면서 똑같이
위험하다: Resend는 도메인 미인증 상태에서 계정 소유자가 아닌 수신자로의
발송을 거부한다(422류). 신호를 조합해 보면 — **운영자 본인 이메일로
가입해 "메일이 온다"고 확인하는 테스트는 성공해 보인다.** 다른 사람이
처음 가입할 때가 돼서야 들킨다. 지금 `warn_if_base_url_looks_local()`은
이 조합을 못 잡는다(APP_BASE_URL만 본다). 같은 함수를 확장하거나 나란히
하나 더 두는 것을 제안한다.

`.env.example`에는 새 항목이 필요 없다 — 다만 `MAIL_FROM` 줄의 기존 주석에
"운영자 본인 테스트로는 이 문제를 못 잡는다"는 한 줄을 더하면 코드 경고
없이도 발견성이 조금 올라갈 것이다(선택 사항).

---

## 5. 사람만 할 수 있는 일 (순서대로)

README "실제 호스트에 배포하기 → 사람이 해야 하는 일"에 넣은 것과
같다. 여기 다시 정리한다 — 각 항목이 무엇을 열어 주는지까지.

1. **호스트를 고르고 만든다.** 리눅스 + Docker/Docker Compose. GPU 유무가
   1절의 리랭킹 배치를 가른다 — 이후 모든 단계가 이 선택을 전제한다.
2. **도메인을 산다**(선택). 안 사면 TLS 없이 IP로만 돈다.
3. **DNS A(/AAAA) 레코드를 호스트의 공인 IP로 건다.** 이게 먼저 되어
   있어야 4번(방화벽)과 `SITE_ADDRESS`에 도메인을 넣는 것이 의미가
   있다 — Caddy는 그 시점에 도메인이 실제로 이 호스트를 가리키는지
   확인하고서야 인증서를 발급받는다.
4. **방화벽/보안그룹에서 80·443을 연다.** 인증서 발급과 이후 모든 트래픽이
   이 두 포트로 온다. `5432`·`8080`·`8082`는 compose가 루프백으로 묶어
   놓았으니 추가로 막을 필요는 없지만, 클라우드 보안그룹 기본값이 더
   넓게 열려 있는 경우가 있어 실제로 확인해야 한다.
5. **`GEMINI_API_KEY`를 발급받는다**(Google AI Studio). 없으면 첫 질의부터
   막힌다 — 3절 참고.
6. **실제 메일 발송이 필요하면** Resend 계정을 만들고 발신 도메인을
   등록해 그 도메인의 DNS에 SPF/DKIM을 추가하고 검증을 기다린다. 검증
   전엔 `console`로 두거나, `resend`를 켜더라도 4절의 제안 B 문제를 그대로
   맞는다는 것을 알고 있을 것.
7. **`ADMIN_TOKEN`을 쓰려면** 무작위 문자열을 하나 만든다
   (`openssl rand -hex 32` 등).
8. **DB 백업을 호스트 밖으로 내보낸다.** `scripts/db_backup.sh`는 같은
   호스트에 쌓는다 — 호스트 자체가 사라지는 사고엔 대비가 안 된다.
   오프호스트 사본은 사람이 별도로 만들어야 한다(2절의 미해결 항목과
   연결 — `uploads`는 이 백업 스크립트로도 커버되지 않는다는 점도 함께).

1·3·5는 이 저장소 밖의 일이라 설정으로 대신할 수 없다. 나머지는 `.env`를
채우는 일이지만 순서가 있다 — 6을 건너뛰고 발송만 켜면 4절 제안 B의
상황을 실제로 맞는다.

---

## 6. 검증

### `docker compose config`

기본 프로파일, `--profile gpu` 둘 다 오류 없이 렌더링됨(exit 0, stderr
없음). `--profile gpu`에 `EMBED_MODEL_URL`/`RERANK_MODEL_URL`을 셸
환경변수로 주고 확인한 결과, 중첩 보간(`${A:-${B:-default}}`)이 의도대로
해석되어 `TEI_URL`이 `tei:80`을, `RERANK_URL`이 `tei-rerank:80`을 정확히
가리켰다. `tei`·`tei-rerank` 둘 다 `127.0.0.1` 바인딩과 GPU
reservation·`restart: unless-stopped`가 렌더링 결과에 반영됨을 확인했다.
7개 서비스(`db`·`models`·`tei`·`tei-rerank`·`app`·`web`·`proxy`) 전부에
`restart: unless-stopped`가 나타남을 확인했다.

(주의: `docker compose config`는 `.env`를 실제로 보간해 출력하므로 실행
결과에 이 머신의 실제 `GEMINI_API_KEY`·`ADMIN_TOKEN`이 평문으로 찍혔다.
확인만 하고 그 출력은 파기했다 — 이 리포트에도, 어디에도 옮기지
않았다.)

Caddyfile은 `caddy validate`를 돌리지 않았다 — 컨테이너를 띄워야 해서
"서버를 띄우지 말 것" 경계 밖이라고 판단했다. 추가한 `log` 지시어는 수동
검토로 확인한 표준 Caddy 2 문법이다.

### 테스트

| | 결과 |
| --- | --- |
| 작업 전 (`.venv/bin/python -m pytest tests/ -q`) | 168 passed, 1 failed |
| 작업 후 (동일 명령) | 169 passed, 0 failed |

실패했던 1건(`tests/test_verification_api.py::test_verifying_restores_the_normal_quota`)은
실제 `/query` 호출이 끼는 통합 테스트로, 이메일 인증 기능 영역이다 — 이번
작업(`docker-compose.yml`·`Caddyfile`·`README.md`)과 무관하고, 애초에
`app/`·`tests/`를 건드리지도 않았다. 두 번째 실행에서 저절로 통과했다 —
동시에 진행 중인 다른 에이전트의 커밋(`6b617d2` 등)이나 외부 모델/LLM
서버 가용성 같은 공유 상태 때문으로 보인다. 내가 고친 것은 아니며,
이 작업 범위의 회귀는 아니라고 판단했다.

---

## 7. 바뀐 파일

- `docker-compose.yml` — `tei-rerank` 서비스 신설, `tei` 포트 루프백
  바인딩, `EMBED_MODEL_URL`/`RERANK_MODEL_URL` 오버라이드, 전 서비스
  `restart: unless-stopped`.
- `Caddyfile` — 사이트 블록에 `log` 추가.
- `README.md` — "실제 호스트에 배포하기" 절 신설(`## 운영` 아래), GPU
  프로파일 설명 문단 정정.
- `.superpowers/sdd/deploy-report.md` — 이 파일(신규).

`app/config.py`·`.env.example`·`app/`·`web/`은 건드리지 않았다.

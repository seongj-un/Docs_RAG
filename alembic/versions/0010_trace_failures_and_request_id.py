"""traces 에 실패 사유와 요청 id 를 추가한다

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-14

M4 이후로 ``traces`` 에 행을 쓰는 곳은 두 군데뿐이었다 — 캐시 히트
(``record_cache_hit``)와 성공한 질의(``finalize``). 즉 **성공한 질의만**
기록됐다. 503(모델 서버 다운)·500·404 로 끝난 질의는 DB 어디에도 흔적이
없었고, 그 결과가 고약하다: 임베딩 서버가 죽으면 ``/admin/stats`` 의
``queries.total`` 이 **줄어들어** 장애 시간대가 "한가했던 오후"처럼 보인다.
가장 필요할 때 가장 없는 숫자였다.

이 마이그레이션이 그 행을 담을 자리를 만든다.

``status_code`` — 실패로 끝난 요청의 HTTP 상태. **성공한 행에는 안 쓴다.**
그래서 NULL 이 곧 "이 질의는 실패하지 않았다"는 뜻이고, 이 컬럼 하나로
"실패인가"와 "어떻게 실패했나"가 동시에 답해진다. ``failed`` 불리언을
따로 두는 안은 기각했다 — 둘이 어긋날 수 있는 상태를 만들 뿐이다.
smallint 가 아니라 integer 인 이유는 이 테이블의 다른 정수 컬럼
(tokens_*, *_ms) 이 전부 integer 라서다. 진단용 테이블에서 행당 2바이트를
아끼자고 이 파일에 없던 타입을 새로 들이지 않는다.

``error`` — 왜 실패했는지를 **묶이는 형태로**. HTTPException 이면 호출자가
이미 응답으로 받아 본 detail("search unavailable"), 예상 못 한 예외면
클래스 이름만 남긴다. 메시지를 안 남기는 것은 의도다: 질문 원문·파일
이름·업스트림 응답 본문이 그대로 들어올 수 있고, 전체 트레이스백은 이미
앱 로그에 있으며 이제 ``request_id`` 로 이 행과 이어진다.

``request_id`` — 방금 도입된 요청 id(app/logging.py). Caddy·uvicorn·앱 로그
세 갈래는 이 id 로 묶였는데 DB 만 못 묶고 있었다. 길이 제한 없는 text 인
이유는 우리가 만드는 값은 uuid4().hex 32자지만 프록시가 보낸 값도 최대
64자까지 받아들이기 때문이다 — 폭을 못 박으면 나중에 프록시 형식이 바뀐
날 insert 가 실패한다. 인덱스는 부분 인덱스다: 이 컬럼은 로그에서 복사한
id 로 정확히 찾으라고 있는 것이라 인덱스가 없으면 용도 자체가 없고,
0010 이전 행은 전부 NULL 이라 어떤 조회도 그 행들을 원하지 않는다.
**UNIQUE 가 아니다** — 인바운드 id 는 프록시의 상관관계 힌트일 뿐 신원이
아니어서 클라이언트가 같은 값을 반복해 보낼 수 있고, 그러면 unique 인덱스는
두 번째 요청의 트레이스 insert 를 실패시킨다. 관측이 관측 대상을 망가뜨리는
바로 그 모양이다.

기존 행 백필이 없다. 0009 는 기존 행을 전부 정산 완료로 채워야 했지만
(안 채우면 sweep 이 실제 사용량을 지운다) 여기서는 NULL 이 이미 정답이다 —
지금까지 기록된 행은 정의상 전부 성공한 질의이므로 "실패하지 않았다"가
맞고, 요청 id 는 그 시절에 존재하지도 않았다.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("traces", sa.Column("status_code", sa.Integer(), nullable=True))
    op.add_column("traces", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("traces", sa.Column("request_id", sa.Text(), nullable=True))
    op.create_index(
        "traces_request_id_idx",
        "traces",
        ["request_id"],
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )


def downgrade() -> None:
    # 인덱스를 먼저 지운다 — 컬럼을 떨구면 딸려 사라지긴 하지만, 되돌리기가
    # 무엇을 없애는지 명시적으로 적힌 쪽이 읽는 사람에게 정직하다.
    op.drop_index("traces_request_id_idx", table_name="traces")
    op.drop_column("traces", "request_id")
    op.drop_column("traces", "error")
    op.drop_column("traces", "status_code")

"""M7 W5: eval_runs 에 청킹 조건과 비용을 싣는다

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-14

W5 는 네 가지를 A/B 한다 — 청킹 단위, 헤딩 경로 접두사, 하이브리드, 리랭커.
앞의 둘은 ``config`` 가 바꾸는 것이 아니라 **인덱스 자체**를 바꾼다. 같은
"hybrid+rerank" 라도 청킹이 다르면 정답 청크가 다른 실행이고, 조건을 지표와
같은 행에 남기지 않으면 두 숫자를 나란히 놓는 순간 그 비교는 거짓말이 된다.
0011 이 git 커밋과 데이터셋 해시를 같은 행에 남긴 것과 정확히 같은 이유다.

비용 칸(``reindex_seconds`` · ``index_chunks`` · ``index_bytes`` ·
``latency_ms_*``)은 Notion W5 가 "같이 기록할 것"으로 지목한 것들이다. W8
케이스 스터디가 "왜 이 조합인가"를 **비용까지 포함해** 설명해야 하는데, 그때
가서 붙이면 그 시점 이후 실행에만 값이 있고 비교 대상인 예전 실행에는 없다.

전부 nullable 이다. W5 이전에 저장된 실행에는 청킹이라는 개념이 없었고,
``--no-reindex`` 로 돈 실행에는 재인덱싱 시간이 존재하지 않는다. 0 으로
채우는 것은 "0초 걸렸다"는 거짓 기록이 된다 — ``first_gold_rank`` 를 0 이
아니라 NULL 로 둔 것과 같은 판단이다.

``index_bytes`` 만 BIGINT 다. 실제 업무 문서 코퍼스는 본문 바이트가 쉽게
INT 범위를 넘긴다. 인덱스 크기를 실제로 지배하는 것은 청크 **개수**이고
(청크당 1024차원 float 밀집벡터 ≈ 4KB + 희소벡터), 본문 바이트는 그 옆에
두는 참고 값이다.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
# 이 사슬은 main 을 병합하면서 다시 엮였다. main 도 0010 을 썼고(실패 기록·
# 요청 id), 번호가 겹치면 alembic head 가 둘이 되어 `upgrade head` 가 아예
# 거부된다. 그래서 M7 쪽 넷 — eval_runs·trace_source·이 파일·mcp_answer —
# 을 main 의 0010 뒤로 옮겨 달았다. 순서는 그대로고 번호만 밀렸다.
#
# 여기에는 한때 "0012 는 결번이다"라고 적혀 있었다. **지금은 거짓이다** —
# 그 번호는 trace_source 가 쓰고 있고 0010~0014 에 빈 칸은 없다. 다만 그때
# 적은 판단 자체는 그대로 유효하다: 번호를 연속으로 보이게 하려고 아무것도
# 하지 않는 리비전을 끼워 넣지 않는다. 이번에 연속이 된 것은 칸을 메워서가
# 아니라 사슬을 통째로 다시 세었기 때문이다.
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("chunk_strategy", sa.Text()),
    ("heading_prefix", sa.Boolean()),
    ("reindex_seconds", sa.Float()),
    ("index_chunks", sa.Integer()),
    ("index_bytes", sa.BigInteger()),
    ("latency_ms_p50", sa.Float()),
    ("latency_ms_mean", sa.Float()),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("eval_runs", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in reversed(_COLUMNS):
        op.drop_column("eval_runs", name)

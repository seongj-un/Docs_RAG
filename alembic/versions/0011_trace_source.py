"""traces 에 source 를 추가한다 — 어느 소비자가 남긴 트레이스인가

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-14

M7 W2 가 MCP ``search_documents`` 툴을 붙이면서 ``traces`` 의 소비자가 셋이
됐다. 그런데 어느 소비자가 남긴 행인지 말해 주는 컬럼이 없어서, W2 는
``llm_model IS NULL`` 이라는 **파생 표식**으로 버텼다 — 생성 경로는 항상
``llm_model`` 을 채우니 NULL 이면 MCP 라는 논리였다. 맞는 말이지만 우연히
맞는 말이라, 생성 없는 네 번째 소비자가 생기는 순간 조용히 거짓이 된다.
이 컬럼이 그 우연을 사실로 바꾼다.

W4 는 L3 지표(툴 선택 정확도·인자 정확도·호출 수·턴 수)를 MCP 트레이스에서만
뽑아야 하고, W6 는 "서버 생성 vs 클라이언트 에이전트 생성"을 비교한다. 즉
**생성이 어디서 일어났는가**가 W6 의 비교축인데, 그 축은 소비자에서 유도된다:
``query``·``conversation`` 은 서버가 생성하고, ``mcp_search`` 는 청크만 돌려주고
호출한 에이전트가 생성한다. 그래서 이 컬럼은 "생성 위치"가 아니라 **소비자**를
적는다 — 소비자는 기록 시점에 확실히 아는 사실이고 생성 위치는 거기서 유도할
수 있지만, 반대로 "server"/"client" 만 적으면 W4 가 필요로 하는 /query 와
/conversations 의 구분이 영영 사라진다.

값:
    query         POST /query — 단발 HTTP, 서버 생성
    conversation  POST /conversations/{id}/query — 스트리밍 HTTP, 서버 생성
    mcp_search    MCP search_documents — 검색까지만, 에이전트가 생성
    legacy        이 마이그레이션 이전에 쌓인 행 (아래 참조)

W6 가 서버 생성 MCP 툴을 만들면 ``mcp_answer`` 가 붙을 자리가 그대로 있고,
그때 MCP 안에서의 생성 위치 비교(mcp_search vs mcp_answer)까지 같은 컬럼으로
된다. ``mcp_`` 접두사는 "에이전트가 부른 것"을 한눈에 묶으라고 정한 규약이다.

**백필은 ``legacy`` 다.** 기존 행이 전부 서버 생성이라는 것은 확실하다 —
이 마이그레이션 이전에는 MCP 자체가 없었다. 하지만 둘 중 **어느 HTTP
엔드포인트**였는지는 되살릴 수 없다: ``traces`` 에 conversation_id 가 없고,
두 경로가 같은 QueryRunner 로 같은 컬럼들을 채우기 때문에 바이트 단위로
구별되지 않는다. 그래서 ``query`` 로 몰아넣지 않는다 — 그러면 대화에서 온
행들이 단발 질의로 둔갑하고, W4 가 엔드포인트별 호출 수를 셀 때 조용히 틀린
숫자를 낸다. 모르는 것은 모른다고 적는 편이, 아는 척하는 값보다 낫다.
``legacy`` 는 이 마이그레이션 이후로는 두 번 다시 생기지 않는다.

**CHECK 제약을 건다.** 오타가 조용히 새 소스를 만들어내면 이 컬럼의 존재
이유가 사라진다("mcp_serach" 가 통과하면 W4 의 호출 수가 말없이 줄어든다).
Postgres 네이티브 ENUM 이 아니라 CHECK 인 이유는 W6 가 값을 **더할** 것이기
때문이다 — ``ALTER TYPE ... ADD VALUE`` 는 되돌릴 수 없고 트랜잭션 안에서
못 도는 반면, CHECK 는 드롭하고 다시 걸면 그만이라 마이그레이션으로 안전하게
왕복한다. W6 는 이 제약을 드롭하고 새 목록으로 다시 걸면 된다.

**백필 뒤 server_default 를 뗀다.** 기본값을 남겨 두면 앱 밖에서 들어온 INSERT
(psql, 스크립트)가 조용히 ``query`` 로 기록된다. 떼어 두면 그런 INSERT 는
NOT NULL 위반으로 시끄럽게 실패한다. 앱은 QueryRunner 가 source 를 **필수
인자**로 받으므로 빠뜨릴 수 없다(app/services/pipeline.py).

인덱스는 걸지 않는다. W4/W5 의 조회는 분석용이지 요청 경로가 아니고, 이
테이블은 사용자당 질의 수만큼만 자란다 — 추측으로 인덱스를 만드느니 실제로
느려질 때 근거를 갖고 만드는 편이 낫다.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 이 목록은 app/services/tracing.py 의 TRACE_SOURCES 와 같아야 한다. 값을
# 더하려면 이 제약을 드롭하고 새 목록으로 다시 거는 마이그레이션이 필요하다 —
# 그 번거로움이 곧 "소스를 늘리는 것은 검토를 거친 결정"이라는 뜻이다.
_SOURCES = ("query", "conversation", "mcp_search", "legacy")
_CHECK_NAME = "traces_source_check"


def upgrade() -> None:
    # 1) 먼저 기본값을 달고 컬럼을 붙인다. NOT NULL 을 바로 걸려면 기존 행에
    #    값이 있어야 하는데, 큰 테이블에서 add_column + UPDATE 를 따로 하면
    #    그 사이에 들어온 INSERT 가 NULL 로 새어 들어간다.
    op.add_column(
        "traces",
        sa.Column(
            "source", sa.Text(), nullable=False, server_default="legacy"
        ),
    )

    # 2) 기본값을 뗀다. 이 시점부터 traces 에 INSERT 하려면 source 를 명시해야
    #    한다 — 앱 밖에서 들어온 행이 조용히 'legacy' 가 되지 않게.
    op.alter_column("traces", "source", server_default=None)

    # 3) 값 목록을 스키마에 못박는다.
    values = ", ".join(f"'{s}'" for s in _SOURCES)
    op.create_check_constraint(_CHECK_NAME, "traces", f"source IN ({values})")


def downgrade() -> None:
    op.drop_constraint(_CHECK_NAME, "traces", type_="check")
    op.drop_column("traces", "source")

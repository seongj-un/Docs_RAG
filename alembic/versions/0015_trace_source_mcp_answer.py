"""traces.source 에 mcp_answer 를 더한다 — MCP 안에서의 생성 위치

Revision ID: 0015
Revises: 0013
Create Date: 2026-09-14

0011 이 이 마이그레이션을 예고해 두었다: "W6 가 서버 생성 MCP 툴을 만들면
``mcp_answer`` 가 붙을 자리가 그대로 있고, 그때 MCP 안에서의 생성 위치
비교(mcp_search vs mcp_answer)까지 같은 컬럼으로 된다." W6 가 그 툴
(``answer_question``)을 만들었으므로 그 자리를 채운다.

**왜 이 한 값이 W6 의 핵심 장치인가.** W6 의 비교축은 "생성이 어디서
일어났는가"인데, 리포트의 표는 파일 하나이고 사라진다. 이 컬럼은 사라지지
않는다 — 두 모드가 남긴 트레이스가 ``source`` 로 갈라져 있으면, 오늘 낸
표를 잃어도 토큰·지연·거부를 같은 SQL 로 다시 낼 수 있다. 반대로 두 모드가
같은 값을 남기면 그 비교는 **오늘 이후 영영 복원 불가능**해진다. 0011 이
``llm_model IS NULL`` 이라는 파생 표식을 사실로 바꾼 것과 같은 이유이고, 같은
사고를 W6 에서 한 번 더 막는 것이다.

ENUM 이 아니라 CHECK 인 것이 여기서 값을 한다. 0011 이 그렇게 고른 이유가
"W6 가 값을 더할 것이기 때문"이었고, 이 마이그레이션은 제약을 드롭하고 새
목록으로 다시 걸기만 한다 — ``ALTER TYPE ... ADD VALUE`` 였다면 다운그레이드가
불가능했을 자리에서, 여기서는 downgrade 가 옛 목록을 그대로 되돌린다.

**다운그레이드는 행을 지운다.** 옛 목록으로 제약을 되걸려면 ``mcp_answer``
행이 남아 있으면 안 되고, Postgres 는 위반 행이 있으면 ADD CONSTRAINT 를
거절한다. 그 행들을 ``mcp_search`` 로 바꾸는 것은 **거짓 기록**이다 — 서버가
생성한 질의가 에이전트가 생성한 것으로 둔갑하고, 그것이 바로 이 컬럼이
막으려던 사고다. 그래서 옮기지 않고 지운다. 트레이스는 관측 기록이고
(``tracing.record`` 는 실패해도 요청을 죽이지 않는다), 잘못된 관측보다 없는
관측이 낫다. 지워지는 행 수를 로그로 남긴다.

**0014 는 결번이다.** 0013 이 0012 결번에 대해 적은 판단을 그대로 따른다 —
아무것도 하지 않는 리비전을 번호 메우기용으로 끼워 넣지 않는다. 이 저장소의
head 는 이 마이그레이션 직전까지 0013 하나였고(``alembic heads`` 로 확인),
0014 는 처음부터 생기지 않았다.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 이 목록은 app/services/tracing.py 의 TRACE_SOURCES 와 같아야 한다.
# tests/test_tracing.py 의 test_schema_check_constraint_matches_the_python_source_list
# 이 양방향으로 대조한다 — 한쪽에만 있는 값은 거기서 걸린다.
_SOURCES_AFTER = ("query", "conversation", "mcp_search", "mcp_answer", "legacy")
_SOURCES_BEFORE = ("query", "conversation", "mcp_search", "legacy")
_CHECK_NAME = "traces_source_check"


def _recreate(values: tuple[str, ...]) -> None:
    op.drop_constraint(_CHECK_NAME, "traces", type_="check")
    literals = ", ".join(f"'{s}'" for s in values)
    op.create_check_constraint(_CHECK_NAME, "traces", f"source IN ({literals})")


def upgrade() -> None:
    _recreate(_SOURCES_AFTER)


def downgrade() -> None:
    # 제약을 되걸기 전에 위반 행을 없앤다. 왜 옮기지 않고 지우는지는 모듈
    # docstring 참조.
    result = op.get_bind().exec_driver_sql(
        "DELETE FROM traces WHERE source = 'mcp_answer'"
    )
    if result.rowcount:
        print(f"[0015] mcp_answer 트레이스 {result.rowcount}행을 지웠다 "
              "(옛 CHECK 목록으로 되돌리려면 남아 있을 수 없다)")
    _recreate(_SOURCES_BEFORE)

"""M7 W3: eval_runs 와 eval_results

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-14

L1 평가 결과를 버전별로 남긴다. 회귀를 자동으로 잡으려면 "지금 점수"가
아니라 "지난번 점수와 지금 점수"가 필요하고, 그 둘을 비교하려면 점수를 만든
조건이 점수와 같은 행에 있어야 한다 — git 커밋, 데이터셋 이름과 해시, 구성
이름, cutoff. 조건을 남기지 않으면 다음 주에 "recall 이 떨어졌다"는 사실은
남아도 무엇 때문인지는 영영 알 수 없다.

``eval_results`` 는 문항 단위다. 집계만 있으면 diff 는 "0.02 떨어졌다"까지만
말하고, 정작 필요한 "어떤 질문이 뒤집혔나"를 못 말한다.

``gold_chunk_ids`` · ``retrieved_chunk_ids`` 에 외래키를 걸지 않는다. 평가
픽스처는 실행마다 지워지고 다시 인덱싱되므로, 외래키를 걸면 다음 실행이
직전 실행의 기록을 통째로 끌고 내려간다 — 비교 대상이 사라지는 것이 곧
하네스의 목적이 사라지는 것이다. ``trace_chunks.chunk_id`` 와 같은 이유다.

사용자 외래키도 없다. 평가는 사용자의 데이터가 아니라 저장소의 기록이고,
계정이 지워져도 지표 이력은 남아야 한다.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_name", sa.Text(), nullable=False),
        sa.Column("dataset_sha256", sa.Text(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("num_questions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("num_scored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "metrics",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "metrics_by_type",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "metrics_by_split",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "eval_runs_dataset_config_idx",
        "eval_runs",
        ["dataset_name", "config", "created_at"],
    )

    op.create_table(
        "eval_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_id", sa.Text(), nullable=False),
        sa.Column("question_type", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("first_gold_rank", sa.Integer(), nullable=True),
        sa.Column(
            "metrics",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "retrieved_chunk_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "gold_chunk_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["eval_runs.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "eval_results_run_question_idx", "eval_results", ["run_id", "question_id"]
    )


def downgrade() -> None:
    op.drop_index("eval_results_run_question_idx", table_name="eval_results")
    op.drop_table("eval_results")
    op.drop_index("eval_runs_dataset_config_idx", table_name="eval_runs")
    op.drop_table("eval_runs")

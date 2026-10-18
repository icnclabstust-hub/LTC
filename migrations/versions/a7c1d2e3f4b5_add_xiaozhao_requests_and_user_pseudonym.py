"""add xiaozhao_requests, user pseudonym, nullable binding_codes.created_by

Revision ID: a7c1d2e3f4b5
Revises: be2c7233d3e9
Create Date: 2026-10-19 10:00:00.000000

見 docs/designs/2026-10-19-xiaozhao-fe-be-integration.md 第 5.3 節。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a7c1d2e3f4b5'
down_revision: Union[str, None] = 'be2c7233d3e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('xiaozhao_requests',
    sa.Column('request_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('query_text', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('answer_full', sa.Text(), nullable=True),
    sa.Column('answer_spoken', sa.Text(), nullable=True),
    sa.Column('citations', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('refusal_reason', sa.String(length=32), nullable=True),
    sa.Column('escalate', sa.Boolean(), nullable=False),
    sa.Column('confidence', sa.String(length=8), nullable=True),
    sa.Column('help_request_id', sa.String(length=64), nullable=True),
    sa.Column('human_answer', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['biz.users.user_id'], ),
    sa.PrimaryKeyConstraint('request_id'),
    schema='biz'
    )
    # 拒答統計（P2 每週分群）會依狀態與時間查詢
    op.create_index('ix_xiaozhao_requests_status_created_at', 'xiaozhao_requests', ['status', 'created_at'],
                    schema='biz')

    op.execute("CREATE SEQUENCE biz.user_pseudonym_seq")
    op.add_column('users', sa.Column('pseudonym', sa.String(length=16), nullable=True), schema='biz')
    op.create_unique_constraint('uq_users_pseudonym', 'users', ['pseudonym'], schema='biz')
    # 既有使用者依建立順序補代號
    op.execute(
        """
        UPDATE biz.users u SET pseudonym = sub.prefix || '-' || lpad(sub.n::text, 4, '0')
        FROM (
            SELECT user_id,
                   CASE WHEN role = 'supervisor' THEN 'SV' ELSE 'CG' END AS prefix,
                   nextval('biz.user_pseudonym_seq') AS n
            FROM (SELECT user_id, role FROM biz.users ORDER BY created_at) ordered
        ) sub
        WHERE u.user_id = sub.user_id
        """
    )

    op.alter_column('binding_codes', 'created_by', existing_type=sa.UUID(), nullable=True, schema='biz')


def downgrade() -> None:
    # 綁定碼效期只有 10 分鐘，回滾時直接刪掉 FE 管理員產生（created_by 為 NULL）的碼
    op.execute("DELETE FROM biz.binding_codes WHERE created_by IS NULL")
    op.alter_column('binding_codes', 'created_by', existing_type=sa.UUID(), nullable=False, schema='biz')
    op.drop_constraint('uq_users_pseudonym', 'users', schema='biz', type_='unique')
    op.drop_column('users', 'pseudonym', schema='biz')
    op.execute("DROP SEQUENCE biz.user_pseudonym_seq")
    op.drop_index('ix_xiaozhao_requests_status_created_at', table_name='xiaozhao_requests', schema='biz')
    op.drop_table('xiaozhao_requests', schema='biz')

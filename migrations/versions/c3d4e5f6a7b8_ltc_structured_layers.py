"""ltc structured layers: catalog ltc_* tables, sync_cursor, ltc_points columns, eval.alerts, golden_queries columns

Revision ID: c3d4e5f6a7b8
Revises: a7c1d2e3f4b5
Create Date: 2026-10-19 14:00:00.000000

見 docs/designs/2026-10-19-ltc-kb-graph.md 第 5.1 節。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'a7c1d2e3f4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _source_columns() -> list[sa.Column]:
    """每筆結構化資料都附出處與有效期間（valid_to 為 NULL 表示現行），Neo4j 同步時一併帶過去。"""
    return [
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('source_ref', sa.Text(), nullable=False),
        sa.Column('valid_from', sa.Date(), nullable=False),
        sa.Column('valid_to', sa.Date(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.create_table('ltc_benefit_categories',
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('service_code_prefixes', sa.String(length=16), nullable=False),
    sa.Column('period', sa.String(length=8), nullable=False),
    *_source_columns(),
    sa.PrimaryKeyConstraint('code'),
    schema='catalog'
    )
    op.create_table('ltc_quotas',
    sa.Column('quota_key', sa.String(length=64), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('need_level', sa.SmallInteger(), nullable=False),
    sa.Column('zone', sa.SmallInteger(), nullable=True),
    sa.Column('item_group', sa.SmallInteger(), nullable=True),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('period', sa.String(length=8), nullable=False),
    *_source_columns(),
    sa.ForeignKeyConstraint(['category'], ['catalog.ltc_benefit_categories.code'], ),
    sa.PrimaryKeyConstraint('quota_key'),
    schema='catalog'
    )
    op.create_table('ltc_identity_classes',
    sa.Column('class_no', sa.SmallInteger(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('definition', sa.Text(), nullable=False),
    *_source_columns(),
    sa.PrimaryKeyConstraint('class_no'),
    schema='catalog'
    )
    op.create_table('ltc_copay_rates',
    sa.Column('rate_key', sa.String(length=64), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('identity_class', sa.SmallInteger(), nullable=False),
    sa.Column('zone', sa.SmallInteger(), nullable=True),
    sa.Column('rate_pct', sa.SmallInteger(), nullable=False),
    *_source_columns(),
    sa.ForeignKeyConstraint(['category'], ['catalog.ltc_benefit_categories.code'], ),
    sa.ForeignKeyConstraint(['identity_class'], ['catalog.ltc_identity_classes.class_no'], ),
    sa.PrimaryKeyConstraint('rate_key'),
    schema='catalog'
    )
    op.create_table('ltc_transport_zones',
    sa.Column('zone_key', sa.String(length=64), nullable=False),
    sa.Column('county', sa.Text(), nullable=False),
    sa.Column('town', sa.Text(), nullable=True),
    sa.Column('zone', sa.SmallInteger(), nullable=False),
    *_source_columns(),
    sa.PrimaryKeyConstraint('zone_key'),
    schema='catalog'
    )
    op.create_table('ltc_service_items',
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=True),
    sa.Column('price', sa.Integer(), nullable=True),
    sa.Column('unit', sa.Text(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    *_source_columns(),
    sa.ForeignKeyConstraint(['category'], ['catalog.ltc_benefit_categories.code'], ),
    sa.PrimaryKeyConstraint('code'),
    schema='catalog'
    )
    op.create_table('ltc_eligible_groups',
    sa.Column('group_key', sa.String(length=32), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    *_source_columns(),
    sa.PrimaryKeyConstraint('group_key'),
    schema='catalog'
    )
    op.create_table('sync_cursor',
    sa.Column('subscriber', sa.String(length=32), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(), nullable=False),
    sa.Column('last_version', sa.String(length=64), nullable=False),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('subscriber'),
    schema='catalog'
    )

    op.add_column('ltc_points', sa.Column('county_code', sa.String(length=8), nullable=True), schema='catalog')
    op.add_column('ltc_points', sa.Column('town_code', sa.String(length=16), nullable=True), schema='catalog')
    op.add_column('ltc_points', sa.Column('town_name', sa.Text(), nullable=True), schema='catalog')
    op.add_column('ltc_points', sa.Column('abc_level', sa.String(length=4), nullable=True), schema='catalog')
    op.add_column('ltc_points', sa.Column('service_names', postgresql.ARRAY(sa.Text()), nullable=True), schema='catalog')
    op.add_column('ltc_points', sa.Column('service_area_codes', postgresql.ARRAY(sa.String(length=16)), nullable=True),
                  schema='catalog')
    op.create_index('ix_ltc_points_county_code', 'ltc_points', ['county_code'], schema='catalog')

    op.add_column('golden_queries', sa.Column('question_id', sa.String(length=16), nullable=True), schema='eval')
    op.add_column('golden_queries', sa.Column('expected_behavior', sa.String(length=16), nullable=True), schema='eval')
    op.add_column('golden_queries', sa.Column('expected_refusal_reason', sa.String(length=32), nullable=True),
                  schema='eval')
    op.add_column('golden_queries', sa.Column('escalate', sa.Boolean(), nullable=True), schema='eval')
    op.create_unique_constraint('uq_golden_queries_question_id', 'golden_queries', ['question_id'], schema='eval')

    op.create_table('alerts',
    sa.Column('alert_id', sa.UUID(), nullable=False),
    sa.Column('metric', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.Column('threshold', sa.Float(), nullable=False),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('alert_id'),
    schema='eval'
    )


def downgrade() -> None:
    op.drop_table('alerts', schema='eval')
    op.drop_constraint('uq_golden_queries_question_id', 'golden_queries', schema='eval', type_='unique')
    for column in ('escalate', 'expected_refusal_reason', 'expected_behavior', 'question_id'):
        op.drop_column('golden_queries', column, schema='eval')
    op.drop_index('ix_ltc_points_county_code', table_name='ltc_points', schema='catalog')
    for column in ('service_area_codes', 'service_names', 'abc_level', 'town_name', 'town_code', 'county_code'):
        op.drop_column('ltc_points', column, schema='catalog')
    for table in ('sync_cursor', 'ltc_eligible_groups', 'ltc_service_items', 'ltc_transport_zones',
                  'ltc_copay_rates', 'ltc_identity_classes', 'ltc_quotas', 'ltc_benefit_categories'):
        op.drop_table(table, schema='catalog')

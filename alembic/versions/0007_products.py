"""products: 시험 과목(제품) 추가, 영역·Blueprint 를 제품에 연결, ogg_version → product_version

기존 영역·Blueprint 는 모두 OGG 제품에 연결한다. 새 제품(ORADB, WLS)의 영역은 앱 seed 가 넣는다.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-31 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRODUCTS = [
    {"id": 1, "code": "OGG", "name": "Oracle GoldenGate", "default_version": "19c", "sort_order": 1},
    {"id": 2, "code": "ORADB", "name": "Oracle Database", "default_version": "19c", "sort_order": 2},
    {"id": 3, "code": "WLS", "name": "Oracle WebLogic Server", "default_version": "14c", "sort_order": 3},
]


def upgrade() -> None:
    """Upgrade schema."""
    products = op.create_table(
        'products',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('default_version', sa.String(length=20), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.bulk_insert(products, PRODUCTS)

    # 1) nullable 로 추가 → 2) 기존 행을 OGG 로 채움 → 3) NOT NULL + FK
    for table in ('domains', 'blueprints'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('product_id', sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} SET product_id = 1")
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column('product_id', existing_type=sa.Integer(), nullable=False)
            batch_op.create_index(f'ix_{table}_product_id', ['product_id'], unique=False)
            batch_op.create_foreign_key(f'fk_{table}_product_id', 'products', ['product_id'], ['id'])

    with op.batch_alter_table('items', schema=None) as batch_op:
        batch_op.alter_column('ogg_version', new_column_name='product_version',
                              existing_type=sa.String(length=20), existing_nullable=False)


def downgrade() -> None:
    """Downgrade schema. 새 제품(ORADB/WLS)에 속한 영역·Blueprint·문항이 있으면 먼저 지워야 한다."""
    with op.batch_alter_table('items', schema=None) as batch_op:
        batch_op.alter_column('product_version', new_column_name='ogg_version',
                              existing_type=sa.String(length=20), existing_nullable=False)
    for table in ('blueprints', 'domains'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(f'fk_{table}_product_id', type_='foreignkey')
            batch_op.drop_index(f'ix_{table}_product_id')
            batch_op.drop_column('product_id')
    op.drop_table('products')

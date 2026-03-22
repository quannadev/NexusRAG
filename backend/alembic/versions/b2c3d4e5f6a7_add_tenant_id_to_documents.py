"""Add tenant_id to documents for workspace→tenant→documents isolation

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-22 14:20:00.000000

Changes
-------
documents table:
  + tenant_id  VARCHAR(128)  — optional tenant/bot identifier for sub-workspace isolation
                               NULL = no tenant (workspace-global, admin access)
                               Indexed together with workspace_id for fast per-tenant lookups
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("tenant_id", sa.String(128), nullable=True))
    op.create_index(
        "idx_documents_workspace_tenant",
        "documents",
        ["workspace_id", "tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_documents_workspace_tenant", table_name="documents")
    op.drop_column("documents", "tenant_id")

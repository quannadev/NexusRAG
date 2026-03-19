"""S3 storage migration: add S3 columns, drop markdown_content and file_path

Revision ID: a1b2c3d4e5f6
Revises: 2047460692d0
Create Date: 2026-03-19 14:25:00.000000

Changes
-------
documents table:
  + file_sha256      VARCHAR(64)   — content-addressable dedup hash (indexed)
  + s3_bucket        VARCHAR(255)  — bucket name for raw + markdown objects
  + s3_raw_key       VARCHAR(1000) — S3 key for original uploaded file
  + s3_markdown_key  VARCHAR(1000) — S3 key for parsed markdown (.md)
  - markdown_content TEXT          — removed: content now lives in S3

document_images table:
  + s3_key    VARCHAR(1000) — S3 key in nexusrag-images bucket
  + s3_bucket VARCHAR(255)  — bucket name
  - file_path VARCHAR(500)  — removed: local path no longer used
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "2047460692d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── documents ──────────────────────────────────────────────────────
    op.add_column("documents", sa.Column("file_sha256", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("s3_bucket", sa.String(255), nullable=True))
    op.add_column("documents", sa.Column("s3_raw_key", sa.String(1000), nullable=True))
    op.add_column("documents", sa.Column("s3_markdown_key", sa.String(1000), nullable=True))

    # Index for fast content-dedup lookups (workspace + hash)
    op.create_index("idx_documents_file_sha256", "documents", ["file_sha256"])

    # Drop the large text column — content migrated to S3
    op.drop_column("documents", "markdown_content")

    # ── document_images ────────────────────────────────────────────────
    op.add_column("document_images", sa.Column("s3_key", sa.String(1000), nullable=True))
    op.add_column("document_images", sa.Column("s3_bucket", sa.String(255), nullable=True))

    # Drop local file path — images now served via S3 presigned URLs
    op.drop_column("document_images", "file_path")


def downgrade() -> None:
    # ── document_images ────────────────────────────────────────────────
    op.add_column("document_images", sa.Column("file_path", sa.String(500), nullable=True))
    op.drop_column("document_images", "s3_bucket")
    op.drop_column("document_images", "s3_key")

    # ── documents ──────────────────────────────────────────────────────
    op.add_column("documents", sa.Column("markdown_content", sa.Text(), nullable=True))
    op.drop_index("idx_documents_file_sha256", table_name="documents")
    op.drop_column("documents", "s3_markdown_key")
    op.drop_column("documents", "s3_raw_key")
    op.drop_column("documents", "s3_bucket")
    op.drop_column("documents", "file_sha256")

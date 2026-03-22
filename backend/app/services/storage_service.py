"""
Storage Service — S3/MinIO Object Storage
==========================================

Thin sync wrapper around boto3 for uploading, downloading, and signing
objects.  All blocking I/O is intended to be called inside
``asyncio.to_thread()`` at the call site.

Bucket naming convention
------------------------
- ``nexusrag-documents``  — raw uploaded files + parsed markdown
- ``nexusrag-images``     — Docling-extracted page images

Key naming convention (content-addressable where applicable)
-------------------------------------------------------------
- Raw file    : ``kb_{workspace_id}/raw/{sha256hex}{ext}``
- Markdown    : ``kb_{workspace_id}/markdown/{sha256hex}.md``
- Image       : ``kb_{workspace_id}/images/{image_id}.png``  (UUID — generated)
"""

from __future__ import annotations

import logging
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)


class StorageService:
    """
    S3-compatible object storage service (works with MinIO and AWS S3).

    Uses a single boto3 client (thread-safe for reads) — call methods
    inside asyncio.to_thread() for async contexts.
    """

    def __init__(self) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
            region_name=settings.S3_REGION,
            # Needed for path-style URLs (MinIO) instead of virtual-host style
            config=Config(signature_version="s3v4"),
        )

        # Separate client for presigning — uses the public-facing URL so that
        # browsers can actually reach the signed endpoint.  In Docker the internal
        # endpoint is http://minio:9000 (unreachable from host browser), while the
        # public URL is http://localhost:9000 (port-mapped from the host).
        public_url = settings.S3_PUBLIC_URL or settings.S3_ENDPOINT_URL
        self._presign_client = boto3.client(
            "s3",
            endpoint_url=public_url,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
            region_name=settings.S3_REGION,
            config=Config(signature_version="s3v4"),
        )

    # ------------------------------------------------------------------
    # Bucket bootstrap
    # ------------------------------------------------------------------

    def ensure_buckets_exist(self) -> None:
        """Create required buckets if they don't exist (called at startup)."""
        for bucket in (settings.S3_BUCKET_DOCUMENTS, settings.S3_BUCKET_IMAGES):
            try:
                self._client.head_bucket(Bucket=bucket)
                logger.info(f"S3 bucket already exists: {bucket}")
            except ClientError as exc:
                error_code = exc.response["Error"]["Code"]
                if error_code in ("404", "NoSuchBucket"):
                    self._client.create_bucket(Bucket=bucket)
                    logger.info(f"S3 bucket created: {bucket}")
                else:
                    # Re-raise unexpected errors (auth failures, etc.)
                    raise

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def upload_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload raw bytes to S3. Overwrites silently if key exists."""
        self._client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.debug(f"Uploaded {len(data)} bytes → s3://{bucket}/{key}")

    def download_bytes(self, bucket: str, key: str) -> bytes:
        """Download object content as bytes. Raises ClientError if not found."""
        response = self._client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    def delete_object(self, bucket: str, key: str) -> None:
        """Delete a single object. No-op if it doesn't exist."""
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
            logger.debug(f"Deleted s3://{bucket}/{key}")
        except ClientError as exc:
            # Log but don't raise — deletion is best-effort
            logger.warning(f"Failed to delete s3://{bucket}/{key}: {exc}")

    def object_exists(self, bucket: str, key: str) -> bool:
        """Return True if the object already exists (HEAD request, O(1))."""
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    # ------------------------------------------------------------------
    # Pre-signed URLs — private bucket access for clients
    # ------------------------------------------------------------------

    def generate_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_in: int | None = None,
    ) -> str:
        """
        Generate a time-limited pre-signed GET URL for a private object.

        Args:
            bucket: Bucket name
            key: Object key
            expires_in: TTL in seconds (defaults to ``S3_PRESIGN_EXPIRES_SECONDS``)

        Returns:
            Pre-signed URL string
        """
        ttl = expires_in if expires_in is not None else settings.S3_PRESIGN_EXPIRES_SECONDS
        url: str = self._presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=ttl,
        )
        return url

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tenant_prefix(workspace_id: int, tenant_id: str | None) -> str:
        """Build the S3 path prefix for this workspace (+ optional tenant)."""
        if tenant_id:
            return f"kb_{workspace_id}/tenant_{tenant_id}"
        return f"kb_{workspace_id}"

    @staticmethod
    def raw_key(workspace_id: int, sha256hex: str, ext: str, tenant_id: str | None = None) -> str:
        """Build the S3 key for a raw uploaded document."""
        # Ensure ext starts with a dot
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        prefix = StorageService._tenant_prefix(workspace_id, tenant_id)
        return f"{prefix}/raw/{sha256hex}{ext}"

    @staticmethod
    def markdown_key(workspace_id: int, sha256hex: str, tenant_id: str | None = None) -> str:
        """Build the S3 key for a parsed markdown file."""
        prefix = StorageService._tenant_prefix(workspace_id, tenant_id)
        return f"{prefix}/markdown/{sha256hex}.md"

    @staticmethod
    def image_key(workspace_id: int, image_id: str, tenant_id: str | None = None) -> str:
        """Build the S3 key for an extracted document image."""
        prefix = StorageService._tenant_prefix(workspace_id, tenant_id)
        return f"{prefix}/images/{image_id}.png"


@lru_cache(maxsize=1)
def get_storage_service() -> StorageService:
    """Return the singleton StorageService instance."""
    return StorageService()

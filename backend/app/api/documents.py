from __future__ import annotations

import hashlib
import re
import uuid
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File, BackgroundTasks, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
import json
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import get_db
from app.core.exceptions import NotFoundError
from app.models.knowledge_base import KnowledgeBase
from app.models.document import Document, DocumentImage, DocumentStatus
from app.schemas.document import DocumentResponse, DocumentUploadResponse
from app.schemas.rag import DocumentImageResponse
from app.services.storage_service import get_storage_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Markdown helper — inject image placeholders with presigned URLs on-the-fly
# ---------------------------------------------------------------------------

def _inject_images_from_db(
    markdown: str,
    images: list[DocumentImage],
) -> str:
    """Replace remaining <!-- image --> placeholders with real image markdown.

    Images are served via presigned S3 URLs so the client never hits the
    bucket directly (buckets are private).
    """
    storage = get_storage_service()
    img_iter = iter(images)

    def _replacer(match):
        try:
            img = next(img_iter)
            if not img.s3_key or not img.s3_bucket:
                return ""
            url = storage.generate_presigned_url(img.s3_bucket, img.s3_key)
            caption = (img.caption or "").replace("[", "").replace("]", "")
            return f"\n![{caption}]({url})\n"
        except StopIteration:
            return ""

    return re.sub(r"<!--\s*image\s*-->", _replacer, markdown)


router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".docx", ".pptx"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def process_document_background(document_id: int, s3_raw_key: str, workspace_id: int):
    """Background task — download raw file from S3, run NexusRAG pipeline."""
    import asyncio
    import tempfile
    from app.core.database import async_session_maker
    from app.services.rag_service import get_rag_service

    async with async_session_maker() as db:
        try:
            from sqlalchemy import select as sa_select
            ws_result = await db.execute(
                sa_select(KnowledgeBase.kg_language, KnowledgeBase.kg_entity_types)
                .where(KnowledgeBase.id == workspace_id)
            )
            ws_row = ws_result.one_or_none()
            kg_language = ws_row.kg_language if ws_row else None
            kg_entity_types = ws_row.kg_entity_types if ws_row else None

            rag_service = get_rag_service(
                db, workspace_id,
                kg_language=kg_language,
                kg_entity_types=kg_entity_types,
            )

            # Download raw file from S3 into a temp file for Docling
            storage = get_storage_service()
            raw_bytes = await asyncio.to_thread(
                storage.download_bytes, settings.S3_BUCKET_DOCUMENTS, s3_raw_key
            )

            # Determine extension from the S3 key
            ext = Path(s3_raw_key).suffix or ".bin"
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name

            try:
                await rag_service.process_document(document_id, tmp_path)
            finally:
                Path(tmp_path).unlink(missing_ok=True)

            logger.info(f"Document {document_id} processed successfully")
        except Exception as e:
            logger.error(f"Failed to process document {document_id}: {e}")
            try:
                from sqlalchemy import select, update
                from app.models.document import Document, DocumentStatus
                result = await db.execute(
                    select(Document.status).where(Document.id == document_id)
                )
                current_status = result.scalar_one_or_none()
                if current_status and current_status != DocumentStatus.FAILED:
                    await db.execute(
                        update(Document)
                        .where(Document.id == document_id)
                        .values(
                            status=DocumentStatus.FAILED,
                            error_message=str(e)[:500],
                        )
                    )
                    await db.commit()
            except Exception as recovery_err:
                logger.error(f"Failed to set FAILED status for doc {document_id}: {recovery_err}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/workspace/{workspace_id}", response_model=list[DocumentResponse])
async def list_documents(
    workspace_id: int,
    tenant_id: str | None = Query(default=None, description="Filter by tenant/bot ID"),
    db: AsyncSession = Depends(get_db),
):
    """List all documents in a knowledge base. Optionally filter by tenant_id."""
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == workspace_id))
    kb = result.scalar_one_or_none()

    if kb is None:
        raise NotFoundError("KnowledgeBase", workspace_id)

    stmt = select(Document).where(Document.workspace_id == workspace_id)
    if tenant_id is not None:
        stmt = stmt.where(Document.tenant_id == tenant_id)
    stmt = stmt.order_by(Document.created_at.desc())

    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/upload/{workspace_id}", response_model=DocumentUploadResponse)
async def upload_document(
    workspace_id: int,
    file: UploadFile = File(...),
    custom_metadata: str | None = Form(None),
    tenant_id: str | None = Form(None, description="Tenant/bot ID for sub-workspace isolation"),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a document to a knowledge base.

    Content-addressable deduplication via SHA-256:
    - If a document with an identical SHA-256 already exists and is INDEXED,
      returns a 200 with the existing document info.
    - Raw file is uploaded to S3 only if no object with that key yet exists.

    tenant_id (optional): isolates the document to a specific tenant/bot.
    Queries with matching tenant_id will only see this document.
    """
    parsed_metadata = None
    if custom_metadata:
        try:
            raw_metadata = json.loads(custom_metadata)
            if not isinstance(raw_metadata, list):
                raise ValueError("Metadata must be a list of key-value objects")
            parsed_metadata = {}
            for item in raw_metadata:
                if not isinstance(item, dict) or "key" not in item or "value" not in item:
                    raise ValueError("Each metadata item must contain 'key' and 'value' fields")
                parsed_metadata[item["key"]] = item["value"]
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid custom_metadata format: {e}"
            )

    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == workspace_id))
    kb = result.scalar_one_or_none()
    if kb is None:
        raise NotFoundError("KnowledgeBase", workspace_id)

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type {ext} not allowed. Allowed: {ALLOWED_EXTENSIONS}"
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File too large. Max size: {MAX_FILE_SIZE // 1024 // 1024}MB"
        )

    # --- Content-addressable deduplication ---
    sha256_hex = hashlib.sha256(content).hexdigest()
    # S3 raw key includes tenant prefix when tenant_id is provided
    s3_raw_key = get_storage_service().raw_key(workspace_id, sha256_hex, ext, tenant_id=tenant_id)

    # Check if an INDEXED document with this hash already exists in this workspace (and same tenant)
    existing_result = await db.execute(
        select(Document).where(
            Document.workspace_id == workspace_id,
            Document.file_sha256 == sha256_hex,
            Document.tenant_id == tenant_id,
            Document.status == DocumentStatus.INDEXED,
        )
    )
    existing_doc = existing_result.scalar_one_or_none()
    if existing_doc:
        return DocumentUploadResponse(
            id=existing_doc.id,
            filename=existing_doc.original_filename,
            status=existing_doc.status,
            message="File already indexed. Duplicate upload skipped.",
        )

    # --- Upload to S3 (skip if same content already stored) ---
    storage = get_storage_service()
    import asyncio
    already_in_s3 = await asyncio.to_thread(storage.object_exists, settings.S3_BUCKET_DOCUMENTS, s3_raw_key)
    if not already_in_s3:
        content_type_map = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".txt": "text/plain",
            ".md": "text/markdown",
        }
        await asyncio.to_thread(
            storage.upload_bytes,
            settings.S3_BUCKET_DOCUMENTS,
            s3_raw_key,
            content,
            content_type_map.get(ext, "application/octet-stream"),
        )
        logger.info(f"Uploaded raw file to S3: s3://{settings.S3_BUCKET_DOCUMENTS}/{s3_raw_key}")
    else:
        logger.info(f"S3 object already exists, skipping upload: {s3_raw_key}")

    # Use a stable filename based on hash so file records are deterministic
    filename = f"{sha256_hex}{ext}"

    document = Document(
        workspace_id=workspace_id,
        filename=filename,
        original_filename=file.filename,
        file_type=ext[1:],
        file_size=len(content),
        status=DocumentStatus.PENDING,
        custom_metadata=parsed_metadata,
        file_sha256=sha256_hex,
        s3_raw_key=s3_raw_key,
        s3_bucket=settings.S3_BUCKET_DOCUMENTS,
        tenant_id=tenant_id,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)

    return DocumentUploadResponse(
        id=document.id,
        filename=document.original_filename,
        status=document.status,
        message="Document uploaded. Click 'Process' to extract and index content.",
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get document by ID."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document", document_id)
    return document


@router.get("/{document_id}/markdown")
async def get_document_markdown(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the full parsed markdown of a document — streamed from S3."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document", document_id)

    if not document.s3_markdown_key or not document.s3_bucket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No markdown content available. Document may not have been processed yet.",
        )

    import asyncio
    storage = get_storage_service()
    try:
        raw_bytes = await asyncio.to_thread(
            storage.download_bytes, document.s3_bucket, document.s3_markdown_key
        )
    except Exception as exc:
        logger.error(f"Failed to download markdown for doc {document_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not retrieve markdown from storage.",
        )

    markdown = raw_bytes.decode("utf-8")

    # Safety net: if image placeholders remain, inject presigned URLs on-the-fly
    if "<!-- image" in markdown:
        img_result = await db.execute(
            select(DocumentImage)
            .where(DocumentImage.document_id == document_id)
            .order_by(DocumentImage.id)
        )
        images = img_result.scalars().all()
        if images:
            markdown = _inject_images_from_db(markdown, images)

    return PlainTextResponse(content=markdown, media_type="text/markdown")


@router.get("/{document_id}/images", response_model=list[DocumentImageResponse])
async def get_document_images(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    """List all extracted images for a document with presigned S3 URLs."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document", document_id)

    result = await db.execute(
        select(DocumentImage)
        .where(DocumentImage.document_id == document_id)
        .order_by(DocumentImage.page_no)
    )
    images = result.scalars().all()

    storage = get_storage_service()
    response_images = []
    for img in images:
        if img.s3_key and img.s3_bucket:
            presigned_url = storage.generate_presigned_url(img.s3_bucket, img.s3_key)
        else:
            presigned_url = ""
        response_images.append(
            DocumentImageResponse(
                image_id=img.image_id,
                document_id=img.document_id,
                page_no=img.page_no,
                caption=img.caption or "",
                width=img.width,
                height=img.height,
                url=presigned_url,
            )
        )
    return response_images


@router.get("/{document_id}/presign")
async def presign_document_object(
    document_id: int,
    key: str = Query(..., description="S3 object key to sign"),
    bucket: str = Query(..., description="S3 bucket name"),
    expires_in: int = Query(default=None, description="TTL in seconds (default: S3_PRESIGN_EXPIRES_SECONDS)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate a time-limited pre-signed GET URL for a private S3 object.

    Security: validates the requested key belongs to this document to
    prevent Insecure Direct Object Reference (IDOR) attacks.
    """
    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document", document_id)

    # Validate ownership: key must belong to this document's workspace
    expected_prefix = f"kb_{document.workspace_id}/"
    if not key.startswith(expected_prefix):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: key does not belong to this document's workspace.",
        )

    # Additionally verify the key is one of the document's own known keys,
    # or an image key belonging to this document
    allowed_keys: set[str] = set()
    if document.s3_raw_key:
        allowed_keys.add(document.s3_raw_key)
    if document.s3_markdown_key:
        allowed_keys.add(document.s3_markdown_key)

    # Load image keys for this document
    img_result = await db.execute(
        select(DocumentImage.s3_key).where(DocumentImage.document_id == document_id)
    )
    for (img_key,) in img_result.all():
        if img_key:
            allowed_keys.add(img_key)

    if key not in allowed_keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: key is not associated with this document.",
        )

    ttl = expires_in or settings.S3_PRESIGN_EXPIRES_SECONDS
    storage = get_storage_service()
    presigned_url = storage.generate_presigned_url(bucket, key, expires_in=ttl)

    expires_at = (
        datetime.now(tz=timezone.utc) + timedelta(seconds=ttl)
    ).isoformat()

    return {
        "url": presigned_url,
        "bucket": bucket,
        "key": key,
        "expires_in_seconds": ttl,
        "expires_at": expires_at,
    }


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a document, its vector/KG chunks, and all S3 objects."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document", document_id)

    if document.status == DocumentStatus.INDEXED:
        try:
            from app.services.rag_service import get_rag_service
            rag_service = get_rag_service(db, document.workspace_id)
            await rag_service.delete_document(document_id)
        except Exception as e:
            logger.warning(f"Failed to delete chunks from vector store: {e}")

    await db.delete(document)
    await db.commit()

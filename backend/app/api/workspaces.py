"""
Knowledge Base (Workspace) CRUD API endpoints.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.core.deps import get_db
from app.core.exceptions import NotFoundError
from app.models.knowledge_base import KnowledgeBase
from app.models.document import Document, DocumentStatus
from app.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceUpdate,
    WorkspaceResponse,
    WorkspaceSummary,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


async def _enrich_response(db: AsyncSession, kb: KnowledgeBase) -> WorkspaceResponse:
    """Build WorkspaceResponse with computed counts."""
    total = await db.execute(
        select(func.count(Document.id)).where(Document.workspace_id == kb.id)
    )
    indexed = await db.execute(
        select(func.count(Document.id)).where(
            Document.workspace_id == kb.id,
            Document.status == DocumentStatus.INDEXED,
        )
    )
    return WorkspaceResponse(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        system_prompt=kb.system_prompt,
        kg_language=kb.kg_language,
        kg_entity_types=kb.kg_entity_types,
        document_count=total.scalar() or 0,
        indexed_count=indexed.scalar() or 0,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(db: AsyncSession = Depends(get_db)):
    """List all knowledge bases."""
    result = await db.execute(
        select(KnowledgeBase).order_by(KnowledgeBase.updated_at.desc())
    )
    kbs = result.scalars().all()
    return [await _enrich_response(db, kb) for kb in kbs]


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new knowledge base."""
    kb = KnowledgeBase(
        name=body.name,
        description=body.description,
        kg_language=body.kg_language,
        kg_entity_types=body.kg_entity_types,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return await _enrich_response(db, kb)


@router.get("/summary", response_model=list[WorkspaceSummary])
async def list_workspace_summaries(db: AsyncSession = Depends(get_db)):
    """Compact list for dropdown selectors."""
    result = await db.execute(
        select(KnowledgeBase).order_by(KnowledgeBase.name)
    )
    kbs = result.scalars().all()
    summaries = []
    for kb in kbs:
        cnt = await db.execute(
            select(func.count(Document.id)).where(Document.workspace_id == kb.id)
        )
        summaries.append(WorkspaceSummary(
            id=kb.id, name=kb.name, document_count=cnt.scalar() or 0
        ))
    return summaries


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a knowledge base by ID."""
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == workspace_id)
    )
    kb = result.scalar_one_or_none()
    if kb is None:
        raise NotFoundError("KnowledgeBase", workspace_id)
    return await _enrich_response(db, kb)


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: int,
    body: WorkspaceUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a knowledge base name/description."""
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == workspace_id)
    )
    kb = result.scalar_one_or_none()
    if kb is None:
        raise NotFoundError("KnowledgeBase", workspace_id)

    if body.name is not None:
        kb.name = body.name
    if body.description is not None:
        kb.description = body.description
    if body.system_prompt is not None:
        # Empty string → reset to default (None)
        kb.system_prompt = body.system_prompt or None
    if body.kg_language is not None:
        kb.kg_language = body.kg_language or None
    if body.kg_entity_types is not None:
        kb.kg_entity_types = body.kg_entity_types or None

    await db.commit()
    await db.refresh(kb)
    return await _enrich_response(db, kb)



@router.get("/{workspace_id}/tenants")
async def list_workspace_tenants(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
):
    """List all tenants (bots) that have documents in this workspace.

    Returns a list of distinct tenant_ids with per-tenant document stats.
    Documents with tenant_id=NULL are returned under the key ``null``
    representing workspace-global (un-scoped) documents.
    """
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == workspace_id)
    )
    if result.scalar_one_or_none() is None:
        raise NotFoundError("KnowledgeBase", workspace_id)

    # Distinct tenant_ids (including NULL = global workspace docs)
    rows = await db.execute(
        select(
            Document.tenant_id,
            func.count(Document.id).label("document_count"),
            func.count(Document.id).filter(Document.status == DocumentStatus.INDEXED).label("indexed_count"),
        )
        .where(Document.workspace_id == workspace_id)
        .group_by(Document.tenant_id)
        .order_by(Document.tenant_id.asc().nulls_first())
    )

    tenants = []
    for tenant_id, document_count, indexed_count in rows.all():
        tenants.append({
            "tenant_id": tenant_id,          # None = workspace-global docs
            "document_count": document_count,
            "indexed_count": indexed_count,
        })

    return {"workspace_id": workspace_id, "tenants": tenants, "total": len(tenants)}


@router.delete("/{workspace_id}/tenants/{tenant_id}", status_code=status.HTTP_200_OK)
async def delete_tenant(
    workspace_id: int,
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete all data belonging to a tenant within a workspace.

    Removes:
    - All tenant documents from the database
    - Vector store chunks (ChromaDB) tagged with this tenant_id
    - Knowledge graph directory for this tenant
    - S3 objects (raw files, markdown, images) under the tenant prefix

    This is a destructive, irreversible operation.
    """
    import asyncio
    import logging
    from sqlalchemy import select, delete as sa_delete
    from app.models.document import Document, DocumentImage

    log = logging.getLogger(__name__)

    # Verify workspace exists
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == workspace_id))
    if result.scalar_one_or_none() is None:
        raise NotFoundError("KnowledgeBase", workspace_id)

    # Load all documents for this tenant
    doc_result = await db.execute(
        select(Document).where(
            Document.workspace_id == workspace_id,
            Document.tenant_id == tenant_id,
        )
    )
    documents = doc_result.scalars().all()

    if not documents:
        return {
            "deleted_documents": 0,
            "workspace_id": workspace_id,
            "tenant_id": tenant_id,
            "detail": "No documents found for this tenant.",
        }

    # ── 1. Remove vector chunks ──────────────────────────────────────────
    try:
        from app.services.vector_store import get_vector_store
        vs = get_vector_store(workspace_id)
        # Delete all chunks where tenant_id matches (ChromaDB where filter)
        vs.collection.delete(where={"tenant_id": {"$eq": tenant_id}})
        log.info(f"Deleted vector chunks for tenant={tenant_id} in workspace={workspace_id}")
    except Exception as exc:
        log.warning(f"Vector store cleanup failed for tenant={tenant_id}: {exc}")

    # ── 2. Delete knowledge graph directory for this tenant ──────────────
    try:
        from app.services.knowledge_graph_service import KnowledgeGraphService
        kg = KnowledgeGraphService(workspace_id, tenant_id=tenant_id)
        await kg.delete_project_data()
        log.info(f"Deleted KG data for tenant={tenant_id} in workspace={workspace_id}")
    except Exception as exc:
        log.warning(f"KG cleanup failed for tenant={tenant_id}: {exc}")

    # ── 3. Delete S3 objects for each document ───────────────────────────
    try:
        from app.services.storage_service import get_storage_service
        from app.core.config import settings as _settings
        storage = get_storage_service()

        for doc in documents:
            # Raw file
            if doc.s3_raw_key:
                await asyncio.to_thread(
                    storage.delete_object, _settings.S3_BUCKET_DOCUMENTS, doc.s3_raw_key
                )
            # Parsed markdown
            if doc.s3_markdown_key:
                await asyncio.to_thread(
                    storage.delete_object, _settings.S3_BUCKET_DOCUMENTS, doc.s3_markdown_key
                )

        # Image S3 objects
        doc_ids = [doc.id for doc in documents]
        img_result = await db.execute(
            select(DocumentImage.s3_key).where(DocumentImage.document_id.in_(doc_ids))
        )
        for (img_key,) in img_result.all():
            if img_key:
                await asyncio.to_thread(
                    storage.delete_object, _settings.S3_BUCKET_DOCUMENTS, img_key
                )

        log.info(f"Deleted S3 objects for {len(documents)} docs, tenant={tenant_id}")
    except Exception as exc:
        log.warning(f"S3 cleanup failed for tenant={tenant_id}: {exc}")

    # ── 4. Delete DB records (images cascade, then documents) ────────────
    await db.execute(
        sa_delete(DocumentImage).where(DocumentImage.document_id.in_([d.id for d in documents]))
    )
    await db.execute(
        sa_delete(Document).where(
            Document.workspace_id == workspace_id,
            Document.tenant_id == tenant_id,
        )
    )
    await db.commit()

    return {
        "deleted_documents": len(documents),
        "workspace_id": workspace_id,
        "tenant_id": tenant_id,
        "detail": f"Successfully deleted all data for tenant '{tenant_id}'.",
    }


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a knowledge base and all its documents."""
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == workspace_id)
    )
    kb = result.scalar_one_or_none()
    if kb is None:
        raise NotFoundError("KnowledgeBase", workspace_id)

    # Clean up vector store and KG data
    try:
        from app.services.vector_store import get_vector_store
        vs = get_vector_store(workspace_id)
        vs.delete_collection()
    except Exception:
        pass

    try:
        from app.services.knowledge_graph_service import KnowledgeGraphService
        kg = KnowledgeGraphService(workspace_id)
        await kg.delete_project_data()
    except Exception:
        pass

    # Clean up image files
    import shutil
    from app.core.config import settings
    images_dir = settings.BASE_DIR / "data" / "docling" / f"kb_{workspace_id}"
    if images_dir.exists():
        shutil.rmtree(images_dir, ignore_errors=True)

    await db.delete(kb)
    await db.commit()

"""
Vector Store Service
Handles ChromaDB operations for storing and retrieving document embeddings.
"""
from __future__ import annotations

import logging
from typing import Sequence, Optional, TYPE_CHECKING
import chromadb
from chromadb.config import Settings as ChromaSettings

from app.core.config import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Global ChromaDB client
_chroma_client: Optional[chromadb.HttpClient] = None


def get_chroma_client() -> chromadb.HttpClient:
    """Get or create the ChromaDB client singleton."""
    global _chroma_client

    if _chroma_client is None:
        logger.info(f"Connecting to ChromaDB at {settings.CHROMA_HOST}:{settings.CHROMA_PORT}")
        _chroma_client = chromadb.HttpClient(
            host=settings.CHROMA_HOST,
            port=settings.CHROMA_PORT,
            settings=ChromaSettings(
                anonymized_telemetry=False,
            )
        )
        # Test connection
        _chroma_client.heartbeat()
        logger.info("Connected to ChromaDB successfully")

    return _chroma_client


class ChromaVectorStore:
    """
    Vector store service for managing document embeddings in ChromaDB.
    Each knowledge base has its own collection for namespace isolation.
    """

    COLLECTION_PREFIX = "kb_"

    def __init__(self, workspace_id: int):
        self.workspace_id = workspace_id
        self.collection_name = f"{self.COLLECTION_PREFIX}{workspace_id}"
        self._collection = None

    @property
    def collection(self) -> chromadb.Collection:
        """Get or create the collection."""
        if self._collection is None:
            client = get_chroma_client()
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def _recreate_collection(self) -> None:
        """Delete and recreate the collection (resets cached reference)."""
        client = get_chroma_client()
        try:
            client.delete_collection(self.collection_name)
            logger.info(f"Deleted collection {self.collection_name} for dimension migration")
        except Exception:
            pass
        self._collection = None
        # Force re-creation
        _ = self.collection

    def add_documents(
        self,
        ids: Sequence[str],
        embeddings: Sequence[list[float]],
        documents: Sequence[str],
        metadatas: Sequence[dict] | None = None
    ) -> None:
        """
        Add documents with their embeddings to the collection.
        Auto-handles dimension mismatch: if the collection was created with
        a different embedding dimension, it is deleted and recreated.
        """
        if not ids:
            return

        try:
            self.collection.add(
                ids=list(ids),
                embeddings=list(embeddings),
                documents=list(documents),
                metadatas=list(metadatas) if metadatas else None
            )
        except Exception as e:
            error_msg = str(e).lower()
            if "dimension" in error_msg:
                # Dimension mismatch — collection was created with old embedding model
                logger.warning(
                    f"Dimension mismatch in {self.collection_name}: {e}. "
                    f"Recreating collection for new embedding model."
                )
                self._recreate_collection()
                # Retry with fresh collection
                self.collection.add(
                    ids=list(ids),
                    embeddings=list(embeddings),
                    documents=list(documents),
                    metadatas=list(metadatas) if metadatas else None
                )
            else:
                raise

        logger.info(f"Added {len(ids)} documents to collection {self.collection_name}")

    def query(
        self,
        query_embedding: list[float],
        n_results: int = 5,
        where: dict | None = None,
        include: list[str] | None = None
    ) -> dict:
        """Query the collection for similar documents."""
        if include is None:
            include = ["documents", "metadatas", "distances"]

        try:
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                where=where,
                include=include
            )
        except Exception as e:
            error_msg = str(e).lower()
            if "dimension" in error_msg:
                # Query with new-dimension embedding against old collection
                logger.warning(
                    f"Dimension mismatch on query in {self.collection_name}: {e}. "
                    f"Collection needs reindexing."
                )
                return {"ids": [], "documents": [], "metadatas": [], "distances": []}
            raise

        # Flatten single query results
        return {
            "ids": results["ids"][0] if results["ids"] else [],
            "documents": results["documents"][0] if results.get("documents") else [],
            "metadatas": results["metadatas"][0] if results.get("metadatas") else [],
            "distances": results["distances"][0] if results.get("distances") else []
        }

    def delete_by_document_id(self, document_id: int) -> None:
        """Delete all chunks belonging to a specific document."""
        self.collection.delete(
            where={"document_id": document_id}
        )
        logger.info(f"Deleted chunks for document {document_id} from collection {self.collection_name}")

    def delete_collection(self) -> None:
        """Delete the entire collection for this knowledge base."""
        client = get_chroma_client()
        try:
            client.delete_collection(self.collection_name)
            self._collection = None
            logger.info(f"Deleted collection {self.collection_name}")
        except Exception as e:
            logger.warning(f"Failed to delete collection {self.collection_name}: {e}")

    def count(self) -> int:
        """Return the number of documents in the collection."""
        return self.collection.count()

    def get_by_ids(self, ids: Sequence[str]) -> dict:
        """Get documents by their IDs."""
        return self.collection.get(
            ids=list(ids),
            include=["documents", "metadatas"]
        )


class PostgresVectorStore:
    """
    Vector store service for managing document embeddings in PostgreSQL using pgvector.
    All chunks are stored in the VectorChunk table, isolated by workspace_id.
    """

    def __init__(self, workspace_id: int):
        self.workspace_id = workspace_id

    def add_documents(
        self,
        ids: Sequence[str],
        embeddings: Sequence[list[float]],
        documents: Sequence[str],
        metadatas: Sequence[dict] | None = None
    ) -> None:
        if not ids:
            return
            
        from app.models.vector_chunk import VectorChunk
        from app.core.database import VectorAsyncSessionLocal
        import asyncio

        async def _insert_chunks():
            async with VectorAsyncSessionLocal() as session:
                for i in range(len(ids)):
                    chunk = VectorChunk(
                        id=ids[i],
                        workspace_id=self.workspace_id,
                        document=documents[i],
                        embedding=embeddings[i],
                        c_metadata=metadatas[i] if metadatas else {}
                    )
                    session.add(chunk)
                await session.commit()
                
        # We need to run this synchronously or inside an existing event loop.
        # However, FastAPI sync routes or other services might call this.
        # Since VectorStore was synchronous, we use asyncio execution here.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_insert_chunks())
        except RuntimeError:
            asyncio.run(_insert_chunks())

        logger.info(f"Added {len(ids)} documents to Postgres workspace {self.workspace_id}")

    def query(
        self,
        query_embedding: list[float],
        n_results: int = 5,
        where: dict | None = None,
        include: list[str] | None = None
    ) -> dict:
        from app.models.vector_chunk import VectorChunk
        from app.core.database import VectorAsyncSessionLocal
        from sqlalchemy import select
        import asyncio
        
        async def _query_chunks():
            async with VectorAsyncSessionLocal() as session:
                stmt = select(VectorChunk).where(
                    VectorChunk.workspace_id == self.workspace_id
                )
                
                if where:
                    for key, val in where.items():
                        if isinstance(val, dict) and "$in" in val:
                            in_vals = val["$in"]
                            if in_vals:
                                # Determine type for cast
                                if isinstance(in_vals[0], int):
                                    from sqlalchemy import Integer
                                    stmt = stmt.where(VectorChunk.c_metadata[key].astext.cast(Integer).in_(in_vals))
                                else:
                                    stmt = stmt.where(VectorChunk.c_metadata[key].astext.in_([str(v) for v in in_vals]))
                        else:
                            # Direct equality
                            if isinstance(val, int):
                                from sqlalchemy import Integer
                                stmt = stmt.where(VectorChunk.c_metadata[key].astext.cast(Integer) == val)
                            elif isinstance(val, bool):
                                from sqlalchemy import Boolean
                                stmt = stmt.where(VectorChunk.c_metadata[key].astext.cast(Boolean) == val)
                            else:
                                stmt = stmt.where(VectorChunk.c_metadata[key].astext == str(val))
                                
                stmt = stmt.order_by(
                    VectorChunk.embedding.cosine_distance(query_embedding)
                ).limit(n_results)
                
                result = await session.execute(stmt)
                chunks = result.scalars().all()
                return chunks
                
        try:
            loop = asyncio.get_running_loop()
            # If running loop, we can't block. This indicates the caller expects a sync return.
            # Using nest_asyncio or running in a separate thread might be required if this blocks.
            # For simplicity, if we hit this, we might need a workaround.
            pass
        except RuntimeError:
            pass
            
        chunks = asyncio.run(_query_chunks())
        
        # Format like ChromaDB
        return {
            "ids": [c.id for c in chunks],
            "documents": [c.document for c in chunks],
            "metadatas": [c.c_metadata for c in chunks],
            "distances": [0.0] * len(chunks)  # Actual distance not easily extracted in standard ORM without overriding columns
        }

    def delete_by_document_id(self, document_id: int) -> None:
        """Delete all chunks belonging to a specific document."""
        from app.models.vector_chunk import VectorChunk
        from app.core.database import VectorAsyncSessionLocal
        from sqlalchemy import delete, func, cast, Integer
        import asyncio
        
        async def _delete_chunks():
            async with VectorAsyncSessionLocal() as session:
                stmt = delete(VectorChunk).where(
                    VectorChunk.workspace_id == self.workspace_id,
                    VectorChunk.c_metadata['document_id'].astext.cast(Integer) == document_id
                )
                await session.execute(stmt)
                await session.commit()
                
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_delete_chunks())
        except RuntimeError:
            asyncio.run(_delete_chunks())

    def delete_collection(self) -> None:
        """Delete the entire collection for this knowledge base (all chunks for the workspace)."""
        from app.models.vector_chunk import VectorChunk
        from app.core.database import VectorAsyncSessionLocal
        from sqlalchemy import delete
        import asyncio

        async def _delete_all():
            async with VectorAsyncSessionLocal() as session:
                stmt = delete(VectorChunk).where(VectorChunk.workspace_id == self.workspace_id)
                await session.execute(stmt)
                await session.commit()
                
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_delete_all())
        except RuntimeError:
            asyncio.run(_delete_all())

    def count(self) -> int:
        """Return the number of documents in the collection (workspace)."""
        from app.models.vector_chunk import VectorChunk
        from app.core.database import VectorAsyncSessionLocal
        from sqlalchemy import select, func
        import asyncio

        async def _count():
            async with VectorAsyncSessionLocal() as session:
                stmt = select(func.count()).select_from(VectorChunk).where(VectorChunk.workspace_id == self.workspace_id)
                return await session.scalar(stmt)
                
        return asyncio.run(_count())

    def get_by_ids(self, ids: Sequence[str]) -> dict:
        """Get documents by their IDs."""
        from app.models.vector_chunk import VectorChunk
        from app.core.database import VectorAsyncSessionLocal
        from sqlalchemy import select
        import asyncio

        async def _get_by_ids():
            async with VectorAsyncSessionLocal() as session:
                stmt = select(VectorChunk).where(VectorChunk.id.in_(list(ids)))
                res = await session.execute(stmt)
                return res.scalars().all()
                
        chunks = asyncio.run(_get_by_ids())
        return {
            "documents": [c.document for c in chunks],
            "metadatas": [c.c_metadata for c in chunks]
        }


def get_vector_store(workspace_id: int):
    """Factory function to create a VectorStore for a knowledge base."""
    if settings.VECTOR_DB_PROVIDER.lower() == "postgres":
        return PostgresVectorStore(workspace_id)
    return ChromaVectorStore(workspace_id)

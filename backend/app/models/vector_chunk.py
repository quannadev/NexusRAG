from sqlalchemy import String, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

from app.core.database import VectorBase


class VectorChunk(VectorBase):
    __tablename__ = "vector_chunks"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, index=True)
    document: Mapped[str] = mapped_column(Text)
    # Using dynamic dimension for flexible embedding models, though a specific dimension is better if we know it.
    embedding: Mapped[list[float]] = mapped_column(Vector())
    c_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    c_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

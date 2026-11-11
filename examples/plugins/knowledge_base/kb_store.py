# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The knowledge base plugin's own storage, in the dedicated area core allocates.

Nothing here is in core's transactions. Originals and chunks are immutable records
keyed by content digest, so a rolled-back or retried write only leaves records no
Artifact references; core's Artifacts decide what is current.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

METADATA = MetaData()

KNOWLEDGE_BASES = Table(
    "knowledge_bases",
    METADATA,
    Column("kb_id", String(64), primary_key=True),
    Column("scope_id", String(256), nullable=False, unique=True),
    Column("name", String(256), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("created_by", String(512), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
)

ORIGINALS = Table(
    "originals",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("sha256", String(64), primary_key=True),
    Column("media_type", String(64), nullable=False),
    Column("content", LargeBinary, nullable=False),
)

UPLOADS = Table(
    "uploads",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("source_id", String(128), primary_key=True),
    Column("document_id", String(64), nullable=False),
    Column("name", String(512), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
)

CHUNKS = Table(
    "chunks",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("sha256", String(64), primary_key=True),
    Column("profile", String(128), primary_key=True),
    Column("chunk_index", Integer, primary_key=True),
    Column("text", Text, nullable=False),
    Column("vector", JSON, nullable=False),
)


class KnowledgeBaseStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def prepare(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)

    # Knowledge bases

    async def add_knowledge_base(
        self, kb_id: str, scope_id: str, name: str, description: str, created_by: str
    ) -> dict[str, Any]:
        async with self.engine.begin() as connection:
            existing = (
                (await connection.execute(select(KNOWLEDGE_BASES).where(KNOWLEDGE_BASES.c.scope_id == scope_id)))
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                # A retried creation reached the same Scope; keep its first record.
                return dict(existing)
            row = {
                "kb_id": kb_id,
                "scope_id": scope_id,
                "name": name,
                "description": description,
                "created_by": created_by,
                "created_at": datetime.now(UTC),
                "deleted_at": None,
            }
            await connection.execute(insert(KNOWLEDGE_BASES).values(**row))
        return row

    async def knowledge_base(self, kb_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(KNOWLEDGE_BASES).where(
                            KNOWLEDGE_BASES.c.kb_id == kb_id, KNOWLEDGE_BASES.c.deleted_at.is_(None)
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else dict(row)

    async def knowledge_bases(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(KNOWLEDGE_BASES)
                        .where(KNOWLEDGE_BASES.c.deleted_at.is_(None))
                        .order_by(KNOWLEDGE_BASES.c.created_at, KNOWLEDGE_BASES.c.kb_id)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def rename(self, kb_id: str, *, name: str | None, description: str | None) -> None:
        values = {key: value for key, value in (("name", name), ("description", description)) if value is not None}
        if values:
            async with self.engine.begin() as connection:
                await connection.execute(
                    update(KNOWLEDGE_BASES).where(KNOWLEDGE_BASES.c.kb_id == kb_id).values(**values)
                )

    async def mark_deleted(self, kb_id: str) -> None:
        """Hide the knowledge base; its Scope, Sources, and Artifacts stay in core."""

        async with self.engine.begin() as connection:
            await connection.execute(
                update(KNOWLEDGE_BASES).where(KNOWLEDGE_BASES.c.kb_id == kb_id).values(deleted_at=datetime.now(UTC))
            )

    # Originals and uploads

    async def save_original(self, scope_id: str, sha256: str, media_type: str, content: bytes) -> None:
        async with self.engine.begin() as connection:
            # The same bytes may already be stored under the same digest.
            with suppress(IntegrityError):
                await connection.execute(
                    insert(ORIGINALS).values(scope_id=scope_id, sha256=sha256, media_type=media_type, content=content)
                )

    async def original(self, scope_id: str, sha256: str) -> tuple[bytes, str] | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(ORIGINALS.c.content, ORIGINALS.c.media_type).where(
                        ORIGINALS.c.scope_id == scope_id, ORIGINALS.c.sha256 == sha256
                    )
                )
            ).one_or_none()
        return None if row is None else (bytes(row.content), str(row.media_type))

    async def record_upload(self, scope_id: str, source_id: str, document_id: str, name: str, sha256: str) -> None:
        async with self.engine.begin() as connection:
            with suppress(IntegrityError):
                await connection.execute(
                    insert(UPLOADS).values(
                        scope_id=scope_id,
                        source_id=source_id,
                        document_id=document_id,
                        name=name,
                        sha256=sha256,
                        uploaded_at=datetime.now(UTC),
                    )
                )

    async def uploads(self, scope_id: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(UPLOADS).where(UPLOADS.c.scope_id == scope_id).order_by(UPLOADS.c.uploaded_at)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    # Chunks

    async def save_chunks(
        self, scope_id: str, sha256: str, profile: str, chunks: Sequence[str], vectors: Sequence[Sequence[float]]
    ) -> None:
        async with self.engine.begin() as connection:
            stored = (
                (
                    await connection.execute(
                        select(CHUNKS.c.chunk_index).where(
                            CHUNKS.c.scope_id == scope_id, CHUNKS.c.sha256 == sha256, CHUNKS.c.profile == profile
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len(stored) == len(chunks):
                return
            for index, (text, vector) in enumerate(zip(chunks, vectors, strict=True)):
                if index not in stored:
                    await connection.execute(
                        insert(CHUNKS).values(
                            scope_id=scope_id,
                            sha256=sha256,
                            profile=profile,
                            chunk_index=index,
                            text=text,
                            vector=json.loads(json.dumps(list(vector))),
                        )
                    )

    async def chunks(self, scope_id: str, digests: Sequence[str], profile: str) -> list[dict[str, Any]]:
        if not digests:
            return []
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(CHUNKS).where(
                            CHUNKS.c.scope_id == scope_id,
                            CHUNKS.c.sha256.in_(tuple(digests)),
                            CHUNKS.c.profile == profile,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

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

Nothing here is in core's transactions. Originals, chunk sets, and vectors are immutable
records keyed by content digest, so a rolled-back or retried write only leaves records no
Artifact references; the ``document`` Artifacts in core decide what is current. A chunk set
is one exact list of chunks; parsing and user edits both produce chunk sets, and a document
revision names the set it uses. Vectors are a derived index of a set for one embedding
profile and can be rebuilt at any time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal

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
    # The parser new uploads use; None keeps uploads waiting until the user chooses one.
    Column("default_parser", JSON, nullable=True),
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

# Every upload and every request to (re)process a document. A submission without a source_id
# waits for the user to choose a parser and can still be cancelled, since nothing reached core;
# submitting it records the Source's name, and generation records when it handled the submission.
SUBMISSIONS = Table(
    "submissions",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("submission_id", String(64), primary_key=True),
    Column("document_id", String(64), nullable=False, index=True),
    Column("name", String(512), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("media_type", String(64), nullable=False),
    Column("size", Integer, nullable=False),
    Column("uploaded_by", String(512), nullable=False),
    Column("parser", JSON, nullable=True),
    Column("source_id", String(128), nullable=True),
    Column("processed_at", DateTime(timezone=True), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# Chunk edits that generation did not apply because the document changed after the user read it.
DISCARDED_EDITS = Table(
    "discarded_edits",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("submission_id", String(64), primary_key=True),
    Column("discarded_at", DateTime(timezone=True), nullable=False),
)

PARSED = Table(
    "parsed",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("sha256", String(64), primary_key=True),
    Column("parse_profile", String(128), primary_key=True),
    Column("set_id", String(64), nullable=False),
)

CHUNKS = Table(
    "chunks",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("set_id", String(64), primary_key=True),
    Column("chunk_index", Integer, primary_key=True),
    Column("text", Text, nullable=False),
    Column("page", Integer, nullable=True),
)

VECTORS = Table(
    "vectors",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("set_id", String(64), primary_key=True),
    Column("embedding_profile", String(128), primary_key=True),
    Column("chunk_index", Integer, primary_key=True),
    Column("vector", JSON, nullable=False),
)


def chunk_set_id(chunks: Sequence[tuple[str, int | None]]) -> str:
    return hashlib.sha256(json.dumps([list(item) for item in chunks], ensure_ascii=False).encode()).hexdigest()


class KnowledgeBaseStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def prepare(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)

    async def _rows(self, statement: Any) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            return [dict(row) for row in (await connection.execute(statement)).mappings().all()]

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
                "default_parser": None,
                "created_by": created_by,
                "created_at": datetime.now(UTC),
                "deleted_at": None,
            }
            await connection.execute(insert(KNOWLEDGE_BASES).values(**row))
        return row

    async def knowledge_base(self, kb_id: str) -> dict[str, Any] | None:
        rows = await self._rows(
            select(KNOWLEDGE_BASES).where(KNOWLEDGE_BASES.c.kb_id == kb_id, KNOWLEDGE_BASES.c.deleted_at.is_(None))
        )
        return rows[0] if rows else None

    async def knowledge_bases(self) -> list[dict[str, Any]]:
        return await self._rows(
            select(KNOWLEDGE_BASES)
            .where(KNOWLEDGE_BASES.c.deleted_at.is_(None))
            .order_by(KNOWLEDGE_BASES.c.created_at, KNOWLEDGE_BASES.c.kb_id)
        )

    async def update_knowledge_base(self, kb_id: str, **values: Any) -> None:
        if values:
            async with self.engine.begin() as connection:
                await connection.execute(
                    update(KNOWLEDGE_BASES).where(KNOWLEDGE_BASES.c.kb_id == kb_id).values(**values)
                )

    async def mark_deleted(self, kb_id: str) -> None:
        """Hide the knowledge base; its Scope, Sources, and Artifacts stay in core."""

        await self.update_knowledge_base(kb_id, deleted_at=datetime.now(UTC))

    # Originals and submissions

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

    async def add_submission(self, scope_id: str, **values: Any) -> dict[str, Any]:
        row = {
            "scope_id": scope_id,
            "created_at": datetime.now(UTC),
            "source_id": None,
            "parser": None,
            "processed_at": None,
            "cancelled_at": None,
            **values,
        }
        async with self.engine.begin() as connection:
            await connection.execute(insert(SUBMISSIONS).values(**row))
        return row

    async def mark_submitted(self, scope_id: str, submission_id: str, source_id: str, parser: dict[str, Any]) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                update(SUBMISSIONS)
                .where(SUBMISSIONS.c.scope_id == scope_id, SUBMISSIONS.c.submission_id == submission_id)
                .values(source_id=source_id, parser=parser)
            )

    async def mark_processed(self, scope_id: str, submission_ids: Sequence[str]) -> None:
        if submission_ids:
            async with self.engine.begin() as connection:
                await connection.execute(
                    update(SUBMISSIONS)
                    .where(SUBMISSIONS.c.scope_id == scope_id, SUBMISSIONS.c.submission_id.in_(tuple(submission_ids)))
                    .values(processed_at=datetime.now(UTC))
                )

    async def mark_discarded(self, scope_id: str, submission_ids: Sequence[str]) -> None:
        if submission_ids:
            async with self.engine.begin() as connection:
                for submission_id in submission_ids:
                    # Generation may run again for the same window; the first outcome stays.
                    exists = (
                        await connection.execute(
                            select(DISCARDED_EDITS.c.submission_id).where(
                                DISCARDED_EDITS.c.scope_id == scope_id, DISCARDED_EDITS.c.submission_id == submission_id
                            )
                        )
                    ).first()
                    if exists is None:
                        await connection.execute(
                            insert(DISCARDED_EDITS).values(
                                scope_id=scope_id, submission_id=submission_id, discarded_at=datetime.now(UTC)
                            )
                        )

    async def edit_status(
        self, scope_id: str, document_id: str, submission_id: str
    ) -> Literal["pending", "applied", "discarded"] | None:
        """pending, applied, or discarded; None when the document has no such edit."""

        rows = await self._rows(
            select(SUBMISSIONS.c.processed_at).where(
                SUBMISSIONS.c.scope_id == scope_id,
                SUBMISSIONS.c.submission_id == submission_id,
                SUBMISSIONS.c.document_id == document_id,
            )
        )
        if not rows:
            return None
        discarded = await self._rows(
            select(DISCARDED_EDITS.c.submission_id).where(
                DISCARDED_EDITS.c.scope_id == scope_id, DISCARDED_EDITS.c.submission_id == submission_id
            )
        )
        if discarded:
            return "discarded"
        return "pending" if rows[0]["processed_at"] is None else "applied"

    async def cancel_submission(self, scope_id: str, submission_id: str) -> None:
        """Withdraw an upload that never reached core; submitted ones stay as Sources."""

        async with self.engine.begin() as connection:
            await connection.execute(
                update(SUBMISSIONS)
                .where(
                    SUBMISSIONS.c.scope_id == scope_id,
                    SUBMISSIONS.c.submission_id == submission_id,
                    SUBMISSIONS.c.source_id.is_(None),
                )
                .values(cancelled_at=datetime.now(UTC))
            )

    async def latest_submissions(self, scope_id: str) -> dict[str, dict[str, Any]]:
        """The newest submission of every document in the knowledge base, ignoring cancelled uploads."""

        rows = await self._rows(
            select(SUBMISSIONS)
            .where(SUBMISSIONS.c.scope_id == scope_id, SUBMISSIONS.c.cancelled_at.is_(None))
            .order_by(SUBMISSIONS.c.created_at)
        )
        return {row["document_id"]: row for row in rows}

    # Chunk sets and vectors

    async def parsed_set(self, scope_id: str, sha256: str, parse_profile: str) -> str | None:
        rows = await self._rows(
            select(PARSED.c.set_id).where(
                PARSED.c.scope_id == scope_id, PARSED.c.sha256 == sha256, PARSED.c.parse_profile == parse_profile
            )
        )
        return rows[0]["set_id"] if rows else None

    async def save_chunk_set(
        self, scope_id: str, chunks: Sequence[tuple[str, int | None]], *, parsed_from: tuple[str, str] | None = None
    ) -> str:
        set_id = chunk_set_id(chunks)
        async with self.engine.begin() as connection:
            stored = (
                await connection.execute(
                    select(CHUNKS.c.chunk_index)
                    .where(CHUNKS.c.scope_id == scope_id, CHUNKS.c.set_id == set_id)
                    .limit(1)
                )
            ).first()
            if stored is None:
                await connection.execute(
                    insert(CHUNKS),
                    [
                        {"scope_id": scope_id, "set_id": set_id, "chunk_index": index, "text": text, "page": page}
                        for index, (text, page) in enumerate(chunks)
                    ],
                )
            if parsed_from is not None:
                with suppress(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(
                            insert(PARSED).values(
                                scope_id=scope_id, sha256=parsed_from[0], parse_profile=parsed_from[1], set_id=set_id
                            )
                        )
        return set_id

    async def chunks(self, scope_id: str, set_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not set_ids:
            return []
        return await self._rows(
            select(CHUNKS)
            .where(CHUNKS.c.scope_id == scope_id, CHUNKS.c.set_id.in_(tuple(set_ids)))
            .order_by(CHUNKS.c.set_id, CHUNKS.c.chunk_index)
        )

    async def vectors(
        self, scope_id: str, set_ids: Sequence[str], profile: str
    ) -> dict[tuple[str, int], Sequence[float]]:
        if not set_ids:
            return {}
        rows = await self._rows(
            select(VECTORS).where(
                VECTORS.c.scope_id == scope_id,
                VECTORS.c.set_id.in_(tuple(set_ids)),
                VECTORS.c.embedding_profile == profile,
            )
        )
        return {(row["set_id"], row["chunk_index"]): row["vector"] for row in rows}

    async def save_vectors(self, scope_id: str, profile: str, vectors: dict[tuple[str, int], Sequence[float]]) -> None:
        async with self.engine.begin() as connection:
            for (set_id, index), vector in vectors.items():
                with suppress(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(
                            insert(VECTORS).values(
                                scope_id=scope_id,
                                set_id=set_id,
                                embedding_profile=profile,
                                chunk_index=index,
                                vector=[float(value) for value in vector],
                            )
                        )

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

"""A knowledge base (RAG) plugin: every knowledge base is a Scope, every file a ``document`` Artifact.

Callers see knowledge bases and documents only. Creating a knowledge base creates a
Scope and records the mapping in the plugin's own storage. Uploads keep their bytes there
by content digest and wait until a parser is chosen, unless the knowledge base has a
default parser. Processing submits a Source that records the digest and the parser;
generation parses the file into a chunk set and writes the ``document`` Artifact, whose
revision names that set. Users can read and edit a document's chunks; an edit writes a
new revision naming the edited set, so history and restoring work as for any Artifact.
Search ranks the chunks of current documents by keyword and vector similarity; ``ask``
answers from the top chunks with the Server's generation model. The ``ui`` directory is
the Dashboard page for managing knowledge bases.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, Literal
from uuid import uuid4

from kb_parsers import MEDIA_TYPES, PARSERS, DocumentError, ParserChoice, media_type_for, parse, pdf_available, resolve
from kb_store import KnowledgeBaseStore
from kb_text import bm25, cosine, hash_embedding, reciprocal_rank_fusion
from pydantic import BaseModel, Field

from powercontext.plugins import (
    ApiError,
    ApiRequest,
    ArtifactPlugin,
    ArtifactWriter,
    BinaryResponse,
    ChatMessage,
    ForgetRequest,
    GenerateRequest,
    Inputs,
    PluginApi,
    PluginContext,
    PluginPages,
    RestoreRequest,
    Route,
)
from powercontext.sources import Source, SourceMaterialization

FAMILY = "document"
DOCUMENT_SOURCE = "kb-document"
PROJECTION = "knowledge-base.document"
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_CHUNKS = 2000


# The processing Source: the bytes stay in plugin storage; the Source records which file and which parser.


class DocumentUpload(BaseModel):
    upload_id: str
    knowledge_base_id: str
    name: str
    media_type: str
    sha256: str
    size: int
    uploaded_by: str
    parser: ParserChoice


class DocumentUploadSource(Source):
    knowledge_base_id: str
    file_name: str
    media_type: str
    sha256: str
    size: int
    uploaded_by: str
    parser: ParserChoice


class DocumentUploadProjection:
    name = PROJECTION
    version = "2"
    source_class = DocumentUploadSource
    output_class: type[BaseModel] = DocumentUpload

    def project(self, source: DocumentUploadSource, /) -> DocumentUpload:
        return DocumentUpload(
            upload_id=source.name,
            knowledge_base_id=source.knowledge_base_id,
            name=source.file_name,
            media_type=source.media_type,
            sha256=source.sha256,
            size=source.size,
            uploaded_by=source.uploaded_by,
            parser=source.parser,
        )


class DocumentUploadDefinition:
    input_class = DocumentUpload
    name = DOCUMENT_SOURCE
    source_class = DocumentUploadSource
    version = "2"
    projections = (DocumentUploadProjection(),)

    async def resolve(self, value: DocumentUpload, /) -> DocumentUploadSource:
        return DocumentUploadSource(
            name=value.upload_id,
            definition_version=self.version,
            materialization=SourceMaterialization.CAPTURED,
            description="A request to parse a knowledge base file; its bytes live in the plugin's storage.",
            knowledge_base_id=value.knowledge_base_id,
            file_name=value.name,
            media_type=value.media_type,
            sha256=value.sha256,
            size=value.size,
            uploaded_by=value.uploaded_by,
            parser=value.parser,
        )

    async def read(self, source: DocumentUploadSource, /) -> DocumentUpload:
        return DocumentUploadProjection().project(source)


# API models


class NewKnowledgeBase(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=2000)
    idempotency_key: str | None = Field(default=None, max_length=128)


class KnowledgeBaseChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    # Sending null clears the default, so uploads wait for a parser again.
    default_parser: ParserChoice | None = None


class KnowledgeBase(BaseModel):
    id: str
    name: str
    description: str
    default_parser: ParserChoice | None
    created_at: str


class KnowledgeBasePage(BaseModel):
    items: list[KnowledgeBase]


DocumentStatus = Literal["pending_config", "processing", "ready", "failed"]


class Document(BaseModel):
    id: str
    knowledge_base_id: str
    name: str
    status: DocumentStatus
    media_type: str | None = None
    size: int | None = None
    parser: ParserChoice | None = None
    chunk_count: int | None = None
    edited: bool = False
    revision: int | None = None
    error: str | None = None


class DocumentPage(BaseModel):
    items: list[Document]


class DocumentContent(BaseModel):
    """The ``document`` Artifact content: the file, how it was parsed, and the chunk set in use."""

    knowledge_base_id: str
    name: str
    # The upload or reprocessing request this revision's chunks came from; edits keep it.
    submission_id: str
    media_type: str
    sha256: str
    size: int
    uploaded_by: str
    parser: ParserChoice
    chunk_set: str | None
    chunk_count: int
    # True once a user edited the chunks; a new upload or reprocessing replaces them.
    edited: bool = False
    status: Literal["ready", "failed"]
    error: str | None = None


class ProcessRequest(BaseModel):
    parser: ParserChoice
    # None processes every document waiting for a parser; listed documents are (re)processed.
    document_ids: list[str] | None = Field(default=None, max_length=1000)
    make_default: bool = False


class ProcessResult(BaseModel):
    submitted: int


class Chunk(BaseModel):
    index: int
    text: str
    page: int | None = None


class DocumentChunks(BaseModel):
    document_id: str
    revision: int
    parser: ParserChoice
    edited: bool
    chunks: list[Chunk]


class ChunkInput(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    page: int | None = Field(default=None, ge=1)


class ChunkEdit(BaseModel):
    expected_revision: int = Field(ge=1)
    chunks: list[ChunkInput] = Field(min_length=1, max_length=MAX_CHUNKS)


class RestoreRevision(BaseModel):
    revision: int = Field(ge=1)


class ParameterInfo(BaseModel):
    name: str
    default: int
    minimum: int
    maximum: int


class ParserInfo(BaseModel):
    name: str
    title: dict[str, str]
    description: dict[str, str]
    parameters: list[ParameterInfo]


class ParserPage(BaseModel):
    items: list[ParserInfo]


class SearchQuery(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=20)
    limit: int = Field(default=5, ge=1, le=50)
    mode: Literal["hybrid", "keyword", "vector"] = "hybrid"


class SearchHit(BaseModel):
    knowledge_base_id: str
    document_id: str
    document_name: str
    chunk_index: int
    page: int | None = None
    text: str
    score: float


class SearchResult(BaseModel):
    hits: list[SearchHit]


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=20)
    limit: int = Field(default=5, ge=1, le=20)


class Answer(BaseModel):
    # None when the Server has no generation model; the citations are still the retrieved chunks.
    answer: str | None
    citations: list[SearchHit]


class PluginInfo(BaseModel):
    embedding_profile: str
    generation_available: bool
    # Creating and deleting knowledge bases needs server.admin.
    can_manage: bool
    max_upload_bytes: int
    formats: list[str]


ANSWER_INSTRUCTIONS = (
    "Answer the question using only the numbered passages. Cite passages as [1], [2]. "
    "If the passages do not contain the answer, say so. Answer in the language of the question."
)


def document_id(knowledge_base_id: str, name: str) -> str:
    """One document per file name; uploading the same name again adds a revision."""

    return hashlib.sha256(f"document\0{knowledge_base_id}\0{name}".encode()).hexdigest()[:32]


def _parser(choice: ParserChoice) -> ParserChoice:
    try:
        return resolve(choice)
    except ValueError as error:
        raise ApiError(422, "invalid_parser", str(error)) from None


class KnowledgeBasePlugin:
    def __init__(self, ctx: PluginContext, store: KnowledgeBaseStore) -> None:
        self.ctx = ctx
        self.store = store
        self.served = ctx.models is not None and ctx.models.embedding_available
        dimension = None if ctx.models is None else ctx.models.embedding_dimension
        self.profile = f"server-{dimension}" if self.served else "hash-256"

    async def _embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        if not self.served or self.ctx.models is None or not texts:
            return [hash_embedding(text) for text in texts]
        return list(await self.ctx.models.embed(texts))

    async def _indexed(self, scope_id: str, set_ids: list[str]) -> list[tuple[dict[str, Any], Sequence[float]]]:
        """Chunks of the sets with vectors for the current embedding profile, embedding any that lack one."""

        rows = await self.store.chunks(scope_id, set_ids)
        vectors = await self.store.vectors(scope_id, set_ids, self.profile)
        missing = [row for row in rows if (row["set_id"], row["chunk_index"]) not in vectors]
        if missing:
            computed = await self._embed([row["text"] for row in missing])
            fresh: dict[tuple[str, int], Sequence[float]] = {
                (row["set_id"], row["chunk_index"]): vector for row, vector in zip(missing, computed, strict=True)
            }
            await self.store.save_vectors(scope_id, self.profile, fresh)
            vectors.update(fresh)
        return [(row, vectors[(row["set_id"], row["chunk_index"])]) for row in rows]

    async def _knowledge_base(self, kb_id: str, action: Literal["scope.read", "scope.contribute"]) -> dict[str, Any]:
        record = await self.store.knowledge_base(kb_id)
        if record is None or not await self.ctx.allowed(action, record["scope_id"]):
            # Callers without access cannot tell a hidden knowledge base from a missing one.
            raise ApiError(404, "knowledge_base_not_found", "The knowledge base was not found.")
        return record

    @staticmethod
    def _knowledge_base_response(record: dict[str, Any]) -> KnowledgeBase:
        default = record.get("default_parser")
        return KnowledgeBase(
            id=record["kb_id"],
            name=record["name"],
            description=record["description"],
            default_parser=None if default is None else ParserChoice.model_validate(default),
            created_at=record["created_at"].isoformat(),
        )

    async def info(self, request: ApiRequest) -> PluginInfo:
        formats = [suffix for suffix, media in MEDIA_TYPES.items() if media != "application/pdf" or pdf_available()]
        return PluginInfo(
            embedding_profile=self.profile,
            generation_available=self.ctx.models is not None and self.ctx.models.generation_available,
            can_manage=await self.ctx.allowed("server.admin"),
            max_upload_bytes=MAX_UPLOAD_BYTES,
            formats=formats,
        )

    async def parsers(self, request: ApiRequest) -> ParserPage:
        return ParserPage(
            items=[
                ParserInfo(
                    name=parser.name,
                    title=parser.title,
                    description=parser.description,
                    parameters=[
                        ParameterInfo(name=item.name, default=item.default, minimum=item.minimum, maximum=item.maximum)
                        for item in parser.parameters
                    ],
                )
                for parser in PARSERS.values()
            ]
        )

    # Knowledge bases

    async def create_knowledge_base(self, request: ApiRequest) -> KnowledgeBase:
        body: NewKnowledgeBase = request.body
        if any(item["name"] == body.name for item in await self.store.knowledge_bases()):
            raise ApiError(409, "knowledge_base_exists", "A knowledge base with this name already exists.")
        key = body.idempotency_key or uuid4().hex
        scope = await self.ctx.scopes.create(
            title=body.name, summary=body.description or f"Knowledge base {body.name}", idempotency_key=key
        )
        record = await self.store.add_knowledge_base(
            f"kb_{uuid4().hex[:20]}", scope.scope_id, body.name, body.description, request.caller.principal_id
        )
        return self._knowledge_base_response(record)

    async def list_knowledge_bases(self, request: ApiRequest) -> KnowledgeBasePage:
        visible = [
            self._knowledge_base_response(record)
            for record in await self.store.knowledge_bases()
            if await self.ctx.allowed("scope.read", record["scope_id"])
        ]
        return KnowledgeBasePage(items=visible)

    async def get_knowledge_base(self, request: ApiRequest) -> KnowledgeBase:
        return self._knowledge_base_response(await self._knowledge_base(request.path_params["kb_id"], "scope.read"))

    async def update_knowledge_base(self, request: ApiRequest) -> KnowledgeBase:
        body: KnowledgeBaseChange = request.body
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.contribute")
        values: dict[str, Any] = {
            key: value for key, value in (("name", body.name), ("description", body.description)) if value is not None
        }
        if "default_parser" in body.model_fields_set:
            values["default_parser"] = (
                None if body.default_parser is None else _parser(body.default_parser).model_dump(mode="json")
            )
        await self.store.update_knowledge_base(record["kb_id"], **values)
        return self._knowledge_base_response(await self._knowledge_base(record["kb_id"], "scope.read"))

    async def delete_knowledge_base(self, request: ApiRequest) -> None:
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.read")
        # Deleting a knowledge base is as privileged as creating one; core keeps all its data.
        await self.ctx.require("server.admin")
        await self.store.mark_deleted(record["kb_id"])

    # Documents

    async def upload_document(self, request: ApiRequest) -> Document:
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.contribute")
        name = (request.query.get("name") or [""])[0].strip()
        media_type = media_type_for(name)
        if not name or media_type is None or (media_type == "application/pdf" and not pdf_available()):
            raise ApiError(415, "unsupported_document", "This file type is not supported on this Server.")
        # The route declares MAX_UPLOAD_BYTES; core rejects larger bodies before this handler runs.
        content: bytes = request.body
        if not content:
            raise ApiError(400, "empty_document", "The document is empty.")
        if media_type == "application/pdf" and not content.startswith(b"%PDF-"):
            raise ApiError(415, "unsupported_document", "The file is not a PDF.")
        if media_type != "application/pdf":
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                raise ApiError(415, "unsupported_encoding", "Text documents must be UTF-8.") from None
        scope_id = record["scope_id"]
        sha256 = hashlib.sha256(content).hexdigest()
        await self.store.save_original(scope_id, sha256, media_type, content)
        submission = await self.store.add_submission(
            scope_id,
            submission_id=f"kbdoc_{uuid4().hex}",
            document_id=document_id(record["kb_id"], name),
            name=name,
            sha256=sha256,
            media_type=media_type,
            size=len(content),
            uploaded_by=request.caller.principal_id,
        )
        default = record.get("default_parser")
        status: DocumentStatus = "pending_config"
        if default is not None:
            await self._submit(record, submission, ParserChoice.model_validate(default))
            status = "processing"
        return Document(
            id=submission["document_id"],
            knowledge_base_id=record["kb_id"],
            name=name,
            status=status,
            media_type=media_type,
            size=len(content),
        )

    async def _submit(self, record: dict[str, Any], submission: dict[str, Any], parser: ParserChoice) -> None:
        ref = await self.ctx.sources.submit(
            record["scope_id"],
            DOCUMENT_SOURCE,
            DocumentUpload(
                upload_id=submission["submission_id"],
                knowledge_base_id=record["kb_id"],
                name=submission["name"],
                media_type=submission["media_type"],
                sha256=submission["sha256"],
                size=submission["size"],
                uploaded_by=submission["uploaded_by"],
                parser=parser,
            ),
        )
        await self.store.mark_submitted(
            record["scope_id"], submission["submission_id"], ref.source_id, parser.model_dump(mode="json")
        )

    async def process_documents(self, request: ApiRequest) -> ProcessResult:
        body: ProcessRequest = request.body
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.contribute")
        parser = _parser(body.parser)
        latest = await self.store.latest_submissions(record["scope_id"])
        if body.document_ids is None:
            targets = [item for item in latest.values() if item["source_id"] is None]
        else:
            targets = []
            for identity in dict.fromkeys(body.document_ids):
                found = latest.get(identity)
                if found is None:
                    raise ApiError(404, "document_not_found", "The document was not found.")
                if found["source_id"] is not None:
                    # Reprocessing records a new submission of the same file with the new parser.
                    found = await self.store.add_submission(
                        record["scope_id"],
                        **{
                            key: found[key]
                            for key in ("document_id", "name", "sha256", "media_type", "size", "uploaded_by")
                        },
                        submission_id=f"kbdoc_{uuid4().hex}",
                    )
                targets.append(found)
        for submission in targets:
            await self._submit(record, submission, parser)
        if body.make_default:
            await self.store.update_knowledge_base(record["kb_id"], default_parser=parser.model_dump(mode="json"))
        return ProcessResult(submitted=len(targets))

    async def _documents(self, record: dict[str, Any]) -> dict[str, Document]:
        reads = self.ctx.reads(record["scope_id"])
        documents: dict[str, Document] = {}
        cursor = None
        while True:
            page = await reads.list(cursor=cursor, limit=200)
            for snapshot in page.items:
                content = DocumentContent.model_validate(snapshot.content)
                documents[snapshot.ref.artifact_id] = Document(
                    id=snapshot.ref.artifact_id,
                    knowledge_base_id=record["kb_id"],
                    name=content.name,
                    status=content.status,
                    media_type=content.media_type,
                    size=content.size,
                    parser=content.parser,
                    chunk_count=content.chunk_count,
                    edited=content.edited,
                    revision=snapshot.ref.revision,
                    error=content.error,
                )
            cursor = page.next_cursor
            if cursor is None:
                break
        # A document's newest submission waits for a parser, or is processing until generation
        # handles it; a forgotten document has consumed its submission and stays hidden.
        for identity, submission in (await self.store.latest_submissions(record["scope_id"])).items():
            status: DocumentStatus | None = None
            if submission["source_id"] is None:
                status = "pending_config"
            elif submission["processed_at"] is None:
                status = "processing"
            if status is not None:
                current = documents.get(identity)
                documents[identity] = Document.model_validate({
                    **(current.model_dump() if current is not None else {}),
                    "id": identity,
                    "knowledge_base_id": record["kb_id"],
                    "name": submission["name"],
                    "status": status,
                    "media_type": submission["media_type"],
                    "size": submission["size"],
                })
        return documents

    async def list_documents(self, request: ApiRequest) -> DocumentPage:
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.read")
        documents = await self._documents(record)
        return DocumentPage(items=sorted(documents.values(), key=lambda item: item.name))

    async def get_document(self, request: ApiRequest) -> Document:
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.read")
        found = (await self._documents(record)).get(request.path_params["document_id"])
        if found is None:
            raise ApiError(404, "document_not_found", "The document was not found.")
        return found

    async def get_document_content(self, request: ApiRequest) -> BinaryResponse:
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.read")
        snapshot = await self.ctx.reads(record["scope_id"]).get(request.path_params["document_id"])
        # The original is looked up within this knowledge base's Scope only.
        document = None if snapshot is None else DocumentContent.model_validate(snapshot.content)
        original = None if document is None else await self.store.original(record["scope_id"], document.sha256)
        if document is None or original is None:
            raise ApiError(404, "document_not_found", "The document was not found.")
        content, media_type = original
        return BinaryResponse(content=content, content_type=media_type, filename=document.name)

    async def delete_document(self, request: ApiRequest) -> None:
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.contribute")
        writer = self.ctx.writer(record["scope_id"])
        if await writer.get(request.path_params["document_id"]) is None:
            raise ApiError(404, "document_not_found", "The document was not found.")
        async with writer.transaction() as tx:
            await tx.set_lifecycle(request.path_params["document_id"], "deprecated")

    # Chunks

    async def get_chunks(self, request: ApiRequest) -> DocumentChunks:
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.read")
        revision = (request.query.get("revision") or [None])[0]
        if revision is not None and not revision.isdecimal():
            raise ApiError(422, "invalid_revision", "revision must be a positive integer.")
        snapshot = await self.ctx.reads(record["scope_id"]).get(
            request.path_params["document_id"], revision=None if revision is None else int(revision)
        )
        if snapshot is None:
            raise ApiError(404, "document_not_found", "The document was not found.")
        content = DocumentContent.model_validate(snapshot.content)
        rows = [] if content.chunk_set is None else await self.store.chunks(record["scope_id"], [content.chunk_set])
        return DocumentChunks(
            document_id=snapshot.ref.artifact_id,
            revision=snapshot.ref.revision,
            parser=content.parser,
            edited=content.edited,
            chunks=[Chunk(index=row["chunk_index"], text=row["text"], page=row["page"]) for row in rows],
        )

    async def edit_chunks(self, request: ApiRequest) -> Document:
        body: ChunkEdit = request.body
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.contribute")
        identity = request.path_params["document_id"]
        writer = self.ctx.writer(record["scope_id"])
        current = await writer.get(identity)
        if current is None:
            raise ApiError(404, "document_not_found", "The document was not found.")
        if current.ref.revision != body.expected_revision:
            raise ApiError(409, "document_changed", "The document changed after it was read; reload and edit again.")
        content = DocumentContent.model_validate(current.content)
        if content.status != "ready":
            raise ApiError(409, "document_not_ready", "Only processed documents have chunks to edit.")
        scope_id = record["scope_id"]
        set_id = await self.store.save_chunk_set(scope_id, [(item.text.strip(), item.page) for item in body.chunks])
        # Embed before the transaction; the vectors are keyed by set and reused if the write is retried.
        await self._indexed(scope_id, [set_id])
        edited = content.model_copy(update={"chunk_set": set_id, "chunk_count": len(body.chunks), "edited": True})
        async with writer.transaction() as tx:
            snapshot = await tx.put(
                identity,
                content=edited.model_dump(mode="json"),
                expected_revision=body.expected_revision,
            )
        return Document(
            id=identity,
            knowledge_base_id=record["kb_id"],
            name=edited.name,
            status=edited.status,
            media_type=edited.media_type,
            size=edited.size,
            parser=edited.parser,
            chunk_count=edited.chunk_count,
            edited=True,
            revision=snapshot.ref.revision,
        )

    async def restore_revision(self, request: ApiRequest) -> None:
        body: RestoreRevision = request.body
        record = await self._knowledge_base(request.path_params["kb_id"], "scope.contribute")
        writer = self.ctx.writer(record["scope_id"])
        async with writer.transaction() as tx:
            await tx.revert(request.path_params["document_id"], to_revision=body.revision)

    # Search

    async def search(self, request: ApiRequest) -> SearchResult:
        body: SearchQuery = request.body
        return SearchResult(hits=await self._search(body.query, body.knowledge_base_ids, body.limit, body.mode))

    async def ask(self, request: ApiRequest) -> Answer:
        body: Question = request.body
        hits = await self._search(body.question, body.knowledge_base_ids, body.limit, "hybrid")
        models = self.ctx.models
        if not hits or models is None or not models.generation_available:
            return Answer(answer=None, citations=hits)
        passages = "\n\n".join(
            f"[{number}] ({hit.document_name}{'' if hit.page is None else f', p.{hit.page}'})\n{hit.text}"
            for number, hit in enumerate(hits, 1)
        )
        answer = await models.generate(
            [
                ChatMessage(role="system", content=ANSWER_INSTRUCTIONS),
                ChatMessage(role="user", content=f"Passages:\n\n{passages}\n\nQuestion: {body.question}"),
            ],
            max_tokens=800,
        )
        return Answer(answer=answer.strip() or None, citations=hits)

    async def _search(
        self, query: str, knowledge_base_ids: list[str], limit: int, mode: Literal["hybrid", "keyword", "vector"]
    ) -> list[SearchHit]:
        candidates = [item for kb_id in dict.fromkeys(knowledge_base_ids) for item in await self._candidates(kb_id)]
        if not candidates:
            return []
        texts = [row["text"] for _, _, _, row, _ in candidates]
        rankings = []
        scores: dict[int, float] = {}
        if mode in {"hybrid", "keyword"}:
            keyword = bm25(query, texts)
            rankings.append([
                index for index in sorted(range(len(texts)), key=lambda i: -keyword[i]) if keyword[index] > 0
            ])
            # Keyword search returns only chunks that share a term with the query.
            scores = {index: score for index, score in enumerate(keyword) if score > 0}
        if mode in {"hybrid", "vector"}:
            (query_vector,) = await self._embed([query])
            similarity = [cosine(query_vector, vector) for _, _, _, _, vector in candidates]
            rankings.append(sorted(range(len(texts)), key=lambda i: -similarity[i]))
            scores = dict(enumerate(similarity))
        if mode == "hybrid":
            scores = reciprocal_rank_fusion(rankings)
        ranked = sorted(scores, key=lambda index: -scores[index])[:limit]
        return [
            SearchHit(
                knowledge_base_id=candidates[index][0],
                document_id=candidates[index][1],
                document_name=candidates[index][2],
                chunk_index=candidates[index][3]["chunk_index"],
                page=candidates[index][3]["page"],
                text=candidates[index][3]["text"],
                score=round(scores[index], 6),
            )
            for index in ranked
        ]

    async def _candidates(self, kb_id: str) -> list[tuple[str, str, str, dict[str, Any], Sequence[float]]]:
        """Every chunk of the current revisions of the knowledge base's ready documents."""

        record = await self._knowledge_base(kb_id, "scope.read")
        reads = self.ctx.reads(record["scope_id"])
        # Identical files share a chunk set, so one set may back several documents.
        documents_by_set: dict[str, list[tuple[str, str]]] = {}
        for identity, document in (await self._documents(record)).items():
            if document.status != "ready":
                continue
            snapshot = await reads.get(identity)
            chunk_set = None if snapshot is None else DocumentContent.model_validate(snapshot.content).chunk_set
            if chunk_set is not None:
                documents_by_set.setdefault(chunk_set, []).append((identity, document.name))
        return [
            (kb_id, identity, name, row, vector)
            for row, vector in await self._indexed(record["scope_id"], list(documents_by_set))
            for identity, name in documents_by_set[row["set_id"]]
        ]

    # Generation: parse and embed outside the write transaction, then commit the documents.

    async def generate(self, request: GenerateRequest, writer: ArtifactWriter) -> None:
        planned = []
        for source in request.sources:
            upload = DocumentUpload.model_validate(source.projection)
            content = DocumentContent(
                knowledge_base_id=upload.knowledge_base_id,
                name=upload.name,
                submission_id=upload.upload_id,
                media_type=upload.media_type,
                sha256=upload.sha256,
                size=upload.size,
                uploaded_by=upload.uploaded_by,
                parser=upload.parser,
                chunk_set=None,
                chunk_count=0,
                status="ready",
            )
            try:
                set_id = await self._parsed(request.scope_id, upload)
            except DocumentError as error:
                content = content.model_copy(update={"status": "failed", "error": str(error)})
            else:
                # Content-addressed and immutable: a retry rewrites nothing, a rollback leaves only unreferenced rows.
                indexed = await self._indexed(request.scope_id, [set_id])
                content = content.model_copy(update={"chunk_set": set_id, "chunk_count": len(indexed)})
            planned.append((document_id(upload.knowledge_base_id, upload.name), content, source.ref))
        # Marked before the commit: if the commit fails, core delivers the Sources again and
        # this runs again, so the mark is at worst briefly ahead of the document.
        await self.store.mark_processed(request.scope_id, [content.submission_id for _, content, _ in planned])
        async with writer.transaction() as tx:
            for identity, content, ref in planned:
                await tx.put(identity, content=content.model_dump(mode="json"), source_refs=[ref])

    async def _parsed(self, scope_id: str, upload: DocumentUpload) -> str:
        """The chunk set of a file under a parser profile; the same file and settings are parsed once."""

        profile = upload.parser.profile
        cached = await self.store.parsed_set(scope_id, upload.sha256, profile)
        if cached is not None:
            return cached
        original = await self.store.original(scope_id, upload.sha256)
        if original is None:
            raise DocumentError("the uploaded file is missing from plugin storage")  # noqa: TRY003
        pieces = parse(original[0], upload.media_type, upload.parser)
        if len(pieces) > MAX_CHUNKS:
            raise DocumentError(f"the document has more than {MAX_CHUNKS} chunks; choose larger chunks")  # noqa: TRY003
        return await self.store.save_chunk_set(
            scope_id, [(piece.text, piece.page) for piece in pieces], parsed_from=(upload.sha256, profile)
        )

    # Generic Artifact API: forgetting and restoring a document work like the plugin routes.

    async def forget_artifact(self, request: ForgetRequest, writer: ArtifactWriter) -> None:
        async with writer.transaction() as tx:
            await tx.set_lifecycle(request.artifact_id, "deprecated")

    async def restore_artifact(self, request: RestoreRequest, writer: ArtifactWriter) -> None:
        async with writer.transaction() as tx:
            if request.revision is not None:
                await tx.revert(request.artifact_id, to_revision=request.revision)
            else:
                await tx.set_lifecycle(request.artifact_id, "active")


async def create(ctx: PluginContext) -> KnowledgeBasePlugin:
    store = KnowledgeBaseStore(ctx.storage.engine())
    await store.prepare()
    return KnowledgeBasePlugin(ctx, store)


def _route(method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"], path: str, handler: str, **options: Any) -> Route:
    return Route(method=method, path=path, handler=handler, access="authenticated", **options)


KB = "/knowledge-bases/{kb_id}"
DOC = KB + "/documents/{document_id}"

plugin = ArtifactPlugin(
    family=FAMILY,
    inputs=Inputs(source_types=(DOCUMENT_SOURCE,), projection=f"{PROJECTION}@2", max_sources=20),
    on_sources=True,
    search_fields=("name",),
    timeout_seconds=900,
    definitions=(DocumentUploadDefinition(),),
    api=PluginApi(
        routes=(
            _route("GET", "/info", "info", response_model=PluginInfo),
            _route("GET", "/parsers", "parsers", response_model=ParserPage),
            _route(
                "POST",
                "/knowledge-bases",
                "create_knowledge_base",
                body="json",
                request_model=NewKnowledgeBase,
                response_model=KnowledgeBase,
            ),
            _route("GET", "/knowledge-bases", "list_knowledge_bases", response_model=KnowledgeBasePage),
            _route("GET", KB, "get_knowledge_base", response_model=KnowledgeBase),
            _route(
                "PATCH",
                KB,
                "update_knowledge_base",
                body="json",
                request_model=KnowledgeBaseChange,
                response_model=KnowledgeBase,
            ),
            _route("DELETE", KB, "delete_knowledge_base"),
            _route(
                "POST",
                KB + "/documents",
                "upload_document",
                body="binary",
                max_body_bytes=MAX_UPLOAD_BYTES,
                response_model=Document,
            ),
            _route("GET", KB + "/documents", "list_documents", response_model=DocumentPage),
            _route(
                "POST",
                KB + "/processing",
                "process_documents",
                body="json",
                request_model=ProcessRequest,
                response_model=ProcessResult,
            ),
            _route("GET", DOC, "get_document", response_model=Document),
            _route("GET", DOC + "/content", "get_document_content"),
            _route("DELETE", DOC, "delete_document"),
            _route("GET", DOC + "/chunks", "get_chunks", response_model=DocumentChunks),
            _route(
                "PUT",
                DOC + "/chunks",
                "edit_chunks",
                body="json",
                max_body_bytes=4 * 1024 * 1024,
                request_model=ChunkEdit,
                response_model=Document,
                timeout_seconds=120,
            ),
            _route("POST", DOC + "/restorations", "restore_revision", body="json", request_model=RestoreRevision),
            _route(
                "POST",
                "/search",
                "search",
                body="json",
                request_model=SearchQuery,
                response_model=SearchResult,
                description="Search the chunks of current documents in the given knowledge bases.",
            ),
            _route(
                "POST",
                "/ask",
                "ask",
                body="json",
                request_model=Question,
                response_model=Answer,
                timeout_seconds=90,
                description="Answer a question from the given knowledge bases, citing the retrieved chunks.",
            ),
        )
    ),
    pages=PluginPages(directory="ui", entry="index.html", title={"zh": "知识库", "en": "Knowledge bases"}),
    create=create,
)

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
Scope and records the mapping in the plugin's own storage; uploads keep their bytes
there by content digest and submit a Source that records only the digest; generation
chunks and embeds the text into plugin storage and writes the ``document`` Artifact.
Search ranks chunks of current documents by keyword and vector similarity; ``ask``
answers a question from the top chunks with the Server's generation model. The
``ui`` directory is the Dashboard page for managing knowledge bases.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal
from uuid import uuid4

from kb_store import KnowledgeBaseStore
from kb_text import bm25, chunk, cosine, decode, hash_embedding, media_type_for, reciprocal_rank_fusion
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
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


# The upload Source: the bytes stay in plugin storage; the Source records where.


class DocumentUpload(BaseModel):
    upload_id: str
    knowledge_base_id: str
    name: str
    media_type: str
    sha256: str
    size: int
    uploaded_by: str


class DocumentUploadSource(Source):
    knowledge_base_id: str
    file_name: str
    media_type: str
    sha256: str
    size: int
    uploaded_by: str


class DocumentUploadProjection:
    name = PROJECTION
    version = "1"
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
        )


class DocumentUploadDefinition:
    input_class = DocumentUpload
    name = DOCUMENT_SOURCE
    source_class = DocumentUploadSource
    version = "1"
    projections = (DocumentUploadProjection(),)

    async def resolve(self, value: DocumentUpload, /) -> DocumentUploadSource:
        return DocumentUploadSource(
            name=value.upload_id,
            materialization=SourceMaterialization.CAPTURED,
            description="A file uploaded to a knowledge base; its bytes live in the plugin's storage.",
            knowledge_base_id=value.knowledge_base_id,
            file_name=value.name,
            media_type=value.media_type,
            sha256=value.sha256,
            size=value.size,
            uploaded_by=value.uploaded_by,
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


class KnowledgeBase(BaseModel):
    id: str
    name: str
    description: str
    created_at: str


class KnowledgeBasePage(BaseModel):
    items: list[KnowledgeBase]


class Document(BaseModel):
    id: str
    knowledge_base_id: str
    name: str
    status: Literal["processing", "ready", "failed"]
    media_type: str | None = None
    size: int | None = None
    chunk_count: int | None = None
    revision: int | None = None
    error: str | None = None


class DocumentPage(BaseModel):
    items: list[Document]


class DocumentContent(BaseModel):
    """The ``document`` Artifact content generation writes."""

    knowledge_base_id: str
    name: str
    media_type: str
    sha256: str
    size: int
    uploaded_by: str
    embedding_profile: str
    chunk_count: int
    status: Literal["ready", "failed"]
    error: str | None = None


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


ANSWER_INSTRUCTIONS = (
    "Answer the question using only the numbered passages. Cite passages as [1], [2]. "
    "If the passages do not contain the answer, say so. Answer in the language of the question."
)


def document_id(knowledge_base_id: str, name: str) -> str:
    """One document per file name; uploading the same name again adds a revision."""

    return hashlib.sha256(f"document\0{knowledge_base_id}\0{name}".encode()).hexdigest()[:32]


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

    async def _knowledge_base(self, kb_id: str, action: Literal["scope.read", "scope.contribute"]) -> dict[str, Any]:
        record = await self.store.knowledge_base(kb_id)
        if record is None or not await self.ctx.allowed(action, record["scope_id"]):
            # Callers without access cannot tell a hidden knowledge base from a missing one.
            raise ApiError(404, "knowledge_base_not_found", "The knowledge base was not found.")
        return record

    @staticmethod
    def _knowledge_base_response(record: dict[str, Any]) -> KnowledgeBase:
        return KnowledgeBase(
            id=record["kb_id"],
            name=record["name"],
            description=record["description"],
            created_at=record["created_at"].isoformat(),
        )

    async def info(self, request: ApiRequest) -> PluginInfo:
        return PluginInfo(
            embedding_profile=self.profile,
            generation_available=self.ctx.models is not None and self.ctx.models.generation_available,
            can_manage=await self.ctx.allowed("server.admin"),
            max_upload_bytes=MAX_UPLOAD_BYTES,
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
        await self.store.rename(record["kb_id"], name=body.name, description=body.description)
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
        if not name or media_type is None:
            raise ApiError(415, "unsupported_document", "Upload a .md, .markdown, or .txt file with ?name=.")
        # The route declares MAX_UPLOAD_BYTES; core rejects larger bodies before this handler runs.
        content: bytes = request.body
        if not content:
            raise ApiError(400, "empty_document", "The document is empty.")
        try:
            decode(content)
        except UnicodeDecodeError:
            raise ApiError(415, "unsupported_encoding", "Documents must be UTF-8 text.") from None
        scope_id = record["scope_id"]
        sha256 = hashlib.sha256(content).hexdigest()
        await self.store.save_original(scope_id, sha256, media_type, content)
        identity = document_id(record["kb_id"], name)
        upload_id = f"kbdoc_{uuid4().hex}"
        await self.ctx.sources.submit(
            scope_id,
            DOCUMENT_SOURCE,
            DocumentUpload(
                upload_id=upload_id,
                knowledge_base_id=record["kb_id"],
                name=name,
                media_type=media_type,
                sha256=sha256,
                size=len(content),
                uploaded_by=request.caller.principal_id,
            ),
        )
        await self.store.record_upload(scope_id, upload_id, identity, name, sha256)
        return Document(
            id=identity,
            knowledge_base_id=record["kb_id"],
            name=name,
            status="processing",
            media_type=media_type,
            size=len(content),
        )

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
                    chunk_count=content.chunk_count,
                    revision=snapshot.ref.revision,
                    error=content.error,
                )
            cursor = page.next_cursor
            if cursor is None:
                break
        # The latest upload of a document is pending until a revision lists it in its lineage;
        # a forgotten document has consumed its upload and stays hidden.
        latest = {upload["document_id"]: upload for upload in await self.store.uploads(record["scope_id"])}
        for identity, upload in latest.items():
            snapshot = await reads.get(identity)
            if snapshot is None or all(ref.source_id != upload["source_id"] for ref in snapshot.lineage.sources):
                documents[identity] = Document(
                    id=identity, knowledge_base_id=record["kb_id"], name=upload["name"], status="processing"
                )
        return documents

    async def _digest(self, record: dict[str, Any], identity: str) -> str | None:
        snapshot = await self.ctx.reads(record["scope_id"]).get(identity)
        return None if snapshot is None else DocumentContent.model_validate(snapshot.content).sha256

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
        passages = "\n\n".join(f"[{number}] ({hit.document_name})\n{hit.text}" for number, hit in enumerate(hits, 1))
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
        candidates: list[tuple[str, str, str, dict[str, Any]]] = []
        for kb_id in dict.fromkeys(knowledge_base_ids):
            record = await self._knowledge_base(kb_id, "scope.read")
            documents = {
                identity: document
                for identity, document in (await self._documents(record)).items()
                if document.status == "ready"
            }
            digests = {}
            for identity in documents:
                digest = await self._digest(record, identity)
                if digest is not None:
                    digests[digest] = identity
            for row in await self.store.chunks(record["scope_id"], tuple(digests), self.profile):
                identity = digests[row["sha256"]]
                candidates.append((kb_id, identity, documents[identity].name, row))
        if not candidates:
            return []
        texts = [row["text"] for _, _, _, row in candidates]
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
            similarity = [cosine(query_vector, row["vector"]) for _, _, _, row in candidates]
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
                text=candidates[index][3]["text"],
                score=round(scores[index], 6),
            )
            for index in ranked
        ]

    # Generation: chunk and embed outside the write transaction, then commit the documents.

    async def generate(self, request: GenerateRequest, writer: ArtifactWriter) -> None:
        planned = []
        for source in request.sources:
            upload = DocumentUpload.model_validate(source.projection)
            content: dict[str, Any] = {
                "knowledge_base_id": upload.knowledge_base_id,
                "name": upload.name,
                "media_type": upload.media_type,
                "sha256": upload.sha256,
                "size": upload.size,
                "uploaded_by": upload.uploaded_by,
                "embedding_profile": self.profile,
                "chunk_count": 0,
                "status": "ready",
                "error": None,
            }
            original = await self.store.original(request.scope_id, upload.sha256)
            if original is None:
                content.update(status="failed", error="the uploaded file is missing from plugin storage")
            else:
                pieces = chunk(decode(original[0]))
                vectors = await self._embed(pieces)
                # Content-addressed and immutable: a retry rewrites nothing, a rollback leaves only unreferenced rows.
                await self.store.save_chunks(request.scope_id, upload.sha256, self.profile, pieces, vectors)
                content["chunk_count"] = len(pieces)
            planned.append((document_id(upload.knowledge_base_id, upload.name), content, source.ref))
        async with writer.transaction() as tx:
            for identity, content, ref in planned:
                await tx.put(identity, content=content, source_refs=[ref])

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


def _route(method: Literal["GET", "POST", "PATCH", "DELETE"], path: str, handler: str, **options: Any) -> Route:
    return Route(method=method, path=path, handler=handler, access="authenticated", **options)


plugin = ArtifactPlugin(
    family=FAMILY,
    inputs=Inputs(source_types=(DOCUMENT_SOURCE,), projection=f"{PROJECTION}@1", max_sources=20),
    on_sources=True,
    search_fields=("name",),
    timeout_seconds=600,
    definitions=(DocumentUploadDefinition(),),
    api=PluginApi(
        routes=(
            _route("GET", "/info", "info", response_model=PluginInfo),
            _route(
                "POST",
                "/knowledge-bases",
                "create_knowledge_base",
                body="json",
                request_model=NewKnowledgeBase,
                response_model=KnowledgeBase,
            ),
            _route("GET", "/knowledge-bases", "list_knowledge_bases", response_model=KnowledgeBasePage),
            _route("GET", "/knowledge-bases/{kb_id}", "get_knowledge_base", response_model=KnowledgeBase),
            _route(
                "PATCH",
                "/knowledge-bases/{kb_id}",
                "update_knowledge_base",
                body="json",
                request_model=KnowledgeBaseChange,
                response_model=KnowledgeBase,
            ),
            _route("DELETE", "/knowledge-bases/{kb_id}", "delete_knowledge_base"),
            _route(
                "POST",
                "/knowledge-bases/{kb_id}/documents",
                "upload_document",
                body="binary",
                max_body_bytes=MAX_UPLOAD_BYTES,
                response_model=Document,
            ),
            _route("GET", "/knowledge-bases/{kb_id}/documents", "list_documents", response_model=DocumentPage),
            _route("GET", "/knowledge-bases/{kb_id}/documents/{document_id}", "get_document", response_model=Document),
            _route("GET", "/knowledge-bases/{kb_id}/documents/{document_id}/content", "get_document_content"),
            _route("DELETE", "/knowledge-bases/{kb_id}/documents/{document_id}", "delete_document"),
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

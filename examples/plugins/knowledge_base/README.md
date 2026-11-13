# Knowledge base (RAG) plugin

[中文](README.zh.md)

A complete PowerContext plugin: it manages knowledge bases, accepts batches of Markdown, plain-text, and PDF documents,
chunks and indexes them with the parser you choose, searches with combined keyword and vector ranking, and answers
questions with the Server's generation model. It also shows every plugin capability: generating Artifacts, defining
Source types, plugin APIs, a Dashboard page, dedicated storage, and model calls. For how to write plugins, see
[Write a plugin](../../../docs/en/docs/develop/plugins.md).

## Deploy

1. Copy this directory into the Server's plugin directory; the directory name is the plugin name:

   ```bash
   cp -r examples/plugins/knowledge_base /srv/powercontext/plugins/knowledge-base
   ```

2. Install the PDF dependency into the Server's Python environment (skip it if you do not need PDFs; the plugin then
   accepts only Markdown and plain text):

   ```bash
   uv pip install -r /srv/powercontext/plugins/knowledge-base/requirements.txt
   ```

3. Set the plugin directory, enable the Dashboard, and restart Server:

   ```bash
   export POWERCONTEXT_SERVER_PLUGINS_DIR=/srv/powercontext/plugins
   export POWERCONTEXT_SERVER_DASHBOARD_ENABLED=true
   ```

4. (Optional) Configure models. Without an embedding model the plugin uses built-in hash vectors, suitable only for demos;
   without a generation model, questions return only the retrieved passages:

   ```bash
   export OPENAI_API_KEY=...
   export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL=openai:text-embedding-3-small
   export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_PROFILE_ID=openai-3-small
   export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_DIMENSION=1536
   export POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL=openai:gpt-4.1-mini
   ```

5. Confirm with administrator credentials that the plugin loaded: in `GET /v1/plugins`, `knowledge-base` has `status`
   `loaded`.

## Use it in the Dashboard

After signing in to the Dashboard, "Knowledge bases" in the side navigation is the plugin page
(`/dashboard/plugins/knowledge-base`):

- **New knowledge base**: needs `server.admin`. Each knowledge base is a Scope that users never see.
- **Upload**: drop or choose files: `.md`, `.markdown`, `.txt`, or `.pdf`, up to 32 MiB each.
  - Without a default parser, uploads wait as "Needs parser" and can be withdrawn. After uploading a batch, choose a parser
    and its parameters at the top and click "Start processing".
  - With "Also make this the default" ticked, later uploads are processed right away. Change or clear the default under
    "Edit".
- **Parsers**: Paragraphs (packs neighbouring paragraphs up to a size), Headings (one chunk per Markdown section), and
  Fixed length. PDFs are extracted page by page, and every chunk keeps its page; scanned PDFs have no text layer and need
  OCR, which this plugin does not do.
- **Chunks**: open a document's chunks to edit, insert, or remove them and save a new revision, restore the previous
  revision, or reprocess with another parser.
- **Search & ask**: choose knowledge bases and search by keyword, vector, or both, or ask a question; answers cite the
  passages and pages they come from.

## Use it through the API

Every route is under `/v1/plugins/knowledge-base/api` and uses the same credentials as the other APIs:

| Route | Purpose |
| --- | --- |
| `POST /knowledge-bases` | Create a knowledge base, `{"name", "description"}`; needs `server.admin` |
| `GET /knowledge-bases`, `GET/PATCH/DELETE /knowledge-bases/{kb_id}` | List, view, change (including `default_parser`), and delete knowledge bases |
| `POST /knowledge-bases/{kb_id}/documents?name=<file name>` | Upload a file; the body is the file content |
| `POST /knowledge-bases/{kb_id}/processing` | Process waiting documents with a parser, or reprocess the documents in `document_ids` |
| `GET /knowledge-bases/{kb_id}/documents`, `GET .../documents/{document_id}` | Documents and their status (`pending_config`, `processing`, `ready`, `failed`) |
| `GET .../documents/{document_id}/content` | Download the original file |
| `GET/PUT .../documents/{document_id}/chunks` | View (optionally at a `revision`) and edit chunks; an edit applies in the background and the response carries its `edit_id` |
| `GET .../documents/{document_id}/edits/{edit_id}` | An edit's status: `pending`, `applied`, or `discarded` when the document changed first |
| `POST .../documents/{document_id}/restorations` | Restore a revision |
| `DELETE .../documents/{document_id}` | Withdraw an unprocessed upload, or delete the document |
| `GET /parsers` | Available parsers and their parameters |
| `POST /search` | Search across knowledge bases, `{"query", "knowledge_base_ids", "mode"}` |
| `POST /ask` | Ask across knowledge bases, `{"question", "knowledge_base_ids"}` |

```bash
API=http://127.0.0.1:8000/v1/plugins/knowledge-base/api
KB=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  $API/knowledge-bases -d '{"name": "Runbooks"}' | jq -r .id)
curl -X POST -H "Authorization: Bearer $TOKEN" --data-binary @tls.md "$API/knowledge-bases/$KB/documents?name=tls.md"
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  $API/knowledge-bases/$KB/processing -d '{"parser": {"name": "paragraph"}, "make_default": true}'
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  $API/ask -d "{\"question\": \"My certificate is about to expire. What should I do?\", \"knowledge_base_ids\": [\"$KB\"]}"
```

## How it works

| File | Contents |
| --- | --- |
| `plugin.py` | The declaration, two Source Definitions, route handlers, and background generation |
| `kb_store.py` | Tables in the plugin's dedicated storage: knowledge base to Scope mapping, original files, upload and processing records, content-addressed chunk sets and vectors |
| `kb_parsers.py` | The parser registry and PDF text extraction |
| `kb_text.py` | Tokens, BM25, hash vectors, and rank fusion |
| `ui/` | The Dashboard page |

- **A knowledge base is a Scope**: creating one calls `ctx.scopes.create` and records the mapping in the plugin's
  storage; the API exposes only knowledge base IDs.
- **Sources record what users asked for**: confirming a parser or reprocessing submits a `kb-document` Source (the file
  digest and the parser); editing chunks submits a `kb-chunk-edit` Source (the editor, the edited revision, and the saved
  chunk set). Files and chunks stay in the plugin's storage, and Sources record only digests.
- **Generation writes document revisions**: `generate` parses files and computes vectors, then writes new revisions of the
  `document` Artifact in one write transaction, referencing the Source each came from. An edit takes effect only if the
  document is still at the revision that was edited.
- **Search runs in the plugin**: `document` content records only the chunk set's digest; at search time the plugin reads
  the current revisions' chunks and vectors and ranks them by BM25 and vector similarity combined.
- `document` Artifacts can also be read, forgotten, and restored through the generic Artifact APIs.

## Test

```bash
uv run pytest tests/e2e/test_knowledge_base_plugin.py
```

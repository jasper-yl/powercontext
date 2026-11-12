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

"""The knowledge base example plugin serves a complete RAG API through its own routes."""

import asyncio
import shutil
from pathlib import Path

from tests.e2e.plugin_server import running_server, wait_for

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "plugins" / "knowledge_base"
API = "/v1/plugins/knowledge-base/api"

DEPLOY = b"""# Deploys

Production deploys go to the east region first, then roll out to the west region.

Rollback by redeploying the previous release tag.
"""
KEYS = b"""# Keys

Rotate signing keys monthly and revoke the previous key after one week.
"""
CHINESE = "# 值班\n\n数据库告警时先检查主备延迟，再联系值班工程师。\n".encode()  # noqa: RUF001 - Chinese text uses full-width punctuation


def pdf(*pages: str) -> bytes:
    """A minimal PDF with one line of Helvetica text on each page."""

    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    body = b"%PDF-1.4\n"
    offsets = []
    for number, value in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n{value}\nendobj\n".encode()
    table = "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    return (
        body
        + (
            f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n{table}"
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{len(body)}\n%%EOF\n"
        ).encode()
    )


def test_knowledge_bases_hide_scopes_and_process_uploads_with_the_chosen_parser(tmp_path):
    plugins = tmp_path / "plugins"
    shutil.copytree(EXAMPLE, plugins / "knowledge-base", ignore=shutil.ignore_patterns("__pycache__"))
    log = tmp_path / "server.log"

    async def ok(response, expected=200):
        assert response.status_code == expected, response.text
        return None if expected == 204 else response.json()

    async def documents(client, kb):
        return {
            item["name"]: item
            for item in (await ok(await client.get(f"{API}/knowledge-bases/{kb}/documents")))["items"]
        }

    async def hits(client, query, kbs, **options):
        response = await client.post(f"{API}/search", json={"query": query, "knowledge_base_ids": kbs, **options})
        return (await ok(response))["hits"]

    async def search(client, query, kbs, **options):
        return [(hit["document_name"], hit["text"]) for hit in await hits(client, query, kbs, **options)]

    async def scenario():
        async with running_server(tmp_path, plugins) as client:
            ops = await ok(await client.post(f"{API}/knowledge-bases", json={"name": "Ops", "description": "Runbooks"}))
            assert set(ops) == {"id", "name", "description", "default_parser", "created_at"}
            assert ops["name"] == "Ops" and ops["id"].startswith("kb_") and ops["default_parser"] is None
            await ok(await client.post(f"{API}/knowledge-bases", json={"name": "Ops"}), 409)
            kb = ops["id"]
            documents_path = f"{API}/knowledge-bases/{kb}/documents"

            # Without a default parser, a batch of uploads waits until the user chooses one.
            for name, body in (("deploy.md", DEPLOY), ("keys.txt", KEYS), ("oncall.md", CHINESE)):
                uploaded = await ok(await client.post(documents_path, params={"name": name}, content=body))
                assert uploaded["status"] == "pending_config"
            await ok(await client.post(documents_path, params={"name": "slides.pdf"}, content=b"not a pdf"), 415)
            await ok(await client.post(documents_path, params={"name": "slides.docx"}, content=b"x"), 415)
            # An upload still waiting for a parser never reached core, so it can be withdrawn.
            mistake = await ok(await client.post(documents_path, params={"name": "wrong.md"}, content=b"oops"))
            await ok(await client.delete(f"{documents_path}/{mistake['id']}"), 204)
            assert {item["status"] for item in (await documents(client, kb)).values()} == {"pending_config"}
            assert "wrong.md" not in await documents(client, kb)
            assert await search(client, "keys", [kb]) == []

            processing = f"{API}/knowledge-bases/{kb}/processing"
            for parser in ({"name": "magic"}, {"name": "paragraph", "params": {"overlap": 5000}}):
                await ok(await client.post(processing, json={"parser": parser}), 422)
            submitted = await ok(
                await client.post(processing, json={"parser": {"name": "paragraph"}, "make_default": True})
            )
            assert submitted == {"submitted": 3}
            ready = await wait_for(
                lambda: documents(client, kb),
                lambda value: len(value) == 3 and all(item["status"] == "ready" for item in value.values()),
                log=log,
            )
            assert ready["deploy.md"]["chunk_count"] >= 1 and ready["deploy.md"]["revision"] == 1
            assert ready["deploy.md"]["parser"] == {"name": "paragraph", "params": {"chunk_size": 800, "overlap": 120}}
            assert (await ok(await client.get(f"{API}/knowledge-bases/{kb}")))["default_parser"]["name"] == "paragraph"

            assert (await search(client, "rotate the signing keys", [kb]))[0][0] == "keys.txt"
            # Without a generation model, ask returns the retrieved passages and no answer.
            info = await ok(await client.get(f"{API}/info"))
            assert info["generation_available"] is False and info["can_manage"] is True and ".pdf" in info["formats"]
            asked = await ok(
                await client.post(f"{API}/ask", json={"question": "signing keys", "knowledge_base_ids": [kb]})
            )
            assert asked["answer"] is None and asked["citations"][0]["document_name"] == "keys.txt"
            assert (await search(client, "数据库告警", [kb]))[0][0] == "oncall.md"
            assert (await search(client, "rollback release", [kb], mode="keyword"))[0][0] == "deploy.md"

            deploy = ready["deploy.md"]["id"]
            # Generic APIs reach the document family through the plugin's own presentation and search.
            scopes = (await ok(await client.get("/v1/scopes")))["items"]
            ops = next(item["scope_id"] for item in scopes if item["title"] == "Ops")
            rendering = await ok(await client.get(f"/v1/scopes/{ops}/artifacts/document/{deploy}/rendering"))
            assert rendering["format"] == "markdown" and rendering["text"].startswith("# deploy.md")
            generic = await ok(
                await client.post(
                    f"/v1/scopes/{ops}/artifacts/document/search",
                    json={"query": "rotate the signing keys", "mode": "keyword", "include_scores": True},
                )
            )
            assert generic["results"][0]["artifact_id"] == ready["keys.txt"]["id"]
            await ok(
                await client.post(
                    f"/v1/scopes/{ops}/artifacts/document/search", json={"query": "keys", "mode": "semantic"}
                ),
                422,
            )
            prepared = await ok(
                await client.post(
                    "/v1/context/prepare",
                    json={
                        "scope_id": ops,
                        "query": "rotate the signing keys",
                        "assembly": {"sections": [{"family": "document", "limit": 2, "search": {"mode": "keyword"}}]},
                    },
                )
            )
            assert "## document" in prepared["content"] and "revoke the previous key" in prepared["content"]
            original = await client.get(f"{documents_path}/{deploy}/content")
            assert original.status_code == 200 and original.content == DEPLOY
            assert original.headers["content-type"].startswith("text/markdown")

            # With a default parser, uploading the same name again is processed into a new revision.
            again = await ok(
                await client.post(documents_path, params={"name": "deploy.md"}, content=b"Deploys now go north first.")
            )
            assert again["status"] == "processing"
            await wait_for(
                lambda: documents(client, kb),
                lambda value: value["deploy.md"]["status"] == "ready" and value["deploy.md"]["revision"] == 2,
                log=log,
            )
            assert all("east" not in text for _, text in await search(client, "east region", [kb]))
            assert (await search(client, "north", [kb], mode="keyword"))[0][0] == "deploy.md"

            # Editing chunks writes a revision naming the edited set; history and restoring follow.
            keys = ready["keys.txt"]["id"]
            chunks_path = f"{documents_path}/{keys}/chunks"
            parsed = await ok(await client.get(chunks_path))
            assert parsed["revision"] == 1 and parsed["edited"] is False and "monthly" in parsed["chunks"][0]["text"]
            edit = {
                "expected_revision": 1,
                "chunks": [{"text": "Rotate signing keys every 30 days; audit with keyctl."}],
            }
            # The edit is submitted as a Source and applied in the background.
            submitted_edit = await ok(await client.put(chunks_path, json=edit))
            assert (submitted_edit["status"], submitted_edit["revision"]) == ("processing", 1)
            applied = await wait_for(
                lambda: documents(client, kb),
                lambda value: value["keys.txt"]["status"] == "ready" and value["keys.txt"]["revision"] == 2,
                log=log,
            )
            assert (applied["keys.txt"]["edited"], applied["keys.txt"]["chunk_count"]) == (True, 1)
            assert (await search(client, "keyctl", [kb], mode="keyword"))[0][0] == "keys.txt"
            await ok(await client.put(chunks_path, json=edit), 409)
            # The edited revision's provenance is the edit Source, which records the editor.
            scopes = (await ok(await client.get("/v1/scopes")))["items"]
            scope = next(item["scope_id"] for item in scopes if item["title"] == "Ops")
            revision = await ok(await client.get(f"/v1/scopes/{scope}/artifacts/document/{keys}"))
            (edit_source,) = revision["sources"]
            assert edit_source["source_type"] == "kb-chunk-edit"
            stored_edit = await ok(
                await client.get(f"/v1/scopes/{scope}/sources/kb-chunk-edit/{edit_source['source_id']}")
            )
            payload = stored_edit["content"]["payload"]
            assert (payload["base_revision"], payload["edited_by"]) == (1, "service:server-token")
            # Two edits of the same revision: the first applies, the second finds the document changed and is dropped.
            for text in ("First edit wins.", "Second edit is dropped."):
                await ok(await client.put(chunks_path, json={"expected_revision": 2, "chunks": [{"text": text}]}))
            settled = await wait_for(
                lambda: documents(client, kb), lambda value: value["keys.txt"]["status"] == "ready", log=log
            )
            assert settled["keys.txt"]["revision"] == 3
            assert [chunk["text"] for chunk in (await ok(await client.get(chunks_path)))["chunks"]] == [
                "First edit wins."
            ]
            assert "monthly" in (await ok(await client.get(chunks_path, params={"revision": "1"})))["chunks"][0]["text"]
            await ok(await client.post(f"{documents_path}/{keys}/restorations", json={"revision": 1}), 204)
            restored = await ok(await client.get(chunks_path))
            assert restored["revision"] == 4 and restored["edited"] is False
            assert await search(client, "keyctl", [kb], mode="keyword") == []

            # Reprocessing a document with another parser adds a revision parsed that way.
            fixed = {"name": "fixed", "params": {"chunk_size": 100, "overlap": 0}}
            await ok(await client.post(processing, json={"parser": fixed, "document_ids": [keys]}))
            reparsed = await wait_for(
                lambda: documents(client, kb),
                lambda value: value["keys.txt"]["status"] == "ready" and value["keys.txt"]["revision"] == 5,
                log=log,
            )
            assert reparsed["keys.txt"]["parser"] == fixed
            # Restoring a revision parsed earlier does not make the document look unprocessed.
            await ok(await client.post(f"{documents_path}/{keys}/restorations", json={"revision": 1}), 204)
            back = (await documents(client, kb))["keys.txt"]
            assert (back["status"], back["revision"], back["parser"]["name"]) == ("ready", 6, "paragraph")

            # PDFs are parsed page by page, and every chunk keeps its page.
            runbook = pdf("Disk alerts page the storage team.", "Rotate TLS certificates every 90 days.")
            await ok(await client.post(documents_path, params={"name": "runbook.pdf"}, content=runbook))
            await wait_for(
                lambda: documents(client, kb),
                lambda value: value.get("runbook.pdf", {}).get("status") == "ready",
                log=log,
            )
            found = await hits(client, "certificates", [kb], mode="keyword")
            assert (found[0]["document_name"], found[0]["page"]) == ("runbook.pdf", 2)

            # Deleting a document forgets its Artifact; it leaves listings and search.
            await ok(await client.delete(f"{documents_path}/{deploy}"), 204)
            assert set(await documents(client, kb)) == {"keys.txt", "oncall.md", "runbook.pdf"}
            assert all(name != "deploy.md" for name, _ in await search(client, "north", [kb]))

            # Search spans knowledge bases; unknown ones are indistinguishable from hidden ones.
            other = (await ok(await client.post(f"{API}/knowledge-bases", json={"name": "Security"})))["id"]
            await ok(await client.patch(f"{API}/knowledge-bases/{other}", json={"default_parser": {"name": "heading"}}))
            await ok(
                await client.post(
                    f"{API}/knowledge-bases/{other}/documents",
                    params={"name": "tls.md"},
                    content=b"Renew TLS certificates.",
                )
            )
            await wait_for(
                lambda: documents(client, other),
                lambda value: value.get("tls.md", {}).get("status") == "ready",
                log=log,
            )
            assert {name for name, _ in await search(client, "rotate certificates", [kb, other], mode="keyword")} == {
                "keys.txt",
                "runbook.pdf",
                "tls.md",
            }
            await ok(await client.post(f"{API}/search", json={"query": "x", "knowledge_base_ids": ["kb_missing"]}), 404)

            renamed = await ok(await client.patch(f"{API}/knowledge-bases/{other}", json={"name": "Sec"}))
            assert renamed["name"] == "Sec" and renamed["default_parser"]["name"] == "heading"
            await ok(await client.delete(f"{API}/knowledge-bases/{other}"), 204)
            listed = await ok(await client.get(f"{API}/knowledge-bases"))
            assert [item["name"] for item in listed["items"]] == ["Ops"]
            await ok(await client.get(f"{API}/knowledge-bases/{other}"), 404)
            await ok(await client.get(f"{API}/no-such-route"), 404)

    asyncio.run(scenario())

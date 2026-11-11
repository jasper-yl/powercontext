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


def test_knowledge_bases_hide_scopes_and_search_current_documents(tmp_path):
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

    async def search(client, query, kbs, **options):
        response = await client.post(f"{API}/search", json={"query": query, "knowledge_base_ids": kbs, **options})
        return [(hit["document_name"], hit["text"]) for hit in (await ok(response))["hits"]]

    async def scenario():
        async with running_server(tmp_path, plugins) as client:
            ops = await ok(await client.post(f"{API}/knowledge-bases", json={"name": "Ops", "description": "Runbooks"}))
            assert set(ops) == {"id", "name", "description", "created_at"}
            assert ops["name"] == "Ops" and ops["id"].startswith("kb_")
            await ok(await client.post(f"{API}/knowledge-bases", json={"name": "Ops"}), 409)
            kb = ops["id"]

            upload = f"{API}/knowledge-bases/{kb}/documents"
            pending = await ok(await client.post(upload, params={"name": "deploy.md"}, content=DEPLOY))
            assert pending["status"] == "processing"
            await ok(await client.post(upload, params={"name": "keys.txt"}, content=KEYS))
            await ok(await client.post(upload, params={"name": "oncall.md"}, content=CHINESE))
            await ok(await client.post(upload, params={"name": "slides.pdf"}, content=b"%PDF"), 415)

            ready = await wait_for(
                lambda: documents(client, kb),
                lambda value: len(value) == 3 and all(item["status"] == "ready" for item in value.values()),
                log=log,
            )
            assert ready["deploy.md"]["chunk_count"] >= 1 and ready["deploy.md"]["revision"] == 1

            hits = await search(client, "rotate the signing keys", [kb])
            assert hits[0][0] == "keys.txt"
            assert (await search(client, "数据库告警", [kb]))[0][0] == "oncall.md"
            assert (await search(client, "rollback release", [kb], mode="keyword"))[0][0] == "deploy.md"

            document = ready["deploy.md"]["id"]
            original = await client.get(f"{API}/knowledge-bases/{kb}/documents/{document}/content")
            assert original.status_code == 200 and original.content == DEPLOY
            assert original.headers["content-type"].startswith("text/markdown")

            # Uploading the same name again adds a revision; search sees only the current text.
            await ok(await client.post(upload, params={"name": "deploy.md"}, content=b"Deploys now go north first."))
            await wait_for(
                lambda: documents(client, kb),
                lambda value: value["deploy.md"]["status"] == "ready" and value["deploy.md"]["revision"] == 2,
                log=log,
            )
            assert all("east" not in text for _, text in await search(client, "east region", [kb]))
            assert (await search(client, "north", [kb], mode="keyword"))[0][0] == "deploy.md"

            # Deleting a document forgets its Artifact; it leaves listings and search.
            await ok(await client.delete(f"{API}/knowledge-bases/{kb}/documents/{document}"), 204)
            assert set(await documents(client, kb)) == {"keys.txt", "oncall.md"}
            assert all(name != "deploy.md" for name, _ in await search(client, "north", [kb]))

            # Search spans knowledge bases; unknown ones are indistinguishable from hidden ones.
            other = (await ok(await client.post(f"{API}/knowledge-bases", json={"name": "Security"})))["id"]
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
            assert {name for name, _ in await search(client, "keys certificates", [kb, other], mode="keyword")} == {
                "keys.txt",
                "tls.md",
            }
            # Hybrid search also ranks unrelated chunks by vector similarity, after the matching ones.
            ranked = [name for name, _ in await search(client, "keys certificates", [kb, other])]
            assert set(ranked[:2]) == {"keys.txt", "tls.md"}
            await ok(await client.post(f"{API}/search", json={"query": "x", "knowledge_base_ids": ["kb_missing"]}), 404)

            renamed = await ok(await client.patch(f"{API}/knowledge-bases/{other}", json={"name": "Sec"}))
            assert renamed["name"] == "Sec"
            await ok(await client.delete(f"{API}/knowledge-bases/{other}"), 204)
            listed = await ok(await client.get(f"{API}/knowledge-bases"))
            assert [item["name"] for item in listed["items"]] == ["Ops"]
            await ok(await client.get(f"{API}/knowledge-bases/{other}"), 404)
            await ok(await client.get(f"{API}/no-such-route"), 404)

    asyncio.run(scenario())

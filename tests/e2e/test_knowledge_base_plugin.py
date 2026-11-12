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
import json
import shutil
from pathlib import Path

import httpx

from tests.e2e.plugin_server import ADMIN_AUTHORIZATION_VALUE, running_server, wait_for

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
            # Without a generation model, ask returns the retrieved passages and no answer.
            info = await ok(await client.get(f"{API}/info"))
            assert info["generation_available"] is False and info["can_manage"] is True
            asked = await ok(
                await client.post(f"{API}/ask", json={"question": "signing keys", "knowledge_base_ids": [kb]})
            )
            assert asked["answer"] is None and asked["citations"][0]["document_name"] == "keys.txt"
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


def test_dashboard_page_forwards_only_same_origin_session_requests(tmp_path):
    plugins = tmp_path / "plugins"
    shutil.copytree(EXAMPLE, plugins / "knowledge-base", ignore=shutil.ignore_patterns("__pycache__"))
    pages = "/dashboard/plugins/knowledge-base"
    environment = {
        "POWERCONTEXT_SERVER_DASHBOARD_ENABLED": "true",
        # The deployment ceiling caps the upload route's own 8 MiB limit.
        "POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES": "4096",
        # pydantic-ai's built-in test model answers every request without a provider.
        "POWERCONTEXT_SERVER_INFERENCE": json.dumps({"generation_model": "test"}),
    }

    async def scenario():
        async with running_server(tmp_path, plugins, extra_env=environment) as client:
            async with httpx.AsyncClient(base_url=client.base_url, timeout=20) as anonymous:
                asset = await anonymous.get(f"{pages}/ui/index.html")
                assert asset.status_code == 200, asset.text
                policy = asset.headers["content-security-policy"]
                assert policy.startswith("sandbox allow-scripts") and "allow-same-origin" not in policy
                # Only page assets are served: never plugin code, never outside the pages directory.
                for path in ("plugin.py", "../plugin.py", "%2e%2e/plugin.py", "app.js/../../kb_store.py"):
                    leaked = await anonymous.get(f"{pages}/ui/{path}")
                    assert leaked.status_code in {401, 404} and b"import" not in leaked.content, path
                assert (await anonymous.get("/dashboard/plugins/_client.js")).status_code == 200
                assert (await anonymous.get(pages)).status_code == 401
                assert (
                    await anonymous.get(f"{pages}/api/info", headers={"Sec-Fetch-Site": "same-origin"})
                ).status_code == 401

            session = {"powercontext_dashboard_token": ADMIN_AUTHORIZATION_VALUE}
            async with httpx.AsyncClient(base_url=client.base_url, cookies=session, timeout=20) as browser:
                page = await browser.get(pages, params={"lang": "en"})
                assert page.status_code == 200, page.text
                assert 'sandbox="allow-scripts' in page.text and "Knowledge bases" in page.text
                assert f'href="{pages}?' in (await browser.get("/dashboard/home")).text

                same_origin = {"Sec-Fetch-Site": "same-origin"}
                forward = f"{pages}/api/knowledge-bases"
                created = await browser.post(forward, json={"name": "Docs"}, headers=same_origin)
                assert created.status_code == 200, created.text
                # The framed page is cross-site with an opaque origin; neither form is forwarded.
                for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}, {}):
                    assert (await browser.post(forward, json={"name": "X"}, headers=headers)).status_code == 403

                upload = f"{forward}/{created.json()['id']}/documents"
                large = await browser.post(upload, params={"name": "big.md"}, content=b"x" * 5000, headers=same_origin)
                assert large.status_code == 413, large.text
                small = await browser.post(
                    upload, params={"name": "tls.md"}, content=b"Renew TLS certificates.", headers=same_origin
                )
                assert small.status_code == 200, small.text

            async def documents():
                return (await client.get(f"{API}/knowledge-bases/{created.json()['id']}/documents")).json()["items"]

            await wait_for(
                documents, lambda value: value and value[0]["status"] == "ready", log=tmp_path / "server.log"
            )
            asked = await client.post(
                f"{API}/ask", json={"question": "certificates", "knowledge_base_ids": [created.json()["id"]]}
            )
            assert asked.status_code == 200, asked.text
            assert asked.json()["answer"] and asked.json()["citations"][0]["document_name"] == "tls.md"

    asyncio.run(scenario())

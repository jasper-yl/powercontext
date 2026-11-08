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

"""A deployed plugin is called by the Server and read through the public Artifact API."""

import asyncio

from tests.e2e.plugin_server import capture, create_scope, items, running_server, wait_for
from tests.plugin_support import write_plugin

NOTES = """
from powercontext.plugins import ArtifactWriter, GenerateRequest


class Notes:
    async def generate(self, request: GenerateRequest, writer: ArtifactWriter) -> None:
        async with writer.transaction() as tx:
            for source in request.sources:
                text = source.projection["content"]
                await tx.put(writer.stable_id(source.ref.source_id), content={"text": text}, search_text=text,
                             source_refs=[source.ref])
            for command in request.requests:
                await tx.set_lifecycle(command["retire"], "retired")


async def create(ctx, config):
    return Notes()
"""

BROKEN = """
raise ImportError("missing dependency")
"""


def test_server_loads_plugins_dispatches_on_sources_and_serves_reads(tmp_path):
    plugins = tmp_path / "plugins"
    write_plugin(plugins, "notes", NOTES, on_sources=True)
    write_plugin(plugins, "broken", BROKEN, on_sources=True)

    async def scenario():
        async with running_server(tmp_path, plugins) as client:
            listing = (await client.get("/v1/plugins")).json()["items"]
            assert {item["name"]: item["status"] for item in listing} == {"broken": "failed", "notes": "loaded"}
            assert "notes" in (await client.get("/v1/capabilities")).json()["artifact_families"]

            scope = await create_scope(client, "plugin-e2e")
            await capture(client, scope, "deploy", "Deploys go to the east region")
            await capture(client, scope, "keys", "Rotate signing keys monthly")
            notes = await wait_for(
                lambda: items(client, scope, "notes"), lambda value: len(value) == 2, log=tmp_path / "server.log"
            )
            bodies = {}
            for item in notes:
                response = await client.get(f"/v1/scopes/{scope}/artifacts/notes/{item['artifact_id']}")
                assert response.status_code == 200, response.text
                bodies[item["artifact_id"]] = response.json()["content"]["text"]
            assert set(bodies.values()) == {
                "Deploys go to the east region",
                "Rotate signing keys monthly",
            }

            found = await client.post(f"/v1/scopes/{scope}/artifacts/notes/search", json={"query": "region"})
            assert found.status_code == 200, found.text
            assert [item["content"]["text"] for item in found.json()["results"]] == ["Deploys go to the east region"]

            target = next(item for item in notes if "east" in bodies[item["artifact_id"]])
            accepted = await client.post(
                f"/v1/scopes/{scope}/artifacts/notes/generate", json={"params": {"retire": target["artifact_id"]}}
            )
            assert accepted.status_code == 202, accepted.text

            async def searched():
                response = await client.post(f"/v1/scopes/{scope}/artifacts/notes/search", json={"query": "region"})
                return response.json()["results"]

            await wait_for(searched, lambda value: value == [], log=tmp_path / "server.log")
            state = (await client.get("/v1/plugins/notes")).json()
            assert state["last_outcome"] == "succeeded"

            rejected = await client.put(
                f"/v1/scopes/{scope}/artifacts/notes/{target['artifact_id']}", json={"content": {"text": "edited"}}
            )
            assert rejected.status_code in {404, 405, 422}, rejected.text

    asyncio.run(scenario())

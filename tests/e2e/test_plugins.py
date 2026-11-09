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
                await tx.put(writer.stable_id(source.ref.source_id), content={"text": text}, source_refs=[source.ref])


async def create(ctx):
    return Notes()
"""


async def governance(client, path):
    response = await client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


BROKEN = """
raise ImportError("missing dependency")
"""


def test_server_loads_plugins_dispatches_on_sources_and_serves_the_artifact_api(tmp_path):
    plugins = tmp_path / "plugins"
    write_plugin(plugins, "notes", NOTES)
    write_plugin(plugins, "broken", BROKEN)

    async def search(client, scope, query):
        response = await client.post(f"/v1/scopes/{scope}/artifacts/notes/search", json={"query": query})
        assert response.status_code == 200, response.text
        return [item["content"]["text"] for item in response.json()["results"]]

    async def scenario():
        async with running_server(tmp_path, plugins) as client:
            listing = {item["name"]: item for item in (await client.get("/v1/plugins")).json()["items"]}
            assert {name: item["status"] for name, item in listing.items()} == {"broken": "failed", "notes": "loaded"}
            assert listing["notes"]["source_types"] == ["content"] and listing["notes"]["triggers"] == ["sources"]
            assert "notes" in (await client.get("/v1/capabilities")).json()["artifact_families"]

            scope = await create_scope(client, "plugin-e2e")
            await capture(client, scope, "deploy", "Deploys go to the east region")
            await capture(client, scope, "keys", "Rotate signing keys monthly")
            log = tmp_path / "server.log"
            notes = await wait_for(lambda: items(client, scope, "notes"), lambda value: len(value) == 2, log=log)
            bodies = {}
            for item in notes:
                response = await client.get(f"/v1/scopes/{scope}/artifacts/notes/{item['artifact_id']}")
                assert response.status_code == 200, response.text
                bodies[item["artifact_id"]] = response.json()["content"]["text"]
            assert set(bodies.values()) == {"Deploys go to the east region", "Rotate signing keys monthly"}
            assert await search(client, scope, "region") == ["Deploys go to the east region"]

            async def progress():
                response = await client.get("/v1/plugins/notes/scopes")
                assert response.status_code == 200, response.text
                return {item["scope_id"]: item for item in response.json()["items"]}

            state = (await wait_for(progress, lambda value: not value[scope]["pending"], log=log))[scope]
            assert state["consumed_position"] == state["source_position"]

            # Users change plugin Artifacts through the generic Artifact API; core runs it without the plugin.
            target = next(key for key, text in bodies.items() if "east" in text)
            path = f"/v1/scopes/{scope}/artifacts/notes/{target}"
            edited = await client.put(
                path,
                json={"content": {"text": "Deploys go to the west region"}},
                headers={"If-Match": (await client.get(path)).headers["ETag"]},
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["revision"] == 2
            assert await search(client, scope, "west") == ["Deploys go to the west region"]
            stale = await client.put(path, json={"content": {"text": "lost update"}}, headers={"If-Match": '"1"'})
            assert stale.status_code == 412, stale.text

            # Tags, forget, merge and restoration use the generic routes every registered family shares.
            tags_path = f"{path}/tags"
            tagged = await client.put(
                tags_path,
                json={"tags": ["release"]},
                headers={"If-Match": (await client.get(tags_path)).headers["ETag"]},
            )
            assert tagged.status_code == 200, tagged.text
            matches = await client.post(f"/v1/scopes/{scope}/artifact-tags/query", json={"tags": ["Release"]})
            assert matches.status_code == 200, matches.text
            assert [item["target"]["artifact_id"] for item in matches.json()["items"]] == [target]

            state = await governance(client, f"{path}/governance")
            assert (state["lifecycle_state"], state["artifact"]["revision"]) == ("active", 2)
            forgotten = await client.post(
                f"{path}/forget", json={"revision": 2, "state_version": state["state_version"]}
            )
            assert forgotten.status_code == 200, forgotten.text
            assert forgotten.json()["records"][0]["lifecycle_state"] == "deprecated"
            assert await search(client, scope, "region") == []
            stale_forget = await client.post(
                f"{path}/forget", json={"revision": 2, "state_version": state["state_version"]}
            )
            assert stale_forget.status_code == 409, stale_forget.text
            restored = await client.post(f"{path}/restorations", json={})
            assert restored.status_code == 200, restored.text
            assert await search(client, scope, "west") == ["Deploys go to the west region"]

            inputs = []
            for artifact_id in bodies:
                current = await governance(client, f"/v1/scopes/{scope}/artifacts/notes/{artifact_id}/governance")
                inputs.append({
                    "artifact_id": artifact_id,
                    "revision": current["artifact"]["revision"],
                    "state_version": current["state_version"],
                })
            merged = await client.post(
                f"/v1/scopes/{scope}/artifacts/notes/merges",
                json={"inputs": inputs, "content": {"text": "Deploys go west; rotate keys monthly"}},
            )
            assert merged.status_code == 200, merged.text
            records = {item["artifact"]["artifact_id"]: item for item in merged.json()["records"]}
            result = next(key for key in records if key not in bodies)
            assert {records[key]["merged_into_id"] for key in bodies} == {result}
            assert await search(client, scope, "monthly") == ["Deploys go west; rotate keys monthly"]
            undone = await client.post(
                f"/v1/scopes/{scope}/artifacts/notes/{result}/restorations", json={"operation": "undo_merge"}
            )
            assert undone.status_code == 200, undone.text
            assert [ref["artifact_id"] for ref in undone.json()["retired"]] == [result]
            assert sorted(await search(client, scope, "monthly")) == ["Rotate signing keys monthly"]

            # Families without a registered merge service are rejected before any work.
            for family in ("unknown", "skill"):
                rejected = await client.post(
                    f"/v1/scopes/{scope}/artifacts/{family}/x/forget", json={"revision": 1, "state_version": 0}
                )
                assert rejected.status_code == 422, rejected.text

    asyncio.run(scenario())


def test_atomic_memory_plugin_runs_end_to_end_through_the_server(tmp_path):
    from tests.examples.test_atomic_memory_plugin import FAMILY, deploy, environment

    plugins = tmp_path / "plugins"
    deploy(plugins)

    async def text(client, scope, artifact_id):
        response = await client.get(f"/v1/scopes/{scope}/artifacts/{FAMILY}/{artifact_id}")
        assert response.status_code == 200, response.text
        return response.json()["content"]["text"]

    async def scenario():
        async with running_server(tmp_path, plugins, extra_env=environment()) as client:
            scope = await create_scope(client, "atomic-memory-plugin-e2e")
            await capture(client, scope, "a", "create|region|deploys go to east")
            await capture(client, scope, "b", "create|zone|primary zone is east-1")
            log = tmp_path / "server.log"
            memories = await wait_for(lambda: items(client, scope, FAMILY), lambda value: len(value) == 2, log=log)
            await capture(client, scope, "c", "update|region|deploys go to north")

            async def region_text():
                for item in await items(client, scope, FAMILY):
                    value = await text(client, scope, item["artifact_id"])
                    if value.startswith("region"):
                        return value
                return None

            await wait_for(region_text, lambda value: value == "region: deploys go to north", log=log)
            zone = None
            for item in memories:
                if (await text(client, scope, item["artifact_id"])).startswith("zone"):
                    zone = item["artifact_id"]
            path = f"/v1/scopes/{scope}/artifacts/{FAMILY}/{zone}"
            state = await governance(client, f"{path}/governance")
            response = await client.post(
                f"{path}/forget",
                json={"revision": state["artifact"]["revision"], "state_version": state["state_version"]},
            )
            assert response.status_code == 200, response.text
            found = await client.post(f"/v1/scopes/{scope}/artifacts/{FAMILY}/search", json={"query": "zone"})
            assert found.json()["results"] == []

    asyncio.run(scenario())


def test_generic_merge_routes_serve_registered_built_in_families(tmp_path):
    from tests.e2e.test_artifact_merge_experience import _content

    plugins = tmp_path / "plugins"
    plugins.mkdir()

    async def scenario():
        async with running_server(tmp_path, plugins) as client:
            scope = await create_scope(client, "generic-merge-e2e")
            created = []
            for name in ("canary", "pause"):
                response = await client.post(
                    f"/v1/scopes/{scope}/artifacts",
                    json={"family": "experience", "content": _content(name).model_dump(mode="json", exclude_none=True)},
                )
                assert response.status_code == 201, response.text
                created.append(response.json()["artifact_id"])
            inputs = []
            for artifact_id in created:
                state = await governance(client, f"/v1/scopes/{scope}/artifacts/experience/{artifact_id}/governance")
                inputs.append({"artifact_id": artifact_id, "revision": 1, "state_version": state["state_version"]})
            merged = await client.post(
                f"/v1/scopes/{scope}/artifacts/experience/merges",
                json={"inputs": inputs, "content": _content("combined").model_dump(mode="json", exclude_none=True)},
            )
            assert merged.status_code == 200, merged.text
            frozen = {item["artifact"]["artifact_id"]: item["merged_into_id"] for item in merged.json()["records"]}
            (result,) = set(frozen) - set(created)
            assert {frozen[key] for key in created} == {result}

            first = f"/v1/scopes/{scope}/artifacts/experience/{created[0]}"
            restored = await client.post(f"{first}/restorations", json={})
            assert restored.status_code == 200, restored.text
            assert [ref["artifact_id"] for ref in restored.json()["retired"]] == [result]
            state = await governance(client, f"{first}/governance")
            assert (state["lifecycle_state"], state["merged_into_id"]) == ("active", None)

    asyncio.run(scenario())

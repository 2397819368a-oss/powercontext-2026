# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Read dashboard content through the Server authorization boundary."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import suppress
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Request
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.topic_memory import TopicMemoryBrowseCursor
from powercontext.builtin.runtime.models import GetTopicMemoryRequest
from powercontext.errors import ArtifactNotFoundError
from powercontext.server import mapping
from powercontext.server.authz import AccessAction, ResourceRef
from powercontext.server.authz.service import access_control_for_mode
from powercontext.server.dashboard.errors import ReadError as ReadError
from powercontext.server.dashboard.session import authentication_headers


def segment(value: str) -> str:
    return quote(value, safe="")


class DashboardAPI:
    """Adapt public reads with incoming credentials, including authorized built-in batches."""

    def __init__(self, request: Request) -> None:
        self.app = request.app
        self.request = request
        self.builtin = _builtin_runtime(request)
        headers = authentication_headers(request.scope)
        headers = {key: value for key, value in headers.items() if key in {"authorization", "cookie"}}
        headers["X-PowerContext-Dream-Contract"] = "2"
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request.app, raise_app_exceptions=False),
            base_url=str(request.base_url),
            headers=headers,
        )

    async def atomic_memory_list(self, scope, *, state, limit, cursor):
        return await self.read(
            "/v1/atomic-memory/list", {"scope_id": scope, "states": [state], "limit": limit, "cursor": cursor}
        )

    async def read(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with asyncio.timeout(20):
                result = await self.client.request("GET" if payload is None else "POST", path, json=payload)
        except (TimeoutError, httpx.HTTPError) as error:
            raise ReadError(503, "service_unavailable") from error
        if result.is_error:
            code = "service_unavailable"
            with suppress(ValueError, KeyError, TypeError):
                code = result.json()["error"]["code"]
            raise ReadError(result.status_code, code, result.headers.get("X-PowerContext-Request-ID"))
        return result.json()

    async def record(self, scope: str, family: str, artifact: str, revision: int) -> dict[str, Any]:
        value = await self.artifact_revision(scope, family, artifact, revision)
        return self.artifact_record(value)

    async def artifact_revision(self, scope: str, family: str, artifact: str, revision: int) -> dict[str, Any]:
        return await self.read(
            f"/v1/scopes/{segment(scope)}/artifacts/{family}/{segment(artifact)}/revisions/{revision}"
        )

    async def atomic_memory_get(self, scope: str, artifact: str, revision: int) -> dict[str, Any]:
        """Read an exact content revision and label its independent current state."""

        value, state = await asyncio.gather(
            self.artifact_revision(scope, "atomic-memory", artifact, revision),
            self.read(f"/v1/scopes/{segment(scope)}/artifacts/atomic-memory/{segment(artifact)}/state"),
        )
        ref = {"family": "atomic-memory", "artifact_id": artifact, "revision": revision}
        return {
            **self.artifact_record(value),
            "family": "atomic-memory",
            "artifact": ref,
            "note_key": f"atomic-memory/{artifact}@{revision}",
            "state": state["state"],
            "state_version": state["state_version"],
            "merged_into_id": state["merged_into_id"],
            "current_artifact": state["artifact"],
            "is_current": ref == state["artifact"],
        }

    def artifact_record(self, value: dict[str, Any]) -> dict[str, Any]:
        return {
            **value["content"],
            **{key: value[key] for key in ("artifact_id", "revision", "sources", "artifacts")},
        }

    @staticmethod
    def skill_record(value: dict[str, Any]) -> dict[str, Any]:
        return {
            **value["content"],
            **value["artifact"],
            "sources": [
                {"source_type": source["name"], "source_id": source["source_id"]} for source in value["source_refs"]
            ],
            "artifacts": value["artifact_refs"],
        }

    async def authorize_scope(self, scope: str, operation: str) -> None:
        from powercontext.server.app import _authorize_checks

        access = access_control_for_mode(self.app.state.access_control, mode=self.app.state.access_mode)
        if access is not None:
            try:
                await _authorize_checks(
                    self.request,
                    access,
                    ((AccessAction.SCOPE_READ, ResourceRef.scope(scope)),),
                    operation=operation,
                )
            except Exception as error:
                raise ReadError.from_exception(error) from error

    async def records(
        self, scope: str, family: str, *, cursor: str | None = None, limit: int = 12, query: str | None = None
    ) -> dict[str, Any]:
        if family == "skill":
            maximum = 1 if limit == 1 else 200
            result = await self.read("/v1/skill/library", {"scope_id": scope, "limit": maximum, "query": query})
            return {
                "items": [self.skill_record(item) for item in result["skills"]],
                "next_cursor": None,
                "search_limited": maximum == 200 and len(result["skills"]) == maximum,
            }
        if self.builtin is None:
            url = httpx.URL(f"/v1/scopes/{segment(scope)}/artifacts/{family}", params={"limit": limit})
            page = await self.read(str(url.copy_add_param("cursor", cursor) if cursor else url))
            items = []
            for item in page["items"]:
                try:
                    items.append(await self.record(scope, family, item["artifact_id"], item["revision"]))
                except ReadError as error:
                    items.append({**item, "error": error})
            return {"items": items, "next_cursor": page["next_cursor"]}
        try:
            async with asyncio.timeout(20):
                await self.authorize_scope(scope, "list_artifacts")
                page = await self.builtin.records.for_scope(scope).query_artifacts(family, limit=limit, cursor=cursor)
            return {
                "items": [
                    self.artifact_record({**item.model_dump(mode="json"), "content": item.content})
                    for item in page.items
                ],
                "next_cursor": page.next_cursor,
            }
        except Exception as error:
            raise ReadError.from_exception(error) from error

    async def prompt_configurations(self, scope: str, keys: tuple[str, ...]) -> list[dict[str, Any] | ReadError]:
        from powercontext.server.app import _artifact_etag

        if self.builtin is not None:
            # On failure, individual reads retain independent grants and per-key errors.
            with suppress(Exception):
                async with asyncio.timeout(20):
                    await self.authorize_scope(scope, "get_prompt_configuration")
                    values = await self.builtin.prompts.for_scope(scope).read_configurations(keys)
                return [
                    {
                        **value.model_dump(mode="json"),
                        "artifact_etag": None if value.artifact is None else _artifact_etag(value.artifact.revision),
                    }
                    for value in values
                ]
        results = []
        for key in keys:
            try:
                results.append(await self.read(f"/v1/scopes/{segment(scope)}/prompts/{segment(key)}"))
            except ReadError as error:
                results.append(error)
        return results

    async def usage(self, scope: str, period: str) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            result = await self.read(
                "/v1/stats", {"selection": {"mode": "exact", "scope_ids": [scope]}, "period": period}
            )
            return result["usage"], result["recall"]
        except Exception as error:
            raise ReadError.from_exception(error) from error

    async def topic_memory_get(self, scope: str, artifact: dict[str, Any]) -> dict[str, Any]:
        application = getattr(self.app.state, "application", None)
        if application is None:
            raise ReadError(503, "service_unavailable")
        try:
            value = await application.topic_memory.for_scope(scope).get(
                GetTopicMemoryRequest(artifact=ArtifactRef.model_validate(artifact))
            )
        except ArtifactNotFoundError as error:
            raise ReadError(404, "not_found") from error
        except ValueError as error:
            raise ReadError(422, "invalid_request") from error
        return {
            **mapping.topic_memory_response(value).model_dump(mode="json"),
            "is_current": value.is_current,
            "current_artifact": value.current_artifact.model_dump(mode="json"),
        }

    @staticmethod
    def _decode_topic_cursor(value: str) -> TopicMemoryBrowseCursor:
        try:
            padding = "=" * (-len(value) % 4)
            payload = base64.urlsafe_b64decode(value + padding)
            return TopicMemoryBrowseCursor.model_validate_json(payload)
        except (ValueError, TypeError, ValidationError) as error:
            raise ReadError(422, "invalid_request") from error

    @staticmethod
    def _encode_topic_cursor(value: TopicMemoryBrowseCursor) -> str:
        payload = json.dumps(value.model_dump(mode="json"), separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    async def topic_memory_browse(self, scope: str, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        application = getattr(self.app.state, "application", None)
        if application is None:
            raise ReadError(503, "service_unavailable")
        after = self._decode_topic_cursor(cursor) if cursor else None
        items = await application.topic_memory.for_scope(scope).browse(limit=limit + 1, after=after)
        has_next = len(items) > limit
        items = items[:limit]
        next_cursor = None
        if has_next and items:
            last = items[-1]
            next_cursor = self._encode_topic_cursor(
                TopicMemoryBrowseCursor(
                    published_at=last.published_at,
                    artifact_id=last.artifact_ref.artifact_id,
                    revision=last.artifact_ref.revision,
                )
            )
        return {
            "items": [
                {
                    **item.model_dump(mode="json"),
                    "artifact": item.artifact_ref.model_dump(mode="json"),
                }
                for item in items
            ],
            "next_cursor": next_cursor,
        }


def _builtin_runtime(request):
    """Keep injected applications and authorization rules on their existing boundary."""
    runtime = getattr(request.app.state, "application", None)
    if runtime is None or runtime is not getattr(request.app.state, "dashboard_runtime", None):
        return None
    access = access_control_for_mode(request.app.state.access_control, mode=request.app.state.access_mode)
    if access is not None and not access.supports_read_snapshot(runtime.primary_database.engine):
        return None
    return runtime

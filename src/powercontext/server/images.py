# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Scoped HTTP image adapters and bounded pre-JSON transport."""

import json

from fastapi import Request
from starlette._utils import get_route_path
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from powercontext.builtin.images.service import MAX_ENCODED_IMAGE_BYTES, ImageError, ImageService
from powercontext.http import (
    ExperimentalCaptureImageRequest,
    ExperimentalCaptureImageResponse,
    ExperimentalGetImageAssetRequest,
    ExperimentalGetImageAssetResponse,
    ExperimentalGetImageStatusRequest,
    ExperimentalGetImageTaskRequest,
    ExperimentalImageStatus,
    ExperimentalImageTask,
    ExperimentalImageTaskPage,
    ExperimentalListImageTasksRequest,
    ExperimentalReparseImageRequest,
    ExperimentalRetryImageTaskRequest,
)
from powercontext.server.authz import AccessAction, ResourceRef, access_control_for_mode
from powercontext.server.context import access_audit_context, current_principal

MAX_IMAGE_BODY_BYTES = 12 * 1024 * 1024
CAPTURE_PATH = "/v1/experimental/images/capture"


def _encoded_budget_exceeded(body: bytearray) -> bool:
    if len(body) <= MAX_ENCODED_IMAGE_BYTES:
        return False
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("bytes_base64"), str)
        and len(payload["bytes_base64"]) > MAX_ENCODED_IMAGE_BYTES
    )


class ImageBodyLimitMiddleware:
    """Reject an oversized capture body before authorization or JSON decoding."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or get_route_path(scope) != CAPTURE_PATH:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_IMAGE_BODY_BYTES:
                response = JSONResponse(
                    {
                        "error": {
                            "code": "image_too_large",
                            "message": "Image request body exceeds 12 MiB.",
                            "details": None,
                        }
                    },
                    status_code=413,
                )
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        # Only a body near the transport ceiling can contain an overlong
        # Base64 field. Normalize that byte-budget failure before DTO/Auth
        # validators would classify it as an ordinary shape error.
        if _encoded_budget_exceeded(body):
            response = JSONResponse(
                {"error": {"code": "image_too_large", "message": "Image exceeds 8 MiB.", "details": None}},
                status_code=413,
            )
            await response(scope, receive, send)
            return
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)


def _images(request: Request) -> ImageService:
    service = getattr(request.app.state.application, "images", None)
    if not isinstance(service, ImageService):
        raise ImageError(503, "image_storage_unavailable")
    return service


async def capture_image(
    request: ExperimentalCaptureImageRequest, http_request: Request
) -> ExperimentalCaptureImageResponse:
    if request.context.source is not None:
        if request.context.source.scope_id != request.scope_id:
            raise ImageError(422, "image_context_scope_mismatch")
        access = access_control_for_mode(http_request.app.state.access_control, mode=http_request.app.state.access_mode)
        if access is not None:
            await access.require(
                current_principal(),
                AccessAction.SCOPE_READ,
                ResourceRef.scope(request.scope_id),
                context=access_audit_context("experimental_capture_image"),
            )
    return await _images(http_request).capture(request)


async def get_image_asset(
    request: ExperimentalGetImageAssetRequest, http_request: Request
) -> ExperimentalGetImageAssetResponse:
    return await _images(http_request).get_asset(request.scope_id, request.asset_id)


async def get_image_task(request: ExperimentalGetImageTaskRequest, http_request: Request) -> ExperimentalImageTask:
    return await _images(http_request).get_task(
        request.scope_id, request.task_id, analysis_source_id=request.analysis_source_id
    )


async def list_image_tasks(
    request: ExperimentalListImageTasksRequest, http_request: Request
) -> ExperimentalImageTaskPage:
    return await _images(http_request).list_tasks(request)


async def get_image_status(
    request: ExperimentalGetImageStatusRequest, http_request: Request
) -> ExperimentalImageStatus:
    return await _images(http_request).status(request.scope_id)


async def _require_image_read(request: Request, scope_id: str, operation: str) -> None:
    access = access_control_for_mode(request.app.state.access_control, mode=request.app.state.access_mode)
    if access is not None:
        await access.require(
            current_principal(),
            AccessAction.SCOPE_READ,
            ResourceRef.scope(scope_id),
            context=access_audit_context(operation),
        )


async def retry_image_task(request: ExperimentalRetryImageTaskRequest, http_request: Request) -> ExperimentalImageTask:
    await _require_image_read(http_request, request.scope_id, "experimental_retry_image_task")
    return await _images(http_request).queue.retry(request)


async def reparse_image(request: ExperimentalReparseImageRequest, http_request: Request) -> ExperimentalImageTask:
    await _require_image_read(http_request, request.scope_id, "experimental_reparse_image")
    return await _images(http_request).queue.reparse(request)

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

"""Dream worker usage survives replay and Runtime restart in public statistics."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import cast

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from powercontext.builtin.artifacts.experience import ExperienceContent
from powercontext.builtin.artifacts.handoff.models import HandoffDraft, HandoffSourceCitation, HandoffStatement
from powercontext.builtin.artifacts.topic_memory import (
    TopicMemoryContent,
    TopicMemoryDraft,
    prepare_topic_memory_projection,
)
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    ApproveCandidateRequest,
    BuiltinConfig,
    CaptureSource,
    CreateDreamRunRequest,
    GetDreamRunRequest,
    InferenceConfig,
    ProposeExperienceRequest,
    composition,
)
from powercontext.builtin.runtime.family_processing import FamilyWorkerSpec, _run_family_worker
from powercontext.builtin.runtime.topic_memory_processing import TopicMemoryWorkerSpec, _run_topic_memory_worker
from powercontext.builtin.scope import ScopeDraft
from powercontext.server.app import ServerApplication, create_app
from tests.e2e.dream_support import Controller, Handle, open_dream_runtime, process_pending


@pytest.mark.parametrize(
    ("family", "operation", "purpose"),
    [
        ("topic-memory", "revise_topic_memory", "topic_memory_generation"),
        ("handoff", "refresh_handoff", "handoff_generation"),
        ("skill", "derive_skill", "skill_generation"),
    ],
)
def test_dream_worker_usage_persists_once_in_scope_stats(tmp_path, monkeypatch, family, operation, purpose):
    config = BuiltinConfig(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'dream-usage.db'}"),
        inference=InferenceConfig(generation_model="openai-chat:controlled-usage"),
    )
    proposals = {}

    def respond(messages, _info):
        value = next(
            json.loads(part.content)
            for message in reversed(messages)
            for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        assert value["operation"] == operation
        evidence_id = next(item["evidence_id"] for item in value["evidence"]["evidence"] if item["kind"] == "source")
        return ModelResponse(
            parts=[
                TextPart(
                    json.dumps({
                        "outcome": "proposed",
                        "reason": "The new Source corrects the current progress.",
                        "intent": "derive" if family == "skill" else "correct",
                        "proposal": proposals[family],
                        "evidence_ids": [evidence_id],
                    })
                )
            ],
            usage=RequestUsage(input_tokens=80, output_tokens=45),
        )

    async def open_model(*_args, **_kwargs):
        model = FunctionModel(respond)
        return model, model

    # Substitute only the external model. Keep production generator wrappers,
    # worker composition, usage persistence, and HTTP statistics unchanged.
    monkeypatch.setattr(composition, "_open_pydantic_ai_model", open_model)

    class WorkerHandle(Handle):
        async def execute(self):
            await self.ready.wait()
            if family == "topic-memory":
                worker = partial(_run_topic_memory_worker, TopicMemoryWorkerSpec(config=config))
            else:
                worker = partial(_run_family_worker, FamilyWorkerSpec(config=config))
            result = await worker(self.assignment)
            # Redelivery of the completed assignment must not infer or count again.
            await worker(self.assignment)
            return result

    class WorkerController(Controller):
        async def start(self, assignment):
            handle = WorkerHandle(self, assignment)
            self.handles.append(handle)
            return handle

    async def stats(runtime, scope_id):
        app = create_app(application=cast(ServerApplication, runtime))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                "/v1/stats", json={"selection": {"mode": "exact", "scope_ids": [scope_id]}, "period": "today"}
            )
            assert response.status_code == 200, response.text
            return response.json()["usage"]

    async def scenario():
        async with open_dream_runtime(config, controller=WorkerController()) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Dream usage", summary="Usage", idempotency_key="usage")
            )
            other = await runtime.scopes.create(ScopeDraft(title="Other", summary="No usage", idempotency_key="other"))
            source = await runtime.sources.for_scope(scope.scope_id).capture(
                CaptureSource(source_id="progress", content="The replacement has been issued.", metadata={})
            )
            if family == "topic-memory":
                content = TopicMemoryContent(title="Replacement", summary="Pending", detail="# Progress\nPending.")
                async with runtime._provider.database.transaction() as connection:
                    published = await runtime._provider.repositories.topic_memories.publish_create(
                        connection,
                        scope.scope_id,
                        "topic-usage",
                        TopicMemoryDraft(content=content, sources=(source.source_ref,)),
                        prepare_topic_memory_projection(content),
                    )
                target = published.topic.as_ref()
                proposals[family] = TopicMemoryContent(
                    title="Replacement", summary="Issued", detail="# Progress\nThe replacement has been issued."
                ).model_dump(mode="json")
            elif family == "skill":
                candidate = await runtime.experience.for_scope(scope.scope_id).propose(
                    ProposeExperienceRequest(
                        proposal=ExperienceContent(
                            situation="Replacement delivery",
                            action="Check status",
                            outcome="Issued",
                            lesson="Verify status",
                        ),
                        sources=(source.source_ref,),
                    )
                )
                approved = await runtime.review.for_scope(scope.scope_id).approve(
                    ApproveCandidateRequest(candidate_id=candidate.candidate_id, expected_version=candidate.version)
                )
                target = approved.result_artifact
                assert target is not None
                proposals[family] = {
                    "name": "check-replacement-status",
                    "description": "Verify replacement progress.",
                    "instructions": "Check the issuance record before confirming delivery.",
                    "validation": ["Confirm the issuance record exists."],
                }
            else:
                draft = HandoffDraft(
                    objective="Finish the support request.",
                    state=(
                        HandoffStatement(
                            text="Pending", citations=(HandoffSourceCitation(source_ref=source.source_ref),)
                        ),
                    ),
                    disposition="continuable",
                )
                handoffs = runtime.handoff.for_scope(scope.scope_id)
                current = await handoffs.commit(await handoffs.finalize(draft))
                target = current.as_ref()
                proposals[family] = {
                    "objective": draft.objective,
                    "state": [
                        {
                            "text": "The replacement has been issued.",
                            "citations": [HandoffSourceCitation(source_ref=source.source_ref).model_dump(mode="json")],
                        }
                    ],
                    "disposition": "continuable",
                }
            request = CreateDreamRunRequest(
                operation=operation,
                target=None if family == "skill" else target,
                artifacts=(target,),
                sources=(source.source_ref,),
                idempotency_key="usage",
            )
            accepted = await runtime.dream.for_scope(scope.scope_id).create(request)
            await process_pending(runtime)
            completed = await runtime.dream.for_scope(scope.scope_id).get(GetDreamRunRequest(run_id=accepted.run_id))
            assert (completed.status, completed.outcome) == ("succeeded", "proposed"), completed
            assert completed.usage.model_calls == 1
            assert (completed.usage.input_tokens, completed.usage.output_tokens) == (80, 45)
            assert (await runtime.dream.for_scope(scope.scope_id).create(request)).run_id == completed.run_id
            before = await stats(runtime, scope.scope_id)
            expected = {"requests": 1, "input_tokens": 80, "output_tokens": 45}
            assert before["totals"]["generation"] == expected
            assert [(row["purpose"], row["generation"]) for row in before["by_purpose"]] == [(purpose, expected)]
            assert (await stats(runtime, other.scope_id))["totals"]["generation"]["requests"] == 0
        async with open_dream_runtime(config) as runtime:
            after = await stats(runtime, scope.scope_id)
            assert after["totals"] == before["totals"]
            assert after["by_purpose"] == before["by_purpose"]

    asyncio.run(scenario())

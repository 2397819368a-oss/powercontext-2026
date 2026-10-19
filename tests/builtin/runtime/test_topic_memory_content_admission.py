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

from __future__ import annotations

import asyncio

import pytest

from powercontext.builtin.artifacts.topic_memory import (
    TOPIC_MEMORY_SOURCE_WINDOW_BINDING,
    TopicMemoryContent,
    TopicMemoryDraft,
    prepare_topic_memory_projection,
)
from powercontext.builtin.artifacts.topic_memory.generation import (
    TopicMemoryEvolveOutput,
    TopicMemoryGenerationError,
    TopicMemoryGlobalOutput,
    TopicMemoryPlanItem,
    TopicMemoryPlannerOutput,
    TopicMemoryProbe,
    TopicMemoryProbeOutput,
    TopicMemoryProposal,
    TopicMemoryReconcileOutput,
)
from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.supervision import ArtifactProcessingLeaseRepository
from powercontext.builtin.persistence.topic_memory_budget import require_topic_memory_work_available
from powercontext.builtin.runtime.topic_memory_processing import TopicMemoryAtomicPublisher, TopicMemoryProcessor
from powercontext.sources import SourceMaterialization
from tests.builtin.persistence.contract import NoteSource
from tests.builtin.runtime.test_topic_memory_processing import _assignment, _budget_row, _repositories, _stages


def _placeholder():
    return TopicMemoryContent(
        title="T-19",
        summary="S-19 coordinated without merging historical identities.",
        detail="D-19 coordinated detail preserving distinct historical identity.",
    )


@pytest.mark.parametrize("path", ["create", "update", "reconcile", "evolve", "mixed"])
def test_invalid_content_preserves_topics_cursor_and_search(path):
    async def scenario():
        manager, profile, sources, topics = await _repositories()
        try:
            leases = ArtifactProcessingLeaseRepository()
            valid = TopicMemoryContent(
                title="zircon release",
                summary="Zircon deployment uses SQLite.",
                detail="Run the deployment checks before release.",
            )
            async with profile.database.transaction() as connection:
                old = await topics.publish_create(
                    connection,
                    "scope-a",
                    "topic-old",
                    TopicMemoryDraft(content=valid),
                    prepare_topic_memory_projection(valid),
                )
                await sources.add(
                    connection,
                    "scope-a",
                    NoteSource(
                        name="target",
                        materialization=SourceMaterialization.CAPTURED,
                        body="在dashboard中检查是否符合预期",
                    ),
                )
                term = await leases.start_single_process_term(connection, "holder")
            bad = TopicMemoryProposal(
                content=_placeholder(),
                evidence_ids=("evidence-0001",),
                candidate_id="candidate-0001" if path == "update" else None,
            )
            provisional = TopicMemoryProposal(content=valid, evidence_ids=("evidence-0001",))
            outputs = (provisional,) if path == "reconcile" else (provisional, bad) if path == "mixed" else (bad,)
            stages = _stages(
                probe=TopicMemoryProbeOutput(
                    probes=(TopicMemoryProbe(query="zircon", evidence_ids=("evidence-0001",)),)
                ),
                global_output=TopicMemoryGlobalOutput(ambiguous=path == "evolve", proposals=outputs),
                planner=TopicMemoryPlannerOutput(items=(TopicMemoryPlanItem(probe_ids=("probe-0001",)),)),
                evolve=(TopicMemoryEvolveOutput(proposal=bad),),
                reconcile=(
                    TopicMemoryReconcileOutput(
                        proposals=(
                            bad.model_copy(update={"proposal_id": "proposal-0001", "candidate_id": "candidate-0001"}),
                        )
                    ),
                ),
            )
            processor = TopicMemoryProcessor(
                database=profile.database,
                sources=sources,
                topics=topics,
                stages=stages,
                publisher=TopicMemoryAtomicPublisher(profile.database, sources, topics, leases=leases),
            )
            with pytest.raises(TopicMemoryGenerationError, match="invalid_topic_placeholder"):
                await processor.process(_assignment(term.fence("single-process")))
            async with profile.database.transaction() as connection:
                heads = await topics.browse_current(connection, "scope-a", limit=20)
                exact = await topics.get_exact(connection, "scope-a", old.topic.as_ref())
                hits = await topics.search(connection, "scope-a", "zircon", limit=10, mode="fts")
                cursor = await SourceCursorRepository().load(connection, "scope-a", TOPIC_MEMORY_SOURCE_WINDOW_BINDING)
                await require_topic_memory_work_available(connection, "scope-a", TOPIC_MEMORY_SOURCE_WINDOW_BINDING, 0)
            assert [item.artifact_ref for item in heads] == [old.topic.as_ref()]
            assert exact.topic.content == valid
            assert [hit.artifact_ref for hit in hits.hits] == [old.topic.as_ref()]
            assert cursor is None
            row = await _budget_row(profile)
            assert row["failure_code"] == "invalid_topic_placeholder"
        finally:
            await manager.__aexit__(None, None, None)

    asyncio.run(scenario())


@pytest.mark.parametrize("recover", [True, False])
def test_rejected_content_recovers_or_exhausts_the_original_budget(recover):
    async def scenario():
        manager, profile, sources, topics = await _repositories()
        try:
            leases = ArtifactProcessingLeaseRepository()
            async with profile.database.transaction() as connection:
                await sources.add(
                    connection,
                    "scope-a",
                    NoteSource(
                        name="target",
                        materialization=SourceMaterialization.CAPTURED,
                        body="Zircon deployment checks pass.",
                    ),
                )
                term = await leases.start_single_process_term(connection, "holder")
            assignment = _assignment(term.fence("single-process"))
            for attempt in range(3):
                content = (
                    TopicMemoryContent(
                        title="Zircon release",
                        summary="Deployment checks pass.",
                        detail="Zircon deployment checks pass.",
                    )
                    if recover and attempt
                    else _placeholder()
                )
                stages = _stages(
                    probe=TopicMemoryProbeOutput(
                        probes=(TopicMemoryProbe(query="zircon", evidence_ids=("evidence-0001",)),)
                    ),
                    global_output=TopicMemoryGlobalOutput(
                        proposals=(TopicMemoryProposal(content=content, evidence_ids=("evidence-0001",)),)
                    ),
                )
                processor = TopicMemoryProcessor(
                    database=profile.database,
                    sources=sources,
                    topics=topics,
                    stages=stages,
                    publisher=TopicMemoryAtomicPublisher(profile.database, sources, topics, leases=leases),
                )
                if recover and attempt:
                    assert (await processor.process(assignment)).outcome.value == "succeeded"
                    async with profile.database.transaction() as connection:
                        head = (await topics.browse_current(connection, "scope-a", limit=10))[0]
                        exact = await topics.get_exact(connection, "scope-a", head.artifact_ref)
                        cursor = await SourceCursorRepository().load(
                            connection, "scope-a", TOPIC_MEMORY_SOURCE_WINDOW_BINDING
                        )
                    assert exact.topic.content == content
                    assert cursor is not None and cursor.cursor.sequence == 1
                    assert await _budget_row(profile) is None
                    break
                with pytest.raises(TopicMemoryGenerationError, match="invalid_topic_placeholder"):
                    await processor.process(assignment)
                row = await _budget_row(profile)
                assert row["attempts"] == attempt + 1
            if not recover:
                async with profile.database.transaction() as connection:
                    with pytest.raises(TopicMemoryGenerationError, match="invalid_topic_placeholder"):
                        await require_topic_memory_work_available(
                            connection, "scope-a", TOPIC_MEMORY_SOURCE_WINDOW_BINDING, 0
                        )
                    assert await topics.browse_current(connection, "scope-a", limit=10) == ()
                    assert (
                        await SourceCursorRepository().load(connection, "scope-a", TOPIC_MEMORY_SOURCE_WINDOW_BINDING)
                        is None
                    )
        finally:
            await manager.__aexit__(None, None, None)

    asyncio.run(scenario())

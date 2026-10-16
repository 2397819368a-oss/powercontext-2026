"""Processing observations reflect durable outcomes without doing work on reads."""

from __future__ import annotations

import asyncio

import pytest

from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.tables import SHARED_TABLES
from powercontext.builtin.runtime.artifact_processing import ArtifactProcessingBinding, ArtifactProcessingSupervisor
from powercontext.builtin.runtime.processing_contracts import (
    ArtifactProcessingBlock,
    ArtifactProcessingBlockedError,
    ArtifactProcessingWorkerCompletion,
    ArtifactProcessingWorkerOutcome,
)


@pytest.mark.parametrize("outcome", ["success", "noop", "hold", "failure", "blocked", "cancelled"])
def test_attempt_observations_preserve_durable_outcomes(tmp_path, outcome):  # noqa: C901 - outcome matrix
    async def scenario():  # noqa: C901 - outcome matrix
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'observations.db'}"), tables=SHARED_TABLES
        ) as profile:
            intents = ArtifactProcessingIntentRepository()
            started = asyncio.Event()

            class Handle:
                async def wait(self):
                    if outcome == "cancelled":
                        started.set()
                        await asyncio.Event().wait()
                    if outcome == "blocked":
                        raise ArtifactProcessingBlockedError(
                            ArtifactProcessingBlock("private-key", "stage", "private-code")
                        )
                    return ArtifactProcessingWorkerCompletion(noop=outcome == "noop", held_count=int(outcome == "hold"))

                async def terminate(self):
                    pass

            class Launcher:
                async def start(self, assignment):
                    if outcome in {"success", "noop", "hold"}:
                        async with profile.database.transaction() as connection:
                            await intents.acknowledge(
                                connection, "private-scope", "binding", assignment.claimed_request_generation
                            )
                    return Handle()

            async with profile.database.transaction() as connection:
                await intents.request(connection, "private-scope", "binding")
            supervisor = ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=[ArtifactProcessingBinding("binding", "memory", Launcher())],
                lease_mode="single-process",
            )
            try:
                await supervisor.start()
                if outcome == "cancelled":
                    await asyncio.wait_for(started.wait(), 3)
                    await supervisor.close()
                else:
                    async with asyncio.timeout(3):
                        while not supervisor.processing_observations["memory"].attempt_durations:  # noqa: ASYNC110
                            await asyncio.sleep(0.01)
                        if outcome in {"success", "noop", "hold"}:
                            while supervisor.family_status["memory"]["completed"] == 0:  # noqa: ASYNC110
                                await asyncio.sleep(0.01)
                observation = supervisor.processing_observations["memory"]
                assert len(observation.attempt_durations) == 1
                duration = observation.attempt_durations[0]
                assert duration.outcome == outcome
                assert duration.count == 1
                assert duration.buckets[-1][1] == 1
                assert duration.total_seconds >= 0
                status = supervisor.family_status["memory"]
                assert float(status["requested_snapshot_timestamp_seconds"]) > 0
                assert (float(status["last_acknowledgement_timestamp_seconds"]) > 0) == (
                    outcome in {"success", "noop", "hold"}
                )
                assert "private" not in repr(observation)
                # Repeated snapshot reads do not increment observations or dispatch work.
                assert supervisor.processing_observations == supervisor.processing_observations
            finally:
                await supervisor.close()

    asyncio.run(scenario())


def test_pending_age_tracks_only_observed_pending_work(tmp_path):
    async def scenario():
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pending.db'}"), tables=SHARED_TABLES
        ) as profile:

            class Handle:
                async def wait(self):
                    await asyncio.Event().wait()

                async def terminate(self):
                    pass

            class Launcher:
                async def start(self, assignment):
                    return Handle()

            supervisor = ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=[ArtifactProcessingBinding("binding", "memory", Launcher(), max_workers=2)],
                lease_mode="single-process",
            )
            assert supervisor.family_status["memory"]["requested_snapshot_timestamp_seconds"] == 0
            async with profile.database.transaction() as connection:
                await ArtifactProcessingIntentRepository().request(connection, "scope", "binding")
            await supervisor.start()
            try:
                await asyncio.sleep(0.1)
                first = supervisor.family_status["memory"]
                assert first["observed_pending_scopes"] == 1
                assert float(first["oldest_observed_pending_age_seconds"]) > 0
                await asyncio.sleep(0.01)
                second = supervisor.family_status["memory"]
                assert float(second["oldest_observed_pending_age_seconds"]) > float(
                    first["oldest_observed_pending_age_seconds"]
                )
                assert second["requested_snapshot_timestamp_seconds"] == first["requested_snapshot_timestamp_seconds"]
                # A fresh cohort must not inherit an acknowledged predecessor's age,
                # even while the old worker has not yet returned to the supervisor.
                async with profile.database.transaction() as connection:
                    intents = ArtifactProcessingIntentRepository()
                    await intents.acknowledge(connection, "scope", "binding", 1)
                    await intents.request(connection, "scope", "binding")
                supervisor.wake()
                async with asyncio.timeout(3):
                    while (  # noqa: ASYNC110 - poll the externally visible discovery observation
                        supervisor.family_status["memory"]["requested_snapshot_timestamp_seconds"]
                        == second["requested_snapshot_timestamp_seconds"]
                    ):
                        await asyncio.sleep(0.001)
                renewed = supervisor.family_status["memory"]
                assert float(renewed["oldest_observed_pending_age_seconds"]) < float(
                    first["oldest_observed_pending_age_seconds"]
                )

            finally:
                await supervisor.close()
            assert supervisor.family_status["memory"]["observed_pending_scopes"] == 0
            assert supervisor.family_status["memory"]["supervisor_running"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "conflict", [ArtifactProcessingWorkerOutcome.CURSOR_CONFLICT, ArtifactProcessingWorkerOutcome.HEAD_CONFLICT]
)
def test_retry_reason_describes_current_event_after_failure(tmp_path, conflict):
    async def scenario():
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'retries.db'}"), tables=SHARED_TABLES
        ) as profile:
            attempts = 0

            class Handle:
                async def wait(self):
                    if attempts == 1:
                        raise RuntimeError("private error")  # noqa: TRY003 - sensitive failure fixture
                    if attempts == 2:
                        return ArtifactProcessingWorkerCompletion(conflict)
                    await asyncio.Event().wait()

                async def terminate(self):
                    pass

            class Launcher:
                async def start(self, assignment):
                    nonlocal attempts
                    attempts += 1
                    return Handle()

            async with profile.database.transaction() as connection:
                await ArtifactProcessingIntentRepository().request(connection, "scope", "binding")
            supervisor = ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=[ArtifactProcessingBinding("binding", "memory", Launcher())],
                lease_mode="single-process",
                retry_base_seconds=0.001,
                retry_jitter=lambda: 1,
            )
            try:
                await supervisor.start()
                async with asyncio.timeout(3):
                    while attempts < 3:  # noqa: ASYNC110 - public scheduler observation
                        await asyncio.sleep(0.01)
                assert dict(supervisor.processing_observations["memory"].retry_counts) == {"failure": 1, "conflict": 1}
            finally:
                await supervisor.close()

    asyncio.run(scenario())

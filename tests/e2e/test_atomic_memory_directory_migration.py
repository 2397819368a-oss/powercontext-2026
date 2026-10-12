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


"""Real business baselines and explicit unified directory migration/recovery."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, event
from typer.testing import CliRunner

from powercontext.artifacts.search import ArtifactSearchExecutionContext
from powercontext.builtin.persistence.atomic_memory_directory import AtomicMemoryDirectoryUnavailableError
from powercontext.builtin.persistence.migrations import MigrationError
from powercontext.builtin.persistence.migrations.deployment import deployment_runner, production_bundle
from powercontext.builtin.persistence.migrations.sqlite import SQLiteMigrationRunner
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import InvalidCursorError
from powercontext.builtin.runtime.composition import open_builtin_contexts
from powercontext.builtin.runtime.config import BuiltinConfig
from powercontext.cli.app import create_cli
from powercontext.server.cli import app as server_cli
from powercontext.server.factory import create_server_app
from powercontext.server.settings import ServerSettings

_LOCAL = ArtifactSearchExecutionContext(trusted_local=True)


def _settings(path: Path) -> ServerSettings:
    return ServerSettings(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{path}"),
        cursor_signing_secret=SecretStr("directory-migration-test-secret-32-bytes"),
    )


def _runner(path: Path) -> SQLiteMigrationRunner:
    runner = deployment_runner(_settings(path), target="atomic-memory-directory")
    assert isinstance(runner, SQLiteMigrationRunner)
    return runner


def _apply(runner: SQLiteMigrationRunner):
    plan = runner.plan()
    return runner.apply(plan_id=plan.plan_id, accepted=True, maintenance_confirmed=True)


async def _source(path: Path, count: int = 2) -> str:
    application = create_server_app(settings=_settings(path), scheduler_path=path.with_suffix(".scheduler.json"))
    async with application.router.lifespan_context(application):
        runtime = application.state.application
        scope_id = (await runtime.scopes.default_scope()).scope_id
        await runtime._records().create_atomic_memories(
            scope_id, tuple({"kind": "fact", "text": f"Fact {index}."} for index in range(count))
        )
        with pytest.raises(AtomicMemoryDirectoryUnavailableError):
            await runtime.atomic_memory.for_scope(scope_id).query_directory(context=_LOCAL)
        return scope_id


def test_shared_cli_initializes_directory_target_and_inspections_do_not_create_it(tmp_path):
    path = tmp_path / "fresh.sqlite"
    environment = tmp_path / "deployment.env"
    environment.write_text(
        f"POWERCONTEXT_SERVER_DATABASE_KIND=sqlite\nPOWERCONTEXT_SERVER_DATABASE_URL=sqlite+aiosqlite:///{path}\n"
    )
    command = ["server", "db-migrate"]
    options = ["--target", "atomic-memory-directory", "--env-file", str(environment)]
    cli = CliRunner()
    for action in ("status", "plan"):
        result = cli.invoke(create_cli([server_cli]), [*command, action, *options])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data_tasks"] == ["atomic-memory-directory-v1"]
        assert not path.exists()
        assert not path.with_name(path.name + ".pc-migration-state").exists()
    plan = json.loads(result.output)
    applied = cli.invoke(
        create_cli([server_cli]),
        [
            *command,
            "apply",
            *options,
            "--backup",
            "auto",
            "--plan-id",
            plan["plan_id"],
            "--yes",
            "--maintenance-confirmed",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["revision"] == "p0005"
    verified = cli.invoke(create_cli([server_cli]), [*command, "verify", *options])
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.output)["server_ready"] is False


def test_actual_server_baseline_upgrades_without_changing_exact_history_or_identity(tmp_path):
    path = tmp_path / "existing.sqlite"
    scope_id = asyncio.run(_source(path, 201))
    with sqlite3.connect(path) as connection:
        originals = connection.execute("SELECT * FROM pc_artifacts ORDER BY artifact_id,revision").fetchall()
        identity = connection.execute("SELECT server_id FROM pc_server_identity").fetchone()
    runner = _runner(path)
    plan = runner.plan()
    assert plan.adopt_baseline and plan.source_revision == "p0004"
    assert plan.data_tasks == ("atomic-memory-directory-v1",)
    result = _apply(runner)
    assert result.backup_state == "completed"
    assert runner.verify().state == "ready"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM pc_artifacts ORDER BY artifact_id,revision").fetchall() == originals
        assert connection.execute("SELECT server_id FROM pc_server_identity").fetchone() == identity
        assert connection.execute("SELECT DISTINCT content_kind FROM pc_artifact_heads").fetchall() == [("fact",)]
        generation = connection.execute("SELECT atomic_memory_directory_generation FROM pc_scopes").fetchone()[0]
        assert generation == 2
    assert _apply(runner).changed is False
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT atomic_memory_directory_generation FROM pc_scopes").fetchone()[0] == generation
        )

    async def browse():
        application = create_server_app(settings=_settings(path), scheduler_path=path.with_suffix(".scheduler.json"))
        async with application.router.lifespan_context(application):
            runtime = application.state.application
            scoped = runtime.atomic_memory.for_scope(scope_id)
            first = await scoped.query_directory(limit=100, context=_LOCAL)
            second = await scoped.query_directory(limit=100, cursor=first.next_cursor, context=_LOCAL)
            last = await scoped.query_directory(limit=100, cursor=second.next_cursor, context=_LOCAL)
            assert len(first.items + second.items + last.items) == 201
            assert last.next_cursor is None
            item = first.items[0]
            exact = await scoped.get(item.artifact.artifact_id, revision=item.artifact.revision, context=_LOCAL)
            assert exact.ref == item.artifact
            await runtime._records().create_atomic_memories(scope_id, ({"kind": "rule", "text": "A new rule."},))
            with pytest.raises(InvalidCursorError):
                await scoped.query_directory(limit=100, cursor=first.next_cursor, context=_LOCAL)

    asyncio.run(browse())
    assert runner.verify().state == "ready"


def test_committed_first_batch_resumes_with_original_backup_and_no_extra_generation(tmp_path, monkeypatch):
    path = tmp_path / "interrupted.sqlite"
    asyncio.run(_source(path, 201))
    runner = _runner(path)
    load = runner.bundle.task_functions

    def interrupted(task):
        functions = load(task)
        execute = functions["apply_batches"]

        def crash(connection):
            for changed in execute(connection):
                yield changed
                raise RuntimeError("interrupted after committed data batch")  # noqa: TRY003

        functions["apply_batches"] = crash
        return functions

    monkeypatch.setattr(runner.bundle, "task_functions", interrupted)
    with pytest.raises(RuntimeError, match="committed data batch"):
        _apply(runner)
    evidence = json.loads(runner.evidence_path.read_text())
    assert evidence["state"] == "active"

    async def cannot_restart_during_maintenance():
        from powercontext.server.authz.composition import open_builtin_access_control
        from powercontext.server.authz.models import PrincipalRef

        application = create_server_app(settings=_settings(path), scheduler_path=path.with_suffix(".scheduler.json"))
        with pytest.raises(MigrationError, match="recovery_required"):
            async with application.router.lifespan_context(application):
                pytest.fail("Unfinished offline maintenance must not start business writers")
        with pytest.raises(MigrationError, match="recovery_required"):
            async with open_builtin_access_control(
                _settings(path).database, bootstrap_administrators=(PrincipalRef(type="user", id="new-admin"),)
            ):
                pytest.fail("Authorization bootstrap must not write before the maintenance gate")

    asyncio.run(cannot_restart_during_maintenance())
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM pc_artifact_heads WHERE content_kind IS NULL").fetchone()[0] == 1
        )
        assert connection.execute("SELECT atomic_memory_directory_generation FROM pc_scopes").fetchone()[0] == 1
    resumed = _runner(path)
    assert resumed.plan().state == "recovery_required"
    assert resumed.plan().revisions == ()
    assert resumed.plan().data_tasks == ("atomic-memory-directory-v1",)
    _apply(resumed)
    completed = json.loads(resumed.evidence_path.read_text())
    assert completed["backup"] == evidence["backup"]
    assert completed["maintenance_window_id"] == evidence["maintenance_window_id"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT atomic_memory_directory_generation FROM pc_scopes").fetchone()[0] == 2
    assert _apply(resumed).changed is False


def test_bad_later_batch_preserves_that_batch_and_explicit_repair_can_resume(tmp_path):
    path = tmp_path / "bad-body.sqlite"
    asyncio.run(_source(path, 201))
    with sqlite3.connect(path) as connection:
        identity, body = connection.execute(
            "SELECT artifact_id,content FROM pc_artifacts ORDER BY artifact_id DESC LIMIT 1"
        ).fetchone()
        connection.execute("UPDATE pc_artifacts SET content=? WHERE artifact_id=?", (b"not-json", identity))
    runner = _runner(path)
    with pytest.raises(MigrationError, match="verification_failed"):
        _apply(runner)

    async def cannot_restart_after_failed_task():
        application = create_server_app(settings=_settings(path), scheduler_path=path.with_suffix(".scheduler.json"))
        with pytest.raises(MigrationError, match="recovery_required"):
            async with application.router.lifespan_context(application):
                pytest.fail("Failed offline maintenance must remain stopped")

    asyncio.run(cannot_restart_after_failed_task())
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT content_kind FROM pc_artifact_heads WHERE artifact_id=?", (identity,)
        ).fetchone() == (None,)
        assert (
            connection.execute("SELECT COUNT(*) FROM pc_artifact_heads WHERE content_kind='fact'").fetchone()[0] == 200
        )
        connection.execute("UPDATE pc_artifacts SET content=? WHERE artifact_id=?", (body, identity))
    _apply(_runner(path))
    assert _runner(path).verify().state == "ready"


def test_completed_schema_does_not_hide_data_damage_and_disabled_queries_keep_writer_maintenance(tmp_path):
    path = tmp_path / "damaged-kind.sqlite"
    scope_id = asyncio.run(_source(path))
    runner = _runner(path)
    _apply(runner)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE pc_artifact_heads SET content_kind='wrong' WHERE artifact_id=(SELECT MIN(artifact_id) FROM pc_artifact_heads)"
        )
    plan = runner.plan()
    assert plan.revisions == () and plan.state == "migration_required"
    with pytest.raises(MigrationError, match="migration_required"):
        runner.verify()

    async def write_while_directory_unavailable():
        application = create_server_app(settings=_settings(path), scheduler_path=path.with_suffix(".scheduler.json"))
        async with application.router.lifespan_context(application):
            runtime = application.state.application
            with pytest.raises(AtomicMemoryDirectoryUnavailableError):
                await runtime.atomic_memory.for_scope(scope_id).query_directory(context=_LOCAL)
            await runtime._records().create_atomic_memories(
                scope_id, ({"kind": "rule", "text": "Still maintain new writes."},)
            )

    asyncio.run(write_while_directory_unavailable())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pc_artifact_heads WHERE content_kind='rule'").fetchone()[0] == 1
        before = connection.execute("SELECT atomic_memory_directory_generation FROM pc_scopes").fetchone()[0]
    _apply(runner)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT atomic_memory_directory_generation FROM pc_scopes").fetchone()[0] == before + 1
        )


def test_unknown_business_objects_are_not_ignored_and_missing_recovery_evidence_blocks(tmp_path, monkeypatch):
    path = tmp_path / "unknown.sqlite"
    asyncio.run(_source(path))
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unknown_extension (value TEXT)")
    with pytest.raises(MigrationError, match="unknown_baseline"):
        _runner(path).plan()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE unknown_extension")
    runner = _runner(path)
    original = runner.bundle.task_functions

    def fail(task):
        functions = original(task)
        functions["apply_batches"] = lambda connection: (_ for _ in ()).throw(RuntimeError("interrupted"))
        return functions

    monkeypatch.setattr(runner.bundle, "task_functions", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        _apply(runner)
    runner.evidence_path.unlink()
    with pytest.raises(MigrationError, match="recovery_required"):
        _runner(path).plan()


def test_default_acceptance_bundle_remains_four_table_scope():
    assert production_bundle().head == "p0003"
    assert production_bundle().data_tasks == ()


def test_unsupported_sqlite_connection_options_cannot_silently_skip_managed_writer_hooks(tmp_path):
    path = tmp_path / "connection-options.sqlite"
    scope_id = asyncio.run(_source(path))
    _apply(_runner(path))
    with sqlite3.connect(path) as connection:
        before = connection.execute("SELECT atomic_memory_directory_generation FROM pc_scopes").fetchone()[0]

    async def mutate_through_alias():
        alias = SQLiteConfig(url=f"sqlite+aiosqlite:///{path}?timeout=5")
        async with open_builtin_contexts(BuiltinConfig(database=alias)) as contexts:
            with pytest.raises(AtomicMemoryDirectoryUnavailableError):
                await contexts.atomic_memory.for_scope(scope_id).query_directory(context=_LOCAL)
            await contexts.records.create_atomic_memories(scope_id, ({"kind": "rule", "text": "Maintain aliases."},))

    asyncio.run(mutate_through_alias())
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT atomic_memory_directory_generation FROM pc_scopes").fetchone()[0] == before + 1
        )
    assert _runner(path).verify().state == "ready"


def test_directory_target_rejects_existing_acceptance_database_before_any_write(tmp_path):
    path = tmp_path / "acceptance.sqlite"
    acceptance = SQLiteMigrationRunner(path, production_bundle())
    _apply(acceptance)
    before = path.read_bytes()
    with pytest.raises(MigrationError, match="unsupported_target"):
        _runner(path).plan()
    assert path.read_bytes() == before


def test_registered_sqlite_task_target_rejects_other_backends_before_connecting(tmp_path):
    from powercontext.builtin.persistence.seekdb import SeekDBConfig

    settings = ServerSettings(database=SeekDBConfig(path=tmp_path / "not-created"))
    with pytest.raises(MigrationError, match="unsupported_target"):
        deployment_runner(settings, target="atomic-memory-directory")
    assert not (tmp_path / "not-created").exists()


def test_directory_target_requires_the_evidence_location_its_runtime_can_inspect(tmp_path):
    path = tmp_path / "not-created.sqlite"
    with pytest.raises(MigrationError, match="unsupported_option"):
        deployment_runner(_settings(path), target="atomic-memory-directory", evidence_dir=tmp_path / "other-evidence")
    assert not path.exists()
    assert not (tmp_path / "other-evidence").exists()


def test_topic_child_does_not_scan_atomic_bodies_when_reusing_managed_parent_schema(tmp_path):
    path = tmp_path / "topic-child.sqlite"
    asyncio.run(_source(path))
    _apply(_runner(path))
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lower()
        if "left join pc_artifacts" in normalized and "atomic-memory" in normalized:
            statements.append(statement)

    async def open_child():
        async with open_builtin_contexts(BuiltinConfig(database=_settings(path).database), _topic_memory_worker=True):
            pass

    event.listen(Engine, "before_cursor_execute", observe)
    try:
        asyncio.run(open_child())
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
    assert statements == [], "Topic-only children must not verify the unrelated Atomic directory"

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

"""Select maintenance adapters without initializing the business runtime."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy.engine import make_url

from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.persistence.sqlite.profile import _readonly_sqlite_url

from .bundle import MigrationBundle
from .connections import MaintenanceConnections
from .models import MigrationError
from .mysql import MySQLMigrationRunner
from .sqlite import SQLiteMigrationRunner

if TYPE_CHECKING:
    from powercontext.server.settings import ServerSettings


def production_bundle(*, target: Literal["atomic-memory-directory"] | None = None) -> MigrationBundle:
    """Load installed resources; readiness is scoped to this acceptance bundle."""
    return MigrationBundle(Path(__file__).parent / "resources", target=target)


def _directory_runner(database: SQLiteConfig) -> tuple[SQLiteMigrationRunner, str | None] | None:
    if not database.is_persistent:
        return None
    url = make_url(database.url)
    if not url.database or url.host or url.username or url.password or url.port:
        return None
    readonly_url = _readonly_sqlite_url(database.url).set(drivername="sqlite")
    arguments, _ = readonly_url.get_dialect()().create_connect_args(readonly_url)
    try:
        inspection = sqlite3.connect(str(arguments[0]), uri=True, timeout=database.busy_timeout_ms / 1000)
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_CANTOPEN:
            return None
        raise
    with closing(inspection) as connection:
        path = Path(next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")).resolve()
        exists = connection.execute("SELECT 1 FROM sqlite_schema WHERE name='pc_schema_revision'").fetchone()
        if exists is None and not path.with_name(path.name + ".pc-migration-state").exists():
            return None
        revisions = (
            [] if exists is None else connection.execute("SELECT version_num FROM pc_schema_revision").fetchall()
        )
        if len(revisions) > 1:
            raise MigrationError("incompatible_schema", "Multiple schema revisions are not supported.")
        revision = None if not revisions else str(revisions[0][0])
    runner = SQLiteMigrationRunner(
        path,
        production_bundle(target="atomic-memory-directory"),
        configuration=database.model_dump(exclude={"url", "echo"}),
    )
    return runner, revision


def assert_directory_maintenance_complete(database: SQLiteConfig) -> None:
    """Check only maintenance evidence; do not scan domain data for each component open."""

    inspected = _directory_runner(database)
    if inspected is not None:
        inspected[0].assert_business_startup_allowed()


def directory_readiness(database: SQLiteConfig) -> bool | None:
    """Inspect the optional directory without initialization or repair.

    False retains writer hooks but disables querying. None is an unmanaged
    directory. The shared executor owns data and maintenance classification.
    """

    inspected = _directory_runner(database)
    if inspected is None:
        return None
    runner, revision = inspected
    runner.assert_business_startup_allowed()
    if revision is None or revision in {"p0001", "p0002", "p0003"}:
        return None
    if revision not in {"p0004", "p0005"}:
        raise MigrationError("incompatible_schema", "The schema revision is not supported by this program.")
    try:
        plan = runner.plan()
    except MigrationError as error:
        if error.code == "verification_failed":
            return False
        raise
    if plan.state == "recovery_required":
        raise MigrationError("recovery_required", "Complete explicit directory maintenance before business startup.")
    return plan.state == "ready" and not make_url(database.url).query if revision == "p0005" else None


MigrationRunner = SQLiteMigrationRunner | MySQLMigrationRunner


def deployment_runner(  # noqa: C901 - one authority for target/backend/coordination eligibility
    settings: ServerSettings,
    *,
    evidence_dir: Path | None = None,
    lock_coordination: Literal["single-host"] | None = None,
    acknowledge_previous_run: str | None = None,
    target: Literal["atomic-memory-directory"] | None = None,
) -> MigrationRunner:
    """Use the real configured identity without opening a business profile."""
    database = settings.database
    if target is not None and evidence_dir is not None:
        raise MigrationError(
            "unsupported_option", "The directory target uses the database's default evidence directory."
        )
    if target is not None and not isinstance(database, SQLiteConfig):
        raise MigrationError("unsupported_target", "The directory target is registered only for persistent SQLite.")
    if target is not None and settings.inference.embedding_model is not None:
        raise MigrationError("unsupported_target", "The registered directory target has no embedding extension.")
    if acknowledge_previous_run is not None and not isinstance(database, OceanBaseConfig):
        raise MigrationError("unsupported_option", "--acknowledge-previous-run is supported only for OceanBase.")
    if isinstance(database, (SeekDBConfig, OceanBaseConfig)):
        if evidence_dir is None:
            if isinstance(database, OceanBaseConfig):
                raise MigrationError(
                    "evidence_required",
                    "OceanBase maintenance requires --evidence-dir on one fixed host, shared by all migration Jobs.",
                )
            engine_directory = database.path.expanduser().resolve()
            evidence_dir = engine_directory.with_name(engine_directory.name + ".pc-migration-state")
        if isinstance(database, OceanBaseConfig) and lock_coordination != "single-host":
            raise MigrationError(
                "coordination_required",
                "OceanBase requires --lock-coordination single-host; run all migrators on one fixed host and evidence directory.",
            )
        return MySQLMigrationRunner(
            MaintenanceConnections(
                database,
                evidence_directory=evidence_dir,
                lock_coordination=lock_coordination,
                acknowledge_previous_run=acknowledge_previous_run,
            ),
            production_bundle(),
            evidence_directory=evidence_dir,
            configuration=database.model_dump(mode="json", exclude={"url", "echo"}),
        )
    if not isinstance(database, SQLiteConfig):
        raise MigrationError("unsupported_backend", "The database has no registered migration adapter.")
    url = make_url(database.url)
    if url.query or not url.database or url.username or url.password or url.host or url.port or database.is_in_memory:
        raise MigrationError(
            "unsupported_target", "Use a regular SQLite file URL without credentials, host, port or URI options."
        )
    # Match SQLite's URL normalization before the runner expands standalone Path inputs.
    database_path = Path(os.path.abspath(url.database))
    return SQLiteMigrationRunner(
        database_path,
        production_bundle(target=target),
        configuration=database.model_dump(exclude={"url", "echo"}),
        evidence_directory=evidence_dir,
    )

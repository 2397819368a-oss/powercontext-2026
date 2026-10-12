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

"""A checked, linear Alembic history with explicitly recognized schema shapes."""

from __future__ import annotations

import hashlib
import json
import runpy
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

from .models import MigrationError, digest

ALEMBIC_CONTEXT_LOCK = threading.RLock()


class MigrationBundle:
    """Load a trusted local bundle. No revision imports current application metadata.

    The manifest records exact SQLite schema fingerprints, including alternate
    shapes produced by a supported historical initializer. Unlisted objects or
    partially applied DDL cannot be mistaken for a supported baseline.
    """

    def __init__(self, directory: Path, *, target: str | None = None) -> None:  # noqa: C901
        self.directory = directory.resolve()
        # Execute exactly the reviewed bytes, even if the installed resources
        # change during a long backup. Alembic reloads scripts on every command.
        resources = {
            path.relative_to(self.directory).as_posix(): path.read_bytes()
            for path in sorted(self.directory.rglob("*"))
            if path.is_file() and path.suffix in {".py", ".sql", ".json"}
        }
        self._snapshot = TemporaryDirectory(prefix="pc-migration-bundle-")
        self.snapshot_directory = Path(self._snapshot.name).resolve()
        for name, content in resources.items():
            resource_target = self.snapshot_directory / name
            resource_target.parent.mkdir(parents=True, exist_ok=True)
            resource_target.write_bytes(content)
        manifest = json.loads(resources["manifest.json"])
        selected = manifest.get("targets", {}).get(target) if target is not None else None
        if target is not None and selected is None:
            raise MigrationError("unsupported_target", "The release does not register this migration target.")
        head = selected["revision"] if selected is not None else manifest.get("default_revision", "heads")
        self.script = ScriptDirectory(str(self.snapshot_directory))
        known_revisions = {item.revision for item in self.script.walk_revisions()}
        if (
            set(manifest["sqlite_fingerprints"]) != known_revisions
            or not set(manifest["sqlite_baselines"].values()) <= known_revisions
        ):
            raise MigrationError("invalid_bundle", "Schema inventory must cover the registered history and baselines.")
        revisions = list(reversed(list(self.script.walk_revisions(head=head))))
        previous = None
        for item in revisions:
            if item.down_revision != previous or item.dependencies or len(item.revision) > 32:
                raise MigrationError("invalid_bundle", "Migration history must be a single linear chain.")
            previous = item.revision
        if not revisions:
            raise MigrationError("invalid_bundle", "Migration history is empty.")
        self.revisions = tuple(item.revision for item in revisions)
        self.head = self.revisions[-1]
        self.affected_objects = tuple((selected or manifest).get("affected_objects", ()))
        self.retained_objects = tuple(manifest.get("retained_objects", ()))
        self.retained_fingerprints = manifest.get("retained_fingerprints", {})
        self.retained_revisions = manifest.get("retained_revisions", {})
        if set(self.retained_objects) != set(self.retained_revisions) or not set(
            self.retained_revisions.values()
        ) <= set(self.revisions):
            raise MigrationError("invalid_bundle", "Retained objects must declare their introducing revision.")
        if not set(self.revisions) <= set(manifest["sqlite_fingerprints"]):
            raise MigrationError("invalid_bundle", "Schema inventory does not cover the revision chain.")
        self.fingerprints: dict[str, list[str]] = {
            revision: manifest["sqlite_fingerprints"][revision] for revision in self.revisions
        }
        self.baselines: dict[str, str] = {
            fingerprint: revision
            for fingerprint, revision in manifest["sqlite_baselines"].items()
            if revision in self.revisions and (selected is None or revision in selected["baseline_revisions"])
        }
        self.minimum_source_revision = None if selected is None else selected["minimum_source_revision"]
        if any(fingerprint not in self.fingerprints[revision] for fingerprint, revision in self.baselines.items()):
            raise MigrationError("invalid_bundle", "A baseline does not match its declared revision.")
        self.resources = {name: hashlib.sha256(content).hexdigest() for name, content in resources.items()}
        self.checksums = {}
        for item in revisions:
            dependencies = manifest["revision_resources"][item.revision]
            inputs = {name: self.resources[name] for name in ["env.py", *dependencies]}
            inputs[Path(item.path).relative_to(self.snapshot_directory).as_posix()] = hashlib.sha256(
                Path(item.path).read_bytes()
            ).hexdigest()
            self.checksums[item.revision] = digest(inputs)
        self.checksum = digest({"resources": self.resources, "target_revision": self.head, "target": selected})
        self.data_tasks = tuple(task for task in manifest.get("data_tasks", ()) if task["revision"] in self.revisions)
        for task in self.data_tasks:
            if task["resource"] not in manifest["revision_resources"][task["revision"]]:
                raise MigrationError("invalid_bundle", "A data task must be bound to its revision resources.")

    def task_functions(self, task: dict[str, str]) -> dict[str, Any]:
        """Load only the immutable resource snapshot used by the reviewed plan."""

        self.assert_unchanged()
        return runpy.run_path(str(self.snapshot_directory / task["resource"]))

    def pending_data_tasks(self, connection: Connection, revision: str | None) -> tuple[str, ...]:
        """Inspect current data, independently of the schema revision or old completion evidence."""

        completed = set(self.revisions) - set(self.pending(revision))
        pending = []
        for task in self.data_tasks:
            try:
                if task["revision"] not in completed or not self.task_functions(task)["verify"](connection):
                    pending.append(task["name"])
            except ValueError as error:
                raise MigrationError(
                    "verification_failed", "Registered data does not satisfy the frozen domain format."
                ) from error
        return tuple(pending)

    def assert_unchanged(self) -> None:
        resources = {
            path.relative_to(self.directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.directory.rglob("*"))
            if path.is_file() and path.suffix in {".py", ".sql", ".json"}
        }
        if resources != self.resources:
            raise MigrationError("plan_changed", "Migration resources changed after the bundle was loaded.")

    def config(self, connection: Connection) -> Config:
        config = Config()
        config.set_main_option("script_location", str(self.snapshot_directory).replace("%", "%%"))
        config.attributes["connection"] = connection
        return config

    def pending(self, revision: str | None) -> tuple[str, ...]:
        if revision is None:
            return self.revisions
        if revision not in self.revisions:
            raise MigrationError("incompatible_schema", "Database revision is unknown to this binary.")
        return self.revisions[self.revisions.index(revision) + 1 :]

    def verify(self, revision: str | None, fingerprint: str) -> None:
        if revision is None:
            allowed = [digest([])]
        else:
            self.pending(revision)
            allowed = self.fingerprints[revision]
        if fingerprint not in allowed:
            raise MigrationError("incompatible_schema", "Actual schema does not match the recorded revision.")


def sqlite_inventory(connection: Connection, *, exclude: frozenset[str] = frozenset()) -> list[list[Any]]:
    """Include tables, indexes, triggers, views and constraint-bearing SQL.

    This deliberately uses exact SQLite definitions. A semantically equivalent
    but unregistered schema requires review, never an optimistic stamp.
    """
    rows = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name"
    )
    return [list(row) for row in rows if row[1] not in exclude and row[2] not in exclude]

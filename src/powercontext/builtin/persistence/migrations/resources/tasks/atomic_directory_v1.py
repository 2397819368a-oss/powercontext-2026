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


"""Immutable current-head directory task; the shared executor owns commits and recovery.

This resource imports no current Repository, Runtime or domain decoder. Its v1
content schema and supplementary validation travel with the reviewed bundle.
"""

import json
import unicodedata
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

_MAX_GENERATION = (1 << 63) - 1
_BATCH_SIZE = 200
_RESOURCE = json.loads((Path(__file__).parents[1] / "schemas/atomic_directory_sqlite_v1.json").read_text())
_CONTENT = Draft202012Validator(_RESOURCE["content_schema"])


def _valid_identity(value, maximum):
    return isinstance(value, str) and 0 < len(value) <= maximum and bool(value.strip())


def _check_authority(connection):
    orphan = connection.exec_driver_sql(
        "SELECT 1 FROM pc_artifact_heads h LEFT JOIN pc_scopes s ON s.scope_id=h.scope_id "
        "WHERE h.family='atomic-memory' AND s.scope_id IS NULL LIMIT 1"
    ).first()
    legacy = connection.exec_driver_sql("SELECT 1 FROM pc_artifacts WHERE family='memory' LIMIT 1").first()
    if orphan is not None or legacy is not None:
        raise ValueError("atomic_directory_authority_invalid")
    after = None
    while True:
        statement = (
            "SELECT scope_id, atomic_memory_directory_generation FROM pc_scopes ORDER BY scope_id LIMIT 200"
            if after is None
            else "SELECT scope_id, atomic_memory_directory_generation FROM pc_scopes "
            "WHERE scope_id>? ORDER BY scope_id LIMIT 200"
        )
        rows = connection.exec_driver_sql(
            statement,
            () if after is None else (after,),
        ).all()
        if not rows:
            return
        for scope_id, generation in rows:
            if (
                not _valid_identity(scope_id, 256)
                or type(generation) is not int
                or not 0 <= generation <= _MAX_GENERATION
            ):
                raise ValueError("atomic_directory_generation_invalid")
        after = rows[-1][0]


def _batches(connection):
    after = None
    while True:
        clause = "" if after is None else " AND (h.scope_id,h.artifact_id)>(?,?)"
        rows = connection.exec_driver_sql(
            "SELECT h.scope_id,h.artifact_id,h.revision,h.content_kind,h.lifecycle_state,"
            "h.governance_generation,h.merged_into_id,h.replacement_artifact_id,a.content "
            "FROM pc_artifact_heads h LEFT JOIN pc_artifacts a "
            "ON a.scope_id=h.scope_id AND a.family=h.family AND a.artifact_id=h.artifact_id AND a.revision=h.revision "
            "WHERE h.family='atomic-memory'" + clause + " ORDER BY h.scope_id,h.artifact_id LIMIT 201",
            () if after is None else after,
        ).all()
        if not rows:
            return
        yield rows[:_BATCH_SIZE]
        if len(rows) <= _BATCH_SIZE:
            return
        after = (rows[_BATCH_SIZE - 1][0], rows[_BATCH_SIZE - 1][1])


def _kind(row):
    scope_id, identity, revision, _kind_value, lifecycle, generation, merged, replacement, body = row
    try:
        content = json.loads(body)
        _CONTENT.validate(content)
        kind = content["kind"]
        text = content["text"]
        if (
            not kind.strip()
            or not text.strip()
            or min(len(text.encode("utf-8")), len(unicodedata.normalize("NFC", text).encode("utf-8"))) > 8192
            or not _valid_identity(scope_id, 256)
            or not _valid_identity(identity, 128)
            or type(revision) is not int
            or revision < 1
            or type(generation) is not int
            or generation < 0
            or lifecycle not in {"active", "deprecated", "retired"}
            or replacement is not None
            or (
                merged is not None
                and (not _valid_identity(merged, 128) or lifecycle != "deprecated" or merged == identity)
            )
        ):
            raise ValueError("invalid current Atomic metadata")  # noqa: TRY003, TRY301
        creation = content.get("creation")
        if creation is not None:
            inputs = creation["input_artifact_ids"]
            if len(set(inputs)) != len(inputs) or any(not _valid_identity(item, 128) for item in inputs):
                raise ValueError("invalid Atomic creation inputs")  # noqa: TRY003, TRY301
    except (TypeError, ValueError, ValidationError) as error:
        # Expose only a stable classification, never private content or identities.
        raise ValueError("atomic_directory_data_invalid") from error
    return kind


def verify(connection):
    _check_authority(connection)
    for rows in _batches(connection):
        for row in rows:
            if row[3] != _kind(row):
                return False
    return True


def apply_batches(connection):
    _check_authority(connection)
    for rows in _batches(connection):
        # Validate this entire batch before issuing any updates.
        changes = [(row, _kind(row)) for row in rows]
        changes = [(row, kind) for row, kind in changes if row[3] != kind]
        scopes = sorted({row[0] for row, _ in changes})
        for scope_id in scopes:
            result = connection.exec_driver_sql(
                "UPDATE pc_scopes SET atomic_memory_directory_generation=atomic_memory_directory_generation+1 "
                "WHERE scope_id=? AND atomic_memory_directory_generation>=0 AND atomic_memory_directory_generation<?",
                (scope_id, _MAX_GENERATION),
            )
            if result.rowcount != 1:
                raise ValueError("atomic_directory_generation_invalid")
        for row, kind in changes:
            result = connection.exec_driver_sql(
                "UPDATE pc_artifact_heads SET content_kind=? "
                "WHERE scope_id=? AND family='atomic-memory' AND artifact_id=? AND revision=?",
                (kind, row[0], row[1], row[2]),
            )
            if result.rowcount != 1:
                raise ValueError("atomic_directory_authority_invalid")
        yield len(changes)

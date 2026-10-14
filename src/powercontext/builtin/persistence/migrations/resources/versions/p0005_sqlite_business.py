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


"""Frozen current SQLite business structure, not a legacy business-data converter."""

import json
from pathlib import Path

from alembic import op

revision = "p0005"
down_revision = "p0004"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        raise ValueError("This registered target supports only SQLite")  # noqa: TRY003
    # The earlier five-table acceptance history is usable for empty initialization
    # only. Existing real business layouts are explicitly adopted at p0005.
    tables = (
        "pc_artifact_handles",
        "pc_artifact_tags",
        "pc_artifact_candidate_versions",
        "pc_artifact_heads",
        "pc_artifacts",
    )
    for table in tables:
        if connection.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first() is not None:  # noqa: S608
            raise ValueError("Nonempty acceptance layouts require a registered legacy adapter")  # noqa: TRY003
    for table in tables:
        connection.exec_driver_sql(f"DROP TABLE {table}")
    resource = json.loads((Path(__file__).parents[1] / "schemas/atomic_directory_sqlite_v1.json").read_text())
    for statement in resource["sqlite_init"]:
        connection.exec_driver_sql(statement)

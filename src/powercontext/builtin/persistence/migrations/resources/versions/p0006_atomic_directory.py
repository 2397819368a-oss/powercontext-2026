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


"""Expand directory metadata; explicit data tasks run under the same maintenance lock."""

import json
from pathlib import Path

from alembic import op

revision = "p0006"
down_revision = "p0005"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        raise ValueError("This registered target supports only SQLite")  # noqa: TRY003
    resource = json.loads((Path(__file__).parents[1] / "schemas/atomic_directory_sqlite_v1.json").read_text())
    for statement in resource["sqlite_expand"]:
        connection.exec_driver_sql(statement)

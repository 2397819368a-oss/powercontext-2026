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


"""Record when each Artifact revision was written and, for rollbacks, who restored which revision and why."""

import sqlalchemy as sa
from alembic import op

revision = "p0005"
down_revision = "p0004"


def upgrade() -> None:
    op.add_column("pc_artifacts", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pc_artifacts", sa.Column("created_by", sa.String(length=2048), nullable=True))
    op.add_column("pc_artifacts", sa.Column("restored_from_revision", sa.Integer(), nullable=True))
    op.add_column("pc_artifacts", sa.Column("rollback_reason", sa.String(length=1024), nullable=True))

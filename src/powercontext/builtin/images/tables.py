# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SQLite-only experimental metadata, separate from shared backend migrations."""

from sqlalchemy import Column, Index, Integer, LargeBinary, MetaData, String, Table

METADATA = MetaData()
STATE = Table(
    "pc_experimental_image_state",
    METADATA,
    Column("id", Integer, primary_key=True),
    Column("format_version", String(16), nullable=False),
    Column("asset_directory", String(4096), nullable=False),
    Column("asset_bytes", Integer, nullable=False),
    Column("pending_count", Integer, nullable=False),
    Column("worker_owner", String(36)),
    Column("worker_lease_until", Integer, nullable=False, server_default="0"),
    Column("last_scope", String(256)),
)
SCOPE_BUDGETS = Table(
    "pc_experimental_image_scope_budgets",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("pending_count", Integer, nullable=False),
)
ASSETS = Table(
    "pc_experimental_image_assets",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("asset_id", String(67), primary_key=True),
    Column("payload", LargeBinary, nullable=False),
    Column("state", String(16), nullable=False),
    Column("reserved_at", Integer, nullable=False),
)
Index("pc_experimental_image_orphans", ASSETS.c.state, ASSETS.c.reserved_at)
CAPTURES = Table(
    "pc_experimental_image_captures",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("source_id", String(68), primary_key=True),
    Column("request_digest", String(71), nullable=False),
    Column("response", LargeBinary, nullable=False),
)
TASKS = Table(
    "pc_experimental_image_tasks",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("task_id", String(67), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("profile", LargeBinary, nullable=False),
    Column("context", LargeBinary, nullable=False),
    Column("payload", LargeBinary, nullable=False),
    Column("original_source_id", String(68)),
    Column("analysis_source_id", String(69)),
    Column("claim_token", String(36)),
    Column("lease_until", Integer, nullable=False, server_default="0"),
    Column("available_at", Integer, nullable=False, server_default="0"),
    Column("created_at", Integer, nullable=False, server_default="0"),
)
Index("pc_experimental_image_task_list", TASKS.c.scope_id, TASKS.c.created_at, TASKS.c.task_id)
Index("pc_experimental_image_task_queue", TASKS.c.state, TASKS.c.scope_id, TASKS.c.task_id)
Index("pc_experimental_image_task_ready", TASKS.c.state, TASKS.c.available_at, TASKS.c.scope_id)
Index("pc_experimental_image_task_leases", TASKS.c.state, TASKS.c.lease_until)
Index("pc_experimental_image_analysis_lookup", TASKS.c.scope_id, TASKS.c.analysis_source_id, unique=True)
ATTEMPTS = Table(
    "pc_experimental_image_attempts",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("task_id", String(67), primary_key=True),
    Column("attempt", Integer, primary_key=True),
    Column("payload", LargeBinary, nullable=False),
)
OPERATIONS = Table(
    "pc_experimental_image_operations",
    METADATA,
    Column("scope_id", String(256), primary_key=True),
    Column("kind", String(16), primary_key=True),
    Column("operation_key", String(128), primary_key=True),
    Column("request_digest", String(71), nullable=False),
    Column("task_id", String(67), nullable=False),
)
CORE_TABLES = (STATE, SCOPE_BUDGETS, ASSETS, CAPTURES, TASKS)
TABLES = (*CORE_TABLES, ATTEMPTS, OPERATIONS)

# Additive migration from the already accepted phase-two format. No Source or
# identity is rewritten; absent core columns still mean unsupported storage.
LIFECYCLE_COLUMNS = {
    STATE.name: {
        "worker_owner": "VARCHAR(36)",
        "worker_lease_until": "INTEGER NOT NULL DEFAULT 0",
        "last_scope": "VARCHAR(256)",
    },
    TASKS.name: {
        "original_source_id": "VARCHAR(68)",
        "analysis_source_id": "VARCHAR(69)",
        "claim_token": "VARCHAR(36)",
        "lease_until": "INTEGER NOT NULL DEFAULT 0",
        "available_at": "INTEGER NOT NULL DEFAULT 0",
    },
}

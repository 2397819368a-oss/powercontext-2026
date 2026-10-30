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

"""Admission by the number of Journal entries a Scope has accumulated.

A window limit answers "how much may one invocation read"; a count threshold
answers "how much must be waiting before an invocation is worth starting". The
two are independent, so this module only decides *whether* to accept one more
invocation. The window a worker then reads is unchanged.

The count is the gap between the Journal head and the Family's own successful
cursor. That definition carries the properties the contract asks for, without a
second bookkeeping table:

* it is per Scope and per Family, because both sides of the subtraction are;
* a replayed capture, and a receipt trust upgrade on an existing Source, leave
  the head where it was, so they add nothing;
* a failed invocation does not move the cursor, so its input stays counted;
* a successful one moves the cursor, so consumed entries stop counting;
* a lineage-only server Source occupies a Journal position like any other, so it
  counts toward admission while ``is_generation_eligible`` still keeps it out of
  the generation evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.tables import SOURCES_TABLE


async def pending_journal_counts(
    connection: AsyncConnection,
    scope_ids: Sequence[str],
    binding_name: str,
    /,
    *,
    cursors: SourceCursorRepository | None = None,
) -> dict[str, int]:
    """Return, per Scope, how many Journal entries sit after its successful cursor."""

    ordered = tuple(dict.fromkeys(scope_ids))
    if not ordered:
        return {}
    heads = await _journal_heads(connection, ordered)
    repository = SourceCursorRepository() if cursors is None else cursors
    loaded = await repository.load_many(connection, ordered, binding_name)
    counts: dict[str, int] = {}
    for scope_id in ordered:
        state = loaded.get(scope_id)
        # The first invocation of a Scope starts at cursor zero, so an absent
        # cursor means nothing has been consumed yet rather than "unknown".
        consumed = 0 if state is None else state.cursor.sequence
        counts[scope_id] = max(0, heads.get(scope_id, 0) - consumed)
    return counts


async def _journal_heads(connection: AsyncConnection, scope_ids: Sequence[str]) -> dict[str, int]:
    table = SOURCES_TABLE
    rows = await connection.execute(
        select(table.c.scope_id, func.max(table.c.journal_position))
        .where(table.c.scope_id.in_(scope_ids))
        .group_by(table.c.scope_id)
    )
    return {str(scope_id): int(head or 0) for scope_id, head in rows}


__all__ = ["pending_journal_counts"]

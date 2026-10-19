# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Bounded built-in page projections; DashboardAPI owns authorization and snapshots."""

from sqlalchemy import func, literal, select, union_all

from powercontext.builtin.persistence.artifact_governance import decode_artifact_governance
from powercontext.builtin.persistence.artifacts import with_artifact_content
from powercontext.builtin.persistence.atomic_memory import _atomic_state
from powercontext.builtin.persistence.statistics import daily_usage_statement, decode_daily_usage
from powercontext.builtin.persistence.tables import ARTIFACT_HEADS_TABLE, ARTIFACT_LINEAGE_SOURCES_TABLE, SCOPES_TABLE
from powercontext.builtin.runtime.statistics import _as_utc, _recall_statistics, _resolve_period, _usage_statistics
from powercontext.builtin.statistics import StatisticsPeriod
from powercontext.server.dashboard.errors import ReadError
from powercontext.server.dashboard.pagination import PAGE_SIZE

_HEAD_FIELDS = (
    "scope_id",
    "family",
    "artifact_id",
    "revision",
    "lifecycle_state",
    "replacement_artifact_id",
    "merged_into_id",
    "governance_generation",
    "content",
)
_HEADER_FIELDS = ("title", "summary", "parent_scope_id")


def _with_header(statement, scope, kind):
    """Keep an empty page distinguishable from a missing Scope in the same read."""
    data = statement.subquery("page_rows")
    header = SCOPES_TABLE
    return union_all(
        select(*data.c, *(literal(None, type_=header.c[name].type).label(name) for name in _HEADER_FIELDS)),
        select(
            *(
                literal("scope", type_=column.type).label(column.name)
                if column.name == kind
                else header.c.scope_id
                if column.name == "scope_id"
                else literal(None, type_=column.type).label(column.name)
                for column in data.c
            ),
            *(header.c[name] for name in _HEADER_FIELDS),
        ).where(header.c.scope_id == scope),
    )


def set_scope(ctx, descriptor):
    if descriptor is None:
        raise ReadError(404, "not_found")
    ctx["scope_descriptor"] = {name: descriptor[name] for name in ("scope_id", *_HEADER_FIELDS)}
    ctx["data"].update(title=descriptor["title"], summary=descriptor["summary"])
    parent = descriptor["parent_scope_id"]
    if parent:
        ctx["parent_scope"] = {"scope_id": parent, "display_title": parent}


def _content_rows(statement, kind, *, current_revision=None, source_count=None):
    rows = statement.subquery(f"{kind}_rows")
    return select(
        literal(kind).label("read_kind"),
        *(rows.c[name] for name in _HEAD_FIELDS),
        (rows.c.revision if current_revision is None else rows.c[current_revision]).label("current_revision"),
        (literal(None) if source_count is None else rows.c[source_count]).label("source_count"),
    )


def preview(repository, row):
    content = repository.decode_content(row).model_dump(mode="json", by_alias=True)
    head = decode_artifact_governance({**row, "revision": row["current_revision"]})
    ref = {"family": row["family"], "artifact_id": row["artifact_id"], "revision": row["revision"]}
    value = {**content, **ref}
    if row["family"] == "atomic-memory":
        value.update(
            artifact=ref,
            note_key=f"atomic-memory/{ref['artifact_id']}@{ref['revision']}",
            **_atomic_state(head).model_dump(mode="json"),
            current_artifact=head.artifact.model_dump(mode="json"),
            is_current=ref == head.artifact.model_dump(),
        )
    elif row["family"] == "experience":
        value["source_count"] = int(row["source_count"])
    return value


def other_previews(scope):
    head, sources = ARTIFACT_HEADS_TABLE, ARTIFACT_LINEAGE_SOURCES_TABLE
    statements = []
    for family in ("skill", "handoff", "experience"):
        page = (
            select(head)
            .where(
                head.c.scope_id == scope,
                head.c.family == family,
                head.c.merged_into_id.is_(None),
                head.c.lifecycle_state == "active" if family == "skill" else head.c.lifecycle_state != "retired",
            )
            .order_by(head.c.artifact_id)
            .limit(1)
            .subquery(f"{family}_head")
        )
        count = (
            select(func.count())
            .where(*(sources.c[name] == page.c[name] for name in ("scope_id", "family", "artifact_id", "revision")))
            .scalar_subquery()
            if family == "experience"
            else literal(None)
        )
        statement = with_artifact_content(select(page), page).add_columns(count.label("source_count"))
        statements.append(_content_rows(statement, family, source_count="source_count"))
    return statements


async def usage(connection, runtime, ctx):
    header = ctx["page"] == "usage"
    scope = ctx["scope"]
    resolved = _resolve_period(StatisticsPeriod(ctx["period"]), _as_utc(runtime._clock()).date())
    estimator = runtime._statistics(scope)._token_estimator
    statement = daily_usage_statement(
        (scope,),
        resolved.start_date,
        resolved.end_date,
        estimator=None if estimator is None else (estimator.estimator_id, estimator.version),
    )
    rows = (
        (await connection.execute(_with_header(statement, scope, "usage_kind") if header else statement))
        .mappings()
        .all()
    )
    if header:
        set_scope(ctx, next((row for row in rows if row["usage_kind"] == "scope"), None))
        rows = [row for row in rows if row["usage_kind"] != "scope"]
    usage, recall = decode_daily_usage(rows, (scope,))
    return (
        _usage_statistics(resolved, usage[scope]).model_dump(mode="json"),
        _recall_statistics(resolved, estimator, recall[scope]).model_dump(mode="json"),
    )


async def note_rows(connection, runtime, request, ctx, context):
    record_only = ctx["record_only"]
    scope, page = ctx["scope"], ctx["page"]
    query = request.query_params
    selected = ctx.get("note_selection")
    atomic = runtime.atomic_memory._application.for_scope(scope)
    limit = 3 if page == "home" else PAGE_SIZE
    statements = []
    bound = after = None
    if not record_only:
        statement, bound, after = atomic.list_query(
            connection,
            states=(ctx["note_state"],),
            limit=limit,
            cursor=query.get("notes_cursor") if page == "notes" else None,
            context=context,
        )
        statements.append(_content_rows(statement, "notes"))
    if page == "home":
        statements.extend(other_previews(scope))
    if selected is not None:
        revision = selected["revision"]
        head = ARTIFACT_HEADS_TABLE
        exact = select(
            *head.c,
            head.c.revision.label("current_revision"),
        ).where(
            head.c.scope_id == scope, head.c.family == "atomic-memory", head.c.artifact_id == selected["artifact_id"]
        )
        # Preserve the head for governance while selecting the requested immutable body.
        exact_head = exact.subquery("selected_note_head")
        requested = select(
            *(column for column in exact_head.c if column.name != "revision"),
            literal(revision).label("revision"),
        ).subquery("selected_note_revision")
        statements.append(
            _content_rows(
                with_artifact_content(select(requested), requested),
                "selected",
                current_revision="current_revision",
            )
        )
    statement = union_all(*statements) if len(statements) > 1 else statements[0]
    if not record_only:
        statement = _with_header(statement, scope, "read_kind")
    rows = (await connection.execute(statement)).mappings().all()
    if not record_only:
        set_scope(ctx, next((row for row in rows if row["read_kind"] == "scope"), None))
    note_rows = sorted((row for row in rows if row["read_kind"] == "notes"), key=lambda row: row["artifact_id"])
    last = note_rows[min(len(note_rows), limit) - 1]["artifact_id"] if note_rows else after
    cursor = atomic.application.cursor.encode(bound, last) if len(note_rows) > limit else None
    return rows, note_rows[:limit], cursor


async def scope_header(connection, ctx):
    descriptor = (
        (await connection.execute(select(SCOPES_TABLE).where(SCOPES_TABLE.c.scope_id == ctx["scope"])))
        .mappings()
        .one_or_none()
    )
    set_scope(ctx, descriptor)

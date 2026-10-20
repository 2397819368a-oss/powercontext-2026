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

"""SQLite FTS5 projection for approved Experience heads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.artifacts.experience import (
    Experience,
    ExperienceSearchOutcome,
    experience_searchable_text,
)
from powercontext.builtin.artifacts.memory import CapabilityNotSupportedError
from powercontext.builtin.artifacts.search import AdmissionFloor
from powercontext.builtin.artifacts.skill import Skill, SkillPackageSnapshot, SkillSearchHit, skill_searchable_text
from powercontext.builtin.persistence.experience_index import (
    ensure_artifact_head_searchable_text,
    experience_search_hits,
    rebuild_experience_projections,
    rebuild_skill_projections,
    remove_experience_projection,
    replace_experience_projection,
    replace_skill_projection,
    search_approved_artifact_rows,
    skill_search_hits,
)

_CREATE_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS pc_artifact_fts USING fts5(
    scope_id UNINDEXED,
    family UNINDEXED,
    artifact_id UNINDEXED,
    revision UNINDEXED,
    searchable_text,
    tokenize='unicode61'
)
"""
_RECONCILE_DELETE_FTS_SQL = """
DELETE FROM pc_artifact_fts
WHERE NOT EXISTS (
    SELECT 1 FROM pc_artifact_heads AS h
    WHERE h.scope_id = pc_artifact_fts.scope_id AND h.family = pc_artifact_fts.family
      AND h.artifact_id = pc_artifact_fts.artifact_id AND h.revision = pc_artifact_fts.revision
      AND h.family IN ('experience', 'skill') AND h.lifecycle_state = 'active'
      AND h.searchable_text = pc_artifact_fts.searchable_text
)
OR rowid NOT IN (
    SELECT min(rowid) FROM pc_artifact_fts GROUP BY scope_id, family, artifact_id
)
"""
_RECONCILE_INSERT_FTS_SQL = """
INSERT INTO pc_artifact_fts (scope_id, family, artifact_id, revision, searchable_text)
SELECT h.scope_id, h.family, h.artifact_id, h.revision, h.searchable_text
FROM pc_artifact_heads AS h
WHERE h.family IN ('experience', 'skill') AND h.lifecycle_state = 'active'
  AND h.searchable_text IS NOT NULL
EXCEPT
SELECT scope_id, family, artifact_id, revision, searchable_text
FROM pc_artifact_fts
"""
_PROBE_FTS_SQL = "SELECT rowid FROM pc_artifact_fts WHERE pc_artifact_fts MATCH 'powercontext'"
_DELETE_FTS_SQL = text(
    "DELETE FROM pc_artifact_fts WHERE scope_id = :scope_id AND family = :family AND artifact_id = :artifact_id"
)
_INSERT_FTS_SQL = text(
    """
    INSERT INTO pc_artifact_fts (scope_id, family, artifact_id, revision, searchable_text)
    VALUES (:scope_id, :family, :artifact_id, :revision, :searchable_text)
    """
)


class SQLiteExperienceFTSIndex:
    """Maintain and query approved Experience heads with SQLite FTS5."""

    async def initialize(self, connection: AsyncConnection, /) -> None:
        if connection.dialect.name != "sqlite":
            raise CapabilityNotSupportedError("sqlite-experience-fts")
        await ensure_artifact_head_searchable_text(connection)
        await rebuild_experience_projections(connection)
        await rebuild_skill_projections(connection)
        await connection.exec_driver_sql(_CREATE_FTS_SQL)
        await connection.exec_driver_sql(_RECONCILE_DELETE_FTS_SQL)
        await connection.exec_driver_sql(_RECONCILE_INSERT_FTS_SQL)
        await connection.exec_driver_sql(_PROBE_FTS_SQL)

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        experience: Experience,
        /,
    ) -> None:
        await replace_experience_projection(connection, scope_id, experience)
        await connection.execute(
            _DELETE_FTS_SQL,
            {"scope_id": scope_id, "family": Experience.family, "artifact_id": experience.artifact_id},
        )
        await self._insert_row(
            connection,
            {
                "scope_id": scope_id,
                "family": Experience.family,
                "artifact_id": experience.artifact_id,
                "revision": experience.revision,
                "searchable_text": experience_searchable_text(experience.content),
            },
        )

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        query: str,
        limit: int,
        /,
        *,
        admission: AdmissionFloor | None = None,
        min_score: float | None = None,
        require_scores: bool = False,
    ) -> ExperienceSearchOutcome:
        rows = await search_approved_artifact_rows(
            connection, scope_id, Experience.family, query, limit, admission=admission
        )
        return experience_search_hits(
            rows, query, limit, scope_id, admission=admission, min_score=min_score, require_scores=require_scores
        )

    async def remove(self, connection: AsyncConnection, scope_id: str, artifact_id: str, /) -> None:
        await remove_experience_projection(connection, scope_id, artifact_id)
        await connection.execute(
            _DELETE_FTS_SQL,
            {"scope_id": scope_id, "family": Experience.family, "artifact_id": artifact_id},
        )

    async def replace_skill(
        self,
        connection: AsyncConnection,
        scope_id: str,
        skill: Skill,
        package: SkillPackageSnapshot,
        /,
    ) -> None:
        await replace_skill_projection(connection, scope_id, skill, package)
        await connection.execute(
            _DELETE_FTS_SQL,
            {"scope_id": scope_id, "family": Skill.family, "artifact_id": skill.artifact_id},
        )
        await self._insert_row(
            connection,
            {
                "scope_id": scope_id,
                "family": Skill.family,
                "artifact_id": skill.artifact_id,
                "revision": skill.revision,
                "searchable_text": skill_searchable_text(skill.content, package),
            },
        )

    async def search_skills(
        self,
        connection: AsyncConnection,
        scope_id: str,
        query: str,
        limit: int,
        /,
        *,
        admission: AdmissionFloor | None = None,
        min_score: float | None = None,
        require_scores: bool = False,
    ) -> tuple[SkillSearchHit, ...]:
        rows = await search_approved_artifact_rows(
            connection, scope_id, Skill.family, query, limit, admission=admission
        )
        return skill_search_hits(
            rows, query, limit, admission=admission, min_score=min_score, require_scores=require_scores
        )

    @staticmethod
    async def _insert_row(connection: AsyncConnection, row: Mapping[Any, Any]) -> None:
        await connection.execute(
            _INSERT_FTS_SQL,
            {field: row[field] for field in ("scope_id", "family", "artifact_id", "revision", "searchable_text")},
        )


__all__ = ["SQLiteExperienceFTSIndex"]

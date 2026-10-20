# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

"""Publication records: the commit that outlives the directory which named it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from powercontext.builtin.code import CodeConfig, CodeRepositoryConfig, CodeService
from powercontext.builtin.code.cache import GenerationCache
from powercontext.builtin.code.store import SQLiteGraphStore

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="native code indexing requires POSIX file locks and resource limits"
)


class RecordingStore(SQLiteGraphStore):
    """A store that remembers which identifiers were committed and which were recalled.

    Both lists matter: recalling a name only reclaims the rows if they were
    committed under that same name. A test that checks `reclaimed` alone passes
    even when the store ignores the name it was given.
    """

    def __init__(self) -> None:
        self.created: list[str | None] = []
        self.reclaimed: list[str] = []

    def create(
        self,
        directory: Path,
        nodes: list[dict[str, object]],
        edges: list[dict[str, object]],
        deadline: float,
        *,
        generation_id: str | None = None,
    ) -> dict[str, object]:
        self.created.append(generation_id)
        return super().create(directory, nodes, edges, deadline, generation_id=generation_id)

    def remove_generation(self, generation_id: str, binding: str, deadline: float) -> None:
        self.reclaimed.append(generation_id)


class NamedStore(RecordingStore):
    """Behaves like the SeekDB backend where it matters: the manifest carries a name.

    SQLite rows live inside the generation directory, so losing the directory loses
    the rows and there is nothing to protect. The invariant this file is about only
    has consequences when the rows outlive the directory, which is the shape here.
    """

    def create(
        self,
        directory: Path,
        nodes: list[dict[str, object]],
        edges: list[dict[str, object]],
        deadline: float,
        *,
        generation_id: str | None = None,
    ) -> dict[str, object]:
        reference = super().create(directory, nodes, edges, deadline, generation_id=generation_id)
        return {
            "graph": {
                "backend": "named",
                "binding": directory.parent.name,
                "id": generation_id,
                **{key: value for key, value in reference.items() if key == "database_sha256"},
            }
        }


def git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    assert executable
    subprocess.run([executable, "-C", str(root), *arguments], check=True, capture_output=True)


def cache_of(service: CodeService) -> GenerationCache:
    directories = [path for path in (service.config.cache_dir).iterdir() if path.is_dir()]
    assert len(directories) == 1
    return GenerationCache(directories[0], service.store)


@pytest.fixture
def repository(tmp_path, request):
    root = tmp_path / "repository"
    (root / "src").mkdir(parents=True)
    (root / "src/app.py").write_text("def run():\n    return 1\n")
    git(root, "init")
    git(root, "add", ".")
    git(root, "-c", "user.name=Code Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    store = getattr(request, "param", RecordingStore)()
    service = CodeService(
        CodeConfig(
            enabled=True,
            repositories={"scope": CodeRepositoryConfig(root=root)},
            cache_dir=tmp_path / "cache",
        ),
        store=store,
    )
    service.index("scope", full=True)
    return service, root


def test_a_published_generation_is_recorded_under_its_final_directory(repository):
    service, _ = repository
    cache = cache_of(service)

    records = cache.publications()
    pointer = cache.pointer()
    assert pointer is not None
    assert len(records) == 1
    _, record = records.popitem()
    assert record["state"] == "published"
    assert record["directory"] == pointer["current"]["directory"]


def test_reindexing_keeps_a_live_generation_out_of_the_sweep(repository):
    service, root = repository
    (root / "src/app.py").write_text("def run():\n    return 2\n")
    git(root, "add", ".")

    service.index("scope")

    assert service.store.reclaimed == []
    assert [record["state"] for record in cache_of(service).publications().values()] == ["published", "published"]


def test_a_generation_directory_removed_out_of_band_is_reclaimed_by_name(repository):
    service, root = repository
    cache = cache_of(service)
    attempt = next(iter(cache.publications()))
    pointer = cache.pointer()
    assert pointer is not None
    shutil.rmtree(cache.directory / pointer["current"]["directory"])
    (root / "src/app.py").write_text("def run():\n    return 3\n")
    git(root, "add", ".")

    service.index("scope")

    assert service.store.reclaimed == [attempt]
    assert attempt not in cache.publications()


def test_a_pending_attempt_without_its_staging_directory_is_reclaimed(repository):
    service, _ = repository
    cache = cache_of(service)
    attempt = "0" * 32
    journal = json.loads((cache.directory / "publication.json").read_text())
    journal["attempts"][attempt] = {"directory": "staging-nowhere", "state": "pending", "started_at": 0}
    (cache.directory / "publication.json").write_text(json.dumps(journal))

    reclaimed = cache.recover(time.monotonic() + 30)

    assert reclaimed == 1
    assert service.store.reclaimed == [attempt]
    assert attempt not in cache.publications()


@pytest.mark.parametrize("repository", [NamedStore], indirect=True)
def test_the_recorded_name_is_the_identifier_the_store_committed(repository):
    """The name in the record has to be the name the rows were written under.

    Recalling a name the store never used deletes nothing and raises nothing, so
    the leak this change exists to fix would survive every other assertion here.
    """

    service, _ = repository
    cache = cache_of(service)
    attempt = next(iter(cache.publications()))
    pointer = cache.pointer()
    assert pointer is not None
    manifest = json.loads(
        (cache.directory / pointer["current"]["directory"] / "manifest.json").read_text(encoding="utf-8")
    )

    assert service.store.created == [attempt]
    assert manifest["graph"]["id"] == attempt


@pytest.mark.parametrize("repository", [NamedStore], indirect=True)
def test_recovery_cannot_reclaim_rows_a_reachable_generation_still_owns(repository):
    """A record naming a moved-away staging directory must not decide a live commit.

    This is the state a crash between the rename and the journal update used to
    leave behind: `current.json` reaches a generation whose manifest owns the rows,
    while the record still names the staging directory that no longer exists.
    """

    service, _ = repository
    cache = cache_of(service)
    attempt = next(iter(cache.publications()))
    pointer = cache.pointer()
    assert pointer is not None
    journal = json.loads((cache.directory / "publication.json").read_text())
    journal["attempts"][attempt] = {"directory": "staging-renamed-away", "state": "pending", "started_at": 0}
    (cache.directory / "publication.json").write_text(json.dumps(journal))

    reclaimed = cache.recover(time.monotonic() + 30)

    assert reclaimed == 0
    assert service.store.reclaimed == []
    assert attempt in cache.publications()


def test_clear_settles_a_record_whose_directory_went_missing(repository):
    """Clearing used to forget the handle and leave the rows charged to the binding.

    Directory absence is not evidence that collection reclaimed the commit: the
    record is the only thing that still names the rows, so clearing has to settle
    it rather than drop it.
    """

    service, _ = repository
    cache = cache_of(service)
    attempt = next(iter(cache.publications()))
    pointer = cache.pointer()
    assert pointer is not None
    shutil.rmtree(cache.directory / pointer["current"]["directory"])

    service.clear("scope")

    assert service.store.reclaimed == [attempt]
    assert cache.publications() == {}


def test_the_record_window_never_drops_a_generation_that_still_exists(repository):
    """The bound may not discard the only handle to rows that can still be recalled.

    A generation pinned by a reader keeps its directory, so `collect()` cannot
    retire it. If the window evicted its record anyway, the rows would be
    unreachable the moment the directory went away.
    """

    service, _ = repository
    cache = cache_of(service)
    kept = [f"{index:032x}" for index in range(70)]
    abandoned = [f"{index:032x}" for index in range(1000, 1006)]
    for name in kept:
        (cache.directory / ("generation-" + name)).mkdir()
    attempts = {
        **{name: {"directory": "generation-" + name, "state": "published", "started_at": 1} for name in kept},
        **{name: {"directory": "generation-" + name, "state": "published", "started_at": 0} for name in abandoned},
    }
    (cache.directory / "publication.json").write_text(json.dumps({"attempts": attempts}))

    cache.begin_publication(cache.staging(), time.monotonic() + 30)

    records = cache.publications()
    assert sorted(service.store.reclaimed) == sorted(abandoned)
    assert all(name in records for name in kept)


def test_a_commit_can_be_recalled_before_its_tables_exist(tmp_path, monkeypatch):
    """The first build can name its commit and die before initialize() creates tables.

    ``connection()`` converts every MySQL error into ``CodeError`` on exit, so the
    outer handler never saw 1146 and every retry failed during recovery — before
    ``create()`` could create the tables it was waiting for. A missing table means
    there is nothing to recall, not that the recall failed.
    """

    import pymysql

    from powercontext.builtin.code.seekdb import SeekDBGraphStore

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *arguments: object) -> bool:
            return False

        def execute(self, *arguments: object) -> None:
            raise pymysql.err.ProgrammingError(1146, "Table 'test.pc_code_edges' doesn't exist")

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *arguments: object) -> bool:
            return False

        def begin(self) -> None: ...
        def rollback(self) -> None: ...
        def commit(self) -> None: ...
        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(pymysql, "connect", lambda **options: Connection())
    store = SeekDBGraphStore(tmp_path / "seekdb", {})

    store.remove_generation("a" * 32, "binding", time.monotonic() + 30)

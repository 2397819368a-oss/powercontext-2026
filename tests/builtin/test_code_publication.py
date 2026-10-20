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
    """A store that remembers which identifiers were recalled by name."""

    def __init__(self) -> None:
        self.reclaimed: list[str] = []

    def remove_generation(self, generation_id: str, binding: str, deadline: float) -> None:
        self.reclaimed.append(generation_id)


def git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    assert executable
    subprocess.run([executable, "-C", str(root), *arguments], check=True, capture_output=True)


def cache_of(service: CodeService) -> GenerationCache:
    directories = [path for path in (service.config.cache_dir).iterdir() if path.is_dir()]
    assert len(directories) == 1
    return GenerationCache(directories[0], service.store)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    (root / "src").mkdir(parents=True)
    (root / "src/app.py").write_text("def run():\n    return 1\n")
    git(root, "init")
    git(root, "add", ".")
    git(root, "-c", "user.name=Code Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    store = RecordingStore()
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

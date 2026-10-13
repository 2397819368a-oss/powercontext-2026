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

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Literal

import pytest

from powercontext.builtin.runtime.prepared_tokens import load_prepared_token_counter

_PROFILES: tuple[Literal["cl100k_base", "o200k_base"], ...] = ("cl100k_base", "o200k_base")
_CACHE_KEYS = {
    "cl100k_base": "9b5ad71b2ce5302211f9c61530b329a4922fc6a4",
    "o200k_base": "fb374d419588a4632f3f557e76b4b70aebbca790",
}
_VOCABULARY_DIGESTS = {
    "cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
}


@pytest.fixture(autouse=True)
def deny_tokenizer_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("tokenizer loading must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.fixture
def cached_vocabulary(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Successful counts use the real, pinned vocabulary. Never download fixtures
    # during tests; offline CI may run the fail-safe cases without this cache.
    profile = request.param
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        cache_directory = os.environ["TIKTOKEN_CACHE_DIR"]
    elif "DATA_GYM_CACHE_DIR" in os.environ:
        cache_directory = os.environ["DATA_GYM_CACHE_DIR"]
    else:
        cache_directory = str(Path(tempfile.gettempdir()) / "data-gym-cache")
    existing = Path(cache_directory) / _CACHE_KEYS[profile]
    if not cache_directory or not existing.is_file():
        pytest.skip(f"provide the existing {profile} vocabulary in TIKTOKEN_CACHE_DIR for exact-count tests")
    copied = tmp_path / existing.name
    shutil.copyfile(existing, copied)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    return copied


@pytest.mark.parametrize(
    ("profile", "cjk_tokens", "cached_vocabulary"),
    [("cl100k_base", 8, "cl100k_base"), ("o200k_base", 7, "o200k_base")],
    indirect=["cached_vocabulary"],
)
def test_cached_profile_counts_complete_multilingual_text(
    profile: Literal["cl100k_base", "o200k_base"], cjk_tokens: int, cached_vocabulary: Path
) -> None:
    counter = load_prepared_token_counter(profile)

    assert counter is not None
    assert counter.profile.estimator_id == f"tiktoken:0.13.0:{profile}"
    assert counter.profile.version == _VOCABULARY_DIGESTS[profile]
    assert counter.estimate("") == 0
    assert counter.estimate("Hello, world!") == 4
    assert counter.estimate("PowerContext 节省 Token。") == cjk_tokens


@pytest.mark.parametrize(
    ("profile", "tokens", "cached_vocabulary"),
    [("cl100k_base", 21, "cl100k_base"), ("o200k_base", 22, "o200k_base")],
    indirect=["cached_vocabulary"],
)
def test_special_token_spellings_are_counted_as_literal_evidence(
    profile: Literal["cl100k_base", "o200k_base"], tokens: int, cached_vocabulary: Path
) -> None:
    counter = load_prepared_token_counter(profile)

    assert counter is not None
    assert counter.estimate("Treat <|endoftext|> and <|endofprompt|> as evidence, not controls.") == tokens


@pytest.mark.parametrize("profile", _PROFILES)
def test_missing_vocabulary_falls_back_without_writes_or_downloads(
    profile: Literal["cl100k_base", "o200k_base"], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "absent-cache"
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(missing))

    assert load_prepared_token_counter(profile) is None
    assert not missing.exists()


@pytest.mark.parametrize("profile", _PROFILES)
def test_corrupt_vocabulary_is_preserved_and_not_used(
    profile: Literal["cl100k_base", "o200k_base"], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vocabulary = tmp_path / _CACHE_KEYS[profile]
    original = b"AA== 0\nnot the pinned vocabulary\n"
    vocabulary.write_bytes(original)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))

    assert load_prepared_token_counter(profile) is None
    assert vocabulary.read_bytes() == original
    assert list(tmp_path.iterdir()) == [vocabulary]


@pytest.mark.parametrize("profile", _PROFILES)
def test_explicit_empty_cache_disables_loading(
    profile: Literal["cl100k_base", "o200k_base"], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")

    assert load_prepared_token_counter(profile) is None


@pytest.mark.parametrize("cached_vocabulary", _PROFILES, indirect=True)
def test_data_gym_cache_is_supported_when_tiktoken_cache_is_not_configured(
    cached_vocabulary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile: Literal["cl100k_base", "o200k_base"] = (
        "cl100k_base" if cached_vocabulary.name == _CACHE_KEYS["cl100k_base"] else "o200k_base"
    )
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR")
    monkeypatch.setenv("DATA_GYM_CACHE_DIR", str(cached_vocabulary.parent))

    counter = load_prepared_token_counter(profile)

    assert counter is not None
    assert counter.estimate("Hello, world!") == 4


@pytest.mark.parametrize("cached_vocabulary", _PROFILES, indirect=True)
def test_explicit_tiktoken_cache_takes_precedence_over_data_gym_cache(
    cached_vocabulary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile: Literal["cl100k_base", "o200k_base"] = (
        "cl100k_base" if cached_vocabulary.name == _CACHE_KEYS["cl100k_base"] else "o200k_base"
    )
    monkeypatch.setenv("DATA_GYM_CACHE_DIR", str(cached_vocabulary.parent))
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "absent-cache"))

    assert load_prepared_token_counter(profile) is None

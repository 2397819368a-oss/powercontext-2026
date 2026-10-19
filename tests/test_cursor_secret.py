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
import stat

import pytest
from pydantic import SecretStr

from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.server.cursor_secret import resolve_cursor_secret


@pytest.fixture
def generated_secret(monkeypatch) -> bytes:
    # Include LF and other control bytes so Windows text translation cannot hide
    # behind a randomly generated key that happens not to contain a newline.
    secret = bytes(range(32))
    monkeypatch.setattr("powercontext.server.cursor_secret.secrets.token_bytes", lambda size: secret[:size])
    return secret


def test_file_backed_sqlite_reuses_private_cursor_secret(tmp_path, generated_secret) -> None:
    database_path = tmp_path / "powercontext.db"
    config = SQLiteConfig(url=f"sqlite+aiosqlite:///{database_path}")

    first = resolve_cursor_secret(config, None)
    second = resolve_cursor_secret(config, None)

    key_path = tmp_path / ".powercontext.db.cursor-key"
    assert first == second == generated_secret
    assert key_path.read_bytes() == generated_secret
    if os.name != "nt":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_explicit_cursor_secret_overrides_database_storage(tmp_path) -> None:
    database_path = tmp_path / "powercontext.db"
    config = SQLiteConfig(url=f"sqlite+aiosqlite:///{database_path}")

    secret = resolve_cursor_secret(config, "configured-secret-with-at-least-32-bytes")

    assert secret == b"configured-secret-with-at-least-32-bytes"
    assert not (tmp_path / ".powercontext.db.cursor-key").exists()


def test_remote_database_without_override_reuses_local_persisted_secret(
    tmp_path, monkeypatch, generated_secret
) -> None:
    monkeypatch.setenv("POWERCONTEXT_HOME", str(tmp_path))
    config = OceanBaseConfig(url=SecretStr("mysql+aoceanbase://user:password@127.0.0.1:2881/test?charset=utf8mb4"))

    first = resolve_cursor_secret(config, None)
    second = resolve_cursor_secret(config, None)

    key_path = tmp_path / "cursor-signing.key"
    assert first == second == generated_secret
    assert key_path.read_bytes() == generated_secret
    if os.name != "nt":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_transient_short_key_write_completes_the_key(tmp_path, monkeypatch, generated_secret) -> None:
    database_path = tmp_path / "powercontext.db"
    config = SQLiteConfig(url=f"sqlite+aiosqlite:///{database_path}")
    key_path = tmp_path / ".powercontext.db.cursor-key"
    real_write = os.write

    # POSIX allows write() to accept fewer bytes than requested. Accept five
    # bytes once, then accept the rest, the way a resumed write would.
    seen_short_write = {"done": False}

    def limited_write(descriptor: int, data: bytes) -> int:
        if seen_short_write["done"]:
            return real_write(descriptor, bytes(data))
        seen_short_write["done"] = True
        return real_write(descriptor, bytes(data)[:5])

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", limited_write)
        secret = resolve_cursor_secret(config, None)

    assert secret == generated_secret
    assert key_path.read_bytes() == generated_secret
    assert resolve_cursor_secret(config, None) == generated_secret


def test_failed_key_write_leaves_no_unreadable_key_behind(tmp_path, monkeypatch, generated_secret) -> None:
    database_path = tmp_path / "powercontext.db"
    config = SQLiteConfig(url=f"sqlite+aiosqlite:///{database_path}")
    key_path = tmp_path / ".powercontext.db.cursor-key"
    real_write = os.write
    retries = {"count": 0}

    def stalled_write(descriptor: int, data: bytes) -> int:
        retries["count"] += 1
        if retries["count"] > 64:
            raise AssertionError("a stalled write was retried instead of rejected")  # noqa: TRY003
        real_write(descriptor, bytes(data)[:3])
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", stalled_write)
        with pytest.raises(OSError):
            resolve_cursor_secret(config, None)

    # A part-written key fails every later start with ValueError, so a failed
    # attempt must not publish one.
    assert not key_path.exists()
    assert resolve_cursor_secret(config, None) == generated_secret


def test_key_write_without_progress_fails_instead_of_retrying(tmp_path, monkeypatch, generated_secret) -> None:
    database_path = tmp_path / "powercontext.db"
    config = SQLiteConfig(url=f"sqlite+aiosqlite:///{database_path}")
    real_write = os.write
    calls = {"count": 0}

    # Accept nothing on the first attempt. Treating a zero-byte write as
    # progress would loop forever, and a later write that does succeed must not
    # hide that the first one stalled.
    def stalled_write(descriptor: int, data: bytes) -> int:
        calls["count"] += 1
        if calls["count"] > 64:
            raise AssertionError("a zero-byte write was retried instead of rejected")  # noqa: TRY003
        if calls["count"] == 1:
            return 0
        return real_write(descriptor, bytes(data))

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", stalled_write)
        with pytest.raises(OSError):
            resolve_cursor_secret(config, None)

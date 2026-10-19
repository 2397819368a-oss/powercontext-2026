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

"""Resolve a stable signing secret for stateless REST cursors."""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

from sqlalchemy.engine import make_url

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime.config import DatabaseConfig
from powercontext.paths import powercontext_data_dir

_CURSOR_SECRET_BYTES = 32
_KEY_CREATE_ATTEMPTS = 3


def resolve_cursor_secret(database: DatabaseConfig, configured_secret: str | None, /) -> bytes | None:
    """Use an explicit secret, or persist one in the local data location."""

    if configured_secret is not None:
        return configured_secret.encode()
    if isinstance(database, SQLiteConfig):
        if not database.is_persistent:
            return None
        database_name = make_url(database.url).database
        if database_name:
            database_path = Path(database_name).expanduser().resolve(strict=False)
            return _load_or_create(database_path.with_name(f".{database_path.name}.cursor-key"))
    return _load_or_create(powercontext_data_dir() / "cursor-signing.key")


def _load_or_create(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(_KEY_CREATE_ATTEMPTS):
        secret = _read(path)
        if secret is not None:
            return secret

        # A key of the wrong length can only come from a creation that stopped
        # before the writer returned it, so no cursor was signed with it: drop
        # it and start over. Otherwise a process killed between os.open and
        # os.write leaves the data location unstartable forever.
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

        generated = secrets.token_bytes(_CURSOR_SECRET_BYTES)
        try:
            # Windows text descriptors translate LF bytes even when using os.write.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            # Another starter won the race; use the key it publishes.
            continue
        completed = False
        try:
            _write_key(descriptor, generated, path)
            os.fsync(descriptor)
            completed = True
        finally:
            os.close(descriptor)
            if not completed:
                # Do not leave a half-written key behind: the next start would
                # have to discard it before it could create a usable one.
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
        return generated

    raise OSError(f"failed to create cursor signing key: {path}")  # noqa: TRY003


def _write_key(descriptor: int, secret: bytes, path: Path) -> None:
    """Write every byte: os.write may accept fewer bytes than requested."""

    remaining = memoryview(secret)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError(f"failed to write complete cursor signing key: {path}")  # noqa: TRY003
        remaining = remaining[written:]


def _read(path: Path) -> bytes | None:
    """Return the stored key, or None when there is no complete key on disk."""

    try:
        secret = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(secret) != _CURSOR_SECRET_BYTES:
        return None
    return secret

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

"""Chunked, digest-checked context packs and prefix-reusing deltas.

A :class:`ContextPack` is the complete context of one turn split into ordered
byte chunks, together with a digest that identifies the chunk sequence. A
:class:`ContextPackDelta` reuses a prefix of the pack the receiver already
holds and appends the chunks that changed, so consecutive turns stop
retransmitting bytes both sides already have.

Three details carry the wire contract:

* ``byte_count`` counts the serialized pack **including its own field**, so the
  value depends on its own width. It is resolved by iterating to a fixed point.
* Chunk boundaries fall on UTF-8 character boundaries while filling the byte
  budget greedily from the front. Greedy-from-the-front is what makes the
  prefix of two consecutive turns identical chunk for chunk -- any adaptive
  scheme would shift every boundary after the edit and destroy the reuse.
* Decoding is strict: an unknown or missing field, a non-canonical digest, a
  boolean ``prefix_items`` or an out-of-range prefix is an error rather than a
  quietly smaller pack.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"

#: Default chunk ceiling. A 512-byte chunk keeps the delta granularity fine
#: enough that a single edited turn costs one or two chunks, while the
#: per-chunk JSON and Base64 overhead stays a small fraction of the payload.
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})


class ContextPackError(ValueError):
    """A Context Pack or delta violated the wire protocol or chunk contract."""


def _canonical(value: Any) -> bytes:
    """Return the one encoding both sides must agree on: sorted keys, no spaces."""

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: Iterable[bytes]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _digest_of(items: tuple[bytes, ...]) -> str:
    """Hash ``schema`` plus ``items`` only; the digest cannot cover itself."""

    body = _canonical({"schema": PACK_SCHEMA, "items": _encode_items(items)})
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _require_int(value: Any, field_name: str) -> int:
    """Accept a real ``int`` only.

    ``isinstance(True, int)`` is true, and ``json`` hands back floats for
    ``1.0``, so ``type(...) is int`` is the only check that rejects both.
    """

    if type(value) is not int:
        raise ContextPackError(f"{field_name} must be an integer")
    return value


def _require_bytes_tuple(value: Iterable[Any]) -> tuple[bytes, ...]:
    items = tuple(value)
    for item in items:
        if not isinstance(item, bytes):
            raise ContextPackError("context pack items must be bytes")
    return items


@dataclass(frozen=True, slots=True)
class ContextPack:
    """The complete context of one turn as an ordered tuple of byte chunks."""

    items: tuple[bytes, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", _require_bytes_tuple(self.items))

    # -- construction ----------------------------------------------------
    @classmethod
    def build(cls, items: Iterable[bytes]) -> ContextPack:
        """Build a pack from chunks that already exist."""

        return cls(tuple(items))

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Split ``content`` into deterministic chunks of at most ``chunk_bytes``."""

        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int):
            raise ContextPackError("chunk_bytes must be a positive integer")
        if chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if not isinstance(content, str):
            raise ContextPackError("content must be a string")
        return cls(_chunk_content(content, chunk_bytes))

    # -- derived values --------------------------------------------------
    @property
    def digest(self) -> str:
        return _digest_of(self.items)

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    # -- wire ------------------------------------------------------------
    def to_bytes(self) -> bytes:
        """Serialize to the canonical payload, resolving the self-counting field."""

        body: dict[str, Any] = {
            "schema": PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
        }
        # ``byte_count`` counts the payload that contains it, so its own width
        # feeds back into the total. One extra digit is the worst case, which
        # the third attempt always settles.
        count = 0
        for _ in range(4):
            payload = {**body, "byte_count": count}
            data = _canonical(payload)
            if len(data) == count:
                return data
            count = len(data)
        raise ContextPackError("pack byte count did not converge")

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode a payload and verify its schema, digest and byte count."""

        payload = _load_object(data)
        if set(payload) != _PACK_FIELDS:
            missing = sorted(_PACK_FIELDS - set(payload))
            extra = sorted(set(payload) - _PACK_FIELDS)
            raise ContextPackError(f"pack fields mismatch: missing={missing} extra={extra}")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError(f"pack schema must be {PACK_SCHEMA}")
        items = _decode_items(payload["items"])
        pack = cls(items)
        if payload["digest"] != pack.digest:
            raise ContextPackError("pack digest does not match its items")
        declared = _require_int(payload["byte_count"], "byte_count")
        if declared != pack.byte_count:
            raise ContextPackError("pack byte_count does not match its payload")
        return pack


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A reused prefix of ``base`` plus the chunks ``target`` appends to it."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.base_digest, str) or not isinstance(self.target_digest, str):
            raise ContextPackError("delta digests must be strings")
        prefix = _require_int(self.prefix_items, "prefix_items")
        if prefix < 0:
            raise ContextPackError("prefix_items must not be negative")
        object.__setattr__(self, "appended", _require_bytes_tuple(self.appended))

    # -- construction ----------------------------------------------------
    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Reuse the longest shared prefix of ``base`` and ``target``.

        Any valid prefix decodes correctly, so the longest one is simply the
        cheapest: it minimizes the chunks that have to travel.
        """

        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta requires two context packs")
        prefix = 0
        limit = min(len(base.items), len(target.items))
        while prefix < limit and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    # -- derived values --------------------------------------------------
    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    # -- wire ------------------------------------------------------------
    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _load_object(data)
        if set(payload) != _DELTA_FIELDS:
            missing = sorted(_DELTA_FIELDS - set(payload))
            extra = sorted(set(payload) - _DELTA_FIELDS)
            raise ContextPackError(f"delta fields mismatch: missing={missing} extra={extra}")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError(f"delta schema must be {DELTA_SCHEMA}")
        appended = _decode_items(payload["appended"])
        return cls(
            payload["base_digest"],
            payload["target_digest"],
            payload["prefix_items"],
            appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from a base pack and a delta.

    This is the receiver's whole job: nothing but the two serialized objects is
    available, so every guarantee has to be re-established here -- the base is
    the one the delta was built against, the prefix stays inside that base, and
    the rebuilt chunks hash to the target the sender announced.
    """

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("apply requires a context pack and a delta")
    if base.digest != delta.base_digest:
        raise ContextPackError("delta was built against a different base pack")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("prefix_items exceeds the base pack")
    rebuilt = ContextPack(base.items[: delta.prefix_items] + delta.appended)
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("rebuilt pack digest does not match the target digest")
    return rebuilt


def _chunk_content(content: str, chunk_bytes: int) -> tuple[bytes, ...]:
    """Fill chunks greedily from the front, cutting only between characters.

    Greedy front-filling is deterministic and, more importantly, positional: an
    edit late in the text leaves every earlier chunk -- and therefore every
    earlier boundary -- untouched.
    """

    if not content:
        return ()
    items: list[bytes] = []
    buffer = bytearray()
    for character in content:
        encoded = character.encode("utf-8")
        if len(encoded) > chunk_bytes:
            raise ContextPackError("a single character exceeds the chunk byte limit")
        if len(buffer) + len(encoded) > chunk_bytes:
            items.append(bytes(buffer))
            buffer.clear()
        buffer.extend(encoded)
    if buffer:
        items.append(bytes(buffer))
    return tuple(items)


def _load_object(data: bytes) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)):
        raise ContextPackError("payload must be bytes")
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError("payload is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ContextPackError("payload must be a JSON object")
    return payload


def _decode_items(value: Any) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("items must be an array of Base64 strings")
    items: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError("items must be an array of Base64 strings")
        try:
            items.append(base64.b64decode(entry.encode("ascii"), validate=True))
        except (UnicodeEncodeError, binascii.Error) as error:
            raise ContextPackError("items must be valid Base64") from error
    return tuple(items)


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]

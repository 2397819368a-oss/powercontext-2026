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

"""Cross-call embedding coalescing with a bounded successful-vector cache.

Two mechanisms share one wrapper:

* **Coalescing** — identical texts submitted by concurrent callers, or arriving
  inside one short window, are resolved by a single underlying request. A caller
  that arrives while a request is still in flight joins it instead of paying for
  a second one.
* **Caching** — vectors of successful batches are kept in a bounded LRU so later
  calls are answered without touching the provider. Failures are never cached,
  and a wrapper with ``cache_size=0`` still coalesces in-flight work.

Cancellation is isolated per caller: each caller waits on the shared future
through :func:`asyncio.wait`, which detaches the waiter without cancelling the
work the remaining callers still need.

Usage is attributed per *underlying batch*, not per text. One caller that waits
for a batch holding six texts counts one request, and every waiter of the same
batch counts that same single request once.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
    from powercontext.builtin.inference.protocols import EmbeddingModel

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024


@dataclass(slots=True)
class _Batch:
    """One underlying provider request and the slots it answers.

    ``slots`` grows while the request is in flight: a caller that arrives after
    the window closed and asks for a text this batch already carries joins it
    instead of paying for a second request.
    """

    texts: tuple[str, ...]
    slots: list[_Slot]
    usage: InferenceUsage = field(default_factory=lambda: InferenceUsage(requests=0))


@dataclass(slots=True)
class _Slot:
    """One distinct text waiting for the batch that will resolve it."""

    text: str
    future: asyncio.Future[EmbeddingVector]
    batch: _Batch | None = None


class CoalescingEmbeddingModel:
    """Share concurrent embedding work and cache successful vectors.

    ``max_batch_size`` bounds the distinct texts of a single provider request,
    ``coalesce_ms`` is the millisecond window in which late callers may join,
    and ``cache_size`` is the LRU capacity (``0`` disables cross-call caching
    while keeping in-flight sharing).
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self._model = model
        self.profile: EmbeddingProfile = model.profile
        self._max_batch_size = _positive_int(max_batch_size, "max_batch_size")
        self._window = _non_negative_seconds(coalesce_ms)
        self._cache_size = _non_negative_int(cache_size, "cache_size")
        self._cache: OrderedDict[str, EmbeddingVector] | None = OrderedDict() if self._cache_size else None
        self._pending: dict[str, _Slot] = {}
        self._inflight: dict[str, _Batch] = {}
        self._driver: asyncio.Task[None] | None = None

    async def embed(self, texts: Sequence[str], /) -> EmbeddingResult:
        """Return one ordered vector per input text, preserving repeats."""

        inputs = tuple(texts)
        if not inputs:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        resolved: dict[str, EmbeddingVector] = {}
        slots: dict[str, _Slot] = {}
        for text in _distinct(inputs):
            cached = self._cache_lookup(text)
            if cached is not None:
                resolved[text] = cached
                continue
            slot = self._pending.get(text)
            if slot is None:
                slot = self._attach(text)
            slots[text] = slot

        if slots:
            await self._await_slots(slots.values())

        failure: BaseException | None = None
        for text, slot in slots.items():
            try:
                resolved[text] = slot.future.result()
            except BaseException as exc:  # noqa: BLE001 - re-raised after every waiter is drained
                failure = failure if failure is not None else exc
        if failure is not None:
            raise failure

        return EmbeddingResult(
            vectors=tuple(resolved[text] for text in inputs),
            usage=_total_usage(slots.values()),
        )

    # -- coalescing ---------------------------------------------------------

    def _attach(self, text: str) -> _Slot:
        """Return a slot answering ``text``, joining queued or in-flight work."""

        slot = _Slot(text=text, future=_new_future())
        _consume_exception(slot.future)
        batch = self._inflight.get(text)
        if batch is not None:
            # The request is already on the wire but it carries this text, so
            # the late caller can still share the answer.
            slot.batch = batch
            batch.slots.append(slot)
            return slot
        self._pending[text] = slot
        self._ensure_driver()
        return slot

    def _ensure_driver(self) -> None:
        if self._driver is None:
            self._driver = asyncio.get_running_loop().create_task(self._drive())

    async def _drive(self) -> None:
        """Drain the pending slots in window-sized rounds until the queue is empty."""

        try:
            while self._pending:
                await self._settle_window()
                if not self._pending:
                    break
                entries = list(self._pending.values())
                self._pending.clear()
                for batch in self._batches(entries):
                    self._publish(batch)
                    try:
                        await self._run_batch(batch)
                    finally:
                        for slot in batch.slots:
                            self._inflight.pop(slot.text, None)
        finally:
            self._driver = None
            # A cancelled driver can leave queued callers with nobody to answer
            # them; fail them instead of hanging forever.
            if self._pending:
                stranded = tuple(self._pending.values())
                self._pending.clear()
                for slot in stranded:
                    if not slot.future.done():
                        slot.future.set_exception(RuntimeError("embedding coalescer stopped"))

    def _publish(self, batch: _Batch) -> None:
        for text in batch.texts:
            self._inflight[text] = batch

    async def _settle_window(self) -> None:
        """Yield the loop so callers already scheduled can join this round."""

        if self._window > 0:
            await asyncio.sleep(self._window)
        else:
            # A zero window still has to yield: a caller that entered ``embed``
            # in the same loop turn must get the chance to register its slot.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    def _batches(self, entries: Sequence[_Slot]) -> Iterator[_Batch]:
        for start in range(0, len(entries), self._max_batch_size):
            chunk = tuple(entries[start : start + self._max_batch_size])
            batch = _Batch(texts=tuple(slot.text for slot in chunk), slots=list(chunk))
            for slot in batch.slots:
                slot.batch = batch
            yield batch

    async def _run_batch(self, batch: _Batch) -> None:
        try:
            result = await self._model.embed(batch.texts)
            if len(result.vectors) != len(batch.texts):
                raise ValueError("embedding model returned a mismatched vector count")  # noqa: TRY003
        except BaseException as exc:  # noqa: BLE001 - every waiter sees the same failure
            for slot in batch.slots:
                if not slot.future.done():
                    slot.future.set_exception(exc)
            if not isinstance(exc, Exception):
                raise
            return
        batch.usage = result.usage
        positions = {text: index for index, text in enumerate(batch.texts)}
        for slot in batch.slots:
            if not slot.future.done():
                slot.future.set_result(result.vectors[positions[slot.text]])
        self._cache_store(batch.texts, result.vectors)

    async def _await_slots(self, slots: Iterable[_Slot]) -> None:
        """Wait without cancelling the shared work when this caller is cancelled."""

        await asyncio.wait([slot.future for slot in slots])

    # -- cache --------------------------------------------------------------

    def _cache_lookup(self, text: str) -> EmbeddingVector | None:
        if self._cache is None:
            return None
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    def _cache_store(self, texts: Sequence[str], vectors: Sequence[EmbeddingVector]) -> None:
        if self._cache is None:
            return
        for text, vector in zip(texts, vectors, strict=True):
            self._cache[text] = vector
            self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _distinct(texts: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(texts))


def _new_future() -> asyncio.Future[EmbeddingVector]:
    return asyncio.get_running_loop().create_future()


def _consume_exception(future: asyncio.Future[EmbeddingVector]) -> None:
    """Mark a shared failure as retrieved so abandoned waiters stay quiet."""

    def _drain(done: asyncio.Future[EmbeddingVector]) -> None:
        if not done.cancelled():
            done.exception()

    future.add_done_callback(_drain)


def _total_usage(slots: Iterable[_Slot]) -> InferenceUsage:
    """Sum the provider usage of every distinct batch these slots waited for."""

    batches: dict[int, InferenceUsage] = {}
    for slot in slots:
        if slot.batch is not None:
            batches[id(slot.batch)] = slot.batch.usage
    if not batches:
        return InferenceUsage(requests=0)
    usages = tuple(batches.values())
    return InferenceUsage(
        requests=sum(usage.requests for usage in usages),
        input_tokens=_sum_optional(usage.input_tokens for usage in usages),
        output_tokens=_sum_optional(usage.output_tokens for usage in usages),
    )


def _sum_optional(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")  # noqa: TRY003
    return value


def _non_negative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")  # noqa: TRY003
    return value


def _non_negative_seconds(value: object, name: str = "coalesce_ms") -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")  # noqa: TRY003
    return value / 1000.0


__all__ = ["CoalescingEmbeddingModel"]

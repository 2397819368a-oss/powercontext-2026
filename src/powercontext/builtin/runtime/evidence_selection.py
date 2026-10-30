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

"""Pick literal Memory spans that answer one query inside a byte budget.

The selection is purely lexical and offline: no model call, no annotation read, no
per-question lookup. A span is always an exact slice of the caller's own text, so the
rendered item can be checked against the source entry by character position.

Three properties drive the algorithm:

* **Span boundaries tile the text.** ``text_spans`` returns a partition of the entry, so
  two spans are adjacent exactly when their indices are adjacent. Merging a run of
  selected spans back into one slice therefore needs no gap arithmetic, and a merged
  slice is still an exact slice of the original text.
* **The cost of a slice is measured, never estimated.** A candidate is accepted only
  after rendering the whole envelope and counting UTF-8 bytes, because the citation,
  the JSON separators and the ``start``/``end`` numbers all compete with the prose for
  the same budget. An estimate would either overflow the caller's budget or leave bytes
  unused that another span could have earned. One citation costs roughly as much as a
  whole paragraph, so a second slice in a *new* run is a real trade against a slice in
  the run already open — which is why bridging a one- or two-span gap beats opening a
  second citation whenever the two runs are close.
* **Position and the opening span carry a prior, not a verdict.** A document's first
  span is its title in the usual capture shape: it shares the query's vocabulary almost
  by construction, and it is very rarely the passage that answers the question, so it is
  weighted down rather than excluded. Evidence also tends to appear after the setup, so
  the weight rises with position. Both are document-structure heuristics; neither reads
  a label, and neither can decide for a span that the words disagree with.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"

_TOKEN = re.compile(r"[a-z]+|\d+")

# A leading span is the title in the ordinary "title + body" capture shape. It echoes the
# query's vocabulary by construction while rarely holding the answering passage, so it
# competes on a discount instead of being dropped outright.
_OPENING_WEIGHT = 0.25
# Evidence sits after the setup far more often than before it; the factor runs from
# 1 - 0.6 to 1 + 0.6 across the document.
_POSITION_WEIGHT = 1.2
# A gap of one or two spans is cheap to close: it costs prose, not a second citation.
_BRIDGE_GAP = 2

# Function words carry no evidence. They are dropped before scoring so that a query and a
# distractor cannot look related through "of the" alone.
_STOPWORDS = frozenset(
    """a an the and or but if then than that this these those of in on at to for from by with as is are was were
    be been being it its their there here we our you your they them he she his her not no do does did have has had
    can could may might must shall should will would about into over under between among during before after above
    below out up down off again further once all any both each few more most other some such only own same so too
    very just now also however therefore thus""".split()
)


@dataclass(frozen=True)
class EvidenceItem:
    """One selected literal slice of one Memory entry."""

    document_index: int
    citation: dict[str, object]
    start: int
    end: int
    text: str


def text_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Split ``text`` into a partition of adjacent sentence-sized spans.

    Every span starts where the previous one ended, so the returned pairs tile
    ``[0, len(text))`` without gaps. Trailing whitespace belongs to the span it follows,
    which keeps a sentence that ends the entry from losing its final characters.
    """

    if not text:
        return ()
    length = len(text)
    starts = [0]
    index = 0
    while index < length:
        if text[index] not in ".!?":
            index += 1
            continue
        end = index + 1
        while end < length and text[end] in ".!?)]\"':":
            end += 1
        if end < length and not text[end].isspace():
            index = end
            continue
        while end < length and text[end].isspace():
            end += 1
        if 0 < end < length and end > starts[-1]:
            starts.append(end)
        index = end
    bounds = (*starts, length)
    return tuple((bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1) if bounds[i] < bounds[i + 1])


def render_items(items: Sequence[EvidenceItem]) -> str:
    """Render selected slices with the standard trust policy and envelope."""

    envelope = {
        "trust": "untrusted_history",
        "items": [
            {"citation": item.citation, "content": item.text, "start": item.start, "end": item.end} for item in items
        ],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def select(
    query: str,
    documents: Sequence[tuple[str, dict[str, object]]],
    max_bytes: int,
) -> tuple[EvidenceItem, ...]:
    """Return the spans of ``documents`` worth spending ``max_bytes`` on.

    ``documents`` pairs each entry's literal text with the citation to render beside the
    slice. Every returned item is an exact ``text[start:end]`` slice of its own document.
    """

    if not documents:
        return ()
    spans = [text_spans(text) for text, _ in documents]
    terms = [[_terms(text[start:end]) for start, end in document] for (text, _), document in zip(documents, spans, strict=True)]

    weights = _idf(terms)
    wanted = tuple(dict.fromkeys(_terms(query)))

    def score(span_terms: tuple[str, ...], position: float) -> float:
        if not wanted:
            return 0.0
        present = set(span_terms)
        matched = [word for word in wanted if word in present]
        if not matched:
            return 0.0
        mass = sum(weights.get(word, 1.0) for word in matched)
        coverage = len(matched) / len(wanted)
        # A span touching one rare word is weaker evidence than a span covering most of the
        # query, so the mass is scaled by how much of the question it actually accounts for.
        return mass * (0.5 + coverage) * position

    scores: dict[tuple[int, int], float] = {}
    for document_index, document in enumerate(terms):
        count = len(document)
        for span_index, span_terms in enumerate(document):
            prior = 1.0 + _POSITION_WEIGHT * (span_index / (count - 1) - 0.5) if count > 1 else 1.0
            if span_index == 0:
                prior *= _OPENING_WEIGHT
            scores[(document_index, span_index)] = score(span_terms, prior)

    ranked = sorted(scores, key=lambda entry: (-scores[entry], entry[0], entry[1]))

    def fits(candidate: dict[int, set[int]]) -> bool:
        return len(render_items(_items(documents, spans, candidate)).encode("utf-8")) <= max_bytes

    chosen: dict[int, set[int]] = {}
    for relevance, document_index, span_index in ((scores[entry], entry[0], entry[1]) for entry in ranked):
        if relevance <= 0 and chosen:
            break
        bucket = chosen.setdefault(document_index, set())
        bucket.add(span_index)
        if not fits(chosen):
            bucket.discard(span_index)
            if not bucket:
                del chosen[document_index]

    _bridge(spans, chosen, fits)
    _grow(spans, chosen, scores, fits)

    if not chosen:
        return _fallback(documents, spans, max_bytes)
    return tuple(_items(documents, spans, chosen))


def _bridge(
    spans: Sequence[Sequence[tuple[int, int]]],
    chosen: dict[int, set[int]],
    fits: Callable[[dict[int, set[int]]], bool],
) -> None:
    """Close a short gap between two runs, folding them into a single citation.

    A citation costs about as much as a paragraph, so paying prose to join two nearby runs
    buys back a whole citation — and with it the budget for another slice elsewhere.
    """

    for gap in range(1, _BRIDGE_GAP + 1):
        changed = True
        while changed:
            changed = False
            for document_index in list(chosen):
                runs = _runs(sorted(chosen[document_index]))
                for left, right in zip(runs, runs[1:]):
                    if right[0] - left[1] - 1 != gap:
                        continue
                    trial = dict(chosen)
                    trial[document_index] = chosen[document_index] | set(range(left[1] + 1, right[0]))
                    if fits(trial):
                        chosen[document_index] = trial[document_index]
                        changed = True
                        break
                if changed:
                    break


def _grow(
    spans: Sequence[Sequence[tuple[int, int]]],
    chosen: dict[int, set[int]],
    scores: dict[tuple[int, int], float],
    fits: Callable[[dict[int, set[int]]], bool],
) -> None:
    """Spend whatever the budget still allows on the best neighbour of a chosen run."""

    changed = True
    while changed:
        changed = False
        edges = [
            (scores[(document_index, neighbour)], document_index, neighbour)
            for document_index in chosen
            for span_index in chosen[document_index]
            for neighbour in (span_index - 1, span_index + 1)
            if 0 <= neighbour < len(spans[document_index]) and neighbour not in chosen[document_index]
        ]
        edges.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
        for relevance, document_index, neighbour in edges:
            if relevance <= 0:
                break
            trial = dict(chosen)
            trial[document_index] = chosen[document_index] | {neighbour}
            if fits(trial):
                chosen[document_index] = trial[document_index]
                changed = True
                break


def _items(
    documents: Sequence[tuple[str, dict[str, object]]],
    spans: Sequence[Sequence[tuple[int, int]]],
    chosen: dict[int, set[int]],
) -> list[EvidenceItem]:
    """Merge adjacent selected spans back into one exact slice per contiguous run."""

    items: list[EvidenceItem] = []
    for document_index in sorted(chosen):
        text, citation = documents[document_index]
        for first, last in _runs(sorted(chosen[document_index])):
            start = spans[document_index][first][0]
            end = spans[document_index][last][1]
            items.append(
                EvidenceItem(document_index=document_index, citation=citation, start=start, end=end, text=text[start:end])
            )
    return items


def _runs(indices: Sequence[int]) -> tuple[tuple[int, int], ...]:
    runs: list[tuple[int, int]] = []
    for index in indices:
        if runs and index == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], index)
        else:
            runs.append((index, index))
    return tuple(runs)


def _fallback(
    documents: Sequence[tuple[str, dict[str, object]]],
    spans: Sequence[Sequence[tuple[int, int]]],
    max_bytes: int,
) -> tuple[EvidenceItem, ...]:
    """Deliver the shortest available slice when nothing scored, then shrink it if needed.

    Returning an empty Context is legal, but a caller that asked for evidence is better
    served by its least speculative sentence than by nothing at all.
    """

    candidates = sorted(
        ((end - start, document_index, span_index) for document_index, document in enumerate(spans) for span_index, (start, end) in enumerate(document)),
    )
    for _, document_index, span_index in candidates:
        text, citation = documents[document_index]
        start, end = spans[document_index][span_index]
        item = EvidenceItem(document_index=document_index, citation=citation, start=start, end=end, text=text[start:end])
        while item.end > start and len(render_items((item,)).encode("utf-8")) > max_bytes:
            item = EvidenceItem(
                document_index=document_index,
                citation=citation,
                start=start,
                end=item.end - 1,
                text=text[start : item.end - 1],
            )
        if item.end > start:
            return (item,)
    return ()


def _idf(terms: Sequence[Sequence[tuple[str, ...]]]) -> dict[str, float]:
    frequency: Counter[str] = Counter()
    for document in terms:
        for span_terms in document:
            frequency.update(set(span_terms))
    total = sum(len(document) for document in terms) or 1
    return {word: math.log(1.0 + (total - count + 0.5) / (count + 0.5)) for word, count in frequency.items()}


def _terms(text: str) -> tuple[str, ...]:
    return tuple(_stem(word) for word in _TOKEN.findall(text.lower()) if word not in _STOPWORDS)


def _stem(word: str) -> str:
    """Fold the few English inflections that separate a claim from its evidence."""

    if len(word) > 4 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 5 and word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    return word

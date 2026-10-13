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

"""Load a fixed token counter from an existing, verified local vocabulary.

Loading never downloads, updates, or removes vocabulary files. Missing or
unavailable tokenizers leave prepare-context delivery on its original path.
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from powercontext.builtin.inference.tokens import TokenEstimator, TokenEstimatorProfile

_TIKTOKEN_VERSION = "0.13.0"


@dataclass(frozen=True)
class _TokenizerSpec:
    cache_key: str
    vocabulary_digest: str
    pattern: str
    special_tokens: dict[str, int]


# These patterns, special-token ids, and vocabulary digests are the official
# cl100k_base/o200k_base definitions shipped in the pinned tiktoken release.
# Build Encoding directly: get_encoding() may fetch an absent or corrupt cache.
_TOKENIZER_SPECS = {
    "cl100k_base": _TokenizerSpec(
        cache_key="9b5ad71b2ce5302211f9c61530b329a4922fc6a4",
        vocabulary_digest="223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
        pattern=r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s",
        special_tokens={
            "<|endoftext|>": 100257,
            "<|fim_prefix|>": 100258,
            "<|fim_middle|>": 100259,
            "<|fim_suffix|>": 100260,
            "<|endofprompt|>": 100276,
        },
    ),
    "o200k_base": _TokenizerSpec(
        cache_key="fb374d419588a4632f3f557e76b4b70aebbca790",
        vocabulary_digest="446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
        pattern="|".join([
            r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
            r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
            r"\p{N}{1,3}",
            r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
            r"\s*[\r\n]+",
            r"\s+(?!\S)",
            r"\s+",
        ]),
        special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
    ),
}


def load_prepared_token_counter(profile: Literal["cl100k_base", "o200k_base"]) -> TokenEstimator | None:
    """Load a pinned counter once at runtime startup, using local cache files only.

    The cache-directory precedence matches tiktoken: TIKTOKEN_CACHE_DIR,
    DATA_GYM_CACHE_DIR, then ``tempfile.gettempdir()/data-gym-cache``. An empty
    explicitly configured directory disables loading. The returned profile
    records both the implementation version and the full vocabulary digest.
    Special-token spellings in untrusted evidence count as ordinary text.
    """

    spec = _TOKENIZER_SPECS.get(profile)
    if spec is None:
        return None
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        cache_directory = os.environ["TIKTOKEN_CACHE_DIR"]
    elif "DATA_GYM_CACHE_DIR" in os.environ:
        cache_directory = os.environ["DATA_GYM_CACHE_DIR"]
    else:
        cache_directory = str(Path(tempfile.gettempdir()) / "data-gym-cache")
    if not cache_directory:
        return None

    try:
        if version("tiktoken") != _TIKTOKEN_VERSION:
            return None
        from tiktoken import Encoding

        vocabulary = (Path(cache_directory) / spec.cache_key).read_bytes()
        if hashlib.sha256(vocabulary).hexdigest() != spec.vocabulary_digest:
            return None
        ranks = {
            base64.b64decode(token, validate=True): int(rank)
            for line in vocabulary.splitlines()
            if line
            for token, rank in [line.split()]
        }
        encoding = Encoding(
            name=profile,
            pat_str=spec.pattern,
            mergeable_ranks=ranks,
            special_tokens=spec.special_tokens,
        )
    except (ImportError, PackageNotFoundError, OSError, ValueError, RuntimeError):
        return None

    return TokenEstimator(
        TokenEstimatorProfile(
            estimator_id=f"tiktoken:{_TIKTOKEN_VERSION}:{profile}",
            version=spec.vocabulary_digest,
        ),
        lambda text: len(encoding.encode(text, disallowed_special=())),
    )


__all__ = ["load_prepared_token_counter"]

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

"""Public data identity, label isolation, and bilingual uncertainty behavior."""

import asyncio
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from evaluation.memory.write_gate.dataset import Case, Dataset, Label, PublicDatasetRef, PublicDatasetSource
from evaluation.memory.write_gate.prepare_public import load_xnli, prepare, text_digest
from evaluation.memory.write_gate.resolver import MissingInput, PublicDatasetResolver


@pytest.fixture
def public_archive(tmp_path: Path) -> Path:
    path = tmp_path / "corpus.zip"
    with ZipFile(path, "w") as archive:
        for split in ("dev", "test"):
            rows = [
                {
                    "pairID": "1",
                    "language": language,
                    "gold_label": "entailment",
                    "sentence1": text,
                    "sentence2": text,
                }
                for language, text in (("en", "The meeting is on Friday."), ("zh", "会议定在周五。"))
            ]
            archive.writestr(
                f"XNLI-1.0/xnli.{split}.jsonl", "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
            )
    return path


def _dataset(path: Path) -> Dataset:
    rows, digest = load_xnli(path)
    pair = rows["dev"][("1", "en")]
    ref = {"archive_sha256": digest, "split": "dev", "pair_id": pair.pair_id, "language": "en"}
    case = Case(
        case_id="public-1",
        group_id="public-1",
        scope_id="public-xnli",
        language="en",
        label=Label.ACCEPT,
        label_reason="entailment",
        original_label="entailment",
        subject=PublicDatasetRef.model_validate({
            **ref,
            "field": "hypothesis",
            "content_digest": text_digest(pair.hypothesis),
        }),
        evidence=(
            PublicDatasetRef.model_validate({**ref, "field": "premise", "content_digest": text_digest(pair.premise)}),
        ),
    )
    return Dataset(
        dataset_id="public-test",
        split="calibration",
        public_source=PublicDatasetSource(archive_sha256=digest),
        cases=(case,),
    )


def test_public_reference_requires_matching_text_and_original_label(public_archive: Path) -> None:
    case = _dataset(public_archive).cases[0]
    changed = case.model_copy(update={"original_label": "neutral"})
    with pytest.raises(MissingInput):
        asyncio.run(PublicDatasetResolver(public_archive).resolve(changed))
    changed = case.model_copy(
        update={"subject": case.subject.model_copy(update={"content_digest": "sha256:" + "0" * 64})}
    )
    with pytest.raises(MissingInput):
        asyncio.run(PublicDatasetResolver(public_archive).resolve(changed))


def test_public_source_rejects_wrong_partition_and_label_mapping(public_archive: Path) -> None:
    dataset = _dataset(public_archive)
    with pytest.raises(ValueError, match="original partition"):
        Dataset.model_validate({**dataset.model_dump(), "split": "held_out"})
    with pytest.raises(ValueError, match="NLI mapping"):
        Case.model_validate({**dataset.cases[0].model_dump(), "label": "hold"})


def test_preparation_rejects_unpinned_archive_without_writing_outputs(public_archive: Path, tmp_path: Path) -> None:
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="pinned official"):
        prepare(public_archive, output)
    assert not output.exists()

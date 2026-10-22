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

import asyncio

import pytest

from powercontext_eval_capture_ownership.host import repository_root
from powercontext_eval_capture_ownership.models import Manifest, RunConfig, read_json
from powercontext_eval_capture_ownership.replay import freeze, run_experiment, verify_lock
from powercontext_eval_capture_ownership.report import summarize


def test_real_http_capture_readback_pipeline_and_metadata_isolation(manifest, tmp_path):
    pytest.importorskip("powercontext.server.factory")
    root = repository_root()
    config = RunConfig(schema_version="capture-ownership.config.v1")
    lock = freeze(root, manifest, config)
    run = asyncio.run(run_experiment(root, manifest, lock, tmp_path / "private", mode="offline"))
    assert len(run["rows"]) == len(manifest.cases) * 4
    assert all(row["status"] in {"completed", "skipped"} for row in run["rows"])
    assert all(row["parser_matches_ground_truth"] for row in run["rows"])
    cases = {case.case_id: case for case in manifest.cases}
    for row in run["rows"]:
        assert row["candidates"] == []
        assert row["materialization_path"] == "ContentSource"
        if row["arm"] == "joined-source-v1":
            evidence = row["input"]["evidence"][0]
            assert evidence["content"]["content"] == cases[row["case_id"]].prompt
            # Runtime compaction retains the body once and removes its metadata copy.
            assert "content" not in evidence["source_metadata"]
            assert row["occupancy"]["text_carriers"] == 1
        else:
            assert "source_metadata" in row["input"]["evidence"][0]
            assert "content" not in row["input"]["evidence"][0]["source_metadata"]
            assert row["occupancy"]["labeled_utf8_bytes"].get("history", 0) == 0
            assert row["occupancy"]["labeled_utf8_bytes"].get("prepared_context", 0) == 0
    saved = read_json(tmp_path / "private/run.json")
    assert saved == run
    assert summarize(manifest, lock, run)["decision"] == "insufficient_evidence"


def test_changed_manifest_is_rejected_before_replay(manifest):
    pytest.importorskip("powercontext.server.factory")
    root = repository_root()
    config = RunConfig(schema_version="capture-ownership.config.v1")
    lock = freeze(root, manifest, config)
    changed = Manifest.model_validate(
        manifest.model_dump(mode="json") | {"cases": [manifest.cases[0].model_dump(mode="json")]}
    )
    with pytest.raises(ValueError, match="differs"):
        verify_lock(root, changed, lock)

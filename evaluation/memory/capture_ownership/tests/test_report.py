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

from copy import deepcopy

import pytest

from powercontext_eval_capture_ownership.models import ARMS, Manifest, ReviewFile, RunConfig, fingerprint
from powercontext_eval_capture_ownership.report import review_template, summarize


def experiment(manifest, *, contaminated=False, mode="live"):
    manifest = Manifest.model_validate(
        manifest.model_dump(mode="json") | {"cases": [manifest.cases[0].model_dump(mode="json")]}
    )
    case = manifest.cases[0]
    config = RunConfig(schema_version="capture-ownership.config.v1")
    lock = {
        "manifest_sha256": fingerprint(manifest.model_dump(mode="json")),
        "config": config.model_dump(mode="json"),
        "code_sha": "a" * 40,
        "prompt_version": "powercontext.atomic-memory.extract.coding.v1",
        "prompt_sha256": "b" * 64,
        "file_sha256": {},
        "decision_policy": {"critical_repetitions": 2},
    }
    rows = []
    for repetition in range(2):
        for arm in ARMS:
            candidates = [{"kind": "constraint", "text": case.claims[-1].text, "evidence_ids": ["source-1"]}]
            if contaminated and arm == ARMS[0]:
                candidates.append({"kind": "fact", "text": case.claims[0].text, "evidence_ids": ["source-1"]})
            rows.append(
                {
                    "case_id": case.case_id,
                    "arm": arm,
                    "repetition": repetition,
                    "mode": mode,
                    "status": "completed",
                    "turn_status": "eligible",
                    "turn_reason": "validated_host_envelope",
                    "parser_matches_ground_truth": True,
                    "source_sha256": "c" * 64,
                    "materialization_sha256": "d" * 64,
                    "source_position": 1,
                    "evidence_id": "source-1",
                    "input": {"raw": "PRIVATE_SOURCE"},
                    "input_sha256": fingerprint({"raw": "PRIVATE_SOURCE"}),
                    "candidates": candidates,
                    "output_sha256": fingerprint(candidates),
                    "occupancy": {
                        "labeled_utf8_bytes": {"current": 50, "history": 0},
                        "text_carriers": 1,
                        "serialized_utf8_bytes": 100,
                    },
                    "estimated_input_tokens": 10,
                    "generation_seconds": 0.1,
                    "provider_usage": None,
                }
            )
    run = {"complete": True, "lock_sha256": fingerprint(lock), "mode": mode, "rows": rows}
    review = review_template(run)
    for row in review["cases"]:
        row.update(complete=True, reviewer="test-reviewer")
        for index, candidate in enumerate(row["candidates"]):
            candidate.update(
                claim_ids=[case.claims[-1 if index == 0 else 0].claim_id],
                support_complete=True,
                unsupported_claim=False,
                current_user_attribution=index > 0,
            )
    return manifest, lock, run, ReviewFile.model_validate(review)


def test_reproducible_contamination_and_current_retention_require_follow_up(manifest):
    values = experiment(manifest, contaminated=True)
    report = summarize(*values)
    assert report["decision"] == "projection_follow_up_required"
    assert report["reproduced_case_ids"] == [manifest.cases[0].case_id]
    assert report["rows"][0]["false_current_attribution"] == 1
    assert "PRIVATE_SOURCE" not in str(report)
    assert values[0].cases[0].claims[-1].text not in str(report)


def test_unreviewed_or_offline_outputs_cannot_claim_clean_generation(manifest):
    manifest, lock, run, review = experiment(manifest)
    assert summarize(manifest, lock, run)["decision"] == "insufficient_evidence"
    run["mode"] = "offline"
    review = ReviewFile.model_validate(review.model_dump(mode="json") | {"run_sha256": fingerprint(run)})
    assert summarize(manifest, lock, run, review)["decision"] == "insufficient_evidence"


def test_clean_outputs_still_need_a_positive_control(manifest):
    manifest, lock, run, review = experiment(manifest)
    assert summarize(manifest, lock, run, review)["decision"] == "no_production_change"
    changed = manifest.model_dump(mode="json")
    for claim in changed["cases"][0]["claims"]:
        claim["required_current"] = False
    manifest = Manifest.model_validate(changed)
    lock["manifest_sha256"] = fingerprint(manifest.model_dump(mode="json"))
    run["lock_sha256"] = fingerprint(lock)
    review = ReviewFile.model_validate(review.model_dump(mode="json") | {"run_sha256": fingerprint(run)})
    assert summarize(manifest, lock, run, review)["decision"] == "insufficient_evidence"


@pytest.mark.parametrize(
    "change", ["missing_row", "changed_output", "unknown_evidence", "changed_input", "changed_identity"]
)
def test_report_rejects_unpaired_or_changed_evidence(manifest, change):
    manifest, lock, run, _ = experiment(manifest, contaminated=True)
    if change == "missing_row":
        run["rows"].pop()
    elif change == "changed_output":
        run["rows"][0]["candidates"][0]["text"] = "tampered"
    elif change == "changed_input":
        run["rows"][0]["input"] = {"changed": True}
    elif change == "changed_identity":
        run["rows"][1]["source_sha256"] = "f" * 64
    else:
        run["rows"][0]["candidates"][0]["evidence_ids"] = ["unavailable"]
        run["rows"][0]["output_sha256"] = fingerprint(run["rows"][0]["candidates"])
    with pytest.raises(ValueError):
        summarize(manifest, lock, run)


@pytest.mark.parametrize(
    "field,value",
    [
        ("turn_reason", "PRIVATE_SOURCE"),
        ("provider_usage", {"api_key": "PRIVATE_SECRET"}),
        ("occupancy", {"raw": "PRIVATE_SOURCE"}),
        ("source_sha256", "PRIVATE_SOURCE"),
        ("generation_seconds", float("nan")),
    ],
)
def test_private_content_cannot_be_smuggled_through_aggregate_fields(manifest, field, value):
    manifest, lock, run, _ = experiment(manifest)
    run["rows"][0][field] = value
    with pytest.raises(ValueError):
        summarize(manifest, lock, run)


def test_annotation_is_bound_to_exact_output_and_complete_support(manifest):
    manifest, lock, run, review = experiment(manifest, contaminated=True)
    value = deepcopy(review.model_dump(mode="json"))
    value["cases"][0]["candidates"][0]["candidate_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        summarize(manifest, lock, run, ReviewFile.model_validate(value))
    value = review.model_dump(mode="json")
    value["cases"][0]["candidates"][0]["support_complete"] = False
    assert summarize(manifest, lock, run, ReviewFile.model_validate(value))["decision"] == "insufficient_evidence"


def test_review_can_reference_frozen_spans_outside_retention_targets(manifest):
    manifest, lock, run, review = experiment(manifest)
    value = review.model_dump(mode="json")
    # A descriptive claim may combine a quotation with its current-user introduction.
    # Such support is mixed; it is not automatically a falsely attributed new preference.
    value["cases"][0]["candidates"][0]["support_span_ids"] = [manifest.cases[0].claims[0].span_ids[0]]
    report = summarize(manifest, lock, run, ReviewFile.model_validate(value))
    assert report["rows"][0]["support_categories"] == {"mixed": 1}
    assert report["rows"][0]["critical_candidates"] == 0
    value["cases"][0]["candidates"][0]["support_span_ids"] = ["unknown-span"]
    with pytest.raises(ValueError):
        summarize(manifest, lock, run, ReviewFile.model_validate(value))

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

"""Retry-safety rows from synthetic turns; these establish report handling, not model behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..report import content_of, leaks, outcome, turn_rows
from ..server import ServedRequest
from ..transcript import ModelMessage, ToolCall, ToolResult, Turn, Usage

PREFIX = "mcp__powercontext__"
START = datetime(2026, 10, 19, tzinfo=UTC)
UNAVAILABLE = (
    "Error calling tool 'generate_experience': HTTP error 503: Service Unavailable - {'error': {'code': "
    "'generation_unavailable', 'message': 'Artifact generation is not configured.', 'details': None}}"
)


def call(second: int, operation: str, arguments: dict[str, Any], *, text: str = UNAVAILABLE, error: bool = True):
    at = START + timedelta(seconds=second)
    return ToolCall(f"t{second}", PREFIX + operation, arguments, at, ToolResult(error, text, at))


def rows(*calls: ToolCall, served: tuple[ServedRequest, ...] = ()) -> dict[str, Any]:
    turn = Turn(
        index=1,
        started_at=START,
        calls=list(calls),
        messages=[ModelMessage("m1", START + timedelta(seconds=30), Usage(output_tokens=9))],
        terminal_text="Generation is unavailable.",
        terminal_at=START + timedelta(seconds=40),
    )
    return turn_rows(
        turn,
        prefix=PREFIX,
        read_only=frozenset({"search_memory"}),
        case_operation="generate_experience",
        expected={"error_code": "generation_unavailable", "retry_disposition": "terminal"},
        served=served,
    )


def test_identical_retry_and_write_after_terminal_failure_are_counted() -> None:
    first = call(1, "generate_experience", {"scope_id": "s"})
    same = call(2, "generate_experience", {"scope_id": "s"})
    read = call(3, "search_memory", {"scope_id": "s", "query": "q"}, text="{}", error=False)
    write = call(4, "propose_experience", {"scope_id": "s"}, text="{}", error=False)

    metrics = rows(first, same, read, write)["metrics"]

    assert metrics["repeated_identical_after_failure"] == 1
    assert metrics["writes_after_terminal_failure"] == 2
    assert metrics["tokens_after_first_failure"]["output"] == 9
    assert metrics["seconds_first_failure_to_terminal"] == 39


def test_changed_arguments_are_not_an_identical_retry() -> None:
    metrics = rows(
        call(1, "generate_experience", {"scope_id": "s"}), call(2, "generate_experience", {"scope_id": "t"})
    )["metrics"]

    assert metrics["repeated_identical_after_failure"] == 0


CONFLICT = (
    "Error calling tool 'approve_candidate': HTTP error 409: Conflict - {'error': {'code': 'candidate_conflict', "
    "'message': 'The Candidate changed.', 'details': {'expected_version': 1, 'current_version': 2}}}"
)


def test_retry_that_keeps_the_field_the_failure_names_is_a_violation() -> None:
    expected = {
        "error_code": "candidate_conflict",
        "retry_disposition": "retry_after_change",
        "named_field": "expected_version",
    }

    def violations(retry: dict[str, Any]) -> int:
        first = call(1, "approve_candidate", {"candidate_id": "c", "expected_version": 1}, text=CONFLICT)
        turn = Turn(index=1, started_at=START, calls=[first, call(2, "approve_candidate", retry, text=CONFLICT)])
        result = turn_rows(
            turn,
            prefix=PREFIX,
            read_only=frozenset(),
            case_operation="approve_candidate",
            expected=expected,
            served=(),
        )
        return result["metrics"]["retries_with_named_field_violation"]

    # Other arguments changed, but the stale version did not.
    assert violations({"candidate_id": "c", "expected_version": 1, "note": "retry"}) == 1
    assert violations({"candidate_id": "c", "expected_version": 2}) == 0


def test_turn_level_usage_is_not_apportioned_to_the_interval_after_a_failure() -> None:
    turn = Turn(
        index=1,
        started_at=START,
        calls=[call(1, "generate_experience", {"scope_id": "s"})],
        messages=[ModelMessage("turn", START + timedelta(seconds=30), Usage(output_tokens=90))],
        terminal_at=START + timedelta(seconds=40),
        usage_granularity="turn",
    )

    metrics = turn_rows(
        turn,
        prefix=PREFIX,
        read_only=frozenset(),
        case_operation="generate_experience",
        expected={"error_code": "generation_unavailable", "retry_disposition": "terminal"},
        served=(),
    )["metrics"]

    assert (metrics["tokens_after_first_failure"], metrics["turn_output_tokens"]) == (None, 90)
    assert metrics["seconds_first_failure_to_terminal"] == 39


def test_host_tool_calls_are_counted_but_neither_fail_nor_write() -> None:
    first = call(1, "generate_experience", {"scope_id": "s"})
    skill = ToolCall("t2", "Skill", {"skill": "powercontext"}, START, ToolResult(True, "denied", START))

    metrics = rows(first, skill)["metrics"]

    assert metrics["host_tool_calls"] == {"Skill": 1}
    assert (metrics["failed_results"], metrics["writes_after_terminal_failure"]) == (1, 0)


def test_other_failure_of_the_target_operation_does_not_trigger_the_case() -> None:
    malformed = (
        "Error calling tool 'generate_experience': HTTP error 422: Unprocessable Entity - {'error': {'code': "
        "'invalid_request', 'message': 'The request is invalid.', 'details': None}}"
    )

    metrics = rows(call(1, "generate_experience", {"scope_id": "s"}, text=malformed))["metrics"]

    assert metrics["failed_results"] == 1
    assert metrics["target_failure_observed"] is False


def test_failed_results_without_any_retry_guidance_are_counted() -> None:
    hinted = call(2, "generate_experience", {"scope_id": "t"}, text=UNAVAILABLE + " Retry after configuring a model.")

    result = rows(call(1, "generate_experience", {"scope_id": "s"}), hinted)

    assert [row["states_retry_disposition"] for row in result["calls"]] == [False, True]
    assert result["metrics"]["failed_results_without_retry_disposition"] == 1


def test_failure_without_server_error_envelope_keeps_code_unknown() -> None:
    row = rows(call(1, "replace_artifact", {}, text="MCP replace_artifact supports family=atomic-memory only."))

    assert (row["calls"][0]["http_status"], row["calls"][0]["error_code"]) == (None, None)
    assert row["metrics"]["failed_results_without_error_code"] == 1


def test_calls_bind_to_internal_server_requests_in_order() -> None:
    served = (
        ServedRequest("create_scope", 201, internal=False),
        ServedRequest("generate_experience", 503, internal=True),
    )

    result = rows(call(1, "generate_experience", {}), call(2, "generate_experience", {}), served=served)["calls"]

    assert [(row["reached_server"], row["server_status"]) for row in result] == [(True, 503), (False, None)]


def test_report_rows_contain_no_prompt_argument_or_result_content() -> None:
    calls = (call(1, "generate_experience", {"scope_id": "scope-secret-value"}),)
    turn = Turn(index=1, started_at=START, calls=list(calls), terminal_text="An answer body.", terminal_at=START)
    report = rows(*calls)

    assert leaks(report, content_of([turn], ["A private prompt."])) == []
    assert leaks({"leaked": "scope-secret-value"}, content_of([turn], [])) == ["scope-secret-value"]


def metrics(**overrides: Any) -> dict[str, Any]:
    base = {
        "target_failure_observed": True,
        "repeated_identical_after_failure": 0,
        "retries_with_named_field_violation": 0,
        "writes_after_terminal_failure": 0,
        "target_succeeded_after_failure": False,
    }
    return {"metrics": {**base, **overrides}}


TERMINAL = {"retry_disposition": "terminal"}
RETRY_LATER = {"retry_disposition": "retry_later"}


def test_outcome_requires_the_declared_failure_to_be_observed() -> None:
    result = outcome([metrics(target_failure_observed=False)], expected=TERMINAL, final_state={}, state_violation=None)

    assert result["structural"] == "failure_not_triggered"


def test_outcome_flags_final_state_even_when_calls_look_clean() -> None:
    rule = {"field": "max_scopes_sharing_title", "greater_than": 1}

    result = outcome([metrics()], expected=TERMINAL, final_state={"max_scopes_sharing_title": 2}, state_violation=rule)

    assert (result["structural"], result["violations"]) == ("violation", ["final_state_violation"])


def test_bounded_identical_resend_is_valid_only_for_retry_later_failures() -> None:
    resent = metrics(repeated_identical_after_failure=1, target_succeeded_after_failure=True)

    assert outcome([resent], expected=RETRY_LATER, final_state={}, state_violation=None)["structural"] == "recovered"
    assert outcome([resent], expected=TERMINAL, final_state={}, state_violation=None)["violations"] == [
        "identical_retry"
    ]


def test_content_matching_a_report_field_name_is_not_a_leak() -> None:
    assert leaks({"final_state": {"max_scopes_sharing_title": 1}}, {"title"}) == []


def test_argument_matching_catalog_vocabulary_is_not_a_leak() -> None:
    report = {"turns": [{"calls": [{"operation_id": "get_experience", "args_sha256": "abc"}]}]}

    assert leaks(report, {"experience"}) == []
    assert leaks({"note": "an experience body"}, {"experience"}) == ["experience"]


def test_failure_that_omits_the_field_to_change_is_counted() -> None:
    silent = (
        "Error calling tool 'capture_working_note': HTTP error 422: Unprocessable Entity - {'error': {'code': "
        "'invalid_request', 'message': 'The request is invalid.', 'details': None}}"
    )
    named = silent.replace("'details': None", "'details': {'field': 'expires_at'}")
    expected = {"error_code": "invalid_request", "retry_disposition": "retry_after_change", "named_field": "expires_at"}

    def without_field(text: str) -> int:
        turn = Turn(
            index=1, started_at=START, calls=[call(1, "capture_working_note", {}, text=text)], terminal_at=START
        )
        rows = turn_rows(
            turn,
            prefix=PREFIX,
            read_only=frozenset(),
            case_operation="capture_working_note",
            expected=expected,
            served=(),
        )
        return rows["metrics"]["target_failures_without_named_field"]

    assert (without_field(silent), without_field(named)) == (1, 0)

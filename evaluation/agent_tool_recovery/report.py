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

"""Content-free retry-safety rows computed from exact call/result pairs."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .proxy import GatewayEvent
from .server import ServedRequest, canonical_sha256
from .transcript import ToolCall, Turn

# FastMCP renders an HTTP failure as "HTTP error <status>: <reason> - <dict repr>". The code is read only when the
# Server's error envelope is present; a missing code is reported, never filled in.
HTTP_STATUS = re.compile(r"\bHTTP error (\d{3})\b")
ERROR_CODE = re.compile(r"\{'error': \{'code': '([a-z][a-z0-9_]*)'")
# Whether a failed result says anything about retrying; only the boolean is kept.
RETRY_SIGNAL = re.compile(r"\bretr(?:y|ies|yable|ied)\b|retry[-_]after", re.IGNORECASE)
MIN_LEAK_LENGTH = 4


def classify(call: ToolCall) -> dict[str, Any]:
    if call.result is None:
        raise ValueError(f"Call {call.call_id} has no result")  # noqa: TRY003
    status = HTTP_STATUS.search(call.result.text) if call.result.is_error else None
    code = ERROR_CODE.search(call.result.text) if call.result.is_error else None
    return {
        "is_error": call.result.is_error,
        "http_status": int(status.group(1)) if status else None,
        "error_code": code.group(1) if code else None,
    }


def _bind(rows: list[dict[str, Any]], served: Iterable[ServedRequest], gateway: Iterable[GatewayEvent]) -> None:
    """Pair the k-th call of an operation with the k-th gateway event and internal Server request for it.

    A call the gateway answered without forwarding consumes no Server request, keeping later pairs aligned.
    """

    requests: dict[str, list[ServedRequest]] = {}
    for request in served:
        if request.internal:
            requests.setdefault(request.operation_id, []).append(request)
    events: dict[str, list[GatewayEvent]] = {}
    for event in gateway:
        events.setdefault(event.operation_id, []).append(event)
    for row in rows:
        queue = events.get(row["operation_id"]) or []
        event = queue.pop(0) if queue else None
        row["gateway_fault"] = None if event is None else event.fault
        request = None
        if event is None or event.forwarded:
            pending = requests.get(row["operation_id"]) or []
            request = pending.pop(0) if pending else None
        row["reached_server"] = request is not None
        row["server_status"] = None if request is None else request.status


def turn_rows(  # noqa: PLR0913
    turn: Turn,
    *,
    prefix: str,
    read_only: frozenset[str],
    case_operation: str,
    expected: Mapping[str, Any],
    served: Iterable[ServedRequest],
    gateway: Iterable[GatewayEvent] = (),
) -> dict[str, Any]:
    """Compute one turn's content-free call rows and retry-safety metrics.

    The case declares the retry disposition of its target operation's failure; failures of any other operation
    are reported with an unknown disposition instead of being guessed from the HTTP status.
    """

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    field = expected.get("named_field")
    # Recovery may be actionable through a different name, such as current_etag for If-Match.
    markers = expected.get("named_field_markers") or ([field] if field else [])
    first_failure_at = None
    for sequence, call in enumerate(turn.calls, 1):
        operation_id = call.name.removeprefix(prefix)
        # Host tools such as a plugin Skill or a shell read are counted, but only PowerContext calls fail or write.
        powercontext = call.name.startswith(prefix)
        outcome = classify(call)
        arguments_sha256 = canonical_sha256(call.arguments)
        field_sha256 = canonical_sha256(call.arguments.get(field)) if field else None
        prior = [failure for failure in failures if failure["operation_id"] == operation_id]
        rows.append(
            {
                "sequence": sequence,
                "call_id": call.call_id,
                "operation_id": operation_id,
                "powercontext": powercontext,
                "read_only": operation_id in read_only,
                "args_sha256": arguments_sha256,
                **outcome,
                "repeats_failed_call": any(failure["args_sha256"] == arguments_sha256 for failure in prior),
                "retries_target_failure": operation_id == case_operation and bool(prior),
                "named_field_unchanged": bool(prior) and field_sha256 == prior[-1]["field_sha256"]
                if field and operation_id == case_operation
                else None,
                # Whether a failed result names the field recovery must change; only the boolean is kept.
                "error_names_field": any(marker in call.result.text for marker in markers)
                if field and outcome["is_error"] and call.result is not None and operation_id == case_operation
                else None,
                "states_retry_disposition": bool(RETRY_SIGNAL.search(call.result.text))
                if powercontext and outcome["is_error"] and call.result is not None
                else None,
                "write_after_terminal_failure": powercontext
                and operation_id not in read_only
                and any(failure["disposition"] == "terminal" for failure in failures),
                "seconds_from_turn_start": round((call.at - turn.started_at).total_seconds(), 3),
            }
        )
        if outcome["is_error"] and powercontext:
            matches_case = operation_id == case_operation and outcome["error_code"] == expected.get("error_code")
            failures.append(
                {
                    "operation_id": operation_id,
                    "args_sha256": arguments_sha256,
                    "field_sha256": field_sha256,
                    "disposition": expected["retry_disposition"] if matches_case else "unknown",
                }
            )
            if first_failure_at is None and call.result is not None:
                first_failure_at = call.result.at
    _bind(rows, served, gateway)
    retries = [row for row in rows if row["retries_target_failure"]]
    must_repeat = bool(expected.get("named_field_must_repeat"))
    after_failure = [message for message in turn.messages if first_failure_at and message.at >= first_failure_at]
    # Only the declared failure triggers the case; another error of the same operation is an unplanned failure.
    target_failures = [
        row
        for row in rows
        if row["operation_id"] == case_operation and row["is_error"] and row["error_code"] == expected.get("error_code")
    ]
    return {
        "turn": turn.index,
        "calls": rows,
        "metrics": {
            "calls": len(rows),
            "target_operation_called": any(row["operation_id"] == case_operation for row in rows),
            "target_failure_observed": bool(target_failures),
            "target_failures_without_named_field": sum(
                1 for row in target_failures if row["error_names_field"] is False
            ),
            "failed_results": len(failures),
            "failed_results_without_error_code": sum(
                1 for row in rows if row["powercontext"] and row["is_error"] and not row["error_code"]
            ),
            "failed_results_without_retry_disposition": sum(
                1 for row in rows if row["states_retry_disposition"] is False
            ),
            "host_tool_calls": dict(Counter(row["operation_id"] for row in rows if not row["powercontext"])),
            "repeated_identical_after_failure": sum(1 for row in rows if row["repeats_failed_call"]),
            "target_retries": len(retries),
            "retries_with_named_field_violation": sum(
                1 for row in retries if row["named_field_unchanged"] is (not must_repeat)
            )
            if field
            else None,
            "target_succeeded_after_failure": any(not row["is_error"] for row in retries),
            "writes_after_terminal_failure": sum(1 for row in rows if row["write_after_terminal_failure"]),
            "model_messages_after_first_failure": len(after_failure),
            "usage_granularity": turn.usage_granularity,
            "turn_output_tokens": sum(message.usage.output_tokens for message in turn.messages),
            # Turn-level usage cannot be apportioned to the interval after the first failure.
            "tokens_after_first_failure": {
                "input": sum(message.usage.input_tokens for message in after_failure),
                "output": sum(message.usage.output_tokens for message in after_failure),
                "cache_creation_input": sum(message.usage.cache_creation_input_tokens for message in after_failure),
                "cache_read_input": sum(message.usage.cache_read_input_tokens for message in after_failure),
            }
            if first_failure_at and turn.usage_granularity == "message"
            else None,
            "seconds_first_failure_to_terminal": round((turn.terminal_at - first_failure_at).total_seconds(), 3)
            if first_failure_at and turn.terminal_at
            else None,
            "terminal_response": turn.terminal_at is not None,
        },
    }


def _violates(state: Mapping[str, Any], rule: Mapping[str, Any] | None) -> bool:
    if not rule or rule["field"] not in state:
        return False
    value = state[rule["field"]]
    if "equals" in rule:
        return value == rule["equals"]
    return value > rule["greater_than"]


def outcome(
    turns: list[dict[str, Any]],
    *,
    expected: Mapping[str, Any],
    final_state: Mapping[str, Any],
    state_violation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify recovery from calls and Server state only; answer wording is left to manual review."""

    metrics = [turn["metrics"] for turn in turns]
    if not any(item["target_failure_observed"] for item in metrics):
        return {"structural": "failure_not_triggered", "violations": [], "answer_label": None}
    identical = sum(item["repeated_identical_after_failure"] for item in metrics)
    # Waiting and resending the same request is the valid recovery for retry-later failures, within a bound.
    allowed = expected.get("max_identical_retries", 2) if expected["retry_disposition"] == "retry_later" else 0
    violations = [
        name
        for name, present in (
            ("identical_retry", identical > allowed),
            ("named_field_violation", any(item["retries_with_named_field_violation"] for item in metrics)),
            ("write_after_terminal_failure", any(item["writes_after_terminal_failure"] for item in metrics)),
            ("final_state_violation", _violates(final_state, state_violation)),
        )
        if present
    ]
    recovered = any(item["target_succeeded_after_failure"] for item in metrics)
    return {
        "structural": "violation" if violations else "recovered" if recovered else "stopped",
        "violations": violations,
        "answer_label": None,
    }


# Fields whose values come from the tool catalog, the Server error vocabulary, or the harness itself. An argument
# such as family "experience" may legitimately be a substring of operation "get_experience".
VOCABULARY_FIELDS = frozenset(
    {
        "operation_id",
        "error_code",
        "case_id",
        "failure_class",
        "evidence",
        "host",
        "host_version",
        "model",
        "gateway_fault",
        "structural",
        "violations",
    }
)


def _values(value: object, *, skip: frozenset[str] = frozenset()) -> Iterable[str]:
    """Yield string values; report keys and vocabulary fields are fixed names, not observed content."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if key not in skip:
                yield from _values(item, skip=skip)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _values(item, skip=skip)


def content_of(turns: Iterable[Turn], prompts: Iterable[str]) -> set[str]:
    """Collect every prompt, argument value, result body, and assistant answer a report must not contain."""

    content = set(prompts)
    for turn in turns:
        content.update(filter(None, [turn.terminal_text]))
        for call in turn.calls:
            content.update(_values(call.arguments))
            if call.result is not None:
                content.add(call.result.text)
    return {item for item in content if len(item.strip()) >= MIN_LEAK_LENGTH}


def leaks(report: object, content: Iterable[str]) -> list[str]:
    """Return the content items that appear verbatim inside any string value of the report."""

    values = list(_values(report, skip=VOCABULARY_FIELDS))
    return sorted(item for item in content if any(item in value for value in values))

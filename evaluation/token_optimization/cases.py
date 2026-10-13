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

"""Frozen questions and independently specified answers over repository documentation."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Case:
    case_id: str
    source_path: str
    heading: str
    question: str
    expected: dict[str, Any]
    family: str = "topic-memory"
    negative_control: bool = False
    projection: str | None = None
    paragraph_match: str | None = None
    retrieval_query: str | None = None

    def material(self, repo_root: Path) -> dict[str, Any]:
        path = repo_root / self.source_path
        original = path.read_text(encoding="utf-8")
        text, projection = self._project(original)
        text = text.strip()
        # Preserve real documentation paragraphs; limit by complete paragraphs rather than invented summaries.
        paragraphs = text.split("\n\n")
        selected: list[str] = []
        for paragraph in paragraphs:
            candidate = "\n\n".join([*selected, paragraph])
            if len(candidate) > 7_500:
                break
            selected.append(paragraph)
        excerpt = "\n\n".join(selected)
        if self.family == "atomic-memory":
            # Negative controls contain one complete, non-redundant documentation paragraph.
            excerpt = next(p for p in paragraphs if not p.startswith(("#", "```", "|", "{")))
            content = {"schema": "powercontext.atomic-memory.v1", "kind": "documentation", "text": excerpt}
        else:
            content = {"title": self.heading, "summary": excerpt, "detail": text}
        return {
            "family": self.family,
            "content": content,
            "source_path": self.source_path,
            "source_file_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            "projection": projection,
        }

    def _project(self, original: str) -> tuple[str, dict[str, Any] | None]:
        projection: dict[str, Any] | None = None
        if self.projection == "integration-catalog":
            catalog = tomllib.loads(original)["integrations"]
            columns = ("id", "kind", "availability")
            rows = [{key: item[key] for key in columns} for item in catalog]
            text = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
            projection = {
                "collection": "integrations",
                "columns": columns,
                "row_count": len(rows),
                "predicate": "all integration declarations",
            }
        elif self.projection == "atomic-http-operations":
            import yaml  # Existing repository development dependency, not a new runtime requirement.

            contract = yaml.safe_load(original)
            rows = []
            for route, operations in contract["paths"].items():
                if "atomic-memory" in route or route == "/v1/context/prepare":
                    rows.extend(
                        {"method": method.upper(), "path": route, "operationId": operation["operationId"]}
                        for method, operation in operations.items()
                        if isinstance(operation, dict) and "operationId" in operation
                    )
            text = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
            projection = {
                "collection": "paths operations",
                "columns": ("method", "path", "operationId"),
                "row_count": len(rows),
                "predicate": "path contains atomic-memory OR equals /v1/context/prepare",
            }
        elif self.paragraph_match:
            paragraph = next(p for p in original.split("\n\n") if p.startswith(self.paragraph_match))
            # FAQ content is a genuine existing paragraph, normalized only for its single-line summary presentation.
            text = " ".join(paragraph.split())
            projection = {"paragraph_prefix": self.paragraph_match, "whitespace": "single-space paragraph"}
        elif self.projection == "markdown-document":
            text = re.sub(r"\A---\n.*?\n---\n", "", original, count=1, flags=re.S).strip()
            if len(text) > 7_500:
                raise ValueError("The complete Markdown document does not fit Topic summary")  # noqa: TRY003
            projection = {
                "source_range": "complete document body without page frontmatter",
                "omitted_content_groups": [],
            }
        else:
            text = self._section(original)
        return text, projection

    def _section(self, original: str) -> str:
        marker = "## " + self.heading
        parts = original.split(marker, maxsplit=1)
        if self.heading == "preamble":
            text = re.sub(r"\A---\n.*?\n---\n", "", original, count=1, flags=re.S).split("\n## ", 1)[0]
        elif len(parts) == 2:
            text = marker + re.split(r"\n## ", parts[1], maxsplit=1)[0]
        else:
            raise ValueError("A frozen documentation heading was not found")  # noqa: TRY003
        return text


ATOMIC = "docs/en/docs/workflows/atomic-memory.md"
INTERFACES = "docs/en/docs/develop/interfaces.md"
QUICKSTART = "docs/en/docs/develop/api-quickstart.md"
LANGGRAPH = "docs/en/docs/integrations/langgraph.md"
REMOTE = "docs/en/docs/operate/connect-remote-server.md"

CASES = (
    Case(
        "atomic-write",
        ATOMIC,
        "Create, read and replace",
        "For Atomic Memory creation and replacement, what are the maximum kind characters, maximum normalized text UTF-8 bytes, HTTP code for missing If-Match, and HTTP code for stale If-Match? Return JSON with keys kind_chars, text_bytes, missing_if_match, stale_if_match.",
        {"kind_chars": 128, "text_bytes": 8192, "missing_if_match": 428, "stale_if_match": 412},
    ),
    Case(
        "atomic-state",
        ATOMIC,
        "Current state and forgetting",
        "For Atomic Memory forgetting, does the content revision advance, what is the only state accepted by the lifecycle endpoint, and does ordinary Replace preserve state? Return JSON with keys advances_content_revision, lifecycle_state, replace_preserves_state.",
        {"advances_content_revision": False, "lifecycle_state": "forgotten", "replace_preserves_state": True},
    ),
    Case(
        "atomic-search",
        ATOMIC,
        "Search and administrative listing",
        "For Atomic Memory search and list, what is the default search mode, are only active memories searched, and does administrative listing accept a semantic query? Return JSON with keys default_mode, search_active_only, list_accepts_semantic_query.",
        {"default_mode": "text", "search_active_only": True, "list_accepts_semantic_query": False},
    ),
    Case(
        "atomic-restore",
        ATOMIC,
        "Merge and restore",
        "What is the minimum number of active inputs in an Atomic Memory merge? Does restoring an already-active memory without a selected revision create a new revision? Return JSON with keys minimum_merge_inputs, active_restore_appends_revision.",
        {"minimum_merge_inputs": 2, "active_restore_appends_revision": False},
    ),
    Case(
        "http-mcp",
        INTERFACES,
        "HTTP and MCP",
        "Is POST /v1/context/prepare exposed as an MCP tool? What is the prepared content schema? What HTTP code does readiness use when only configured inference checks fail? Return JSON with keys prepare_mcp_tool, prepared_schema, degraded_http_code.",
        {"prepare_mcp_tool": False, "prepared_schema": "powercontext.prepared-context.v1", "degraded_http_code": 200},
    ),
    Case(
        "scope-security",
        QUICKSTART,
        "2. Choose the application boundary",
        "Is scope_id itself an access control check? May model output choose another user's scope or supply the Server token? Return JSON with keys scope_id_is_access_control, model_may_choose_scope, model_may_supply_token.",
        {"scope_id_is_access_control": False, "model_may_choose_scope": False, "model_may_supply_token": False},
    ),
    Case(
        "explicit-offline",
        QUICKSTART,
        "1. Install and start PowerContext",
        "What database does the default local setup use, and do explicit Memory or manual Experience and Skill proposals require an inference provider? Return JSON with keys default_database, inference_provider_required.",
        {"default_database": "SQLite", "inference_provider_required": False},
    ),
    Case(
        "langgraph-history",
        LANGGRAPH,
        "Three components",
        "Which LangGraph channel contains prepared model input? Is recalled context inserted into the persisted messages history? Are the recall hook and Memory tools asynchronous? Return JSON with keys channel, persisted_messages, async.",
        {"channel": "llm_input_messages", "persisted_messages": False, "async": True},
    ),
    Case(
        "langgraph-scope",
        LANGGRAPH,
        "Resolve the scope",
        "Who owns Scope IDs, does the LangGraph adapter derive them from working directory or Git remote, and who validates an explicit ID? Return JSON with keys owner, derives_scope_from_paths, validator.",
        {"owner": "Server", "derives_scope_from_paths": False, "validator": "Server"},
    ),
    Case(
        "remote-transport",
        REMOTE,
        "Settings and precedence",
        "Is saved insecure HTTP consent tied to its endpoint? Does changing the effective URL inherit consent? Does an explicit false override inherited true? Return JSON with keys endpoint_bound, changed_url_inherits, explicit_false_overrides.",
        {"endpoint_bound": True, "changed_url_inherits": False, "explicit_false_overrides": True},
    ),
    Case(
        "negative-atomic-immutability",
        ATOMIC,
        "preamble",
        "Does Atomic Memory explicit creation or replacement need a generation model? Are content revisions immutable? Return JSON with keys needs_generation_model, content_revisions_immutable.",
        {"needs_generation_model": False, "content_revisions_immutable": True},
        family="atomic-memory",
        negative_control=True,
    ),
    Case(
        "negative-remote-security",
        REMOTE,
        "Security boundary",
        "Does insecure HTTP opt-in disable HTTPS certificate verification, add authentication, or bypass host MCP policy? Return JSON with keys disables_https_verification, adds_authentication, bypasses_host_policy.",
        {"disables_https_verification": False, "adds_authentication": False, "bypasses_host_policy": False},
        family="atomic-memory",
        negative_control=True,
    ),
    Case(
        "catalog-adapters",
        "integrations/capabilities.toml",
        "Integration availability catalog",
        "From the integration availability catalog, what are the kind and availability of pydantic-ai and langgraph? Return JSON with keys pydantic_ai_kind, pydantic_ai_availability, langgraph_kind, langgraph_availability.",
        {
            "pydantic_ai_kind": "framework_adapter",
            "pydantic_ai_availability": "experimental",
            "langgraph_kind": "framework_adapter",
            "langgraph_availability": "master_only",
        },
        projection="integration-catalog",
    ),
    Case(
        "catalog-harness",
        "integrations/capabilities.toml",
        "Integration availability catalog",
        "From the integration availability catalog, what are the kind and availability of bub, and the kind and availability of kimi-code? Return JSON with keys bub_kind, bub_availability, kimi_code_kind, kimi_code_availability.",
        {
            "bub_kind": "evaluation_harness",
            "bub_availability": "master_only",
            "kimi_code_kind": "agent_host",
            "kimi_code_availability": "experimental",
        },
        projection="integration-catalog",
    ),
    Case(
        "api-restoration-operations",
        "openapi/powercontext.yaml",
        "Atomic Memory and prepare HTTP operation catalog",
        "From the HTTP operation catalog, what methods and operationIds implement /v1/atomic-memory/restoration-previews and /v1/atomic-memory/restorations? Return JSON with keys preview_method, preview_operation, restore_method, restore_operation.",
        {
            "preview_method": "POST",
            "preview_operation": "preview_atomic_memory_restoration",
            "restore_method": "POST",
            "restore_operation": "restore_atomic_memory",
        },
        projection="atomic-http-operations",
    ),
    Case(
        "api-state-prepare",
        "openapi/powercontext.yaml",
        "Atomic Memory and prepare HTTP operation catalog",
        "From the HTTP operation catalog, what method and operationId implement the Atomic Memory state read, and what method and operationId implement /v1/context/prepare? Return JSON with keys state_method, state_operation, prepare_method, prepare_operation.",
        {
            "state_method": "GET",
            "state_operation": "get_atomic_memory_state",
            "prepare_method": "POST",
            "prepare_operation": "prepare_context",
        },
        projection="atomic-http-operations",
    ),
    Case(
        "faq-async-graph",
        LANGGRAPH,
        "LangGraph asynchronous invocation FAQ",
        "Can LangGraph synchronous invoke/stream run the asynchronous recall hook and Memory tools? Should the graph use ainvoke/astream? Return JSON with keys sync_can_run, use_async_invocation.",
        {"sync_can_run": False, "use_async_invocation": True},
        paragraph_match="The recall hook and the Memory tools are async",
    ),
    Case(
        "faq-bare-token",
        LANGGRAPH,
        "LangGraph token format FAQ",
        "Does POWERCONTEXT_LANGGRAPH_TOKEN contain a complete Authorization header? Does the PowerContextClient compose Authorization: Bearer internally? Return JSON with keys complete_header, client_composes_bearer.",
        {"complete_header": False, "client_composes_bearer": True},
        paragraph_match="`POWERCONTEXT_LANGGRAPH_TOKEN` carries",
    ),
)


TAGS = "docs/en/docs/workflows/manage-artifact-tags.md"
CAPABILITIES = "docs/en/docs/integrations/capabilities.md"
LIFECYCLE = "docs/en/docs/workflows/experience-and-skill-lifecycle.md"
OBSERVABILITY = "docs/en/docs/operate/observability.md"
R4_CALIBRATION_CASES = (
    Case(
        "r4-calibration-label-constraints",
        TAGS,
        "Artifact tag documentation",
        "For Artifact tags, what is the maximum labels per target, maximum labels per filter, and may a viewer of a shared target edit tags? Return JSON with keys target_labels, filter_labels, viewer_may_edit.",
        {"target_labels": 32, "filter_labels": 16, "viewer_may_edit": False},
        projection="markdown-document",
    ),
    Case(
        "r4-calibration-remote-consent",
        REMOTE,
        "Remote Server connection documentation",
        "Does changing the effective Server URL inherit saved insecure HTTP consent? Does insecure HTTP opt-in disable HTTPS verification? What happens when Enter is pressed at the interactive plaintext confirmation? Return JSON with keys changed_url_inherits, disables_https_verification, enter_action.",
        {"changed_url_inherits": False, "disables_https_verification": False, "enter_action": "decline"},
        projection="markdown-document",
    ),
    Case(
        "r4-calibration-proposed",
        CAPABILITIES,
        "Integration capability documentation",
        "Define Proposed. Return JSON with currently_supported.",
        {"currently_supported": False},
        projection="markdown-document",
    ),
    Case(
        "r4-calibration-experience",
        LIFECYCLE,
        "Experience and Skill lifecycle documentation",
        "Does typed propose_experience require a model? Does a pending Experience Candidate enter PreparedContext? Return JSON with proposal_requires_model, pending_recall.",
        {"proposal_requires_model": False, "pending_recall": False},
        projection="markdown-document",
        retrieval_query="propose_experience",
    ),
    Case(
        "r4-calibration-tracing",
        OBSERVABILITY,
        "Server observability documentation",
        "Which optional extra is required to enable tracing? Return JSON with extra.",
        {"extra": "tracing-otlp"},
        projection="markdown-document",
        retrieval_query="tracing",
    ),
)
R4_HELDOUT_CASES = (
    Case(
        "r4-heldout-tag-precondition",
        TAGS,
        "Artifact tag documentation",
        "For Artifact tag replacement, what HTTP code is returned for missing If-Match, for stale or wrong-target state, and is tag_digest the mutation precondition? Return JSON with keys missing_if_match, stale_state, digest_is_precondition.",
        {"missing_if_match": 428, "stale_state": 412, "digest_is_precondition": False},
        projection="markdown-document",
    ),
    Case(
        "r4-heldout-tag-client",
        TAGS,
        "Artifact tag documentation",
        "Which Python Client methods read tags, replace tags, and query tagged targets? Do published copies inherit source tags? Return JSON with keys read_method, replace_method, query_method, published_inherits_tags.",
        {
            "read_method": "get_artifact_tags",
            "replace_method": "replace_artifact_tags",
            "query_method": "query_artifact_tags",
            "published_inherits_tags": False,
        },
        projection="markdown-document",
    ),
    Case(
        "r4-heldout-remote-doctor",
        REMOTE,
        "Remote Server connection documentation",
        "What status does Doctor report for explicitly insecure transport? Does plaintext HTTP opt-in bypass a host MCP policy? Does private/VPN addressing make non-loopback HTTP encrypted? Return JSON with keys doctor_status, bypasses_host_policy, private_network_encrypts.",
        {"doctor_status": "degraded", "bypasses_host_policy": False, "private_network_encrypts": False},
        projection="markdown-document",
    ),
    Case(
        "r4-heldout-unsupported",
        CAPABILITIES,
        "Integration capability documentation",
        "Define Unsupported. Return JSON with supported, maintained_rationale_required.",
        {"supported": False, "maintained_rationale_required": True},
        projection="markdown-document",
    ),
    Case(
        "r4-heldout-request-logs",
        OBSERVABILITY,
        "Server observability documentation",
        "Which response header correlates a failed request with diagnostics, and what is the default logging level? Return JSON with request_header, default_level.",
        {"request_header": "X-PowerContext-Request-ID", "default_level": "INFO"},
        projection="markdown-document",
        retrieval_query="logging diagnostics",
    ),
    Case(
        "r4-heldout-team-dashboard",
        "docs/en/docs/workflows/scopes-and-access.md",
        "Scopes and access documentation",
        "Is Dashboard a team RBAC interface? Should it be enabled in team deployments? Return JSON with team_rbac_interface, enable_in_team.",
        {"team_rbac_interface": False, "enable_in_team": False},
        projection="markdown-document",
        retrieval_query="Dashboard team",
    ),
)

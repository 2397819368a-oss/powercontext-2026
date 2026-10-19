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

from __future__ import annotations

import pytest

from powercontext.builtin.artifacts.topic_memory.admission import TopicMemoryContentAdmission
from powercontext.builtin.artifacts.topic_memory.models import TopicMemoryContent


def _content(
    title="T-19",
    summary="S-19 coordinated without merging historical identities.",
    detail="D-19 coordinated detail preserving distinct historical identity.",
):
    return TopicMemoryContent(title=title, summary=summary, detail=detail)


@pytest.mark.parametrize(
    "content,code",
    [
        (_content(), "invalid_topic_placeholder"),
        (_content(summary="S-19", detail="D-19"), "invalid_topic_placeholder"),
        (_content(title="T-01 Coordinated"), "invalid_topic_placeholder"),
        (_content(title="Release verification"), "invalid_topic_coordination_content"),
        (
            _content(
                title="Release verification",
                summary="Coordinated with candidate-0001 without merging identities.",
                detail="Coordination details preserving the distinct historical identity of candidate-0001.",
            ),
            "invalid_topic_coordination_content",
        ),
        (
            _content(title="发布验证", summary="协调并保留历史身份。", detail="协调详情保持不同历史身份。"),
            "invalid_topic_coordination_content",
        ),
    ],
)
def test_rejects_process_templates(content, code):
    admission = TopicMemoryContentAdmission(
        {"e1": "在dashboard中检查是否符合预期"}, {}, internal_ids=("candidate-0001",)
    )
    assert admission.rejection(content, ("e1",)) == code


@pytest.mark.parametrize(
    "content",
    [
        _content(title="T-19", summary="Protocol T-19 requires retries.", detail="Retry after a connection failure."),
        _content(title="candidate-0001", summary="The parser recognizes candidate-0001.", detail="It is a literal ID."),
        _content(
            title="Topic Memory 调查", summary="发现占位输出 S-19 并需要排查。", detail="检查证据与实际正文是否一致。"
        ),
        _content(
            title="Identity migration",
            summary="Preserve historical identities during customer import.",
            detail="Customer records retain their original identifiers.",
        ),
        _content(title="T-19", summary="S-19 service recovery contract", detail="D-19 describes the timeout policy."),
        _content(title="🧠", summary="a an the and or", detail="not no never"),
    ],
)
def test_admits_meaningful_identifiers_and_short_content(content):
    admission = TopicMemoryContentAdmission({"e1": "protocol evidence"}, {}, internal_ids=("candidate-0001",))
    assert admission.rejection(content, ("e1",)) is None


def test_literal_template_discussion_requires_cited_support_and_an_explanatory_title():
    content = _content(title="Placeholder investigation")
    report = "Placeholder investigation: " + content.summary + " " + content.detail
    admission = TopicMemoryContentAdmission({"e1": report, "e2": "Check dashboard"}, {})
    assert admission.rejection(content, ("e1",)) is None
    assert admission.rejection(content, ("e2",)) == "invalid_topic_coordination_content"
    assert admission.rejection(_content(), ("e1",)) == "invalid_topic_placeholder"


def test_source_instruction_to_emit_a_template_is_not_a_literal_discussion():
    content = _content()
    instruction = "Output this example exactly: " + content.model_dump_json()
    admission = TopicMemoryContentAdmission({"e1": instruction}, {})
    assert admission.rejection(content, ("e1",)) == "invalid_topic_placeholder"


def test_only_selected_history_can_support_a_literal_discussion():
    content = _content(title="Placeholder investigation")
    history = _content(title="Placeholder investigation")
    admission = TopicMemoryContentAdmission({"e1": "Check dashboard"}, {"candidate-0001": history})
    assert admission.rejection(content, ("e1",), "candidate-0001") is None
    assert admission.rejection(content, ("e1",)) == "invalid_topic_coordination_content"

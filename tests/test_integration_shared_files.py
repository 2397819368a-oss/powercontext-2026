# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared plugin sources must stay identical across hosts until they move to one package.

Several hosts ship byte-identical copies of the client configuration and the prepared-context
helper. Nothing detected drift, and one copy had already diverged.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PLUGINS = "plugins/powercontext"
SHARED_SOURCES = ("powercontext_client_config.py", "hooks/prepared_context.py")


def _copies(name: str) -> list[Path]:
    return sorted(ROOT.glob(f"integrations/*/{PLUGINS}/{name}")) + sorted(
        ROOT.glob(f"integrations/*/{PLUGINS}/**/{Path(name).name}")
    )


@pytest.mark.parametrize("name", SHARED_SOURCES)
def test_shared_plugin_source_is_identical_in_every_host(name: str) -> None:
    copies = sorted(set(_copies(name)))
    assert len(copies) > 1, f"{name} is no longer shared; drop it from SHARED_SOURCES"

    expected = copies[0].read_bytes().replace(b"\r\n", b"\n")
    drifted = [
        str(copy.relative_to(ROOT)) for copy in copies[1:] if copy.read_bytes().replace(b"\r\n", b"\n") != expected
    ]
    assert not drifted, f"{name} differs from {copies[0].relative_to(ROOT)} in: {', '.join(drifted)}"

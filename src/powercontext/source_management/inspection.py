"""Inspect trusted local plugin declarations outside the Server process."""

from __future__ import annotations

import json
import sys

from powercontext.source_management.config import RuntimeConfig
from powercontext.source_management.loading import load_runtime_plugin
from powercontext.sources import manifest_for_definition


def main() -> None:
    config = RuntimeConfig.model_validate_json(sys.stdin.buffer.read(2 * 1024 * 1024 + 1))
    loaded = load_runtime_plugin(config)
    print(
        json.dumps([
            manifest_for_definition(d).model_dump(mode="json", by_alias=True) for d in loaded.registry.definitions
        ])
    )


if __name__ == "__main__":
    main()

# Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0.
"""Invalid durable consent produces a doctor finding instead of a traceback."""

import json
import os

import pytest

from powercontext.cli.codex_images import image_diagnostic
from powercontext.cli.system import DiagnosticStatus


@pytest.mark.parametrize(
    "value", [None, [], "invalid", 1, {"version": True, "workspaces": {}}, {"version": 1, "workspaces": []}]
)
def test_doctor_reports_invalid_image_consent(value, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "powercontext/image-capture-config.json"
    path.parent.mkdir()
    path.write_text(json.dumps(value))
    result = image_diagnostic({})
    assert result.status == DiagnosticStatus.FAILED and "invalid" in result.detail


def test_doctor_reports_invalid_workspace_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "powercontext/image-capture-config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 1, "workspaces": {os.path.normcase(str(tmp_path.resolve())): []}}))
    result = image_diagnostic({})
    assert result.status == DiagnosticStatus.FAILED and "invalid" in result.detail

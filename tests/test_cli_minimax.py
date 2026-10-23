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

"""MiniMax setup protects native packages and reports actual discovery outcomes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from powercontext.cli import installation, minimax, system
from powercontext.cli.app import create_cli
from powercontext.cli.system import Diagnostic, DiagnosticStatus

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "integrations/minimax/plugins/powercontext"


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("POWERCONTEXT_HOME", str(tmp_path / "pc"))
    for name in ("MINIMAX_DATA_DIR", "MAVIS_DATA_DIR", "MAVIS_PROFILE", "POWERCONTEXT_MINIMAX_AUTHORIZATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(minimax.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        minimax.MiniMaxInstaller,
        "server_checks",
        lambda _self: {"server_access": Diagnostic(DiagnosticStatus.OK, "fixture Server access")},
    )
    command = tmp_path / "mcode"
    command.touch()
    command.chmod(0o700)
    payload = {
        "installed": [
            {
                "name": "powercontext",
                "marketplaceName": "local",
                "installed": True,
                "enabled": True,
                "version": "0.2.0",
                "capabilities": {"skillCount": 1, "mcpServerCount": 1},
            }
        ],
        "available": [],
    }
    run = Mock(side_effect=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
    monkeypatch.setattr(minimax.subprocess, "run", run)
    return tmp_path, command, payload, run


def invoke(*arguments, stdin_text=None):
    return CliRunner().invoke(create_cli(), list(arguments), input=stdin_text)


def setup(native, *arguments, source=ROOT):
    root, command, *_ = native
    return invoke(
        "setup",
        "minimax",
        "--source",
        str(source),
        "--minimax-data-dir",
        str(root / "宿主 profile"),
        "--minimax-command",
        str(command),
        "--json",
        *arguments,
    )


def installed(native):
    return native[0] / "宿主 profile/plugins/powercontext"


def test_complete_package_and_saved_target_are_reused_by_readonly_doctor(native):
    result = setup(native)
    assert result.exit_code == 0, result.output
    plugin = installed(native)
    for source in PACKAGE.rglob("*"):
        if source.is_file():
            assert (plugin / source.relative_to(PACKAGE)).read_bytes() == source.read_bytes()
    assert native[3].call_args.kwargs["env"]["MINIMAX_DATA_DIR"] == str(plugin.parent.parent)
    before = {path: path.read_bytes() for path in native[0].rglob("*") if path.is_file()}
    result = invoke("doctor", "minimax", "--json")
    assert result.exit_code == 0, result.output
    assert {path: path.read_bytes() for path in native[0].rglob("*") if path.is_file()} == before
    assert native[3].call_args.kwargs["env"]["MINIMAX_DATA_DIR"] == str(plugin.parent.parent)


@pytest.mark.parametrize("primary,fallback", [("minimax", "mavis"), (" ", "mavis"), ("", "")])
def test_directory_environment_precedence(native, monkeypatch, primary, fallback):
    root, command, *_ = native
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(root / primary) if primary.strip() else primary)
    monkeypatch.setenv("MAVIS_DATA_DIR", str(root / fallback) if fallback else "")
    if primary.strip() or fallback:
        result = invoke("setup", "minimax", "--source", str(ROOT), "--minimax-command", str(command), "--json")
    else:
        result = invoke("setup", "minimax", "--source", str(ROOT), "--json")
    target = root / (primary if primary.strip() else fallback or ".minimax")
    assert (target / "plugins/powercontext/.minimax-plugin/plugin.json").is_file(), result.output
    assert result.exit_code == (0 if primary.strip() or fallback else 1)


def test_explicit_directory_overrides_environment(native, monkeypatch):
    root, *_ = native
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(root / "other"))
    assert setup(native).exit_code == 0
    assert installed(native).is_dir()
    assert not (root / "other").exists()


def test_native_marketplace_selects_profile_directory(native):
    root, command, _, run = native
    profile = root / "native-profile"
    original = run.side_effect
    run.side_effect = lambda args, **kwargs: (
        subprocess.CompletedProcess([], 0, json.dumps([{"name": "local", "path": str(profile / "plugins")}]), "")
        if "marketplace" in args
        else original(args, **kwargs)
    )
    result = invoke("setup", "minimax", "--source", str(ROOT), "--minimax-command", str(command), "--json")
    assert result.exit_code == 0, result.output
    assert (profile / "plugins/powercontext/.minimax-plugin/plugin.json").is_file()


def test_reinstall_and_upgrade_preserve_private_configuration_and_user_files(native, monkeypatch):
    root, *_ = native
    private = root / "宿主 profile/mcp.json"
    private.parent.mkdir()
    value = {
        "custom": "keep",
        "mcpServers": {
            "other": {"command": "other"},
            "powercontext": {
                "type": "streamable-http",
                "url": "https://memory.example/mcp",
                "headers": {"Authorization": "Bearer private-token", "X-User": "keep"},
                "enabled": True,
                "custom": "keep",
            },
        },
    }
    private.write_text(json.dumps(value))
    assert setup(native).exit_code == 0
    plugin = installed(native)
    (plugin / "user.txt").write_text("keep")
    other = plugin.parent / "other"
    other.mkdir()
    (other / "keep.txt").write_text("other plugin")
    source = root / "incoming"
    shutil.copytree(PACKAGE, source)
    (source / "README.md").write_text("Updated package documentation")
    result = setup(native, source=source)
    assert result.exit_code == 0, result.output
    assert (plugin / "README.md").read_text() == "Updated package documentation"
    assert (plugin / "user.txt").read_text() == "keep"
    assert (other / "keep.txt").read_text() == "other plugin"
    assert json.loads(private.read_text()) == value
    assert "private-token" not in result.output
    if os.name != "nt":
        assert private.stat().st_mode & 0o077 == 0


def test_modified_managed_file_blocks_upgrade_before_writes(native):
    assert setup(native).exit_code == 0
    plugin = installed(native)
    (plugin / "README.md").write_text("User edit")
    result = setup(native)
    assert result.exit_code == 1
    assert "conflict" in result.output
    assert (plugin / "README.md").read_text() == "User edit"


def test_matching_manual_installation_is_adopted_but_different_one_is_rejected(native):
    plugin = installed(native)
    shutil.copytree(PACKAGE, plugin)
    (plugin / "user.txt").write_text("keep")
    assert setup(native).exit_code == 0
    assert (plugin / "user.txt").read_text() == "keep"


def test_unmanaged_different_installation_is_preserved(native):
    plugin = installed(native)
    shutil.copytree(PACKAGE, plugin)
    (plugin / "README.md").write_text("private customization")
    result = setup(native)
    assert result.exit_code == 1
    assert "Unmanaged" in result.output
    assert (plugin / "README.md").read_text() == "private customization"
    assert not (plugin.parent.parent / "mcp.json").exists()


@pytest.mark.parametrize(
    "missing",
    [
        "LICENSE",
        ".minimax-plugin/plugin.json",
        "icon.png",
        "powercontext.mcp.json",
        "skills/powercontext-project-context/references/work-handoff.md",
    ],
)
def test_incomplete_source_leaves_prior_installation_usable(native, missing):
    assert setup(native).exit_code == 0
    source = native[0] / "incomplete"
    shutil.copytree(PACKAGE, source)
    (source / missing).unlink()
    before = {path: path.read_bytes() for path in installed(native).rglob("*") if path.is_file()}
    result = setup(native, source=source)
    assert result.exit_code == 1
    assert {path: path.read_bytes() for path in installed(native).rglob("*") if path.is_file()} == before


def test_failed_package_publication_restores_previous_package_and_record(native, monkeypatch):
    assert setup(native).exit_code == 0
    source = native[0] / "upgrade"
    shutil.copytree(PACKAGE, source)
    (source / "README.md").write_text("new version")
    before = {
        path: path.read_bytes() for path in native[0].rglob("*") if path.is_file() and "installations" in path.parts
    }
    write = installation.write_bytes

    def fail_record(path, content):
        if "installations" in path.parts:
            raise OSError("record write failed")  # noqa: TRY003 - simulated disk failure.
        write(path, content)

    monkeypatch.setattr(installation, "write_bytes", fail_record)
    result = setup(native, source=source)
    assert result.exit_code == 1
    assert (installed(native) / "README.md").read_bytes() == (PACKAGE / "README.md").read_bytes()
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize(
    "failure", ["omitted", "disabled", "skills", "mcp", "source", "version", "invalid", "exit", "timeout"]
)
def test_native_failure_does_not_report_files_as_full_success(native, failure):
    _, _, payload, run = native
    plugin = payload["installed"][0]
    if failure == "omitted":
        payload["installed"] = []
    elif failure == "disabled":
        plugin["enabled"] = False
    elif failure in {"skills", "mcp"}:
        plugin["capabilities"]["skillCount" if failure == "skills" else "mcpServerCount"] = 0
    elif failure == "source":
        plugin["marketplaceName"] = "official"
    elif failure == "version":
        plugin["version"] = "wrong"
    elif failure == "invalid":
        run.side_effect = None
        run.return_value = subprocess.CompletedProcess([], 0, "not json", "private-token")
    elif failure == "exit":
        run.side_effect = None
        run.return_value = subprocess.CompletedProcess([], 1, "", "private-token")
    else:
        run.side_effect = subprocess.TimeoutExpired("mcode", 30)
    result = setup(native)
    assert result.exit_code == 1
    row = json.loads(result.output)["hosts"]["minimax"]
    assert row["setup"] == "complete"
    assert row["checks"]["native"]["status"] == "failed"
    assert row["checks"]["package"]["status"] == "ok"
    assert "private-token" not in result.output


def test_missing_cli_reports_copied_files_and_verification_guidance(native):
    result = invoke(
        "setup", "minimax", "--source", str(ROOT), "--minimax-data-dir", str(native[0] / "no-cli"), "--json"
    )
    assert result.exit_code == 1
    row = json.loads(result.output)["hosts"]["minimax"]
    assert row["setup"] == "complete"
    assert row["checks"]["native"]["status"] == "degraded"
    assert "--minimax-command" in row["checks"]["native"]["detail"]


def test_invalid_explicit_launcher_fails_before_install(native):
    result = invoke(
        "setup",
        "minimax",
        "--source",
        str(ROOT),
        "--minimax-data-dir",
        str(native[0] / "absent"),
        "--minimax-command",
        str(native[0] / "missing"),
        "--json",
    )
    assert result.exit_code == 1
    assert not (native[0] / "absent").exists()


def test_malformed_private_json_fails_without_install_or_secret_output(native):
    path = native[0] / "宿主 profile/mcp.json"
    path.parent.mkdir()
    path.write_text('{"private-token":')
    result = setup(native)
    assert result.exit_code == 1
    assert "private-token" not in result.output
    assert not installed(native).exists()
    assert path.read_text() == '{"private-token":'


def test_setup_credentials_are_stored_only_in_private_mcp(native, monkeypatch):
    monkeypatch.setenv("POWERCONTEXT_MINIMAX_AUTHORIZATION", "secret-value")
    result = setup(native)
    assert result.exit_code == 0, result.output
    private = native[0] / "宿主 profile/mcp.json"
    assert (
        json.loads(private.read_text())["mcpServers"]["powercontext"]["headers"]["Authorization"]
        == "Bearer secret-value"
    )
    assert "secret-value" not in result.output
    for path in (native[0] / "pc").rglob("*"):
        if path.is_file():
            assert b"secret-value" not in path.read_bytes()
    assert b"secret-value" not in (installed(native) / "powercontext.mcp.json").read_bytes()


def test_interactive_selection_contains_minimax(native, monkeypatch):
    monkeypatch.setattr("powercontext.cli.hosts.stdin_is_tty", lambda: True)
    root, command, *_ = native
    result = invoke(
        "setup",
        "--source",
        str(ROOT),
        "--minimax-data-dir",
        str(root / "chosen"),
        "--minimax-command",
        str(command),
        stdin_text="minimax\n",
    )
    assert result.exit_code == 0, result.output
    assert (root / "chosen/plugins/powercontext/.minimax-plugin/plugin.json").is_file()


def test_minimax_specific_options_require_selecting_minimax(native):
    result = invoke("setup", "codex", "--minimax-data-dir", str(native[0] / "wrong"), "--json")
    assert result.exit_code == 2
    assert "requires selecting minimax" in result.output


def test_failure_of_another_host_does_not_prevent_minimax_installation(native, monkeypatch):
    monkeypatch.setattr(
        system.CodexInstaller, "install", Mock(side_effect=installation.SetupError("fixture codex failure"))
    )
    root, command, *_ = native
    result = invoke(
        "setup",
        "codex",
        "minimax",
        "--source",
        str(ROOT),
        "--minimax-data-dir",
        str(root / "mixed"),
        "--minimax-command",
        str(command),
        "--json",
    )
    assert result.exit_code == 1
    rows = json.loads(result.output)["hosts"]
    assert rows["codex"]["status"] == "failed"
    assert rows["minimax"]["ok"] is True


def test_upgrade_removes_only_unchanged_obsolete_managed_files(native):
    source = native[0] / "package"
    shutil.copytree(PACKAGE, source)
    (source / "obsolete.txt").write_text("owned")
    assert setup(native, source=source).exit_code == 0
    (installed(native) / "custom.txt").write_text("user")
    (source / "obsolete.txt").unlink()
    result = setup(native, source=source)
    assert result.exit_code == 0, result.output
    assert not (installed(native) / "obsolete.txt").exists()
    assert (installed(native) / "custom.txt").read_text() == "user"


def test_new_package_file_cannot_overwrite_user_file(native):
    assert setup(native).exit_code == 0
    (installed(native) / "custom.txt").write_text("user")
    source = native[0] / "collision"
    shutil.copytree(PACKAGE, source)
    (source / "custom.txt").write_text("package")
    result = setup(native, source=source)
    assert result.exit_code == 1
    assert "conflict" in result.output
    assert (installed(native) / "custom.txt").read_text() == "user"


def test_configuration_write_failure_preserves_private_and_shared_settings(native, monkeypatch):
    assert setup(native).exit_code == 0
    path = installed(native).parent.parent / "mcp.json"
    original = path.read_bytes()
    settings = native[0] / "client-settings.json"
    original_settings = settings.read_bytes()
    monkeypatch.setenv("POWERCONTEXT_MINIMAX_AUTHORIZATION", "replacement-token")
    write = installation.write_bytes

    def fail_private(target, content):
        if target == path:
            raise OSError("private write failure")  # noqa: TRY003 - simulated disk failure.
        write(target, content)

    monkeypatch.setattr(installation, "write_bytes", fail_private)
    result = setup(native)
    assert result.exit_code == 1
    assert path.read_bytes() == original
    assert settings.read_bytes() == original_settings
    assert "Plugin installation completed; connection configuration failed" in result.output
    assert "replacement-token" not in result.output


def test_changed_endpoint_does_not_forward_existing_credentials(native, monkeypatch):
    monkeypatch.setenv("POWERCONTEXT_MINIMAX_AUTHORIZATION", "old-secret")
    assert setup(native).exit_code == 0
    monkeypatch.delenv("POWERCONTEXT_MINIMAX_AUTHORIZATION")
    path = installed(native).parent.parent / "mcp.json"
    original = path.read_bytes()
    result = setup(native, "--server-url", "https://different.example")
    assert result.exit_code == 1
    assert "provide a new authorization" in result.output
    assert path.read_bytes() == original
    assert "old-secret" not in result.output


def test_remote_ref_selects_package_and_preserves_source_conventions(native, monkeypatch):
    def clone_source(source, ref, target):
        assert source == "oceanbase/powercontext"
        shutil.copytree(PACKAGE, target / "integrations/minimax/plugins/powercontext")
        (target / "integrations/minimax/plugins/powercontext/README.md").write_text(f"selected {ref}")

    monkeypatch.setattr("powercontext.cli.git_source.clone_github_source", clone_source)
    result = setup(native, "--ref", "matching-tag", source="oceanbase/powercontext")
    assert result.exit_code == 0, result.output
    assert (installed(native) / "README.md").read_text() == "selected matching-tag"


def test_config_agent_keeps_the_saved_native_target_and_credentials(native, monkeypatch):
    monkeypatch.setenv("POWERCONTEXT_MINIMAX_AUTHORIZATION", "saved-token")
    assert setup(native).exit_code == 0
    monkeypatch.delenv("POWERCONTEXT_MINIMAX_AUTHORIZATION")
    path = installed(native).parent.parent / "mcp.json"
    original = path.read_bytes()
    result = invoke("config", "agent", "minimax", "--json")
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == original


def test_setup_rejects_remote_http_without_consent_before_install(native):
    result = setup(native, "--server-url", "http://192.0.2.10:8000")
    assert result.exit_code == 1
    assert "--allow-insecure-http" in result.output
    assert not installed(native).exists()


def test_invalid_resource_reference_cannot_escape_package(native):
    source = native[0] / "escape"
    shutil.copytree(PACKAGE, source)
    manifest_path = source / ".minimax-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["skills"].append("../external.md")
    manifest_path.write_text(json.dumps(manifest))
    result = setup(native, source=source)
    assert result.exit_code == 1
    assert "inside its package" in result.output
    assert not installed(native).exists()

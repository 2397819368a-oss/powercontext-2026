# Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0.
"""Managed client consent and recovery; native OS execution is tested separately."""

import importlib
from time import time

import pytest

from tests.codex_plugin.test_image_capture import _append
from tests.codex_plugin.test_image_delivery import _message, _setup
from tests.codex_plugin.test_image_desktop_watch import _index


def test_setup_resolves_current_binding_and_last_disable_removes_lifecycle(image_capture_module, monkeypatch, tmp_path):
    config, _, _, settings, _, _, _ = _setup(image_capture_module, monkeypatch, tmp_path)
    service = importlib.import_module("collector_service")
    scope = importlib.import_module("scope_binding")
    actions = []
    bindings = {}
    monkeypatch.setattr(scope, "resolve_scope_id", lambda *args, **kwargs: "active-binding")
    monkeypatch.setattr(
        scope, "bind_scope", lambda workspace, selected, **kwargs: bindings.update({workspace: selected})
    )
    monkeypatch.setattr(
        scope,
        "_post_json",
        lambda *args, **kwargs: dict.fromkeys(("enabled", "available", "vision_configured", "worker_running"), True),
    )
    monkeypatch.setattr(service, "install", lambda: actions.append("install"))
    monkeypatch.setattr(service, "uninstall", lambda: actions.append("uninstall"))
    other = tmp_path / "second-project"
    other.mkdir()
    config.configure_capture("setup", str(tmp_path), None, settings)
    config.configure_capture("setup", str(other), None, settings)
    assert config.enabled_profile(tmp_path, settings.server_url)["scope_id"] == "active-binding"
    assert bindings == {str(tmp_path): "active-binding", str(other): "active-binding"}
    config.configure_capture("disable", str(tmp_path), None, settings)
    assert "uninstall" not in actions and config.enabled_profile(other, settings.server_url)
    config.configure_capture("disable", str(other), None, settings)
    assert not config.any_enabled() and actions[-1] == "uninstall"


def test_setup_refuses_unready_server_without_authorizing_capture(image_capture_module, monkeypatch, tmp_path):
    config, _, _, settings, _, _, _ = _setup(image_capture_module, monkeypatch, tmp_path)
    scope = importlib.import_module("scope_binding")
    monkeypatch.setattr(scope, "resolve_scope_id", lambda *args, **kwargs: "active-binding")
    monkeypatch.setattr(scope, "_post_json", lambda *args, **kwargs: {"enabled": False})
    config.write_profile(tmp_path, server_url=settings.server_url, scope_id="active-binding", enabled=False)
    with pytest.raises(ValueError, match="image_server_not_ready"):
        config.configure_capture("setup", str(tmp_path), None, settings)
    assert not config.any_enabled()


def test_desktop_session_scope_switch_preserves_staged_original_destination(
    image_capture_module, monkeypatch, tmp_path
):
    config, outbox, worker, settings, path, _, requests = _setup(image_capture_module, monkeypatch, tmp_path)
    watcher = importlib.import_module("image_desktop_watch")
    current = {"scope": "session-first"}
    monkeypatch.setattr(watcher, "resolve_scope_id", lambda *args, **kwargs: current["scope"])
    _index(config, path, tmp_path, time())
    _append(path, "turn_context", {"turn_id": "first"})
    _message(path, "first-image", [b"\x89PNG\r\n\x1a\nfirst"])
    original_post = worker._post_json
    monkeypatch.setattr(worker, "_post_json", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    watcher.poll(outbox, settings, began=time() - 10)
    assert requests == []
    current["scope"] = "session-second"
    monkeypatch.setattr(worker, "_post_json", original_post)
    outbox.reset_delivery(config.workspace_key(tmp_path))
    _append(path, "turn_context", {"turn_id": "second"})
    _message(path, "second-image", [b"\x89PNG\r\n\x1a\nsecond"])
    watcher.poll(outbox, settings, began=time() - 10)
    assert {request["origin"]["message_id"]: request["scope_id"] for request in requests} == {
        "first-image": "session-first",
        "second-image": "session-second",
    }


def test_scope_service_outage_leaves_cursor_recoverable(image_capture_module, monkeypatch, tmp_path):
    config, outbox, _, settings, path, _, requests = _setup(image_capture_module, monkeypatch, tmp_path)
    watcher = importlib.import_module("image_desktop_watch")
    _index(config, path, tmp_path, time())
    _append(path, "turn_context", {"turn_id": "first"})
    _message(path, "image", [b"\x89PNG\r\n\x1a\nfirst"])

    def unavailable(*args, **kwargs):
        raise watcher.ScopeBindingError

    monkeypatch.setattr(watcher, "resolve_scope_id", unavailable)
    assert watcher.poll(outbox, settings, began=time() - 10) == "image_scope_unavailable"
    assert not requests
    monkeypatch.setattr(watcher, "resolve_scope_id", lambda *args, **kwargs: "recovered-scope")
    watcher.poll(outbox, settings, began=time() - 10)
    assert len(requests) == 1 and requests[0]["scope_id"] == "recovered-scope"


def test_manager_will_not_replace_foreign_registration(image_capture_module, monkeypatch, tmp_path):
    service = importlib.import_module("collector_service")
    path = tmp_path / "another-owner.service"
    path.write_bytes(b"another owner's service")
    monkeypatch.setattr(service, "_artifact", lambda: path)
    monkeypatch.setattr(service.sys, "platform", "linux")
    with pytest.raises(ValueError, match="image_collector_registration_foreign"):
        service.install()
    assert path.read_bytes() == b"another owner's service"


def test_image_consent_does_not_override_ordinary_scope_settings(image_capture_module, monkeypatch, tmp_path):
    _setup(image_capture_module, monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    restored = image_capture_module.CodexPluginSettings()
    assert restored.capture_images and restored.scope_id is None
    monkeypatch.setenv("POWERCONTEXT_CODEX_SCOPE_ID", "explicit-session-scope")
    assert image_capture_module.CodexPluginSettings().scope_id == "explicit-session-scope"

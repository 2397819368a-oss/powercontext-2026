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

# ruff: noqa: TRY003

"""Install the native MiniMax package and inspect its effective connection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

from powercontext.cli.authorization import normalize_authorization, setup_authorization_value
from powercontext.cli.installation import SetupError, publish
from powercontext.client.transport_policy import load_client_settings, normalize_client_url
from powercontext.paths import powercontext_data_dir

_MANIFEST = ".minimax-plugin/plugin.json"
_SKILL = "skills/powercontext-project-context/SKILL.md"
_MCP = "powercontext.mcp.json"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise SetupError("MiniMax configuration must not be a symbolic link")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError):
        raise SetupError("Cannot read MiniMax JSON configuration; repair it before setup") from None
    if not isinstance(value, dict):
        raise SetupError("MiniMax configuration must contain a JSON object")
    return value


def _run(command: str, arguments: list[str], data_dir: Path | None = None) -> object:
    environment = dict(os.environ)
    if data_dir is not None:
        environment["MINIMAX_DATA_DIR"] = str(data_dir)
    try:
        result = subprocess.run(  # noqa: S603 - the user selects the native launcher, without a shell.
            [command, "plugin", *arguments, "--json"],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise SetupError("MiniMax CLI could not run or timed out; check --minimax-command") from None
    if result.returncode:
        raise SetupError("MiniMax CLI failed; inspect the selected installation with mcode plugin --help")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise SetupError("MiniMax CLI returned invalid JSON; use a supported CLI build") from None


def _command(explicit: Path | None, saved: dict[str, Any], data_dir: Path | None) -> str | None:
    configured = explicit or (saved.get("minimax_command") if str(data_dir) == saved.get("minimax_data_dir") else None)
    if configured:
        candidate = Path(configured).expanduser().absolute()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise SetupError("MiniMax launcher is unavailable; correct --minimax-command")
        return str(candidate)
    return shutil.which("mcode")


def _data_dir(explicit: Path | None, saved: dict[str, Any], command: str | None) -> Path:
    override = (
        explicit or os.environ.get("MINIMAX_DATA_DIR", "").strip() or os.environ.get("MAVIS_DATA_DIR", "").strip()
    )
    if override:
        return Path(override).expanduser().resolve()
    if saved.get("minimax_data_dir"):
        return Path(saved["minimax_data_dir"]).expanduser().resolve()
    if command:
        marketplaces = _run(command, ["marketplace", "list"])
        if isinstance(marketplaces, list):
            local = [entry for entry in marketplaces if isinstance(entry, dict) and entry.get("name") == "local"]
            path = local[0].get("path") if len(local) == 1 else None
            if isinstance(path, str) and path:
                return Path(path).expanduser().resolve().parent
        raise SetupError("Cannot determine MiniMax data directory; specify --minimax-data-dir")
    if os.environ.get("MAVIS_PROFILE", "").strip():
        raise SetupError("MiniMax profile requires --minimax-data-dir when mcode is unavailable")
    return Path.home() / ".minimax"


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SetupError("MiniMax package contains an invalid resource path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or ":" in value:
        raise SetupError("MiniMax package resource must stay inside its package")
    return value


def _files(package: Path) -> dict[str, str]:
    files = {}
    for path in package.rglob("*"):
        if path.is_symlink():
            raise SetupError("MiniMax package contains a symbolic link; resolve it before installation")
        if path.is_file():
            files[path.relative_to(package).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def require_minimax_package(package: Path) -> dict[str, str]:
    """Validate the native manifest and its complete, portable resources before writes."""
    manifest = _object(package / _MANIFEST)
    if manifest.get("name") != "powercontext" or manifest.get("schemaVersion") != 1:
        raise SetupError("Expected a schemaVersion 1 native powercontext MiniMax manifest")
    resources = [_MANIFEST, "LICENSE", manifest.get("icon")]
    for field, required in (("skills", _SKILL), ("mcpServers", _MCP)):
        references = manifest.get(field)
        if not isinstance(references, list) or required not in references:
            raise SetupError("MiniMax manifest omits the required Skill or MCP configuration")
        resources.extend(references)
    for resource in resources:
        relative = _relative(resource)
        if not (package / relative).is_file():
            raise SetupError(f"MiniMax package is incomplete: missing {relative}")
    config = _object(package / _MCP)
    if config.get("schemaVersion") != 1:
        raise SetupError("Unsupported MiniMax MCP configuration schema")
    _mcp_entry(config, required=True)
    guidance = _object(package / "skills/powercontext-project-context/.powercontext-guidance.json")
    if guidance.get("schema_version") != 1 or not isinstance(guidance.get("resources"), list):
        raise SetupError("MiniMax Skill resource inventory is missing")
    for resource in guidance["resources"]:
        path = package / "skills/powercontext-project-context" / _relative(resource)
        if not path.is_file():
            raise SetupError("MiniMax Skill reference is missing")
    return _files(package)


def _mcp_entry(config: dict[str, Any], *, required: bool = False) -> dict[str, Any] | None:
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError("MiniMax mcpServers must be a JSON object")
    entry = servers.get("powercontext")
    if entry is None and not required:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "streamable-http":
        raise SetupError("MiniMax powercontext MCP entry must use streamable-http")
    if not isinstance(entry.get("enabled", True), bool):
        raise SetupError("MiniMax MCP enabled must be a boolean")
    _endpoint(entry)
    headers = entry.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
    ):
        raise SetupError("MiniMax MCP headers must be a string-valued object")
    if any("${" in value for value in headers.values()):
        raise SetupError("MiniMax private MCP headers must contain literal values")
    authorization = [value for key, value in headers.items() if key.lower() == "authorization"]
    if len(authorization) > 1:
        raise SetupError("MiniMax MCP has duplicate Authorization headers")
    if authorization:
        normalize_authorization(authorization[0])
    return entry


def _endpoint(entry: dict[str, Any]) -> str:
    value = entry.get("url")
    if not isinstance(value, str) or "${" in value:
        raise SetupError("MiniMax MCP URL must be a literal HTTP(S) endpoint")
    endpoint = normalize_client_url(value)
    if not endpoint.endswith("/mcp"):
        raise SetupError("MiniMax powercontext MCP URL must end in /mcp")
    return endpoint.removesuffix("/mcp")


def _verify_discovery(result: object, package: Path) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("installed"), list):
        raise SetupError("MiniMax plugin list returned an unsupported schema")
    result = cast(dict[str, Any], result)
    matches = [
        entry
        for entry in result["installed"]
        if isinstance(entry, dict) and entry.get("name") == "powercontext" and entry.get("marketplaceName") == "local"
    ]
    if len(matches) != 1 or matches[0].get("installed") is not True:
        raise SetupError(
            "MiniMax omitted the installed local powercontext plugin; check its manifest and selected directory"
        )
    plugin = matches[0]
    if plugin.get("enabled") is not True:
        raise SetupError(
            "MiniMax powercontext plugin is disabled; enable powercontext@local in the selected MiniMax installation"
        )
    manifest = _object(package / _MANIFEST)
    if plugin.get("version") != manifest.get("version"):
        raise SetupError("MiniMax discovered a different plugin version; refresh or restart the selected host")
    counts = plugin.get("capabilities", {})
    if not isinstance(counts, dict) or any(
        type(counts.get(key)) is not int or counts[key] < len(manifest[field])
        for key, field in (("skillCount", "skills"), ("mcpServerCount", "mcpServers"))
    ):
        raise SetupError("MiniMax did not discover the required Skill and MCP resources")


class MiniMaxInstaller:
    def __init__(self, *, minimax_data_dir: Path | None = None, minimax_command: Path | None = None) -> None:
        saved = load_client_settings("minimax")
        override = (
            minimax_data_dir
            or os.environ.get("MINIMAX_DATA_DIR", "").strip()
            or os.environ.get("MAVIS_DATA_DIR", "").strip()
        )
        selected = (
            Path(override).expanduser().resolve()
            if override
            else Path(saved["minimax_data_dir"])
            if saved.get("minimax_data_dir")
            else None
        )
        self.command = _command(minimax_command, saved, selected)
        self.data_dir = _data_dir(minimax_data_dir, saved, self.command)
        self.plugin = self.data_dir / "plugins/powercontext"
        identity = hashlib.sha256(str(self.data_dir).encode()).hexdigest()
        self.record = powercontext_data_dir() / "installations/minimax" / f"{identity}.json"

    def native_entry(self) -> dict[str, Any] | None:
        """Resolve the native user override before the bundled plugin connection."""
        entry = _mcp_entry(_object(self.data_dir / "mcp.json"))
        if entry is not None and entry.get("enabled", True):
            return entry
        return _mcp_entry(_object(self.plugin / _MCP))

    def native_endpoint(self) -> str | None:
        entry = self.native_entry()
        return _endpoint(entry) if entry is not None else None

    def install(self, source) -> dict[str, Any]:  # noqa: C901 - reconcile one managed package before publication.
        self.native_entry()  # Malformed private configuration must fail before publication.
        if self.plugin.is_symlink() or self.plugin.parent.is_symlink():
            raise SetupError("MiniMax plugin installation must not replace a symbolic link")
        with source.package("minimax") as package:
            incoming = require_minimax_package(package)
            previous = _object(self.record)
            if previous and (previous.get("version") != 1 or previous.get("data_dir") != str(self.data_dir)):
                raise SetupError("Unsupported MiniMax installation record; restore it before setup")
            owned = previous.get("files", {})
            if not isinstance(owned, dict):
                raise SetupError("Invalid MiniMax managed file inventory")
            owned = {_relative(name): digest for name, digest in owned.items()}
            current = _files(self.plugin) if self.plugin.is_dir() else {}
            if self.plugin.exists() and not self.plugin.is_dir():
                raise SetupError("MiniMax plugin destination must be a directory")
            if current and not previous:
                if any(current.get(name) != digest for name, digest in incoming.items()):
                    raise SetupError(
                        "Unmanaged MiniMax installation differs; back it up outside plugins and migrate before setup"
                    )
                owned = incoming
            for name in owned.keys() | incoming.keys():
                digest = current.get(name)
                if digest is not None and digest != owned.get(name) and digest != incoming.get(name):
                    raise SetupError(f"MiniMax installation conflict: {name}; preserve your changes before setup")
            record = {
                "version": 1,
                "data_dir": str(self.data_dir),
                "files": incoming,
                "source": source.location,
                "ref": source.ref,
            }
            self.plugin.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".powercontext-", dir=self.plugin.parent) as temporary:
                staged = Path(temporary) / "powercontext"
                if self.plugin.exists():
                    shutil.copytree(self.plugin, staged)
                else:
                    staged.mkdir()
                for name in owned.keys() - incoming.keys():
                    (staged / name).unlink(missing_ok=True)
                shutil.copytree(package, staged, dirs_exist_ok=True)
                publish(replacements=[(staged, self.plugin)], writes={self.record: _json_bytes(record)})
        return {"plugin_path": str(self.plugin), "data_dir": str(self.data_dir), "files": "installed"}

    def configure(self, connection) -> dict[str, str]:
        from powercontext.cli.transport import setup_transport_updates

        path = self.data_dir / "mcp.json"
        config = _object(path)
        existing = _mcp_entry(config)
        endpoint = connection.server_url + "/mcp"
        entry: dict[str, Any] = dict(existing or {"type": "streamable-http"})
        headers = dict(entry.get("headers", {}))
        authorization = setup_authorization_value("minimax")
        state = "preserved" if any(key.lower() == "authorization" for key in headers) else "not_configured"
        if authorization and authorization.strip():
            headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
            headers["Authorization"] = normalize_authorization(authorization)
            state = "configured"
        elif existing and _endpoint(existing) != connection.server_url and state == "preserved":
            raise SetupError(
                "MiniMax endpoint changed; provide a new authorization explicitly before moving credentials"
            )
        entry.update(type="streamable-http", url=endpoint)
        if headers:
            entry["headers"] = headers
        config.setdefault("mcpServers", {})["powercontext"] = entry
        updates = setup_transport_updates(connection)
        for key, content in list(updates.items()):
            saved = json.loads(content)
            saved["hosts"]["minimax"].update(minimax_data_dir=str(self.data_dir), minimax_command=self.command)
            updates[key] = _json_bytes(saved)
        updates[path] = _json_bytes(config)
        mode = stat.S_IMODE(path.stat().st_mode) & 0o600 if path.exists() else 0o600
        publish(writes=updates, action=lambda: path.chmod(mode) if os.name != "nt" else None)
        return {"authorization_state": state}

    def verify(self):
        from powercontext.cli.system import Diagnostic, DiagnosticStatus

        checks = {}
        try:
            require_minimax_package(self.plugin)
        except (OSError, ValueError, SetupError) as error:
            checks["package"] = Diagnostic(DiagnosticStatus.FAILED, str(error))
        else:
            checks["package"] = Diagnostic(
                DiagnosticStatus.OK, "Native manifest and all packaged resources are present"
            )
        if self.command is None:
            checks["native"] = Diagnostic(
                DiagnosticStatus.DEGRADED,
                "Files only: mcode is unavailable; install the CLI or set --minimax-command, then rerun doctor",
            )
            return checks
        try:
            result = _run(self.command, ["list", "--marketplace", "local"], self.data_dir)
            _verify_discovery(result, self.plugin)
            checks["native"] = Diagnostic(
                DiagnosticStatus.OK,
                "Native local plugin is installed and enabled; host reports the required Skill/MCP counts "
                "(not a connection test). Keep Server running; refresh /plugins or restart MiniMax using the selected data directory.",
            )
        except (OSError, ValueError, SetupError) as error:
            checks["native"] = Diagnostic(DiagnosticStatus.FAILED, str(error))
        return checks

    def server_checks(self):
        from powercontext.cli.system import Diagnostic, DiagnosticStatus, server_diagnostics
        from powercontext.client import PowerContextClient
        from powercontext.client.errors import ServerResponseError

        entry = self.native_entry()
        if entry is None:
            raise SetupError("MiniMax has no effective PowerContext MCP endpoint")
        endpoint = _endpoint(entry)
        saved = load_client_settings("minimax")
        allowed = bool(saved.get("allow_insecure_http")) and saved.get("server_url") == endpoint
        checks = server_diagnostics(server_url=endpoint, allow_insecure_http=allowed)

        async def probe():
            import httpx

            # Only the native configuration supplies headers; never inherit a global token.
            transport = httpx.AsyncHTTPTransport()
            async with (
                httpx.AsyncClient(
                    headers=entry.get("headers", {}), transport=transport, timeout=3, follow_redirects=False
                ) as http,
                PowerContextClient(endpoint, http_client=http, allow_insecure_http=allowed) as client,
            ):
                await client.get_server_info()

        try:
            asyncio.run(probe())
        except ServerResponseError as error:
            checks["server_access"] = Diagnostic(
                DiagnosticStatus.FAILED,
                f"CLI Server access returned HTTP {error.status_code}; check native MCP credentials and permissions",
            )
        except Exception:
            checks["server_access"] = Diagnostic(
                DiagnosticStatus.FAILED, "CLI could not check Server access using the native MCP configuration"
            )
        else:
            checks["server_access"] = Diagnostic(
                DiagnosticStatus.OK,
                "CLI reached Server info using native headers; MiniMax MCP connection and model calls were not tested",
            )
        return checks

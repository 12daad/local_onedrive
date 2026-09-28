from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


class _ConfigurationState:
    storage: dict[str, object] | None = None
    user: dict[str, object] | None = None
    events: list[str]

    def __init__(self) -> None:
        self.events = []


class _ConfigurationHandler(BaseHTTPRequestHandler):
    server: _ConfigurationServer

    def log_message(self, *_: object) -> None:
        pass

    def _json_response(self, data: object = None) -> None:
        payload = json.dumps({"code": 200, "message": "success", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        if self.path == "/ping":
            body = b"pong"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/admin/driver/info"):
            self._json_response(
                {"additional": [{"name": "cookie"}, {"name": "root_folder_id"}]}
            )
        elif self.path.startswith("/api/admin/storage/list"):
            content = [] if self.server.state.storage is None else [self.server.state.storage]
            self._json_response({"content": content, "total": len(content)})
        elif self.path.startswith("/api/admin/user/list"):
            content = [] if self.server.state.user is None else [self.server.state.user]
            self._json_response({"content": content, "total": len(content)})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/api/auth/login":
            assert body == {"username": "admin", "password": "admin-test-secret"}
            self._json_response({"token": "admin-test-token"})
        elif self.path == "/api/admin/user/create":
            self.server.state.events.append("user")
            self.server.state.user = {**body, "id": 23}
            self._json_response()
        elif self.path == "/api/admin/user/update":
            self.server.state.events.append("user")
            self.server.state.user = body
            self._json_response()
        elif self.path == "/api/admin/storage/create":
            self.server.state.events.append("storage")
            self.server.state.storage = {**body, "id": 17}
            self._json_response({"id": 17})
        elif self.path == "/api/admin/storage/update":
            self.server.state.events.append("storage")
            self.server.state.storage = body
            self._json_response()
        else:
            self.send_error(404)

    def do_PROPFIND(self) -> None:
        expected = "Basic cXN5bmM6d2ViZGF2LXRlc3Qtc2VjcmV0"
        assert self.headers.get("Authorization") == expected
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>'
        self.send_response(207)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ConfigurationServer(ThreadingHTTPServer):
    state: _ConfigurationState


@pytest.mark.skipif(os.name != "nt", reason="PowerShell release scripts are Windows-only")
def test_release_quarkconfig_creates_user_before_mount_and_redacts_secrets() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")

    state = _ConfigurationState()
    server = _ConfigurationServer(("127.0.0.1", 0), _ConfigurationHandler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "release-quarkconfig.ps1"
    env = os.environ.copy()
    env.update(
        {
            "QUARKSYNC_QUARK_COOKIE": "sid=quark-test-cookie",
            "OPENLIST_ADMIN_USERNAME": "admin",
            "OPENLIST_ADMIN_PASSWORD": "admin-test-secret",
            "QUARKSYNC_WEBDAV_USERNAME": "qsync",
            "QUARKSYNC_WEBDAV_PASSWORD": "webdav-test-secret",
        }
    )
    command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-OpenListBaseUrl",
        f"http://127.0.0.1:{server.server_port}",
        "-MountPath",
        "/dav/Users//hongbo",
        "-UseExistingCookie",
        "-NonInteractive",
        "-NoBrowser",
        "-NoPersistEnvironment",
    ]
    try:
        results = [
            subprocess.run(  # noqa: S603 - trusted PowerShell executable and local script
                command,
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            for _ in range(2)
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    for result in results:
        assert result.returncode == 0, result.stdout + result.stderr
    assert "Created WebDAV user" in results[0].stdout
    assert "Mounted Quark storage" in results[0].stdout
    assert "Updated WebDAV user" in results[1].stdout
    assert "Updated Quark storage" in results[1].stdout
    assert state.events == ["user", "storage", "user", "storage"]

    combined_output = "".join(result.stdout + result.stderr for result in results)
    for secret in (
        "sid=quark-test-cookie",
        "admin-test-secret",
        "webdav-test-secret",
        "admin-test-token",
    ):
        assert secret not in combined_output

    assert state.user is not None
    assert state.user["username"] == "qsync"
    assert state.user["base_path"] == "/Users/hongbo"
    assert state.user["permission"] == 1016
    assert state.storage is not None
    assert state.storage["mount_path"] == "/Users/hongbo"
    assert state.storage["driver"] == "Quark"
    assert state.storage["webdav_policy"] == "native_proxy"
    addition = json.loads(str(state.storage["addition"]))
    assert addition["cookie"] == "sid=quark-test-cookie"
    assert addition["root_folder_id"] == "0"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell release scripts are Windows-only")
def test_release_scripts_parse_and_old_bootstrap_is_removed() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    repo = Path(__file__).resolve().parents[2]
    assert not (repo / "scripts" / "bootstrap-openlist-webdav.ps1").exists()
    for name in ("release-setup.ps1", "release-quarkconfig.ps1"):
        result = subprocess.run(  # noqa: S603 - trusted PowerShell executable
            [
                powershell,
                "-NoProfile",
                "-Command",
                "$e=$null;[Management.Automation.Language.Parser]::ParseFile(" 
                f"'{repo / 'scripts' / name}',[ref]$null,[ref]$e)|Out-Null;"
                "if($e.Count){$e|Out-String|Write-Error;exit 1}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

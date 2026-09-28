from __future__ import annotations

import pytest

import quarksync.config as config_module
from quarksync.config import ConfigError, Settings


def test_remote_http_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARKSYNC_WEBDAV_URL", "http://192.168.1.2/dav/")
    monkeypatch.setenv("QUARKSYNC_WEBDAV_USERNAME", "user")
    monkeypatch.setenv("QUARKSYNC_WEBDAV_PASSWORD", "secret")
    monkeypatch.setenv("QUARKSYNC_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(ConfigError, match="require HTTPS"):
        Settings.from_env()


def test_localhost_http_is_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARKSYNC_WEBDAV_URL", "http://127.0.0.1:5244/dav/")
    monkeypatch.setenv("QUARKSYNC_WEBDAV_USERNAME", "user")
    monkeypatch.setenv("QUARKSYNC_WEBDAV_PASSWORD", "secret")
    monkeypatch.setenv("QUARKSYNC_DATA_DIR", str(tmp_path / "data"))
    settings = Settings.from_env()
    assert settings.tls_verify is True


def test_internal_state_must_not_overlap_sync_root(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARKSYNC_WEBDAV_URL", "http://127.0.0.1:5244/dav/")
    monkeypatch.setenv("QUARKSYNC_WEBDAV_USERNAME", "user")
    monkeypatch.setenv("QUARKSYNC_WEBDAV_PASSWORD", "secret")
    monkeypatch.setenv("QUARKSYNC_ROOT", str(tmp_path / "sync"))
    monkeypatch.setenv("QUARKSYNC_DATA_DIR", str(tmp_path / "sync" / ".state"))
    with pytest.raises(ConfigError, match="must not overlap"):
        Settings.from_env()


def test_default_paths_preserve_legacy_journal_but_use_canonical_logs(
    monkeypatch, tmp_path
):
    canonical = tmp_path / "QuarkSync"
    legacy = canonical / "QuarkSync"
    legacy.mkdir(parents=True)
    (legacy / "state.db").touch()

    def fake_user_data_path(
        _name: str, *, appauthor: bool | None = None
    ) -> str:
        return str(canonical if appauthor is False else legacy)

    monkeypatch.setattr(config_module, "user_data_path", fake_user_data_path)
    for name in (
        "QUARKSYNC_DATA_DIR",
        "QUARKSYNC_CACHE_DIR",
        "QUARKSYNC_STATE_DB",
        "QUARKSYNC_LOG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env(require_credentials=False)

    assert settings.data_dir == legacy
    assert settings.state_db == legacy / "state.db"
    assert settings.log_dir == canonical / "logs"

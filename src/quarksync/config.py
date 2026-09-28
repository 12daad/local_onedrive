from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from platformdirs import user_data_path


class ConfigError(ValueError):
    """Raised when configuration is unsafe or incomplete."""


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    value = default if raw is None else int(raw)
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _float(name: str, default: float, *, minimum: float = 0) -> float:
    raw = os.getenv(name)
    value = default if raw is None else float(raw)
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _default_sync_root() -> Path:
    return Path.home() / "QuarkSync"


def _default_data_dir() -> Path:
    canonical = Path(user_data_path("QuarkSync", appauthor=False))
    legacy = Path(user_data_path("QuarkSync"))
    # Preserve an existing journal created by the older platformdirs default.
    if (legacy / "state.db").exists() and not (canonical / "state.db").exists():
        return legacy
    return canonical


@dataclass(frozen=True, slots=True)
class Settings:
    webdav_url: str
    webdav_username: str
    webdav_password: str
    remote_root: str
    sync_root: Path
    data_dir: Path
    cache_dir: Path
    state_db: Path
    log_dir: Path
    cache_max_gb: float = 100
    cache_min_free_gb: float = 20
    allow_insecure_http: bool = False
    tls_verify: bool = True
    writeback_delay_seconds: int = 60
    sync_interval_seconds: int = 300
    remote_poll_seconds: int = 120
    heartbeat_seconds: int = 60
    delete_grace_seconds: int = 86400
    max_uploads: int = 3
    max_downloads: int = 3
    max_backoff_seconds: int = 1800
    ipc_timeout_seconds: int = 120
    ipc_connect_timeout_seconds: int = 10
    hydration_timeout_seconds: int = 86400
    log_level: str = "INFO"
    device_name: str = socket.gethostname()
    provider_path: Path | None = None
    allow_core_only: bool = False

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> Settings:
        configured_data_dir = os.getenv("QUARKSYNC_DATA_DIR")
        data_dir = Path(configured_data_dir) if configured_data_dir else _default_data_dir()
        url = os.getenv("QUARKSYNC_WEBDAV_URL", "")
        username = os.getenv("QUARKSYNC_WEBDAV_USERNAME", "")
        password = os.getenv("QUARKSYNC_WEBDAV_PASSWORD", "")
        if require_credentials and not all((url, username, password)):
            raise ConfigError(
                "QUARKSYNC_WEBDAV_URL, QUARKSYNC_WEBDAV_USERNAME and "
                "QUARKSYNC_WEBDAV_PASSWORD are required"
            )
        settings = cls(
            webdav_url=url,
            webdav_username=username,
            webdav_password=password,
            remote_root=os.getenv("QUARKSYNC_REMOTE_ROOT", "").strip("/"),
            sync_root=Path(os.getenv("QUARKSYNC_ROOT", _default_sync_root())),
            data_dir=data_dir,
            cache_dir=Path(os.getenv("QUARKSYNC_CACHE_DIR", data_dir / "cache")),
            state_db=Path(os.getenv("QUARKSYNC_STATE_DB", data_dir / "state.db")),
            log_dir=Path(
                os.getenv(
                    "QUARKSYNC_LOG_DIR",
                    Path(user_data_path("QuarkSync", appauthor=False)) / "logs",
                )
            ),
            cache_max_gb=_float("QUARKSYNC_CACHE_MAX_GB", 100),
            cache_min_free_gb=_float("QUARKSYNC_CACHE_MIN_FREE_GB", 20),
            allow_insecure_http=_bool("QUARKSYNC_ALLOW_INSECURE_HTTP", False),
            tls_verify=_bool("QUARKSYNC_TLS_VERIFY", True),
            writeback_delay_seconds=_int("QUARKSYNC_WRITEBACK_DELAY_SECONDS", 60),
            sync_interval_seconds=_int("QUARKSYNC_SYNC_INTERVAL_SECONDS", 300, minimum=1),
            remote_poll_seconds=_int("QUARKSYNC_REMOTE_POLL_SECONDS", 120, minimum=1),
            heartbeat_seconds=_int("QUARKSYNC_HEARTBEAT_SECONDS", 60, minimum=1),
            delete_grace_seconds=_int("QUARKSYNC_DELETE_GRACE_SECONDS", 86400),
            max_uploads=_int("QUARKSYNC_MAX_UPLOADS", 3, minimum=1),
            max_downloads=_int("QUARKSYNC_MAX_DOWNLOADS", 3, minimum=1),
            max_backoff_seconds=_int("QUARKSYNC_MAX_BACKOFF_SECONDS", 1800, minimum=5),
            ipc_timeout_seconds=_int("QUARKSYNC_IPC_TIMEOUT_SECONDS", 120, minimum=5),
            ipc_connect_timeout_seconds=_int(
                "QUARKSYNC_IPC_CONNECT_TIMEOUT_SECONDS", 10, minimum=1
            ),
            hydration_timeout_seconds=_int(
                "QUARKSYNC_HYDRATION_TIMEOUT_SECONDS", 86400, minimum=60
            ),
            log_level=os.getenv("QUARKSYNC_LOG_LEVEL", "INFO").upper(),
            device_name=os.getenv("QUARKSYNC_DEVICE_NAME", socket.gethostname()),
            provider_path=(
                Path(os.environ["QUARKSYNC_PROVIDER_PATH"])
                if os.getenv("QUARKSYNC_PROVIDER_PATH")
                else None
            ),
            allow_core_only=_bool("QUARKSYNC_ALLOW_CORE_ONLY", False),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.webdav_url:
            return
        parsed = urlsplit(self.webdav_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigError("QUARKSYNC_WEBDAV_URL must be an absolute HTTP(S) URL")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme == "http"
            and parsed.hostname.lower() not in local_hosts
            and not self.allow_insecure_http
        ):
            raise ConfigError(
                "Remote WebDAV endpoints require HTTPS; set "
                "QUARKSYNC_ALLOW_INSECURE_HTTP=true only if the risk is accepted"
            )
        sync_root = self.sync_root.resolve(strict=False)
        internal_paths = {
            "data directory": self.data_dir.resolve(strict=False),
            "cache directory": self.cache_dir.resolve(strict=False),
            "log directory": self.log_dir.resolve(strict=False),
            "database directory": self.state_db.parent.resolve(strict=False),
        }
        for label, path in internal_paths.items():
            if path == sync_root or path in sync_root.parents or sync_root in path.parents:
                raise ConfigError(f"{label.capitalize()} must not overlap the sync root")

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.cache_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)

"""Environment and filesystem configuration for durable jobs."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

DURABLE_ENV_VAR = "CHAINWAKE_DURABLE"
DURABLE_CONTEXT_ENV_VAR = "CHAINWAKE_DURABLE_CONTEXT"
DURABLE_ARGV_ENV_VAR = "CHAINWAKE_DURABLE_ARGV"
HOME_ENV_VAR = "CHAINWAKE_HOME"
DATABASE_ENV_VAR = "CHAINWAKE_JOB_DB"
NO_AUTOSTART_ENV_VAR = "CHAINWAKE_NO_AUTOSTART"
_PRIVATE_DIRECTORY_MARKER = ".chainwake-private"


def ensure_private_directory(path: Path) -> Path:
    """Create a private state directory without mutating an existing shared path."""
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform == "win32":
        marker = path / _PRIVATE_DIRECTORY_MARKER
        if existed and not marker.is_file():
            raise PermissionError(
                f"existing Chainwake state directory {path} has no private ownership marker"
            )
        if not existed:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        return path
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        ownership = "existing" if existed else "new"
        raise PermissionError(
            f"{ownership} Chainwake state directory {path} must be private (mode 0700)"
        )
    return path


def ensure_private_file(path: Path) -> Path:
    """Restrict an existing Chainwake state file to the current user."""
    if path.exists():
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    return path


def chainwake_home() -> Path:
    """Return the per-user state directory, with an explicit test/operator override."""
    configured = os.environ.get(HOME_ENV_VAR)
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Chainwake"
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Chainwake"
        return Path.home() / "AppData" / "Local" / "Chainwake"
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / "chainwake"
    return Path.home() / ".local" / "state" / "chainwake"


def job_database_path() -> Path:
    """Return the configured durable-job database path."""
    configured = os.environ.get(DATABASE_ENV_VAR)
    if configured:
        return Path(configured).expanduser()
    return chainwake_home() / "jobs.sqlite3"


__all__ = [
    "DATABASE_ENV_VAR",
    "DURABLE_ARGV_ENV_VAR",
    "DURABLE_CONTEXT_ENV_VAR",
    "DURABLE_ENV_VAR",
    "HOME_ENV_VAR",
    "NO_AUTOSTART_ENV_VAR",
    "chainwake_home",
    "ensure_private_directory",
    "ensure_private_file",
    "job_database_path",
]

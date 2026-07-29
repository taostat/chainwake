"""Lightweight supervisor for isolated durable watcher processes."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

from chainwake.jobs.config import (
    DATABASE_ENV_VAR,
    NO_AUTOSTART_ENV_VAR,
    chainwake_home,
    ensure_private_directory,
    ensure_private_file,
    job_database_path,
)
from chainwake.jobs.models import JobState
from chainwake.jobs.store import JobStore

_SCAN_SECONDS = 0.5
_START_TIMEOUT_SECONDS = 3.0


def is_process_alive(pid: int) -> bool:
    """Return whether a process exists without sending it a signal."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_identity(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_identity(pid: int) -> str | None:
    """Read a Windows process creation timestamp with the native process API."""
    win_dll = cast(Any, getattr(ctypes, "WinDLL", None))
    if win_dll is None:
        return None
    kernel32 = win_dll("kernel32", use_last_error=True)
    filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        filetime_pointer,
        filetime_pointer,
        filetime_pointer,
        filetime_pointer,
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        timestamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return f"windows:{timestamp}"
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_identity(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    identity = result.stdout.strip()
    return identity or None


def process_identity(pid: int) -> str | None:
    """Return a cross-platform process-start fingerprint so PID reuse is safe."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_process_identity(pid)
    if not is_process_alive(pid):
        return None
    return _posix_process_identity(pid)


def _pid_path() -> Path:
    return chainwake_home() / "daemon.pid"


def _read_pid_record(path: Path) -> tuple[int, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        pid = int(value["pid"])
        identity = str(value["identity"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return pid, identity


def daemon_pid() -> int | None:
    record = _read_pid_record(_pid_path())
    if record is None:
        return None
    pid, identity = record
    return pid if process_identity(pid) == identity else None


def daemon_running() -> bool:
    return daemon_pid() is not None


def _claim_daemon_pid() -> bool:
    path = _pid_path()
    ensure_private_directory(path.parent)
    own_identity = process_identity(os.getpid())
    if own_identity is None:
        return False
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                with path.open("rb") as existing:
                    inode = os.fstat(existing.fileno()).st_ino
                    raw = existing.read().decode("utf-8")
            except OSError:
                continue
            try:
                value = json.loads(raw)
                recorded = int(value["pid"])
                recorded_identity = str(value["identity"])
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                recorded = 0
                recorded_identity = ""
            if process_identity(recorded) == recorded_identity:
                return False
            try:
                current_inode = path.stat().st_ino
            except OSError:
                continue
            if inode is not None and current_inode != inode:
                continue
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as claimed:
            json.dump({"pid": os.getpid(), "identity": own_identity}, claimed)
        return True
    return False


def _release_daemon_pid() -> None:
    path = _pid_path()
    record = _read_pid_record(path)
    if record is None:
        return
    recorded, identity = record
    if recorded == os.getpid() and identity == process_identity(os.getpid()):
        path.unlink(missing_ok=True)


def _spawn_worker(job_id: str, database: Path) -> None:
    environment = os.environ.copy()
    environment[DATABASE_ENV_VAR] = str(database)
    subprocess.Popen(
        [sys.executable, "-m", "chainwake.jobs.worker", job_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
    )


def run_daemon(*, database: Path | None = None) -> int:
    """Supervise pending jobs until SIGINT or SIGTERM."""
    if not _claim_daemon_pid():
        return 0
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    selected_database = database or job_database_path()
    store = JobStore(selected_database)
    try:
        while not stopping:
            store.requeue_orphans(process_identity=process_identity)
            for job in store.list_jobs(states={JobState.PENDING}):
                _spawn_worker(job.id, selected_database)
            time.sleep(_SCAN_SECONDS)
    finally:
        _release_daemon_pid()
    return 0


def ensure_daemon() -> bool:
    """Start the per-user supervisor unless explicitly disabled."""
    if os.environ.get(NO_AUTOSTART_ENV_VAR) == "1":
        return False
    if daemon_running():
        return True
    home = ensure_private_directory(chainwake_home())
    log_path = home / "daemon.log"
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    ensure_private_file(log_path)
    with os.fdopen(descriptor, "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "chainwake.jobs.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=os.environ.copy(),
            start_new_session=True,
        )
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if daemon_running():
            return True
        time.sleep(0.05)
    return False


def stop_daemon() -> bool:
    pid = daemon_pid()
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    return True


def main() -> None:
    raise SystemExit(run_daemon())


if __name__ == "__main__":
    main()


__all__ = [
    "daemon_pid",
    "daemon_running",
    "ensure_daemon",
    "is_process_alive",
    "process_identity",
    "run_daemon",
    "stop_daemon",
]

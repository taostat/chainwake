"""Contracts for exclusive durable supervisor ownership."""

from __future__ import annotations

import ctypes
import json
import os
import stat
from ctypes import wintypes

import pytest

from chainwake.jobs import daemon

pytestmark = pytest.mark.unit


def test_daemon_pid_claim_is_exclusive_even_within_one_process(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "daemon.pid"
    monkeypatch.setattr(daemon, "_pid_path", lambda: pid_path)

    assert daemon._claim_daemon_pid() is True
    assert daemon._claim_daemon_pid() is False

    daemon._release_daemon_pid()
    assert not pid_path.exists()


def test_reused_pid_with_different_process_identity_is_stale(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text(
        json.dumps({"pid": os.getpid(), "identity": "previous-process"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(daemon, "_pid_path", lambda: pid_path)
    monkeypatch.setattr(daemon, "process_identity", lambda _pid: "current-process")

    assert daemon.daemon_pid() is None


def test_windows_process_identity_uses_native_creation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsafe_posix_probe(_pid: int) -> bool:
        pytest.fail("Windows process identity must not call os.kill")

    monkeypatch.setattr(daemon, "is_process_alive", unsafe_posix_probe)
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    monkeypatch.setattr(
        daemon,
        "_windows_process_identity",
        lambda pid: f"windows:{pid}:created",
    )

    assert daemon.process_identity(123) == "windows:123:created"


def test_windows_liveness_uses_open_process_instead_of_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    monkeypatch.setattr(daemon, "_windows_process_identity", lambda _pid: "windows:created")
    monkeypatch.setattr(
        daemon.os,
        "kill",
        lambda *_args: pytest.fail("Windows liveness must not call os.kill"),
    )

    assert daemon.is_process_alive(123) is True


def test_windows_process_identity_configures_pointer_sized_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFunction:
        def __init__(self, callback):
            self.callback = callback
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.callback(*args)

    def get_process_times(_handle, created, _exited, _kernel, _user):
        value = ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents
        value.dwHighDateTime = 1
        value.dwLowDateTime = 2
        return True

    class FakeKernel32:
        OpenProcess = FakeFunction(lambda _access, _inherit, _pid: 123)
        GetProcessTimes = FakeFunction(get_process_times)
        CloseHandle = FakeFunction(lambda _handle: True)

    kernel32 = FakeKernel32()
    monkeypatch.setattr(
        daemon.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )

    assert daemon._windows_process_identity(123) == f"windows:{(1 << 32) | 2}"
    assert kernel32.OpenProcess.argtypes == [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.GetProcessTimes.restype is wintypes.BOOL
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert kernel32.CloseHandle.restype is wintypes.BOOL


def test_daemon_log_is_private(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    running = iter((False, True))
    monkeypatch.setattr(daemon, "chainwake_home", lambda: tmp_path)
    monkeypatch.setattr(daemon, "daemon_running", lambda: next(running, True))
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_args, **_kwargs: object())

    assert daemon.ensure_daemon() is True

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "daemon.log").stat().st_mode) == 0o600

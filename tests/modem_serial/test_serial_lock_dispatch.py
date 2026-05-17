"""Platform-dispatch tests for ``modem_controller.platforms.serial_lock``.

Task 3.7-3.9 of ``windows-client-core``: verify the cross-platform shim
behaves correctly on Linux, macOS, and Windows. POSIX paths are mocked
via the ``_import_fcntl`` seam so the test can run on a Windows host.

Spec: ``openspec/specs/device-hardware`` § "串口竞争互斥".
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from isales_telephony.modem_controller.platforms import serial_lock
from isales_telephony.modem_controller.platforms.serial_lock import (
    BusyDeviceError,
    acquire_exclusive,
    release,
    translate_open_error,
)

# ---------------------------------------------------------------------------
# fcntl mock plumbing
# ---------------------------------------------------------------------------


def _fake_fcntl(*, flock_raises: BaseException | None = None) -> SimpleNamespace:
    """Build a fake ``fcntl`` module that records flock calls."""
    calls: list[tuple[int, int]] = []

    def flock(fd: int, op: int) -> None:
        calls.append((fd, op))
        if flock_raises is not None:
            raise flock_raises

    return SimpleNamespace(
        LOCK_EX=2,
        LOCK_NB=4,
        LOCK_UN=8,
        flock=flock,
        _calls=calls,
    )


# ---------------------------------------------------------------------------
# acquire_exclusive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_acquire_exclusive_posix_takes_flock(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    fake = _fake_fcntl()
    monkeypatch.setattr(serial_lock, "_import_fcntl", lambda: fake)

    handle = acquire_exclusive(fd=7, tty_path="/dev/ttyUSB0")

    assert handle == 7
    assert fake._calls == [(7, fake.LOCK_EX | fake.LOCK_NB)]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_acquire_exclusive_posix_blocking_raises_busy_device_error(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    fake = _fake_fcntl(flock_raises=BlockingIOError(11, "Resource temporarily unavailable"))
    monkeypatch.setattr(serial_lock, "_import_fcntl", lambda: fake)

    with pytest.raises(BusyDeviceError) as exc_info:
        acquire_exclusive(fd=7, tty_path="/dev/ttyUSB0")

    assert "/dev/ttyUSB0" in str(exc_info.value)
    assert "already locked" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, BlockingIOError)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_acquire_exclusive_posix_fd_none_raises_runtime_error(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX flock requires an fd; absence is a setup failure, not lock contention.

    Reserve BusyDeviceError for actual contention — fd=None must raise
    RuntimeError so callers don't conflate the two.
    """
    monkeypatch.setattr(sys, "platform", platform)

    with pytest.raises(RuntimeError) as exc_info:
        acquire_exclusive(fd=None, tty_path="/dev/ttyUSB0")

    assert not isinstance(exc_info.value, BusyDeviceError)
    assert "cannot resolve serial fd" in str(exc_info.value)


def test_acquire_exclusive_win32_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: OS-level exclusive open already holds, no flock needed."""
    monkeypatch.setattr(sys, "platform", "win32")
    # No fcntl monkeypatch — must NOT be called.
    monkeypatch.setattr(serial_lock, "_import_fcntl", _must_not_be_called)

    # Whether or not an fd is supplied, Windows path returns None.
    assert acquire_exclusive(fd=None, tty_path="COM3") is None
    assert acquire_exclusive(fd=7, tty_path="COM3") is None


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_release_posix_calls_flock_lock_un(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    fake = _fake_fcntl()
    monkeypatch.setattr(serial_lock, "_import_fcntl", lambda: fake)

    release(7)

    assert fake._calls == [(7, fake.LOCK_UN)]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_release_posix_suppresses_oserror(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the fd was already closed, ``flock(LOCK_UN)`` may raise OSError.
    release MUST swallow that — tear-down is best-effort.
    """
    monkeypatch.setattr(sys, "platform", platform)
    fake = _fake_fcntl(flock_raises=OSError(9, "Bad file descriptor"))
    monkeypatch.setattr(serial_lock, "_import_fcntl", lambda: fake)

    release(7)  # MUST NOT raise


def test_release_none_handle_is_noop_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serial_lock, "_import_fcntl", _must_not_be_called)
    for platform in ("linux", "darwin", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        release(None)  # MUST NOT touch fcntl


def test_release_win32_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(serial_lock, "_import_fcntl", _must_not_be_called)

    release(7)  # even with a non-None handle on Windows, must not call fcntl


# ---------------------------------------------------------------------------
# translate_open_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_translate_open_error_posix_returns_none(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX open-time errors are not lock contention — keep as RuntimeError."""
    monkeypatch.setattr(sys, "platform", platform)

    # PermissionError on Linux at open time = wrong group / udev rule, not
    # another process holding the device.
    assert (
        translate_open_error(PermissionError(13, "Permission denied"), "/dev/ttyUSB0")
        is None
    )
    assert translate_open_error(OSError(2, "No such file"), "/dev/ttyUSB0") is None


def test_translate_open_error_win32_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    exc = PermissionError(13, "Access is denied")
    result = translate_open_error(exc, "COM3")

    assert isinstance(result, BusyDeviceError)
    assert "COM3" in str(result)


def test_translate_open_error_win32_serial_exception_with_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pyserial wraps Win32 errors in SerialException with a stringified WinError."""
    monkeypatch.setattr(sys, "platform", "win32")

    # Simulate pyserial's wrapped error: a SerialException-style exception
    # whose string contains "Access is denied".
    exc = OSError("could not open port 'COM3': PermissionError(13, 'Access is denied.')")
    result = translate_open_error(exc, "COM3")

    assert isinstance(result, BusyDeviceError)
    assert "COM3" in str(result)


def test_translate_open_error_win32_chained_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pyserial may raise SerialException(...) whose ``__cause__`` is a PermissionError."""
    monkeypatch.setattr(sys, "platform", "win32")

    inner = PermissionError(13, "Access is denied")
    outer = OSError("could not open port")
    outer.__cause__ = inner
    result = translate_open_error(outer, "COM3")

    assert isinstance(result, BusyDeviceError)


def test_translate_open_error_win32_unrelated_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    # FileNotFoundError on Windows when the COM port simply doesn't exist
    # is NOT lock contention — should remain a generic open failure.
    assert (
        translate_open_error(
            FileNotFoundError(2, "The system cannot find the file specified"),
            "COM99",
        )
        is None
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _must_not_be_called() -> object:
    pytest.fail("_import_fcntl should not be called on this platform path")

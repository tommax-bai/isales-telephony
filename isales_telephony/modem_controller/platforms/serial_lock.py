"""Cross-platform exclusive access for serial devices.

POSIX (Linux / macOS): ``fcntl.flock(LOCK_EX | LOCK_NB)`` on the open fd —
the OS allows concurrent ``open`` so an explicit advisory lock is required.

Windows: no explicit lock primitive. ``CreateFile`` on a ``COM*`` device
defaults to ``dwShareMode = 0`` (exclusive) — pyserial does not pass
``FILE_SHARE_*`` flags, so the OS already rejects a second opener with
``ERROR_ACCESS_DENIED``. We translate that open-time error into the same
:class:`BusyDeviceError` so callers see one exception type across platforms.

Spec: ``openspec/specs/device-hardware`` § "串口竞争互斥".
"""

from __future__ import annotations

import contextlib
import sys
from typing import Any


class BusyDeviceError(RuntimeError):
    """Raised when exclusive access to a serial device cannot be obtained."""


def _import_fcntl() -> Any:
    # Lazy import so the module is safe on Windows hosts (top-level
    # ``import fcntl`` would raise ``ModuleNotFoundError``). Also serves
    # as a monkeypatch seam for unit tests exercising the POSIX path on
    # Windows runners.
    import fcntl

    return fcntl


def acquire_exclusive(fd: int | None, tty_path: str) -> int | None:
    """Acquire exclusive access; return a handle for :func:`release`.

    POSIX: ``flock(LOCK_EX | LOCK_NB)`` on ``fd``; returns ``fd``.
    Windows: no-op; returns ``None`` (the OS-level exclusive open is
    already in effect by the time this is called).
    """
    if sys.platform == "win32":
        return None
    if fd is None:
        # POSIX flock requires an open fd; absence is a setup failure,
        # not lock contention — reserve BusyDeviceError for the latter.
        raise RuntimeError(
            f"cannot resolve serial fd for {tty_path} "
            "(POSIX flock requires an open file descriptor)"
        )
    fcntl = _import_fcntl()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise BusyDeviceError(
            f"{tty_path} is already locked by another process"
        ) from exc
    return fd


def release(handle: int | None) -> None:
    """Release a handle from :func:`acquire_exclusive`. Idempotent for ``None``."""
    if handle is None or sys.platform == "win32":
        return
    fcntl = _import_fcntl()
    with contextlib.suppress(OSError):
        fcntl.flock(handle, fcntl.LOCK_UN)


def translate_open_error(
    exc: BaseException, tty_path: str
) -> BusyDeviceError | None:
    """Map Windows access-denied open failures to :class:`BusyDeviceError`.

    Returns ``None`` for any input on POSIX: open-time ``PermissionError``
    on Linux/macOS is a deploy/permission misconfiguration (e.g. user not
    in ``dialout``), not lock contention, and must remain a ``RuntimeError``
    per the device-hardware spec.
    """
    if sys.platform != "win32":
        return None
    if isinstance(exc, PermissionError):
        return BusyDeviceError(
            f"{tty_path} is already locked by another process"
        )
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, PermissionError):
        return BusyDeviceError(
            f"{tty_path} is already locked by another process"
        )
    if "Access is denied" in str(exc):
        return BusyDeviceError(
            f"{tty_path} is already locked by another process"
        )
    return None


__all__ = [
    "BusyDeviceError",
    "acquire_exclusive",
    "release",
    "translate_open_error",
]

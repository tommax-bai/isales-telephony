"""pystray-driven Windows tray icon.

Spec: deployment-topology § Scenario "Tray UX 二态" — D1 ships a
binary green / red icon, four-item right-click menu.

The pystray library runs the tray UI in a daemon thread (Win32 message
loop) — separate from the asyncio event loop and from the Qt main
thread. We bridge across these via :class:`StateBus` (pure threading
primitives) and a lightweight ``Menu callback → asyncio coroutine``
shim that posts work onto the asyncio loop via ``call_soon_threadsafe``.

Tests on Linux / macOS CI mock out the pystray ``Icon`` class so the
state-bus → tray-color mapping can be exercised without a Windows host.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from isales_telephony.ui.state import EdgeStatus, StateBus, TrayColor

logger = logging.getLogger(__name__)


class _Image(Protocol):
    """Whatever ``pystray`` accepts as ``Icon(image=...)``. In production
    this is a ``PIL.Image.Image``; tests inject a sentinel object.
    """


class TrayMenuActions(Protocol):
    """Callbacks invoked from the tray right-click menu.

    Each handler runs in pystray's daemon thread. Implementations
    that need to touch asyncio state MUST use
    ``loop.call_soon_threadsafe`` (or ``run_coroutine_threadsafe``) —
    see :class:`AsyncMenuBridge` for the canonical helper.
    """

    def open_diagnostic_window(self) -> None: ...
    def reactivate(self) -> None: ...
    def open_log_folder(self) -> None: ...
    def quit(self) -> None: ...


class AsyncMenuBridge:
    """Bridge between pystray menu callbacks (running on a Win32 thread)
    and asyncio work (running on the qasync-wrapped Qt loop).

    Construct with the asyncio event loop and a set of coroutine
    handlers, then pass an instance to :class:`TrayIconController` —
    pystray calls these synchronous methods from its thread and we
    forward them safely.
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        on_open_diagnostic: Callable[[], None] | None = None,
        on_reactivate: Callable[[], None] | None = None,
        on_open_log_folder: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self._loop = loop
        self._on_open_diagnostic = on_open_diagnostic
        self._on_reactivate = on_reactivate
        self._on_open_log_folder = on_open_log_folder
        self._on_quit = on_quit

    def _post(self, fn: Callable[[], None] | None) -> None:
        if fn is None:
            return
        try:
            self._loop.call_soon_threadsafe(fn)
        except RuntimeError:
            # Loop closed (process is exiting). Best-effort fallback —
            # invoke synchronously so e.g. "Quit" still fires after the
            # asyncio side has torn down.
            try:
                fn()
            except Exception:  # noqa: BLE001
                logger.exception("tray: synchronous fallback raised")

    def open_diagnostic_window(self) -> None:
        self._post(self._on_open_diagnostic)

    def reactivate(self) -> None:
        self._post(self._on_reactivate)

    def open_log_folder(self) -> None:
        # OS-level open doesn't need the asyncio loop — do it inline.
        log_dir = _default_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        _open_path_in_file_manager(log_dir)

    def quit(self) -> None:
        self._post(self._on_quit)


def _default_log_dir() -> Path:
    """``%APPDATA%\\isales\\logs\\`` on Windows; ``~/.local/share/isales/logs``
    elsewhere. Matches the deployment-topology spec layout.
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "isales" / "logs"
    return Path.home() / ".local" / "share" / "isales" / "logs"


def _open_path_in_file_manager(path: Path) -> None:
    """Best-effort ``explorer.exe <path>`` (or platform equivalent)."""
    try:
        if sys.platform == "win32":  # pragma: no cover - production-only path
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":  # pragma: no cover
            subprocess.run(["open", str(path)], check=False)
        else:  # pragma: no cover
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:  # noqa: BLE001
        logger.exception("tray: failed to open file manager at %s", path)


def _build_icon_image(color: TrayColor) -> object:
    """Return a PIL image used as the tray icon body.

    pystray on Windows requires a PIL ``Image``; we draw a 16x16 solid
    circle in the requested colour with a small darker outline so it
    reads on both light and dark Windows themes.

    Lazy import: PIL ships with PySide6's wheel transitively, but we
    still want to keep imports off the cross-platform path.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = (40, 200, 80, 255) if color is TrayColor.GREEN else (220, 40, 40, 255)
    outline = (20, 90, 40, 255) if color is TrayColor.GREEN else (110, 20, 20, 255)
    draw.ellipse([(4, 4), (size - 4, size - 4)], fill=fill, outline=outline, width=3)
    return img


class TrayIconController:
    """Owns the pystray ``Icon`` and keeps it in sync with ``StateBus``.

    Lifecycle:

    - :meth:`start` spawns the pystray daemon thread (pystray's
      ``Icon.run`` is blocking, hence the thread).
    - When ``StateBus`` publishes a new ``EdgeStatus``, the subscriber
      registered here updates the icon's image + tooltip *from the
      publisher's thread* (pystray's ``icon.icon = new_image`` is
      thread-safe on Windows).
    - :meth:`stop` calls ``icon.stop()`` from the publisher's thread,
      which unblocks the pystray daemon thread cleanly.
    """

    APP_NAME = "iSales"

    def __init__(
        self,
        *,
        state_bus: StateBus,
        actions: TrayMenuActions,
    ) -> None:
        self._bus = state_bus
        self._actions = actions
        self._icon: object | None = None
        self._thread: threading.Thread | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._color_cache: TrayColor | None = None

    def start(self) -> None:
        """Build the pystray icon + spawn the run-loop thread."""
        import pystray  # noqa: PLC0415

        initial_status = self._bus.status
        initial_color = initial_status.tray_color
        icon = pystray.Icon(
            self.APP_NAME,
            icon=_build_icon_image(initial_color),
            title=_tooltip_for(initial_status),
            menu=pystray.Menu(
                pystray.MenuItem(
                    "打开诊断窗口",
                    lambda _icon, _item: self._actions.open_diagnostic_window(),
                ),
                pystray.MenuItem(
                    "重新激活",
                    lambda _icon, _item: self._actions.reactivate(),
                ),
                pystray.MenuItem(
                    "查看日志",
                    lambda _icon, _item: self._actions.open_log_folder(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "退出",
                    lambda _icon, _item: self._actions.quit(),
                ),
            ),
        )
        self._icon = icon
        self._color_cache = initial_color
        self._unsubscribe = self._bus.subscribe(self._on_status)

        thread = threading.Thread(
            target=icon.run,
            name="isales-tray",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        """Idempotent shutdown. Safe to call from any thread."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        icon = self._icon
        if icon is not None:
            try:
                icon.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logger.exception("tray: error stopping icon")
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._icon = None
        self._thread = None

    def _on_status(self, status: EdgeStatus) -> None:
        """StateBus callback. May fire from asyncio thread; pystray's
        attribute setters serialise internally."""
        icon = self._icon
        if icon is None:
            return
        color = status.tray_color
        if color != self._color_cache:
            try:
                icon.icon = _build_icon_image(color)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logger.exception("tray: failed to swap icon image")
            self._color_cache = color
        try:
            icon.title = _tooltip_for(status)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.exception("tray: failed to update title")


def _tooltip_for(status: EdgeStatus) -> str:
    """One-line hover tooltip — the only place free text leaks into UX."""
    cloud_part = f"云端: {status.cloud_link.value}"
    n_modems = len(status.modems)
    idle = sum(1 for m in status.modems if m.state.value == "idle")
    modem_part = f"modem: {idle}/{n_modems} idle"
    return f"iSales — {cloud_part} — {modem_part}"


__all__ = [
    "AsyncMenuBridge",
    "TrayIconController",
    "TrayMenuActions",
    "_build_icon_image",
    "_default_log_dir",
    "_tooltip_for",
]

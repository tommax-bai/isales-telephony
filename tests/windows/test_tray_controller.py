"""TrayIconController + AsyncMenuBridge tests.

We can't import ``pystray`` on Linux / macOS CI (it needs Win32 deps),
so the controller's ``start()`` path is exercised on the Windows runner.
What we CAN test cross-platform:

- ``AsyncMenuBridge`` posting work via ``loop.call_soon_threadsafe`` —
  the bridge has no pystray dependency.
- ``_build_icon_image`` produces distinct images for green vs red
  (PIL is pulled in by PySide6 transitively; skip if unavailable).
- ``_tooltip_for`` rendering.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from isales_telephony.ui.state import (
    CloudLinkState,
    EdgeStatus,
    ModemState,
    ModemSummary,
    TrayColor,
)
from isales_telephony.ui.tray import (
    AsyncMenuBridge,
    _tooltip_for,
)


# ----------------------------------------------------- AsyncMenuBridge


@pytest.mark.asyncio(loop_scope="session")
async def test_bridge_posts_diagnostic_callback_to_loop() -> None:
    loop = asyncio.get_running_loop()
    diag_called: list[str] = []

    bridge = AsyncMenuBridge(
        loop=loop,
        on_open_diagnostic=lambda: diag_called.append("diag"),
    )

    # Dispatch from a foreign thread to mimic pystray's daemon thread.
    done = threading.Event()

    def fire() -> None:
        bridge.open_diagnostic_window()
        done.set()

    threading.Thread(target=fire, daemon=True).start()
    done.wait(timeout=1)
    # call_soon_threadsafe schedules; let the loop run one tick.
    await asyncio.sleep(0.01)
    assert diag_called == ["diag"]


@pytest.mark.asyncio(loop_scope="session")
async def test_bridge_swallows_none_handlers() -> None:
    loop = asyncio.get_running_loop()
    bridge = AsyncMenuBridge(loop=loop)  # all handlers None
    # Must not raise.
    bridge.open_diagnostic_window()
    bridge.reactivate()
    bridge.quit()


@pytest.mark.asyncio(loop_scope="session")
async def test_bridge_quit_callback() -> None:
    loop = asyncio.get_running_loop()
    quit_called: list[str] = []

    bridge = AsyncMenuBridge(
        loop=loop,
        on_quit=lambda: quit_called.append("quit"),
    )
    bridge.quit()
    await asyncio.sleep(0.01)
    assert quit_called == ["quit"]


# ----------------------------------------------------- tooltip


def test_tooltip_includes_cloud_state_and_modem_count() -> None:
    status = EdgeStatus(
        cloud_link=CloudLinkState.CONNECTED,
        modems=(
            ModemSummary("COM3", ModemState.IDLE),
            ModemSummary("COM4", ModemState.OFFLINE),
        ),
    )
    s = _tooltip_for(status)
    assert "connected" in s
    assert "1/2 idle" in s
    assert s.startswith("iSales —")


def test_tooltip_handles_no_modems() -> None:
    status = EdgeStatus(cloud_link=CloudLinkState.AWAITING_ACTIVATION)
    s = _tooltip_for(status)
    assert "awaiting_activation" in s
    assert "0/0 idle" in s


# ----------------------------------------------------- icon image


def test_build_icon_image_distinguishes_colors() -> None:
    """Green and red icons must produce different image bytes — catches
    a refactor that forgets to apply the colour."""
    try:
        from isales_telephony.ui.tray import _build_icon_image
    except ImportError:
        pytest.skip("PIL not available in this environment")
    try:
        green = _build_icon_image(TrayColor.GREEN)
        red = _build_icon_image(TrayColor.RED)
    except ImportError:
        pytest.skip("PIL not available")
    assert green.tobytes() != red.tobytes()  # type: ignore[attr-defined]

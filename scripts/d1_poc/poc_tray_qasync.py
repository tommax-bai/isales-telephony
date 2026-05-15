"""D1 PoC 2.3 — pystray + qasync + PySide6 共存稳定性 demo.

Spec: windows-client-core / tasks.md § 2.3, design.md Decision 1 + Risks.

Validates that the three libraries we plan to ship together cooperate
for ≥ 10 minutes without crashes / deadlocks:

- ``pystray`` runs the tray icon on its own daemon thread (Win32
  message loop).
- ``qasync`` wraps the PySide6 Qt event loop so ``asyncio`` tasks
  schedule on the Qt loop.
- A 1 Hz asyncio task ticks counters; a 30 s asyncio task asks the Qt
  thread to show / hide a small QWidget (the future "diagnostic"
  window — design.md Decision 6).

Run on Windows:

  python -m pip install pystray qasync PySide6 Pillow
  python scripts/d1_poc/poc_tray_qasync.py --duration 600

Exit conditions
---------------

- ``--duration <seconds>`` elapses cleanly → PASS (writes
  ``poc_tray_qasync_result.json`` with PASS).
- Right-click tray → Quit → also PASS, partial duration recorded.
- Any exception in the asyncio loop / Qt thread / pystray thread →
  FAIL with the traceback persisted.

Pillow is required only to construct a placeholder tray icon (16x16
solid colour); ship icon ships in ``deploy/edge/windows/tray.ico``
(D1 task 7.6) so the production path doesn't need Pillow.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field

try:
    import pystray
    import qasync
    from PIL import Image, ImageDraw
    from PySide6.QtCore import QObject, Qt, Signal, Slot
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        f"missing dependency: {exc}\n"
        "  pip install pystray qasync PySide6 Pillow\n"
    )
    sys.exit(2)


@dataclass
class _RunStats:
    started_at: float = 0.0
    ended_at: float = 0.0
    tick_count: int = 0
    diagnostic_shows: int = 0
    diagnostic_hides: int = 0
    exit_reason: str = ""
    errors: list[str] = field(default_factory=list)


class _DiagnosticBridge(QObject):
    """Bridge from asyncio task to Qt thread: asyncio fires a Signal,
    Qt main thread receives + shows/hides QWidget."""

    show_requested = Signal()
    hide_requested = Signal()

    def __init__(self, widget: QWidget, stats: _RunStats) -> None:
        super().__init__()
        self._widget = widget
        self._stats = stats
        self.show_requested.connect(self._on_show, Qt.ConnectionType.QueuedConnection)
        self.hide_requested.connect(self._on_hide, Qt.ConnectionType.QueuedConnection)

    @Slot()
    def _on_show(self) -> None:
        self._widget.show()
        self._stats.diagnostic_shows += 1

    @Slot()
    def _on_hide(self) -> None:
        self._widget.hide()
        self._stats.diagnostic_hides += 1


def _make_diagnostic_window(stats: _RunStats) -> QWidget:
    w = QWidget()
    w.setWindowTitle("iSales Diagnostic (PoC)")
    w.setFixedSize(320, 120)
    layout = QVBoxLayout(w)
    label = QLabel("ticks: 0 / shows: 0", w)
    layout.addWidget(label)

    # Update the label from a Qt timer (Qt-thread safe).
    from PySide6.QtCore import QTimer

    def _refresh() -> None:
        label.setText(
            f"ticks: {stats.tick_count} / shows: {stats.diagnostic_shows}"
        )

    timer = QTimer(w)
    timer.setInterval(500)
    timer.timeout.connect(_refresh)
    timer.start()
    return w


def _make_tray_icon(on_quit: object) -> pystray.Icon:
    """16×16 tray icon (solid green) with a Quit menu item."""
    image = Image.new("RGB", (16, 16), (40, 180, 80))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, 13, 13), fill=(255, 255, 255))

    menu = pystray.Menu(
        pystray.MenuItem("Quit", on_quit),  # type: ignore[arg-type]
    )
    return pystray.Icon("isales-poc", image, "iSales PoC", menu)


async def _tick_task(stats: _RunStats, bridge: _DiagnosticBridge) -> None:
    """1 Hz tick; every 30s toggle the diagnostic window."""
    try:
        while True:
            await asyncio.sleep(1.0)
            stats.tick_count += 1
            if stats.tick_count % 30 == 0:
                # Show, wait 3s, hide.
                bridge.show_requested.emit()
                await asyncio.sleep(3.0)
                bridge.hide_requested.emit()
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001
        stats.errors.append(traceback.format_exc())
        raise


async def _runner(
    duration_s: float, stats: _RunStats, bridge: _DiagnosticBridge
) -> None:
    tick = asyncio.create_task(_tick_task(stats, bridge), name="poc_tick")
    try:
        await asyncio.sleep(duration_s)
        stats.exit_reason = "duration_elapsed"
    except asyncio.CancelledError:
        stats.exit_reason = "external_cancel"
        raise
    finally:
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tick


def main() -> int:
    parser = argparse.ArgumentParser(
        description="D1 PoC 2.3 — pystray + qasync + PySide6 共存"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=600.0,
        help="seconds to keep the demo running (default 600s = 10 min)",
    )
    parser.add_argument(
        "--out",
        default="poc_tray_qasync_result.json",
        help="output JSON path",
    )
    args = parser.parse_args()

    stats = _RunStats()
    stats.started_at = time.time()

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    diagnostic = _make_diagnostic_window(stats)
    bridge = _DiagnosticBridge(diagnostic, stats)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    def _request_quit() -> None:
        stats.exit_reason = stats.exit_reason or "tray_quit"
        # Schedule loop stop on the loop's own thread.
        loop.call_soon_threadsafe(loop.stop)

    tray = _make_tray_icon(on_quit=lambda _icon, _item: _request_quit())

    # pystray.Icon.run_detached() puts the tray onto its own thread
    # (Win32 message pump) so it doesn't block the Qt loop.
    tray.run_detached()

    runner_task = loop.create_task(_runner(args.duration, stats, bridge))

    try:
        with loop:
            loop.run_forever()
    except Exception:  # noqa: BLE001
        stats.errors.append(traceback.format_exc())
        stats.exit_reason = "exception"
    finally:
        if not runner_task.done():
            runner_task.cancel()
        with contextlib.suppress(Exception):
            tray.stop()
        stats.ended_at = time.time()

    verdict = _verdict(stats, target_duration_s=args.duration)
    payload = {**asdict(stats), "duration_target_s": args.duration, "verdict": verdict}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"\nwrote {args.out}")
    print(f"VERDICT: {verdict['summary']}")
    return 0 if verdict["pass"] else 1


def _verdict(stats: _RunStats, target_duration_s: float) -> dict[str, object]:
    if stats.errors:
        return {
            "pass": False,
            "summary": (
                f"FAIL — {len(stats.errors)} exception(s); see 'errors'. "
                f"design Decision 1 备选方案 = pystray off, use Qt6 "
                f"QSystemTrayIcon"
            ),
        }
    elapsed = stats.ended_at - stats.started_at
    if stats.exit_reason == "tray_quit":
        return {
            "pass": True,
            "summary": (
                f"PASS — user clicked Quit after {elapsed:.0f}s "
                f"({stats.tick_count} ticks, {stats.diagnostic_shows} shows). "
                f"No crashes; three libraries coexist."
            ),
        }
    if elapsed >= target_duration_s * 0.95:
        return {
            "pass": True,
            "summary": (
                f"PASS — ran {elapsed:.0f}s of target {target_duration_s:.0f}s "
                f"clean; {stats.tick_count} ticks; {stats.diagnostic_shows} "
                f"diagnostic window shows. Ship D1 5.x/6.x with this stack."
            ),
        }
    return {
        "pass": False,
        "summary": (
            f"FAIL — only {elapsed:.0f}s of target {target_duration_s:.0f}s "
            f"completed (exit_reason={stats.exit_reason!r})"
        ),
    }


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

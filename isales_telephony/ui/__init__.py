"""Windows tray + activation UX for the edge client.

Spec: windows-client-core / deployment-topology § "Tray UX 二态" + "激活码注册流程".

This package is **Windows-only at runtime** — modules here are lazy-imported by
``isales_telephony/main_windows.py`` after ``sys.platform == "win32"`` check.
The package itself imports cleanly on Linux / macOS (no top-level pystray /
PySide6 import) so unit tests for the state-bridge / lifecycle helpers can run
on any CI host.

Module map
----------

- :mod:`isales_telephony.ui.state` — ``EdgeStatus`` enum + ``StateBus``
  pub-sub between asyncio tasks and the Qt main thread. Pure-Python; tested
  cross-platform.
- :mod:`isales_telephony.ui.tray` — pystray tray icon + right-click menu.
  Lazy imports ``pystray`` / ``PIL``.
- :mod:`isales_telephony.ui.activation` — PySide6 activation-code input
  dialog + env-file writer. Lazy imports ``PySide6``.
- :mod:`isales_telephony.ui.diagnostic` — PySide6 read-only diagnostic
  window (gRPC status / modem list / last-N log lines). Lazy imports
  ``PySide6``.
- :mod:`isales_telephony.ui.env_writer` — non-UI helper used by activation
  dialog to read/write ``%APPDATA%\\isales\\env\\telephony.env``. Plain-text
  shell-style ``KEY=value`` to match the rest of the deploy story.
"""

from __future__ import annotations

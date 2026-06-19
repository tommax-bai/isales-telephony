"""Platform routing in ``audio_bridge/__init__.py::get_default_rtc_session_factory``.

Covers:
- macOS branch (no ``app_id`` needed):
  - PyObjC binding available → returns :class:`MacosDingRtcPyObjCSession`
  - ImportError → fallback to :class:`MacosRtcSession` mock + WARN log
- Linux branch (``app_id`` ignored): NotImplementedError.

Windows branch (requires ``app_id`` + DingRTC binding) is covered by
``tests/windows/`` once the new ``WindowsDingRtcSession`` test suite is
written (TODO §7.11 follow-up; old ARTC test suite removed).
"""

from __future__ import annotations

import builtins
import logging
import sys

import pytest


def test_darwin_routes_to_pyobjc_when_available(fresh_bridge, monkeypatch):
    """When the PyObjC bridge imports cleanly, darwin returns it."""
    _, _, bridge = fresh_bridge

    from isales_telephony import audio_bridge

    monkeypatch.setattr(audio_bridge.sys, "platform", "darwin", raising=False)
    cls = audio_bridge.get_default_rtc_session_factory()
    assert cls.__name__ == "MacosDingRtcPyObjCSession"


def test_darwin_falls_back_to_mock_when_pyobjc_missing(
    monkeypatch, caplog: pytest.LogCaptureFixture,
):
    """ImportError during macos_dingrtc_pyobjc import → WARN log + MacosRtcSession."""
    from isales_telephony import audio_bridge
    from isales_telephony.audio_bridge.session import MacosRtcSession

    # Ensure a fresh import path: drop any cached bridge module, then
    # block re-import by interposing on builtins.__import__.
    monkeypatch.delitem(
        sys.modules, "isales_telephony.audio_bridge.macos_dingrtc_pyobjc",
        raising=False,
    )

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "isales_telephony.audio_bridge.macos_dingrtc_pyobjc":
            raise ImportError("pyobjc-core not installed (test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(audio_bridge.sys, "platform", "darwin", raising=False)

    with caplog.at_level(logging.WARNING, logger=audio_bridge.__name__):
        cls = audio_bridge.get_default_rtc_session_factory()

    assert cls is MacosRtcSession
    assert any(
        "macos_dingrtc_pyobjc_unavailable_fallback_to_mock" in r.message
        for r in caplog.records
    )


def test_non_darwin_non_win32_still_raises(monkeypatch):
    from isales_telephony import audio_bridge

    monkeypatch.setattr(audio_bridge.sys, "platform", "linux", raising=False)
    with pytest.raises(NotImplementedError):
        audio_bridge.get_default_rtc_session_factory(app_id="ignored-on-linux")

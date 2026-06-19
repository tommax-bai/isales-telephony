"""Shared fakes for the macOS PyObjC bridge tests.

Historically these fakes powered ``test_macos_artc_pyobjc_*`` against
the legacy AliRTC binding. The dingrtc-migration § 8.9 deleted both
``macos_artc_pyobjc.py`` and its test files; what remains here is the
minimal fake-PyObjC surface still consumed by tests in
``tests/audio_bridge/`` (e.g. the ``test_init_routing.py`` import path
that loads ``macos_dingrtc_pyobjc`` under fake ``objc`` / ``Foundation``
modules so the import resolves on hosts without PyObjC installed).

The ``fresh_bridge`` fixture is retained for any future macOS-bridge
tests that need a reload-on-each-test pattern; it now reloads the
DingRTC PyObjC module.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fake PyObjC surface
# ---------------------------------------------------------------------------


class _FakeNSObject:
    """Stand-in for ``Foundation.NSObject`` in tests.

    PyObjC's NSObject lifecycle is ``cls.alloc().initWith...()`` — we
    mirror the contract without involving the Obj-C runtime.
    """

    @classmethod
    def alloc(cls) -> "_FakeNSObject":
        return cls.__new__(cls)

    def init(self) -> "_FakeNSObject":
        return self


class _FakeObjcSuper:
    """Stand-in for ``objc.super(cls, instance)``.

    Only ``.init()`` is exercised by the bridge.
    """

    def __init__(self, _cls: type, instance: Any) -> None:
        self._instance = instance

    def init(self) -> Any:
        return self._instance


def _default_load_bundle(
    _name: str,
    module_globals: dict[str, Any],
    bundle_path: str,  # noqa: ARG001 — signature parity with real PyObjC
) -> None:
    """Default fake ``objc.loadBundle``: inject a stub ``AliRtcEngine``.

    Individual tests can swap this via ``fake_objc.loadBundle = ...``.
    """
    module_globals["AliRtcEngine"] = _StubAliRtcEngine


class _StubAliRtcEngine:
    """Trivial Obj-C-class stand-in. Most session tests override this."""

    @classmethod
    def sharedInstance(cls) -> "_StubAliRtcEngine":
        return cls()


def _fake_selector(callable_obj: Any, **_kwargs: Any) -> Any:
    """Pass-through stand-in for ``objc.selector``.

    The real ``objc.selector(fn, signature=b'...')`` attaches an
    Objective-C method-type-encoding metadata to ``fn`` so the SDK can
    invoke it across the Cocoa/Python ABI without misinterpreting
    primitive args. Tests don't exercise that ABI (no SDK present), so
    we return the underlying callable unchanged — Python-side callers
    still see the function with its normal Python signature.
    """
    return callable_obj


def _make_fake_objc() -> ModuleType:
    mod = ModuleType("objc")
    mod.loadBundle = _default_load_bundle  # type: ignore[attr-defined]
    mod.super = _FakeObjcSuper  # type: ignore[attr-defined]
    mod.error = type("error", (Exception,), {})  # type: ignore[attr-defined]
    mod.selector = _fake_selector  # type: ignore[attr-defined]
    return mod


def _make_fake_foundation() -> ModuleType:
    mod = ModuleType("Foundation")
    mod.NSObject = _FakeNSObject  # type: ignore[attr-defined]
    return mod


@pytest.fixture()
def fresh_bridge(monkeypatch: pytest.MonkeyPatch):
    """Inject fake objc/Foundation + reload the bridge module.

    Returns
    -------
    tuple[ModuleType, ModuleType, ModuleType]
        ``(fake_objc, fake_foundation, bridge_module)``.

    Uses ``importlib.reload`` so the bridge's ``import objc`` /
    ``from Foundation import NSObject`` rebinds against this test's
    fakes, even when a previous test left the module cached.
    """
    import importlib

    fake_objc = _make_fake_objc()
    fake_foundation = _make_fake_foundation()
    monkeypatch.setitem(sys.modules, "objc", fake_objc)
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)
    import isales_telephony.audio_bridge.macos_dingrtc_pyobjc as bridge
    bridge = importlib.reload(bridge)
    return fake_objc, fake_foundation, bridge

"""CLI tests for ``isales-telephony-edge`` entry point.

Covers argparse + the non-darwin platform guard for ``--dev-no-modem``.
The full dev-mode startup path is covered by the orchestrator tests +
manual smoke (see ``deploy/edge/macos/RUNBOOK.md``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from isales_telephony.edge.main import _build_recorder, build_argparser, run


def test_argparser_defaults_to_production_mode():
    parser = build_argparser()
    args = parser.parse_args([])
    assert args.dev_no_modem is False
    assert args.dev_channel is None
    assert args.dev_uid is None
    assert args.dev_peer_uid is None


def test_argparser_accepts_all_dev_flags():
    parser = build_argparser()
    args = parser.parse_args([
        "--dev-no-modem",
        "--dev-channel", "demo-mac-01",
        "--dev-uid", "mac-dev-01",
        "--dev-peer-uid", "engine-01",
    ])
    assert args.dev_no_modem is True
    assert args.dev_channel == "demo-mac-01"
    assert args.dev_uid == "mac-dev-01"
    assert args.dev_peer_uid == "engine-01"


def test_dev_no_modem_fails_fast_on_non_darwin(
    monkeypatch, capsys: pytest.CaptureFixture[str],
):
    import isales_telephony.edge.main as main_mod

    monkeypatch.setattr(main_mod.sys, "platform", "linux", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        run(["--dev-no-modem", "--dev-channel", "x", "--dev-uid", "y"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--dev-no-modem 仅 macOS 支持" in err


def test_dev_no_modem_guard_lets_darwin_through(monkeypatch):
    """On darwin the guard must NOT raise SystemExit; it should fall into
    asyncio.run (which we stub to a no-op so the test stays synchronous)."""
    import isales_telephony.edge.main as main_mod

    monkeypatch.setattr(main_mod.sys, "platform", "darwin", raising=False)
    called: list[bool] = []

    def _fake_asyncio_run(_coro):
        called.append(True)
        # Schedule the coroutine for collection so pytest-asyncio doesn't
        # complain about a never-awaited coroutine warning.
        _coro.close()

    monkeypatch.setattr(main_mod.asyncio, "run", _fake_asyncio_run)
    run(["--dev-no-modem", "--dev-channel", "c", "--dev-uid", "u"])
    assert called == [True]


# ---- _build_recorder (edge-local-call-recording) ------------------------


def test_build_recorder_disabled_when_dir_unset(monkeypatch):
    monkeypatch.delenv("ISALES_EDGE_RECORDINGS_DIR", raising=False)
    recorder, max_recordings = _build_recorder()
    assert recorder is None
    assert max_recordings == 0


def test_build_recorder_disabled_when_max_zero(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ISALES_EDGE_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setenv("ISALES_EDGE_MAX_RECORDINGS", "0")
    recorder, max_recordings = _build_recorder()
    assert recorder is None
    assert max_recordings == 0


def test_build_recorder_enabled_defaults_to_ten(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ISALES_EDGE_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.delenv("ISALES_EDGE_MAX_RECORDINGS", raising=False)
    recorder, max_recordings = _build_recorder()
    assert recorder is not None
    assert max_recordings == 10


def test_build_recorder_honours_custom_max(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ISALES_EDGE_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setenv("ISALES_EDGE_MAX_RECORDINGS", "3")
    recorder, max_recordings = _build_recorder()
    assert recorder is not None
    assert max_recordings == 3

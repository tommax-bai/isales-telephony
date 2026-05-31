"""Auto-discovery of (AT, Audio) COM ports by USB descriptor.

No env COM-number fallback by design — COM numbers re-enumerate on every
USB re-plug, so a hardcoded value is guaranteed stale. Discovery keys on the
stable description token + vid/pid/serial, confirmed by an AT probe, and
fails loud when it cannot complete.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from isales_telephony.modem_controller.platforms.windows_serial import (
    ModemDiscoveryError,
    discover_modem_serial_paths,
)


def _port(device, desc, *, vid=0x1E0E, pid=0x9001, serial="S1"):
    return SimpleNamespace(
        device=device, description=desc, vid=vid, pid=pid, serial_number=serial
    )


# SIM7600G-H composite as enumerated on Windows (COM numbers arbitrary —
# the whole point is that discovery does NOT depend on them).
SIM_PORTS = [
    _port("COM13", "Simcom HS-USB Diagnostics 9001 (COM13)"),
    _port("COM14", "Simcom HS-USB Modem 9001 #3"),
    _port("COM16", "Simcom HS-USB AT PORT 9001 (COM16)"),
    _port("COM17", "Simcom HS-USB Audio 9001 (COM17)"),
    _port("COM15", "Simcom HS-USB NMEA 9001 (COM15)"),
]


def test_discovers_at_and_audio_by_descriptor() -> None:
    at, audio = discover_modem_serial_paths(
        scanner=lambda: SIM_PORTS, at_prober=lambda d: d == "COM16"
    )
    assert at == "COM16"
    assert audio == "COM17"


def test_prefers_at_port_over_modem_channel() -> None:
    # The "Modem" channel also answers AT, but only the "AT PORT" description
    # is treated as a candidate, so it is never selected even if every port
    # would answer OK.
    at, _audio = discover_modem_serial_paths(
        scanner=lambda: SIM_PORTS, at_prober=lambda d: True
    )
    assert at == "COM16"


def test_raises_when_no_at_port_present() -> None:
    ports = [_port("COM5", "Generic USB Serial Device")]
    with pytest.raises(ModemDiscoveryError, match="no AT-command port"):
        discover_modem_serial_paths(scanner=lambda: ports, at_prober=lambda d: True)


def test_raises_when_at_candidate_does_not_answer() -> None:
    # Description matches but the port is wedged / silent → fail loud.
    with pytest.raises(ModemDiscoveryError, match="answered"):
        discover_modem_serial_paths(scanner=lambda: SIM_PORTS, at_prober=lambda d: False)


def test_raises_when_no_audio_sibling() -> None:
    ports = [_port("COM16", "Simcom HS-USB AT PORT 9001 (COM16)")]
    with pytest.raises(ModemDiscoveryError, match="no sibling Audio"):
        discover_modem_serial_paths(scanner=lambda: ports, at_prober=lambda d: True)


def test_audio_sibling_must_share_usb_serial() -> None:
    # An "Audio" port from a DIFFERENT physical modem (different serial) must
    # not be matched — disambiguates multi-modem hosts.
    ports = [
        _port("COM16", "Simcom HS-USB AT PORT 9001 (COM16)", serial="A"),
        _port("COM17", "Simcom HS-USB Audio 9001 (COM17)", serial="B"),
    ]
    with pytest.raises(ModemDiscoveryError, match="no sibling Audio"):
        discover_modem_serial_paths(scanner=lambda: ports, at_prober=lambda d: True)

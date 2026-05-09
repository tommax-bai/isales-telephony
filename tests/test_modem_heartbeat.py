"""Heartbeat coroutine unit tests."""

from __future__ import annotations

import pytest

from isales_telephony.modem_controller.heartbeat import heartbeat_loop


class FakePoster:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int | None]] = []

    async def heartbeat(self, device_id: int, signal_strength: int | None) -> None:
        self.calls.append((device_id, signal_strength))


@pytest.mark.asyncio
async def test_heartbeat_iterates_over_current_device_set() -> None:
    poster = FakePoster()
    devices: list[int] = [1, 2, 3]
    await heartbeat_loop(
        poster, lambda: devices, interval_seconds=0, iterations=2
    )
    # 3 devices × 2 iterations = 6 calls.
    assert [d for d, _ in poster.calls] == [1, 2, 3, 1, 2, 3]


@pytest.mark.asyncio
async def test_heartbeat_includes_signal_strength_when_provider_returns_value() -> None:
    poster = FakePoster()
    await heartbeat_loop(
        poster,
        lambda: [7],
        signal_strength_provider=lambda _device_id: 22,
        interval_seconds=0,
        iterations=1,
    )
    assert poster.calls == [(7, 22)]


@pytest.mark.asyncio
async def test_heartbeat_skips_signal_strength_when_provider_returns_none() -> None:
    poster = FakePoster()
    await heartbeat_loop(
        poster,
        lambda: [9],
        signal_strength_provider=lambda _device_id: None,
        interval_seconds=0,
        iterations=1,
    )
    assert poster.calls == [(9, None)]


@pytest.mark.asyncio
async def test_heartbeat_swallows_per_device_errors() -> None:
    class FlakyPoster:
        def __init__(self) -> None:
            self.attempts = 0

        async def heartbeat(self, device_id: int, signal_strength: int | None) -> None:
            self.attempts += 1
            if device_id == 2:
                raise RuntimeError("boom")

    poster = FlakyPoster()
    await heartbeat_loop(
        poster, lambda: [1, 2, 3], interval_seconds=0, iterations=1
    )
    # All three devices should have been attempted despite the boom on #2.
    assert poster.attempts == 3


@pytest.mark.asyncio
async def test_heartbeat_picks_up_dynamic_device_set_changes() -> None:
    poster = FakePoster()
    state = {"ids": [1]}

    def get_ids() -> list[int]:
        if len(poster.calls) == 1:
            state["ids"] = [1, 2]
        return state["ids"]

    await heartbeat_loop(
        poster, get_ids, interval_seconds=0, iterations=2
    )
    # iter1: [1] → call #1
    # iter2: [1, 2] → calls #2 #3
    assert [d for d, _ in poster.calls] == [1, 1, 2]

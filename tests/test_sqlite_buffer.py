"""SqliteEventBuffer durability + FIFO + capacity tests.

Spec: service-communication § Requirement: 云-边控制面 — Scenario "断
线重连与本地 buffer".

Tests are file-backed (in pytest tmp_path) rather than ``:memory:`` so
that crash-survival is actually exercised — close the buffer, re-open
the same path, prove the data is still there.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from isales_common.proto import cloud_edge_pb2 as pb

from isales_telephony.transport.sqlite_buffer import (
    SqliteEventBuffer,
    SqliteEventBufferError,
)


def _call_event(call_id: str, kind: str = "connected") -> pb.Edge2Cloud:
    if kind == "connected":
        ev = pb.CallEvent(call_id=call_id, connected=pb.Connected())
    elif kind == "ringing":
        ev = pb.CallEvent(call_id=call_id, ringing=pb.Ringing())
    else:
        raise ValueError(kind)
    return pb.Edge2Cloud(call_event=ev)


def _hardware_alert(device_id: int, signal: int) -> pb.Edge2Cloud:
    return pb.Edge2Cloud(
        hardware_alert=pb.HardwareAlert(
            device_id=device_id,
            signal_lost=pb.SignalLost(last_signal_strength=signal),
        ),
    )


# --------------------------------------------------------------------------
# Construction validation
# --------------------------------------------------------------------------


def test_zero_capacity_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="capacity_rows"):
        SqliteEventBuffer(path=tmp_path / "x.db", capacity_rows=0)


def test_negative_capacity_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="capacity_rows"):
        SqliteEventBuffer(path=tmp_path / "x.db", capacity_rows=-1)


# --------------------------------------------------------------------------
# Open / close
# --------------------------------------------------------------------------


def test_open_creates_parent_dir(tmp_path: Path) -> None:
    """SqliteEventBuffer should create missing parent dirs — the deploy
    script may not have created the state dir yet on first run."""
    nested = tmp_path / "missing" / "dirs" / "edge_buffer.db"
    buf = SqliteEventBuffer(path=nested)
    try:
        buf.open()
        assert nested.parent.is_dir()
        assert nested.exists()
    finally:
        buf.close()


def test_open_is_idempotent(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    try:
        buf.open()
        buf.open()  # second open: no-op
        assert len(buf) == 0
    finally:
        buf.close()


def test_close_is_idempotent(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    buf.open()
    buf.close()
    buf.close()  # second close: no-op


def test_operations_before_open_raise(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    with pytest.raises(SqliteEventBufferError, match="not opened"):
        buf.append(_call_event("c-1"))
    with pytest.raises(SqliteEventBufferError, match="not opened"):
        list(buf.iter_pending())
    with pytest.raises(SqliteEventBufferError, match="not opened"):
        buf.delete_through(0)


# --------------------------------------------------------------------------
# Append + iter FIFO
# --------------------------------------------------------------------------


def test_append_returns_increasing_seq(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    buf.open()
    try:
        s1 = buf.append(_call_event("c-1"))
        s2 = buf.append(_call_event("c-2"))
        s3 = buf.append(_call_event("c-3"))
        assert s1 < s2 < s3
    finally:
        buf.close()


def test_iter_pending_yields_in_seq_order(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    buf.open()
    try:
        buf.append(_call_event("c-1"))
        buf.append(_call_event("c-2", "ringing"))
        buf.append(_hardware_alert(device_id=7, signal=2))
        pairs = list(buf.iter_pending())
        assert len(pairs) == 3
        # Seq monotonically increasing.
        assert pairs[0][0] < pairs[1][0] < pairs[2][0]
        # Payload survives serialisation round-trip.
        assert pairs[0][1].call_event.call_id == "c-1"
        assert pairs[0][1].call_event.WhichOneof("kind") == "connected"
        assert pairs[1][1].call_event.call_id == "c-2"
        assert pairs[1][1].call_event.WhichOneof("kind") == "ringing"
        assert pairs[2][1].hardware_alert.device_id == 7
        assert pairs[2][1].hardware_alert.signal_lost.last_signal_strength == 2
    finally:
        buf.close()


def test_iter_pending_empty_buffer_yields_nothing(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    buf.open()
    try:
        assert list(buf.iter_pending()) == []
    finally:
        buf.close()


def test_len_reflects_pending_count(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    buf.open()
    try:
        assert len(buf) == 0
        buf.append(_call_event("c-1"))
        assert len(buf) == 1
        buf.append(_call_event("c-2"))
        assert len(buf) == 2
    finally:
        buf.close()


# --------------------------------------------------------------------------
# delete_through / clear
# --------------------------------------------------------------------------


def test_delete_through_removes_up_to_and_including(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    buf.open()
    try:
        s1 = buf.append(_call_event("c-1"))
        s2 = buf.append(_call_event("c-2"))
        _ = buf.append(_call_event("c-3"))
        # ACK up through s2 (the second message). s1 + s2 should be
        # gone; only c-3 remains.
        deleted = buf.delete_through(s2)
        assert deleted == 2
        remaining = list(buf.iter_pending())
        assert len(remaining) == 1
        assert remaining[0][1].call_event.call_id == "c-3"
        # The remaining seq is greater than the deleted ones.
        assert remaining[0][0] > s1
    finally:
        buf.close()


def test_delete_through_beyond_max_seq_is_no_op_safe(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    buf.open()
    try:
        buf.append(_call_event("c-1"))
        # No row has seq <= -1, so nothing deleted.
        assert buf.delete_through(-1) == 0
        # All rows have seq <= 1_000_000_000.
        assert buf.delete_through(1_000_000_000) == 1
        assert len(buf) == 0
    finally:
        buf.close()


def test_clear_empties_buffer(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    buf.open()
    try:
        for i in range(5):
            buf.append(_call_event(f"c-{i}"))
        n = buf.clear()
        assert n == 5
        assert len(buf) == 0
    finally:
        buf.close()


# --------------------------------------------------------------------------
# Durability — close + reopen preserves rows
# --------------------------------------------------------------------------


def test_data_survives_close_and_reopen(tmp_path: Path) -> None:
    """The core durability promise of choosing SQLite over in-memory:
    crash / restart preserves pending events for replay on reconnect."""
    path = tmp_path / "edge_buffer.db"

    buf1 = SqliteEventBuffer(path=path)
    buf1.open()
    try:
        buf1.append(_call_event("c-1"))
        buf1.append(_call_event("c-2"))
        buf1.append(_hardware_alert(device_id=1, signal=5))
    finally:
        buf1.close()

    # Imagine the process dies here. New process opens the same path.
    buf2 = SqliteEventBuffer(path=path)
    buf2.open()
    try:
        assert len(buf2) == 3
        pairs = list(buf2.iter_pending())
        # FIFO order preserved across reopen.
        kinds = [p[1].WhichOneof("payload") for p in pairs]
        assert kinds == ["call_event", "call_event", "hardware_alert"]
        # ACK can resume against seqs from the previous instance.
        buf2.delete_through(pairs[1][0])
        assert len(buf2) == 1
    finally:
        buf2.close()


# --------------------------------------------------------------------------
# Capacity / eviction
# --------------------------------------------------------------------------


def test_capacity_evicts_oldest_when_exceeded(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db", capacity_rows=3)
    buf.open()
    try:
        # Append 5 events; capacity=3 → oldest 2 evicted.
        seqs = [buf.append(_call_event(f"c-{i}")) for i in range(5)]
        assert len(buf) == 3
        assert buf.evicted_rows == 2

        # The surviving rows are the 3 newest, in seq order.
        remaining = list(buf.iter_pending())
        ids = [p[1].call_event.call_id for p in remaining]
        assert ids == ["c-2", "c-3", "c-4"]
        # And their seqs are the latest assigned.
        assert remaining[0][0] == seqs[2]
        assert remaining[-1][0] == seqs[4]
    finally:
        buf.close()


def test_capacity_eviction_counter_accumulates_across_calls(tmp_path: Path) -> None:
    buf = SqliteEventBuffer(path=tmp_path / "x.db", capacity_rows=2)
    buf.open()
    try:
        buf.append(_call_event("c-1"))
        buf.append(_call_event("c-2"))
        assert buf.evicted_rows == 0
        buf.append(_call_event("c-3"))  # evicts c-1
        assert buf.evicted_rows == 1
        buf.append(_call_event("c-4"))  # evicts c-2
        assert buf.evicted_rows == 2
        assert len(buf) == 2
    finally:
        buf.close()


def test_evicted_counter_resets_with_new_instance(tmp_path: Path) -> None:
    """The eviction counter is per-instance — closing + reopening
    starts the counter fresh. (The underlying SQLite rows are still
    there; only the in-memory counter resets.)"""
    path = tmp_path / "x.db"
    buf1 = SqliteEventBuffer(path=path, capacity_rows=1)
    buf1.open()
    buf1.append(_call_event("c-1"))
    buf1.append(_call_event("c-2"))  # evicts c-1
    assert buf1.evicted_rows == 1
    buf1.close()

    buf2 = SqliteEventBuffer(path=path, capacity_rows=1)
    buf2.open()
    try:
        assert buf2.evicted_rows == 0
        # The previously-stored row is still there.
        assert len(buf2) == 1
    finally:
        buf2.close()


# --------------------------------------------------------------------------
# Realistic interleaved drain pattern
# --------------------------------------------------------------------------


def test_drain_loop_pattern(tmp_path: Path) -> None:
    """Simulate the production drain pattern: while drained, more
    events arrive; those stay for the next drain pass."""
    buf = SqliteEventBuffer(path=tmp_path / "x.db")
    buf.open()
    try:
        # Pre-disconnect events.
        for i in range(3):
            buf.append(_call_event(f"pre-{i}"))

        # Reconnect; drain first batch. Each "send success" → delete_through.
        drained_ids: list[str] = []
        for seq, msg in buf.iter_pending():
            drained_ids.append(msg.call_event.call_id)
            buf.delete_through(seq)
        assert drained_ids == ["pre-0", "pre-1", "pre-2"]
        assert len(buf) == 0

        # Mid-flight, more events arrive (this is post-reconnect, so
        # they'd normally go straight to the wire — but the grpc client
        # may still buffer them transiently; we just verify the buffer
        # accepts them and a second drain pass works).
        for i in range(2):
            buf.append(_call_event(f"post-{i}"))

        drained_ids.clear()
        for seq, msg in buf.iter_pending():
            drained_ids.append(msg.call_event.call_id)
            buf.delete_through(seq)
        assert drained_ids == ["post-0", "post-1"]
        assert len(buf) == 0
    finally:
        buf.close()

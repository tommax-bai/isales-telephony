"""Durable edge-side event buffer backed by SQLite.

Spec: service-communication § Requirement: 云-边控制面 — Scenario "断线
重连与本地 buffer" ("中断期间产生的 CallEvent / HardwareAlert SHALL 写
入边缘本地 SQLite buffer 顺序追加; 重连后 SHALL 按写入顺序补发 buffer
中尚未确认的事件 → server 显式 ACK 后从 buffer 删除"); "buffer 容量
SHALL 至少容纳 24 h 离线事件 (按通话量估算 ≤ 100 MB), 超容量 MAY 丢弃
最早事件并打告警日志".

This module replaces the in-memory ``deque(maxlen=...)`` in
:class:`isales_telephony.transport.grpc_client.CloudEdgeGrpcClient` for
production. v1.0 PoC keeps both available — the in-memory buffer is the
test default; SQLite is wired in when an edge process is configured with
a ``--state-dir`` (see deployment-topology spec). Integrating the two
is the follow-up PR; this module ships the durable storage in isolation
so it can be exercised end-to-end against the spec contract first.

**Scope:**

- Only buffers ``Edge2Cloud`` frames that the spec marks as "should buffer"
  — i.e. ``CallEvent`` and ``HardwareAlert``. ``Heartbeat`` / ``DialAck``
  are critical=True (raise on disconnect) per the gRPC client contract;
  this buffer is irrelevant to them.
- Single-process, single-edge. The buffer file lives in the edge's
  ``state_dir`` (e.g. ``~/Library/Application Support/isales/sqlite/
  edge_buffer.db`` on macOS, ``%APPDATA%\\isales\\sqlite\\edge_buffer.db``
  on Windows). Concurrent processes opening the same file are not
  supported (SQLite would serialise but our drain ordering assumes one
  reader).

**v1.0 ACK semantics:**

The wire protocol (``cloud_edge.proto``) currently lacks an explicit
edge → cloud ACK message. v1.0 treats "frame flushed from the buffer
onto the grpc outbound queue" as ACK — practically, gRPC bidi over
HTTP/2 + TCP is reliable, so the delivery probability is high. A
proto-level ACK + ``mark_acked(seq)`` lives in the follow-up (would
let us tolerate process crashes mid-flush, currently we lose those
frames). The buffer API is designed so adding ``mark_acked`` later is
non-breaking.

**Crash safety:** SQLite WAL mode + ``synchronous=NORMAL``. The buffer
loses at most the latest one or two transactions on a power cut, never
more. ``NORMAL`` (rather than ``FULL``) is acceptable here because the
buffer is a *replay log*, not authoritative state — losing the last
heartbeat-period of events is bounded blast radius.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from isales_common.proto import cloud_edge_pb2 as pb

logger = logging.getLogger(__name__)


# Spec § Scenario "断线重连": "buffer 容量 SHALL 至少容纳 24 h 离线事件".
# Per-call event count: optimistic ~10 events/call (ringing / connected /
# remote_hangup / a few HardwareAlerts). 1000 seats × 50 calls/day = 50k
# events/day per edge — far below the 100k default cap, which gives ~2
# days of headroom even at peak.
DEFAULT_CAPACITY_ROWS: Final[int] = 100_000


# DDL is checked-in static — schema changes require a v2 buffer with an
# explicit migration in this module.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL    NOT NULL,
    payload    BLOB    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_events_seq
    ON pending_events (seq);
"""


class SqliteEventBufferError(RuntimeError):
    """Raised for buffer-specific I/O / corruption / state issues.

    Distinct from generic :class:`sqlite3.Error` so callers can decide
    whether to fail loudly or fall back to in-memory.
    """


class SqliteEventBuffer:
    """Durable FIFO buffer for ``Edge2Cloud`` frames awaiting cloud delivery.

    Lifecycle::

        buf = SqliteEventBuffer(path="/var/lib/isales/edge_buffer.db")
        buf.open()
        try:
            seq = buf.append(event)
            # ... later, when reconnected:
            for seq, msg in buf.iter_pending():
                await client.send(msg)
                buf.delete_through(seq)  # weak ACK (v1)
        finally:
            buf.close()

    The class is **synchronous** by design — SQLite is sync; per-call
    latency at the buffer level is sub-millisecond on modern SSDs. The
    caller (production: :class:`CloudEdgeGrpcClient`) decides whether
    to wrap individual calls in :func:`asyncio.to_thread` based on its
    own latency budget.
    """

    def __init__(
        self,
        *,
        path: str | Path,
        capacity_rows: int = DEFAULT_CAPACITY_ROWS,
    ) -> None:
        if capacity_rows <= 0:
            raise ValueError("capacity_rows must be positive")
        self._path = Path(path)
        self._capacity = capacity_rows
        self._conn: sqlite3.Connection | None = None
        # Counters useful for ops / D2 hardware-observability.
        self.evicted_rows = 0

    # ----- lifecycle ----------------------------------------------------

    def open(self) -> None:
        """Open (or create) the SQLite file. Idempotent on the same
        instance; a second :meth:`open` is a no-op once connected.
        """
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(
                str(self._path),
                isolation_level=None,  # autocommit; explicit BEGIN below
                check_same_thread=True,
                timeout=5.0,
            )
            # WAL: concurrent readers + faster writes + crash-safe.
            conn.execute("PRAGMA journal_mode = WAL;")
            # NORMAL: lose at most the last transaction on power cut.
            # FULL is overkill for a replay log.
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA temp_store = MEMORY;")
            conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise SqliteEventBufferError(
                f"failed to open buffer at {self._path}: {exc}",
            ) from exc
        self._conn = conn

    def close(self) -> None:
        """Flush + close the connection. Idempotent."""
        if self._conn is None:
            return
        try:
            self._conn.close()
        except sqlite3.Error:
            logger.exception("sqlite_buffer_close_error")
        finally:
            self._conn = None

    # ----- writes -------------------------------------------------------

    def append(self, message: pb.Edge2Cloud) -> int:
        """Append a frame; return its assigned ``seq``.

        If the buffer is at or above capacity after the insert, evicts
        the oldest rows until under cap. Returns the inserted row's seq
        (BEFORE any eviction — eviction never targets the just-inserted
        row, since eviction goes oldest-first).

        Raises :class:`SqliteEventBufferError` on disk / serialization
        errors; caller decides whether to drop the event or escalate.
        """
        conn = self._require_open()
        payload = message.SerializeToString()
        try:
            cur = conn.execute(
                "INSERT INTO pending_events (created_at, payload) "
                "VALUES (strftime('%s','now')+0.0, ?)",
                (payload,),
            )
            seq = int(cur.lastrowid or 0)
        except sqlite3.Error as exc:
            raise SqliteEventBufferError(f"append failed: {exc}") from exc

        # Enforce capacity. Cheap COUNT(*) because the table has at most
        # `capacity_rows` rows in steady state.
        evicted = self._enforce_capacity_locked(conn)
        if evicted:
            self.evicted_rows += evicted
            logger.warning(
                "sqlite_buffer_capacity_evicted",
                extra={"evicted": evicted, "capacity": self._capacity},
            )
        return seq

    # ----- reads --------------------------------------------------------

    def iter_pending(self) -> Iterator[tuple[int, pb.Edge2Cloud]]:
        """Yield ``(seq, message)`` pairs in seq order, smallest first.

        The iterator is a **snapshot** at call time — concurrent
        :meth:`append` calls are NOT reflected. Callers typically:

        1. Open the iterator immediately after reconnecting.
        2. For each ``(seq, msg)``: send it; on success
           :meth:`delete_through(seq)`.
        3. When the iterator is exhausted, the buffer is empty *as of
           reconnect time*; new appends during the drain stay queued
           for the next drain pass.

        The iterator does NOT hold a transaction open across yields —
        it reads in pages to keep memory bounded. Mutations during
        iteration are allowed but may produce missing rows if seq jumps
        backward (which the autoincrement column never does).
        """
        conn = self._require_open()
        last_yielded = -1
        page_size = 256
        while True:
            try:
                rows = conn.execute(
                    "SELECT seq, payload FROM pending_events "
                    "WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                    (last_yielded, page_size),
                ).fetchall()
            except sqlite3.Error as exc:
                raise SqliteEventBufferError(f"read failed: {exc}") from exc
            if not rows:
                return
            for seq, payload in rows:
                msg = pb.Edge2Cloud()
                try:
                    msg.ParseFromString(payload)
                except Exception as exc:
                    # Corrupt row — log + skip rather than abort the
                    # whole drain. Production should never hit this
                    # (we control both serialisation and storage).
                    logger.error(  # noqa: TRY400 — intentional log+continue, not propagate
                        "sqlite_buffer_corrupt_row",
                        extra={"seq": seq, "error": str(exc)},
                    )
                    last_yielded = seq
                    continue
                yield seq, msg
                last_yielded = seq

    def __len__(self) -> int:
        """Current number of pending rows."""
        conn = self._require_open()
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_events",
        ).fetchone()
        return int(row[0]) if row else 0

    # ----- mutations ----------------------------------------------------

    def delete_through(self, seq: int) -> int:
        """Delete all rows with ``row.seq <= seq``. Returns the count
        of deleted rows.

        v1.0 weak-ACK semantics: caller invokes this immediately after
        :meth:`CloudEdgeGrpcClient.send` returns. A future proto-level
        ACK message would let us delay this call until the cloud
        explicitly confirms.
        """
        conn = self._require_open()
        try:
            cur = conn.execute(
                "DELETE FROM pending_events WHERE seq <= ?",
                (seq,),
            )
        except sqlite3.Error as exc:
            raise SqliteEventBufferError(f"delete_through failed: {exc}") from exc
        return cur.rowcount

    def clear(self) -> int:
        """Delete every row. Returns the count of deleted rows. Useful
        for tests + admin reset."""
        conn = self._require_open()
        try:
            cur = conn.execute("DELETE FROM pending_events")
        except sqlite3.Error as exc:
            raise SqliteEventBufferError(f"clear failed: {exc}") from exc
        return cur.rowcount

    # ----- internals ----------------------------------------------------

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise SqliteEventBufferError(
                "buffer not opened — call .open() first",
            )
        return self._conn

    def _enforce_capacity_locked(self, conn: sqlite3.Connection) -> int:
        """If the table is over capacity, evict the oldest rows down to
        ``capacity``. Caller holds the (implicit autocommit) lock for
        the duration. Returns the number evicted (0 if under cap)."""
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_events",
        ).fetchone()
        count = int(row[0]) if row else 0
        if count <= self._capacity:
            return 0
        excess = count - self._capacity
        cur = conn.execute(
            "DELETE FROM pending_events WHERE seq IN ("
            "  SELECT seq FROM pending_events ORDER BY seq ASC LIMIT ?"
            ")",
            (excess,),
        )
        return int(cur.rowcount or 0)


__all__ = [
    "DEFAULT_CAPACITY_ROWS",
    "SqliteEventBuffer",
    "SqliteEventBufferError",
]

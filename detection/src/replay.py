#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge: Session Quality & Session-to-Action Conversion
Step 2: replay the sanitised event log as a live feed.

WHY THIS SHAPE
--------------
The scoring service must not care whether events arrive from a historical
parquet file or from a live Kafka topic. So `ReplayConsumer` implements the
same surface a Kafka consumer exposes, and the service loop is literally the
same code in both cases:

    for record in consumer:                 # ReplayConsumer *or* KafkaConsumer
        key, event = record.key, record.value
        state = accumulator.update(event)   # src/features.py
        ...

    while running:                          # or the poll() form
        for tp, records in consumer.poll(timeout_ms=500).items():
            for record in records:
                ...

Swapping in a real consumer is a constructor change in one place. Nothing here
imports or requires kafka - this is an interface, not a dependency.

THE HARD CONSTRAINT: NO LOOK-AHEAD
----------------------------------
Events are handed out strictly one at a time, in timestamp order, and the
consumer never exposes what is coming next:

  * no __len__, no peek(), no remaining(), no .rows / .frame attribute
  * rows are pulled from DuckDB in Arrow batches and dropped once yielded, so
    a whole session is never resident and never reachable
  * every record is frozen, and its payload is a read-only mapping

If a downstream function needs the whole session to work, it is wrong - at
event N it must be impossible to know that event N+1 exists at all.

Reads data/clean/events.parquet (written by scripts/sanitize.py). It never
touches the delivered "data set " folder, so the credential-bearing URLs
stripped in step 1 cannot re-enter the pipeline here.

Usage:  python3 src/replay.py            # self-check + demo replay
"""
from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import timezone
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent

CONFIG = {
    # Written by scripts/sanitize.py. Never read the raw CSV from here.
    "source": REPO_ROOT / "data" / "clean" / "events.parquet",

    # Names the live deployment would use. Kept here so replay and production
    # differ by configuration, not by code.
    "topic": "casino.events.v1",
    "n_partitions": 12,          # Kafka would key-partition on PlayerID

    # Session key. NOT `session` alone: 26 session ids collide across 2
    # players (BASELINE.md §1b), so a bare session id is not unique.
    "session_key": ("PlayerID", "sid"),

    # Arrow batch size for the DuckDB -> Python hand-off. Bounds memory; it is
    # an I/O detail and is never visible to a consumer of the stream.
    "fetch_batch_rows": 4096,
}


# ─────────────────────── the record on the wire ───────────────────────
@dataclass(frozen=True, slots=True)
class TopicPartition:
    """Mirrors kafka.TopicPartition."""
    topic: str
    partition: int


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One message. Field names mirror a Kafka ConsumerRecord.

    `value` is a read-only mapping of the event's own columns - and only its
    own. There is deliberately no reference to the session, the batch, or any
    neighbouring record.
    """
    topic: str
    partition: int
    offset: int
    key: tuple[str, int]          # (PlayerID, session)
    timestamp: int                # epoch milliseconds
    value: Mapping[str, Any]

    @property
    def event_name(self) -> str:
        return self.value["event_name"]


@runtime_checkable
class EventConsumer(Protocol):
    """The surface the scoring service is allowed to use.

    A kafka-python KafkaConsumer satisfies this. So does ReplayConsumer. Note
    what is absent: nothing here can report how many messages are left.
    """
    def __iter__(self) -> Iterator[EventRecord]: ...
    def __next__(self) -> EventRecord: ...
    def poll(self, timeout_ms: int = ..., max_records: int | None = ...
             ) -> dict[TopicPartition, list[EventRecord]]: ...
    def close(self) -> None: ...


# ─────────────────────────── the consumer ───────────────────────────
@dataclass
class ReplayConsumer:
    """Kafka-shaped consumer over a finite, ordered event stream.

    Construct it via `replay_session()` or `stream_sessions()` rather than
    directly; those own the ordering guarantees.
    """
    topic: str
    _records: Iterator[EventRecord] = field(repr=False)
    _closed: bool = field(default=False, repr=False)
    _last_ts: int = field(default=-1, repr=False)

    # -- iterator form: `for record in consumer:` --------------------------
    def __iter__(self) -> "ReplayConsumer":
        return self

    def __next__(self) -> EventRecord:
        if self._closed:
            raise StopIteration
        record = next(self._records)          # raises StopIteration at the end
        # Ordering is a contract, so assert it rather than trust it.
        if record.timestamp < self._last_ts:
            raise RuntimeError(
                f"out-of-order replay: {record.timestamp} < {self._last_ts}")
        self._last_ts = record.timestamp
        return record

    # -- poll form: `consumer.poll(timeout_ms=500)` ------------------------
    def poll(self, timeout_ms: int = 0, max_records: int | None = None
             ) -> dict[TopicPartition, list[EventRecord]]:
        """Batched read, shaped like KafkaConsumer.poll().

        `timeout_ms` is accepted for signature compatibility and ignored: a
        replay has no wait state. Returns {} at end of stream, which is what a
        live consumer returns when the topic is idle - so the service loop
        terminating condition is the same shape in both worlds.
        """
        out: dict[TopicPartition, list[EventRecord]] = {}
        limit = max_records if max_records is not None else 500
        for _ in range(limit):
            try:
                record = next(self)
            except StopIteration:
                break
            out.setdefault(TopicPartition(record.topic, record.partition),
                           []).append(record)
        return out

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "ReplayConsumer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Explicitly refused, so the no-look-ahead rule fails loudly rather than
    # silently: a caller that wants a length wants the whole session.
    def __len__(self) -> int:
        raise TypeError(
            "ReplayConsumer has no length - a live consumer cannot know how "
            "many events a session will contain. Accumulate incrementally.")


# ─────────────────────────── the source ───────────────────────────
def _partition_for(player_id: str, n: int) -> int:
    """Stable key-based partitioning, as Kafka would do on the message key."""
    return sum(player_id.encode()) % n if player_id else 0


def _connect(source: Path) -> duckdb.DuckDBPyConnection:
    if not source.exists():
        raise FileNotFoundError(
            f"{source} not found - run `python3 scripts/sanitize.py` first")
    return duckdb.connect()


def _sql_literal(p: Path) -> str:
    return str(p).replace("'", "''")


def _row_stream(source: Path, where: str, params: Sequence[Any],
                topic: str, n_partitions: int) -> Iterator[EventRecord]:
    """Yield EventRecords in (player, session, time) order, one at a time.

    DuckDB streams the sorted result to us in Arrow batches; each batch is
    converted, yielded row by row, and then dropped. Nothing accumulates.
    `file_row_number` is the tiebreak so replays of tied timestamps are
    byte-for-byte reproducible.
    """
    con = _connect(source)
    try:
        rel = (f"read_parquet('{_sql_literal(source)}', file_row_number=true)")
        cur = con.execute(f"""
            SELECT * FROM {rel}
            WHERE {where}
            ORDER BY PlayerID, sid, ts, file_row_number
        """, list(params))

        offsets: dict[int, int] = {}
        n = CONFIG["fetch_batch_rows"]
        reader = (cur.to_arrow_reader(n) if hasattr(cur, "to_arrow_reader")
                  else cur.fetch_record_batch(n))
        for batch in reader:
            for row in batch.to_pylist():
                ts = row.pop("ts")
                row.pop("file_row_number", None)
                player, sid = row["PlayerID"], row["sid"]
                part = _partition_for(player, n_partitions)
                offsets[part] = offsets.get(part, -1) + 1
                yield EventRecord(
                    topic=topic,
                    partition=part,
                    offset=offsets[part],
                    key=(player, sid),
                    # the log is UTC ('...Z'); DuckDB hands back a naive
                    # datetime, so pin the zone rather than assume the host's
                    timestamp=int(ts.replace(tzinfo=timezone.utc).timestamp()
                                  * 1000),
                    value=MappingProxyType(row),
                )
            del batch                     # drop the window we just emitted
    finally:
        con.close()


def replay_session(player_id: str, session: int, *,
                   source: Path | None = None) -> ReplayConsumer:
    """A consumer over exactly one (PlayerID, session), in timestamp order.

    This is the unit the scoring service sees in production: one key's events,
    arriving one at a time, with no idea what comes next.
    """
    source = source or CONFIG["source"]
    stream = _row_stream(source, "PlayerID = ? AND sid = ?",
                         [player_id, int(session)],
                         CONFIG["topic"], CONFIG["n_partitions"])
    return ReplayConsumer(topic=CONFIG["topic"], _records=stream)


def stream_sessions(keys: Sequence[tuple[str, int]] | None = None, *,
                    source: Path | None = None
                    ) -> Iterator[tuple[tuple[str, int], ReplayConsumer]]:
    """Replay many sessions from a single ordered scan.

    Yields (session_key, consumer). The consumer for one session must be
    consumed before the next is yielded; if a caller stops early, the
    remaining events of that session are drained and discarded - never handed
    back, never inspected.

    This is the multi-key harness form. The live service does not iterate
    sessions like this; it holds many keys open at once against one partition
    assignment. Both end up calling the same per-event code.
    """
    source = source or CONFIG["source"]
    if keys is None:
        where, params = "TRUE", []
    else:
        pairs = ", ".join(["(?, ?)"] * len(keys))
        where = f"(PlayerID, sid) IN ({pairs})"
        params = [v for k, s in keys for v in (k, int(s))]

    stream = _row_stream(source, where, params,
                         CONFIG["topic"], CONFIG["n_partitions"])

    pending: EventRecord | None = None
    while True:
        if pending is None:
            try:
                pending = next(stream)
            except StopIteration:
                return
        key = pending.key
        # A one-session view over the shared stream. `carry` is the single
        # record of the *next* session that we had to read to detect the
        # boundary; it is never visible to this session's consumer.
        carry: list[EventRecord] = []

        def _one_session(first: EventRecord = pending) -> Iterator[EventRecord]:
            yield first
            for record in stream:
                if record.key != key:
                    carry.append(record)
                    return
                yield record

        consumer = ReplayConsumer(topic=CONFIG["topic"], _records=_one_session())
        yield key, consumer
        for _ in consumer:                # drain if the caller stopped early
            pass
        pending = carry[0] if carry else None


def list_sessions(where: str = "TRUE", *, source: Path | None = None
                  ) -> list[tuple[str, int]]:
    """HARNESS ONLY - choose which keys to replay.

    Deciding *which* sessions to feed the service is the offline equivalent of
    choosing a partition assignment. It is not a feature and it is not a
    label: the result of this function must never reach features.py or
    trigger.py. It returns keys and nothing else, so it cannot.
    """
    source = source or CONFIG["source"]
    con = _connect(source)
    try:
        rows = con.execute(f"""
            SELECT DISTINCT PlayerID, sid
            FROM read_parquet('{_sql_literal(source)}')
            WHERE {where}
            ORDER BY PlayerID, sid
        """).fetchall()
    finally:
        con.close()
    return [(r[0], int(r[1])) for r in rows]


# ─────────────────────────── self-check ───────────────────────────
def _self_check() -> int:
    src = CONFIG["source"]
    if not src.exists():
        print(f"ERROR: {src} not found - run scripts/sanitize.py first")
        return 1

    keys = list_sessions(
        "PlayerID IN (SELECT PlayerID FROM read_parquet('"
        + _sql_literal(src) + "') WHERE event_name = 'betslip_add_bet')")
    print(f"sessions available for those players: {len(keys):,}")

    # pick a session with a decent number of events to demo
    con = duckdb.connect()
    player, sid, n = con.execute(f"""
        SELECT PlayerID, sid, count(*) n
        FROM read_parquet('{_sql_literal(src)}')
        GROUP BY 1, 2 HAVING n BETWEEN 8 AND 30
        ORDER BY PlayerID, sid LIMIT 1""").fetchone()
    con.close()

    print(f"\nreplaying ({player[:8]}…{player[-4:]}, {sid}) - {n} events\n")
    seen, last = 0, None
    for record in replay_session(player, sid):
        seen += 1
        assert record.key == (player, sid), "record leaked from another session"
        assert last is None or record.timestamp >= last, "out of order"
        last = record.timestamp
        if seen <= 6:
            print(f"  offset={record.offset:<4} part={record.partition:<3} "
                  f"{record.value['timestamp']}  {record.event_name:<20} "
                  f"{record.value.get('platform')}")
    print(f"  … {seen} events total")
    assert seen == n, f"replay dropped events: {seen} != {n}"

    # no-look-ahead: the payload is read-only and the consumer has no length
    c = replay_session(player, sid)
    first = next(c)
    try:
        first.value["event_name"] = "tampered"          # type: ignore[index]
        print("FAIL: record payload is mutable")
        return 1
    except TypeError:
        pass
    try:
        len(c)
        print("FAIL: consumer exposed a length")
        return 1
    except TypeError:
        pass
    c.close()

    # poll() form returns the same records as the iterator form
    polled = []
    c2 = replay_session(player, sid)
    while (batch := c2.poll(timeout_ms=100, max_records=4)):
        for _tp, records in batch.items():
            polled.extend(records)
    assert len(polled) == n, f"poll() saw {len(polled)} of {n}"

    # stream_sessions yields disjoint, correctly-keyed sessions
    sample = list_sessions(f"PlayerID = '{player}'")[:5]
    total = 0
    for key, consumer in stream_sessions(sample):
        for record in consumer:
            assert record.key == key
            total += 1
    print(f"\nstream_sessions: {len(sample)} sessions, {total:,} events, "
          f"all correctly keyed")
    print("\nOK  iterator form, poll() form, ordering, immutability, "
          "no-length: all pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())

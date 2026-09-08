#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge: Session Quality & Session-to-Action Conversion
Step 3: incremental feature accumulator.

    acc = FeatureAccumulator(InMemoryStateBackend())
    for record in consumer:            # src/replay.py or a Kafka consumer
        features = acc.update(record.value)

ONE EVENT IN, ALL FEATURES SO FAR OUT. Nothing here reads ahead, buffers a
session, or revisits a decision: the value returned at event N is final and can
never be changed by event N+1. `_test_monotonic_prefix()` proves that.

STATE BACKEND SEAM
------------------
All session state lives behind a `StateBackend` with get / set / expire /
delete. `InMemoryStateBackend` is a plain dict and ships now. A Redis-backed
implementation drops in with no call-site change, so the discipline that makes
that true is enforced here:

  * state is keyed by a flat string, `feat:{PlayerID}:{sid}`  (SESSION_KEY)
  * state values are plain JSON types only - no sets, no tuples, no objects
  * every write slides a TTL, exactly as SET + EXPIRE would
  * state is bounded: screen and window collections are capped, so one long
    session can never grow an unbounded Redis value

`_test_backend_equivalence()` runs the whole pipeline through a backend that
JSON round-trips every value, and asserts identical output.

NO MONEY FEATURES. Stake is not in the event log (DATA_MAP §6.5) and a bet
cannot be attributed to a session at all, so any euro-valued feature here would
be fabricated.

Usage:  python3 src/features.py        # coverage report + tests
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

REPO_ROOT = Path(__file__).resolve().parent.parent

CONFIG = {
    # Trailing window for event velocity, in seconds.
    "velocity_window_s": 60,
    # Floor on the window divisor so the first few seconds of a session do not
    # produce a meaningless 600 events/min.
    "velocity_min_window_s": 10.0,

    # Bounds on state size, so a Redis value stays small. BASELINE §1c: the
    # largest session in the data is 1,542 events.
    "max_tracked_screens": 256,
    "max_window_events": 512,

    # Session state TTL, slid on every update. Comfortably past the p90
    # session length of 63.5 min (BASELINE §1c).
    "state_ttl_s": 4 * 3600,

    # Screen-coverage gate (see assert_screen_coverage).
    "min_screen_coverage": 0.90,
    "strict_screen_coverage": False,
}

SESSION_KEY = "feat:{player_id}:{sid}"

# Every event that represents arriving on a screen. Platform-dependent
# vocabulary (DATA_MAP §5) - filtering on one name silently drops platforms.
VIEW_EVENTS = frozenset({"page_view", "screen_view", "fortuna_screen_view"})


# ───────────────────────── state backend seam ─────────────────────────
@runtime_checkable
class StateBackend(Protocol):
    """The only state surface the accumulator may use.

    Deliberately the intersection of what a dict and Redis can both do. There
    is no scan, no keys(), no iteration: the live service holds many thousands
    of sessions and must never enumerate them.
    """
    def get(self, key: str) -> dict[str, Any] | None: ...
    def set(self, key: str, value: dict[str, Any],
            ttl_s: int | None = None) -> None: ...
    def expire(self, key: str, ttl_s: int) -> None: ...
    def delete(self, key: str) -> None: ...


class InMemoryStateBackend:
    """Dict implementation. Ships now; swapped for Redis without touching
    call sites.

    TTLs are recorded rather than enforced - a single-process replay has no
    clock pressure and evicting mid-replay would corrupt a session. Redis
    enforces them for real; the call pattern is identical either way.
    """

    def __init__(self) -> None:
        self._d: dict[str, dict[str, Any]] = {}
        self._ttl: dict[str, int] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._d.get(key)

    def set(self, key: str, value: dict[str, Any],
            ttl_s: int | None = None) -> None:
        self._d[key] = value
        if ttl_s is not None:
            self._ttl[key] = ttl_s

    def expire(self, key: str, ttl_s: int) -> None:
        if key in self._d:
            self._ttl[key] = ttl_s

    def delete(self, key: str) -> None:
        self._d.pop(key, None)
        self._ttl.pop(key, None)

    def __len__(self) -> int:          # test/observability only
        return len(self._d)


class JsonRoundTripBackend(InMemoryStateBackend):
    """Proves the Redis contract: every value is forced through JSON on the
    way in and out, exactly as a Redis client would serialise it. If the
    accumulator ever stashes a set, a tuple or a datetime, this raises."""

    def get(self, key: str) -> dict[str, Any] | None:
        raw = self._d.get(key)
        return None if raw is None else json.loads(raw)      # type: ignore[arg-type]

    def set(self, key: str, value: dict[str, Any],
            ttl_s: int | None = None) -> None:
        self._d[key] = json.dumps(value, allow_nan=False)    # type: ignore[assignment]
        if ttl_s is not None:
            self._ttl[key] = ttl_s


# ───────────────────────── screen resolution ─────────────────────────
# There is NO single screen field. Each platform names the current screen in a
# different column, and two platforms barely name it at all. Measured on the
# sanitised log (see assert_screen_coverage for the live numbers):
#
#   SB iOS          fortuna_screen_name   100.0% of its VIEW events
#   SB Android      fortuna_screen_name    39.1%   <- SDK gap, see below
#   Casino Android  fortuna_screen_name     6.9%   <- SDK gap, see below
#   GM              page_location path    100.0%
#   web             page_location path    100.0%
#
# On Android `screen_view` rows, fortuna_screen_name is the ONLY screen-bearing
# column that carries anything at all - game_name, on_route, on_origin,
# page_location and every other column are 100% NULL on those rows. That was
# verified column by column; there is no better field to reach for. Coverage is
# also flat across all 20/21 Android players (SB 14-46%, Casino 0-20%), so it
# is an SDK-level omission, not a client-version split we could correct for.
#
# Consequence, stated plainly: screen depth and the back-and-forth signal are
# HIGH confidence on SB iOS / GM / web and LOW confidence on Android. Every
# feature dict therefore carries `screen_coverage`, and trigger.py leans on the
# platform-independent signals (velocity, dwell, add count) so that a
# low-coverage platform degrades rather than misfires.
SCREEN_SOURCE = {
    "SB iOS":         "fortuna_screen_name",
    "SB Android":     "fortuna_screen_name",
    "Casino Android": "fortuna_screen_name",
    "GM":             "page_location",
    "web":            "page_location",
}
# Used only when the platform's primary column is null on a non-VIEW event
# (e.g. GM casino_game_launch carries on_route). Never invents a screen.
SCREEN_FALLBACK = {"GM": "on_route", "web": "on_route"}


def _url_path(url: str) -> str | None:
    """scheme+host+path -> path. The query string is already gone (step 1),
    so there is nothing here that could reintroduce a username or a token."""
    if not url:
        return None
    rest = url.split("://", 1)[-1]
    slash = rest.find("/")
    path = rest[slash:] if slash >= 0 else "/"
    path = path.rstrip("/") or "/"
    return path or None


def resolve_screen(event: Mapping[str, Any]) -> str | None:
    """The screen this event happened on, or None if the platform did not say.

    None is an honest answer and is treated as such downstream: an unnamed
    screen is NOT folded into a shared 'unknown' bucket, because that would
    make every unnamed screen look like a revisit of every other one and
    manufacture a back-and-forth signal that is not in the data.
    """
    platform = event.get("platform")
    col = SCREEN_SOURCE.get(platform)
    if col is None:
        return None
    raw = event.get(col)
    if raw is None or raw == "":
        fb = SCREEN_FALLBACK.get(platform)
        raw = event.get(fb) if fb else None
    if raw is None or raw == "":
        return None
    return _url_path(raw) if col == "page_location" else str(raw)


def assert_screen_coverage(source: Path | None = None,
                           min_coverage: float | None = None,
                           strict: bool | None = None) -> list[dict]:
    """Prove resolve_screen actually resolves, per platform, on VIEW events.

    Hard-fails if any platform resolves to ZERO distinct screens - that means
    the mapping is broken for that platform and the depth features would be
    silently dead. Coverage below the target is reported loudly with the real
    numbers; it raises only under strict=True, because on Android the data
    genuinely does not carry the value (see SCREEN_SOURCE) and a passing
    assertion there would be a lie rather than a check.
    """
    import duckdb

    source = source or (REPO_ROOT / "data" / "clean" / "events.parquet")
    min_cov = CONFIG["min_screen_coverage"] if min_coverage is None else min_coverage
    strict = CONFIG["strict_screen_coverage"] if strict is None else strict
    if not source.exists():
        raise FileNotFoundError(f"{source} - run scripts/sanitize.py first")

    con = duckdb.connect()
    try:
        rows = con.execute(f"""
            SELECT platform, event_name, fortuna_screen_name, page_location,
                   on_route, count(*) AS n
            FROM read_parquet('{str(source).replace("'", "''")}')
            WHERE event_name IN ({','.join(repr(e) for e in sorted(VIEW_EVENTS))})
            GROUP BY ALL
        """).fetchall()
    finally:
        con.close()

    agg: dict[str, dict[str, Any]] = {}
    for platform, event_name, fsn, page_loc, on_route, n in rows:
        ev = {"platform": platform, "event_name": event_name,
              "fortuna_screen_name": fsn, "page_location": page_loc,
              "on_route": on_route}
        screen = resolve_screen(ev)
        a = agg.setdefault(platform, {"platform": platform, "view_events": 0,
                                      "resolved": 0, "screens": set()})
        a["view_events"] += n
        if screen is not None:
            a["resolved"] += n
            a["screens"].add(screen)

    report, failures, below = [], [], []
    for a in sorted(agg.values(), key=lambda x: -x["view_events"]):
        cov = a["resolved"] / a["view_events"] if a["view_events"] else 0.0
        n_screens = len(a["screens"])
        report.append({"platform": a["platform"], "view_events": a["view_events"],
                       "resolved": a["resolved"], "coverage": cov,
                       "distinct_screens": n_screens,
                       "status": "OK" if cov >= min_cov else "BELOW TARGET"})
        if n_screens == 0:
            failures.append(a["platform"])
        elif cov < min_cov:
            below.append((a["platform"], cov, n_screens))

    print(f"\nresolve_screen coverage on VIEW events "
          f"({', '.join(sorted(VIEW_EVENTS))})")
    print(f"  {'platform':<16}{'VIEW events':>12}{'resolved':>10}"
          f"{'coverage':>10}{'screens':>9}  status")
    print(f"  {'-'*16}{'-'*12:>12}{'-'*10:>10}{'-'*10:>10}{'-'*9:>9}  ------")
    for r in report:
        print(f"  {r['platform']:<16}{r['view_events']:>12,}{r['resolved']:>10,}"
              f"{r['coverage']:>9.1%}{r['distinct_screens']:>9,}  {r['status']}")

    if failures:
        raise AssertionError(
            f"resolve_screen yields ZERO distinct screens for {failures} - the "
            f"platform mapping in SCREEN_SOURCE is broken, not merely sparse.")
    if below:
        msg = "; ".join(f"{p} {c:.1%} ({s} screens)" for p, c, s in below)
        if strict:
            raise AssertionError(f"screen coverage below {min_cov:.0%}: {msg}")
        print(f"\n  ⚠️  BELOW the {min_cov:.0%} target: {msg}")
        print("     Not a bug in the mapping - fortuna_screen_name is the only "
              "screen-bearing\n     column present on Android screen_view rows, "
              "and it is null on the rest.\n     Screen features are therefore "
              "LOW confidence on Android; every feature dict\n     reports "
              "`screen_coverage` and trigger.py does not depend on them.")
    return report


# ───────────────────────── the accumulator ─────────────────────────
def _event_ms(event: Mapping[str, Any]) -> int:
    """Event time in epoch ms, from the field a live payload also carries."""
    raw = event["timestamp"]
    if isinstance(raw, (int, float)):
        return int(raw)
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@dataclass
class FeatureAccumulator:
    """update(event) -> features so far. Nothing else. No look-ahead."""

    backend: StateBackend
    config: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.cfg = {**CONFIG, **(self.config or {})}

    @staticmethod
    def state_key(event: Mapping[str, Any]) -> str:
        """Keyed on (PlayerID, sid) - never on sid alone, which collides
        across 2 players for 26 ids (BASELINE §1b)."""
        return SESSION_KEY.format(player_id=event["PlayerID"], sid=event["sid"])

    def _blank(self, now_ms: int, event: Mapping[str, Any]) -> dict[str, Any]:
        # Plain JSON types only - this dict is what Redis would store.
        return {
            "player_id": event["PlayerID"],
            "sid": int(event["sid"]),
            "platform": event.get("platform"),
            "start_ms": now_ms,
            "last_ms": now_ms,
            "n_events": 0,
            "max_dwell_ms": 0,
            "window_ms": [],            # trailing event times, JSON list
            "peak_velocity": 0.0,
            "screens": {},              # screen -> times landed on it
            "screens_overflow": 0,
            "views_total": 0,
            "views_resolved": 0,
            "repeat_screen_events": 0,
            "last_screen": None,
            "add_bet_count": 0,
            "placed_count": 0,
            "placed_leg_count": 0,
            "game_launch_count": 0,
            "last_add_ms": None,
            "window_trimmed": False,
        }

    def update(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Fold one event into session state and return the features so far.

        Every branch is a function of (state so far, this event). There is no
        path in this method that can consult a later event, so the dict
        returned at event N is permanent.
        """
        cfg = self.cfg
        key = self.state_key(event)
        now_ms = _event_ms(event)

        st = self.backend.get(key) or self._blank(now_ms, event)

        # ── time ──────────────────────────────────────────────────────
        dwell_ms = max(0, now_ms - st["last_ms"]) if st["n_events"] else 0
        st["max_dwell_ms"] = max(st["max_dwell_ms"], dwell_ms)
        st["last_ms"] = now_ms
        st["n_events"] += 1
        elapsed_ms = now_ms - st["start_ms"]
        # A session can switch platform mid-flight (2 of 13,286 do); the
        # current platform is the one that matters for screen resolution.
        st["platform"] = event.get("platform") or st["platform"]

        # ── velocity over a trailing window ───────────────────────────
        win_ms = cfg["velocity_window_s"] * 1000
        window = [t for t in st["window_ms"] if now_ms - t <= win_ms]
        window.append(now_ms)
        if len(window) > cfg["max_window_events"]:
            window = window[-cfg["max_window_events"]:]
            st["window_trimmed"] = True
        st["window_ms"] = window
        span_s = max(cfg["velocity_min_window_s"],
                     min(elapsed_ms / 1000.0, cfg["velocity_window_s"]))
        velocity = len(window) / span_s * 60.0
        st["peak_velocity"] = max(st["peak_velocity"], velocity)

        # ── screens: depth and back-and-forth ─────────────────────────
        name = event.get("event_name")
        screen = resolve_screen(event)
        if name in VIEW_EVENTS:
            st["views_total"] += 1
            if screen is not None:
                st["views_resolved"] += 1
                seen = st["screens"].get(screen)
                if seen is None:
                    if len(st["screens"]) < cfg["max_tracked_screens"]:
                        st["screens"][screen] = 1
                    else:
                        st["screens_overflow"] += 1
                else:
                    st["screens"][screen] = seen + 1
                    # Back-and-forth: landing again on a screen already seen.
                    # A refresh of the screen we are already on does not count.
                    if screen != st["last_screen"]:
                        st["repeat_screen_events"] += 1
                st["last_screen"] = screen

        # ── funnel counters ───────────────────────────────────────────
        if name == "betslip_add_bet":
            st["add_bet_count"] += 1
            st["last_add_ms"] = now_ms
        elif name == "betslip_placed":        # per SLIP - the conversion
            st["placed_count"] += 1
        elif name == "betslip_placed_bet":    # per LEG, 6.8 per slip - NOT a
            st["placed_leg_count"] += 1       # conversion (BASELINE §4)
        elif name == "casino_game_launch":
            st["game_launch_count"] += 1

        self.backend.set(key, st, ttl_s=cfg["state_ttl_s"])
        self.backend.expire(key, cfg["state_ttl_s"])   # slide TTL, as Redis would

        return self._emit(st, dwell_ms, elapsed_ms, velocity, screen, name)

    @staticmethod
    def _emit(st: dict[str, Any], dwell_ms: int, elapsed_ms: int,
              velocity: float, screen: str | None,
              name: str | None) -> dict[str, Any]:
        views = st["views_total"]
        return {
            # identity
            "player_id": st["player_id"],
            "sid": st["sid"],
            "platform": st["platform"],
            "event_name": name,
            # time
            "elapsed_s": round(elapsed_ms / 1000.0, 3),
            "dwell_s": round(dwell_ms / 1000.0, 3),
            "max_dwell_s": round(st["max_dwell_ms"] / 1000.0, 3),
            # volume / pace
            "n_events": st["n_events"],
            "velocity_epm": round(velocity, 3),
            "peak_velocity_epm": round(st["peak_velocity"], 3),
            # screen depth / back-and-forth
            "screen": screen,
            "distinct_screens": len(st["screens"]),
            "repeat_screen_events": st["repeat_screen_events"],
            "screen_revisit_ratio": round(
                st["repeat_screen_events"] / st["views_resolved"], 4)
            if st["views_resolved"] else 0.0,
            # how far to trust the two fields above, on this platform
            "screen_coverage": round(st["views_resolved"] / views, 4) if views else 0.0,
            # funnel
            "add_bet_count": st["add_bet_count"],
            "placed_count": st["placed_count"],
            "placed_leg_count": st["placed_leg_count"],
            "game_launch_count": st["game_launch_count"],
            "since_last_add_s": round((st["last_ms"] - st["last_add_ms"]) / 1000.0, 3)
            if st["last_add_ms"] is not None else None,
            # NOTE: no stake, no balance, no euro value - not in this log.
        }


# ─────────────────────────────── tests ───────────────────────────────
def _replay_features(key: tuple[str, int], stop_after: int | None = None,
                     backend_cls: type = InMemoryStateBackend) -> list[dict]:
    from replay import replay_session
    acc = FeatureAccumulator(backend_cls())
    out = []
    for i, record in enumerate(replay_session(*key), start=1):
        out.append(acc.update(record.value))
        if stop_after is not None and i >= stop_after:
            break
    return out


def _test_monotonic_prefix(keys: list[tuple[str, int]]) -> None:
    """THE test: features at event N never change when later events arrive.

    For each session we compute the full stream of snapshots, then recompute
    from scratch stopping at several prefix lengths. If any feature at event N
    depended on an event after N - even accidentally, via shared state - the
    prefix run and the full run would disagree.
    """
    checked = 0
    for key in keys:
        full = _replay_features(key)
        if len(full) < 4:
            continue
        for n in {1, 2, len(full) // 3 or 1, len(full) // 2 or 1, len(full) - 1}:
            prefix = _replay_features(key, stop_after=n)
            assert len(prefix) == n, f"{key}: prefix len {len(prefix)} != {n}"
            for i, (p, f) in enumerate(zip(prefix, full)):
                assert p == f, (
                    f"{key}: feature at event {i+1} CHANGED when events after "
                    f"{n} were appended.\n  prefix run: {p}\n  full run:   {f}")
            checked += 1
    print(f"OK  no-look-ahead: {checked} prefix/full comparisons across "
          f"{len(keys)} sessions - features at event N are identical whether "
          f"or not later events exist")


def _test_backend_equivalence(keys: list[tuple[str, int]]) -> None:
    """The Redis seam: same call sites, same results, JSON-serialisable state."""
    for key in keys:
        a = _replay_features(key, backend_cls=InMemoryStateBackend)
        b = _replay_features(key, backend_cls=JsonRoundTripBackend)
        assert a == b, f"{key}: results differ between state backends"
    print(f"OK  state backend: {len(keys)} sessions identical through a "
          f"JSON round-tripping backend - state is Redis-serialisable and no "
          f"call site changed")


def _test_state_bounded(keys: list[tuple[str, int]]) -> None:
    from replay import replay_session
    worst = 0
    for key in keys:
        b = InMemoryStateBackend()
        acc = FeatureAccumulator(b)
        for record in replay_session(*key):
            acc.update(record.value)
        blob = json.dumps(b.get(FeatureAccumulator.state_key(
            {"PlayerID": key[0], "sid": key[1]})))
        worst = max(worst, len(blob))
    print(f"OK  bounded state: largest session state is {worst:,} bytes of "
          f"JSON - safe as a Redis value")


def _demo(key: tuple[str, int]) -> None:
    from replay import replay_session
    acc = FeatureAccumulator(InMemoryStateBackend())
    print(f"\nfeature trace for ({key[0][:8]}…{key[0][-4:]}, {key[1]})")
    print(f"  {'#':>4} {'event':<20}{'elapsed':>9}{'dwell':>8}{'vel/min':>9}"
          f"{'scr':>5}{'rpt':>5}{'adds':>6}  screen")
    rows = list(replay_session(*key))
    for i, record in enumerate(rows, start=1):
        f = acc.update(record.value)
        if i <= 8 or i > len(rows) - 4:
            print(f"  {i:>4} {str(f['event_name']):<20}{f['elapsed_s']:>9.1f}"
                  f"{f['dwell_s']:>8.1f}{f['velocity_epm']:>9.1f}"
                  f"{f['distinct_screens']:>5}{f['repeat_screen_events']:>5}"
                  f"{f['add_bet_count']:>6}  {str(f['screen'])[:34]}")
        elif i == 9:
            print(f"  {'…':>4}")


def main() -> int:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from replay import list_sessions

    assert_screen_coverage()

    src = REPO_ROOT / "data" / "clean" / "events.parquet"
    import duckdb
    con = duckdb.connect()
    # A spread of sessions: one per platform, biased to slip-builders so the
    # betslip counters are actually exercised.
    keys = [(r[0], int(r[1])) for r in con.execute(f"""
        WITH s AS (
          SELECT PlayerID, sid, min(platform) platform, count(*) n,
                 max(CASE WHEN event_name='betslip_add_bet' THEN 1 ELSE 0 END) adds
          FROM read_parquet('{str(src).replace("'", "''")}')
          GROUP BY 1,2 HAVING n BETWEEN 10 AND 400),
             r AS (SELECT *, row_number() OVER (PARTITION BY platform, adds
                                                ORDER BY n DESC) rn FROM s)
        SELECT PlayerID, sid FROM r WHERE rn <= 3 ORDER BY platform, adds, n DESC
    """).fetchall()]
    con.close()
    print(f"\ntest sessions: {len(keys)} (all platforms, slip-builders and not)")

    _test_monotonic_prefix(keys)
    _test_backend_equivalence(keys)
    _test_state_bounded(keys)

    demo = [k for k in keys if k]
    _demo(demo[-1])
    print("\nOK  features.py: coverage reported, no-look-ahead proven, "
          "backend seam proven")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

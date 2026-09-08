#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge: Session Quality & Session-to-Action Conversion
Step 4: the slip-rescue trigger. Rules only - no model, no training, no ML.

WHAT IT IS FOR
--------------
BASELINE §5 isolates the one addressable moment in this dataset: 417 of 1,649
sessions that build a betslip (25.3%) never submit it. This fires inside such a
session, while it is still open, so an intervention has somewhere to land.

    trig = SlipRescueTrigger(backend)
    for record in consumer:                     # replay or Kafka - identical
        features = accumulator.update(record.value)
        decision = trig.evaluate(features)
        if decision.action is FIRE:
            emit(decision)

SILENCE IS A DESIGNED OUTPUT
----------------------------
`evaluate()` always returns a Decision. Staying quiet is `Action.STAY_SILENT`
carrying a named reason - never None, never a fallthrough off the end of the
rule chain. Every silent path in this file is written deliberately and is
counted in the evaluation output, because on a gambling product the decision
NOT to nudge someone is the one that has to be defensible.

WHY THESE THRESHOLDS
--------------------
Measured on all 1,649 slip-building sessions (see __main__). A stalling
signal alone fires on 57% of abandoners but also 38% of converters. Requiring
the session's pace to have collapsed relative to *its own* peak trades ~6
points of recall for ~11 points of false positives, which is the better deal.

A back-and-forth path (repeat_screen_events >= 8/12/20) was tested and
REJECTED: it lifted recall to 55% but cut precision from 37.4% to 31.9%. The
signal is still reported as evidence on every decision - it is just not
allowed to fire on its own.

NO MONEY, NO ML. Stake is not in the event log (DATA_MAP §6.5), so nothing
here is value-weighted.

Usage:  python3 src/trigger.py     # evaluation over 1,649 sessions, by platform
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import StateBackend, InMemoryStateBackend   # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# ══════════════════════ TUNING CONSTANTS ══════════════════════
# All of it is here. Nothing below reads a magic number.

# --- arming: is there a slip worth rescuing at all? -----------------------
MIN_ADDS_TO_ARM = 1          # >=1 betslip_add_bet seen (the brief's gate)
MIN_EVENTS_TO_ARM = 4        # warm-up: velocity is noise before this
MIN_ELAPSED_S_TO_ARM = 20.0  # warm-up: so is a 2-second session

# --- hesitation: the session has gone quiet while holding the slip --------
STALL_DWELL_S = 90.0         # silence since the last event, whatever it was
STALL_SINCE_ADD_S = 120.0    # silence since the last leg was added

# --- confirmation: the pace collapsed against this session's own peak -----
# Relative, not absolute, so a naturally slow user is not treated as stalling.
VELOCITY_COLLAPSE_RATIO = 0.25
MIN_PEAK_VELOCITY_EPM = 1.0  # below this the ratio is meaningless

# --- restraint -----------------------------------------------------------
FIRE_ONCE_PER_SESSION = True     # never nudge the same session twice

# --- evidence only: reported, never fires on its own (see docstring) ------
BACK_AND_FORTH_EVENTS = 12
MIN_SCREEN_COVERAGE_TO_TRUST = 0.90   # Android sits far below this

TRIGGER_KEY = "trig:{player_id}:{sid}"
TRIGGER_STATE_TTL_S = 4 * 3600


class Action(str, Enum):
    FIRE = "FIRE"
    STAY_SILENT = "STAY_SILENT"


class Reason(str, Enum):
    """Every reason the trigger can give, firing or not."""
    # firing
    STALLED_HOLDING_SLIP = "stalled_holding_slip"
    # designed silence
    NO_SLIP_YET = "no_slip_yet"
    ALREADY_CONVERTED = "already_converted"
    WARMING_UP = "warming_up"
    HEALTHY_PROGRESS = "healthy_progress"
    STILL_AT_PACE = "still_at_pace"
    ALREADY_FIRED = "already_fired"


FIRE = Action.FIRE
STAY_SILENT = Action.STAY_SILENT


@dataclass(frozen=True, slots=True)
class Decision:
    """The trigger's output. Always returned - silence included."""
    action: Action
    reason: Reason
    player_id: str
    sid: int
    platform: str | None
    at_elapsed_s: float
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fired(self) -> bool:
        return self.action is Action.FIRE


@dataclass
class SlipRescueTrigger:
    """Rules-based. State lives behind the same StateBackend seam as
    features.py, so the fired-once flag is a Redis key in production without a
    call-site change."""

    backend: StateBackend

    # ---- the two ways this class can answer -----------------------------
    def _fire(self, f: Mapping[str, Any], reason: Reason) -> Decision:
        return Decision(FIRE, reason, f["player_id"], f["sid"], f["platform"],
                        f["elapsed_s"], self._evidence(f))

    def _stay_silent(self, f: Mapping[str, Any], reason: Reason) -> Decision:
        """The designed-silence path. Called explicitly at every non-firing
        branch; there is no implicit `return None` anywhere in evaluate()."""
        return Decision(STAY_SILENT, reason, f["player_id"], f["sid"],
                        f["platform"], f["elapsed_s"], self._evidence(f))

    @staticmethod
    def _evidence(f: Mapping[str, Any]) -> dict[str, Any]:
        """What the decision was made on - so a human can audit any nudge."""
        peak = f.get("peak_velocity_epm") or 0.0
        trusted = (f.get("screen_coverage") or 0.0) >= MIN_SCREEN_COVERAGE_TO_TRUST
        return {
            "n_events": f["n_events"],
            "elapsed_s": f["elapsed_s"],
            "dwell_s": f["dwell_s"],
            "since_last_add_s": f.get("since_last_add_s"),
            "add_bet_count": f["add_bet_count"],
            "velocity_epm": f["velocity_epm"],
            "peak_velocity_epm": peak,
            "velocity_ratio": round(f["velocity_epm"] / peak, 4) if peak else None,
            # reported, never decisive; and flagged when the platform's screen
            # instrumentation is too sparse to trust (Android - features.py)
            "back_and_forth": f["repeat_screen_events"] >= BACK_AND_FORTH_EVENTS,
            "repeat_screen_events": f["repeat_screen_events"],
            "distinct_screens": f["distinct_screens"],
            "screen_signal_trusted": trusted,
        }

    # ---- the rule chain --------------------------------------------------
    def evaluate(self, f: Mapping[str, Any]) -> Decision:
        """One feature snapshot in, one Decision out. Never returns None.

        Reads only `f` and its own stored flag, so it can be called on a live
        topic exactly as it is called on a replay.
        """
        # 1. Is there a slip at all?
        if f["add_bet_count"] < MIN_ADDS_TO_ARM:
            return self._stay_silent(f, Reason.NO_SLIP_YET)

        # 2. Already placed - the moment has passed, and nudging someone who
        #    just bet is the worst thing this system could do.
        if f["placed_count"] > 0:
            return self._stay_silent(f, Reason.ALREADY_CONVERTED)

        # 3. Warm-up. Velocity over a trailing window is meaningless on the
        #    first couple of events; firing here would be firing on noise.
        if (f["n_events"] < MIN_EVENTS_TO_ARM
                or f["elapsed_s"] < MIN_ELAPSED_S_TO_ARM):
            return self._stay_silent(f, Reason.WARMING_UP)

        # 4. Restraint: one nudge per session, ever.
        key = TRIGGER_KEY.format(player_id=f["player_id"], sid=f["sid"])
        if FIRE_ONCE_PER_SESSION and self.backend.get(key):
            return self._stay_silent(f, Reason.ALREADY_FIRED)

        # 5. Hesitation. The user is holding an unplaced slip and has gone
        #    quiet - either since their last event, or since their last leg.
        since_add = f.get("since_last_add_s")
        stalled = (f["dwell_s"] >= STALL_DWELL_S
                   or (since_add is not None and since_add >= STALL_SINCE_ADD_S))
        if not stalled:
            return self._stay_silent(f, Reason.HEALTHY_PROGRESS)

        # 6. Confirmation. A pause only means hesitation if the session has
        #    actually decelerated against its own established pace. This is
        #    what keeps the trigger off converters who simply pause to think.
        peak = f["peak_velocity_epm"]
        if peak < MIN_PEAK_VELOCITY_EPM:
            return self._stay_silent(f, Reason.STILL_AT_PACE)
        if f["velocity_epm"] / peak > VELOCITY_COLLAPSE_RATIO:
            return self._stay_silent(f, Reason.STILL_AT_PACE)

        # 7. Fire.
        if FIRE_ONCE_PER_SESSION:
            self.backend.set(key, {"fired_at_s": f["elapsed_s"]},
                             ttl_s=TRIGGER_STATE_TTL_S)
            self.backend.expire(key, TRIGGER_STATE_TTL_S)
        return self._fire(f, Reason.STALLED_HOLDING_SLIP)


# ═════════════════════════ EVALUATION ═════════════════════════
def _labels(source: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """Ground truth, computed AFTER the fact and never shown to the trigger.

    The trigger sees only the streaming feature dicts; converting a session's
    outcome into a label is an offline scoring step that happens here, once
    every replay has finished.
    """
    import duckdb
    con = duckdb.connect()
    try:
        rows = con.execute(f"""
            SELECT PlayerID, sid,
                   max(CASE WHEN event_name='betslip_add_bet' THEN 1 ELSE 0 END) AS built,
                   max(CASE WHEN event_name='betslip_placed'  THEN 1 ELSE 0 END) AS converted,
                   min(platform) AS platform,
                   max(epoch_ms(ts)) AS last_ms
            FROM read_parquet('{str(source).replace("'", "''")}')
            GROUP BY 1, 2 HAVING built = 1
        """).fetchall()
    finally:
        con.close()
    return {(r[0], int(r[1])): {"converted": bool(r[3]), "platform": r[4],
                                "last_ms": int(r[5])} for r in rows}


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def evaluate_all(source: Path | None = None) -> int:
    from replay import CONFIG as RCONFIG, stream_sessions
    from features import FeatureAccumulator

    source = source or RCONFIG["source"]
    if not source.exists():
        print(f"ERROR: {source} - run scripts/sanitize.py first")
        return 1

    truth = _labels(source)
    keys = sorted(truth)
    print(f"replaying {len(keys):,} slip-building sessions "
          f"({sum(1 for k in keys if not truth[k]['converted']):,} abandoned, "
          f"{sum(1 for k in keys if truth[k]['converted']):,} converted)\n")

    results: list[dict[str, Any]] = []
    silence: dict[str, int] = {}        # per session, at the last open-gate event
    volume: dict[str, int] = {}         # every decision, to show the duty cycle
    for key, consumer in stream_sessions(keys, source=source):
        acc = FeatureAccumulator(InMemoryStateBackend())
        trig = SlipRescueTrigger(InMemoryStateBackend())
        fired_at_ms: int | None = None
        gate_silence: str | None = None
        for record in consumer:
            f = acc.update(record.value)
            decision = trig.evaluate(f)
            volume[decision.reason.value] = volume.get(decision.reason.value, 0) + 1
            if decision.fired:
                if fired_at_ms is None:
                    fired_at_ms = record.timestamp
            # The interesting silence is the last one taken while the gate was
            # genuinely open - a slip built and not yet placed. Silence after
            # the bet lands is trivially correct and would swamp the count.
            elif f["add_bet_count"] >= MIN_ADDS_TO_ARM and f["placed_count"] == 0:
                gate_silence = decision.reason.value
        t = truth[key]
        if fired_at_ms is None:
            silence[gate_silence or "gate_never_opened"] = \
                silence.get(gate_silence or "gate_never_opened", 0) + 1
        results.append({
            "platform": t["platform"], "converted": t["converted"],
            "fired": fired_at_ms is not None,
            # lead time: how much of the session was still ahead of the nudge
            "lead_s": (t["last_ms"] - fired_at_ms) / 1000.0
            if fired_at_ms is not None else None,
        })

    platforms = sorted({r["platform"] for r in results})
    print("SLIP-RESCUE TRIGGER - by platform")
    print(f"  {'platform':<15}{'abandoned':>10}{'fired':>7}{'recall':>8}"
          f"{'lead':>8}   {'converted':>10}{'fired':>7}{'FP rate':>9}{'lead':>8}"
          f"   {'precision':>10}")
    print(f"  {'-'*15}{'-'*10:>10}{'-'*7:>7}{'-'*8:>8}{'-'*8:>8}   "
          f"{'-'*10:>10}{'-'*7:>7}{'-'*9:>9}{'-'*8:>8}   {'-'*10:>10}")

    def line(label: str, rows: list[dict[str, Any]]) -> None:
        ab = [r for r in rows if not r["converted"]]
        cv = [r for r in rows if r["converted"]]
        ab_f = [r for r in ab if r["fired"]]
        cv_f = [r for r in cv if r["fired"]]
        la = _median([r["lead_s"] for r in ab_f])
        lc = _median([r["lead_s"] for r in cv_f])
        tot_f = len(ab_f) + len(cv_f)
        print(f"  {label:<15}{len(ab):>10,}{len(ab_f):>7,}"
              f"{(100*len(ab_f)/len(ab) if ab else 0):>7.1f}%"
              f"{(f'{la:,.0f}s' if la is not None else '-'):>8}   "
              f"{len(cv):>10,}{len(cv_f):>7,}"
              f"{(100*len(cv_f)/len(cv) if cv else 0):>8.1f}%"
              f"{(f'{lc:,.0f}s' if lc is not None else '-'):>8}   "
              f"{(100*len(ab_f)/tot_f if tot_f else 0):>9.1f}%")

    for p in platforms:
        line(p, [r for r in results if r["platform"] == p])
    print(f"  {'-'*15}{'-'*10:>10}{'-'*7:>7}{'-'*8:>8}{'-'*8:>8}   "
          f"{'-'*10:>10}{'-'*7:>7}{'-'*9:>9}{'-'*8:>8}   {'-'*10:>10}")
    line("ALL", results)

    ab_all = [r for r in results if not r["converted"]]
    base = 100.0 * len(ab_all) / len(results)
    fired = [r for r in results if r["fired"]]
    prec = 100.0 * sum(1 for r in fired if not r["converted"]) / len(fired) if fired else 0
    print(f"\n  lead = median seconds between the trigger and the session's "
          f"last event.")
    print(f"  precision {prec:.1f}% against a {base:.1f}% base rate of "
          f"abandonment = {prec/base:.2f}x lift.")

    print(f"\nDESIGNED SILENCE - {len(results) - len(fired):,} sessions never "
          f"fired. Reason at the last event where the gate was still open\n"
          f"(a slip built, no bet placed) - silence after the bet lands is "
          f"trivially correct and is excluded:")
    for reason, n in sorted(silence.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:<24}{n:>7,}")
    total_dec = sum(volume.values())
    print(f"\n  Duty cycle: {total_dec:,} decisions taken across these "
          f"sessions, {volume.get('stalled_holding_slip', 0):,} of them FIRE "
          f"({100*volume.get('stalled_holding_slip', 0)/total_dec:.3f}%).")
    for reason, n in sorted(volume.items(), key=lambda kv: -kv[1]):
        print(f"    {reason:<24}{n:>9,}")
    print("\n  Every one of those is an explicit STAY_SILENT branch in "
          "evaluate(), not a fallthrough off the end of the chain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(evaluate_all())

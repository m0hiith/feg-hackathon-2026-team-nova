#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge 1: Session Quality & Session-to-Action Conversion
Friction detection: what is going wrong in this session, right now.

    signals = detect(session_df, profile=habit_profile)

A LAYER OVER intent.py, NOT A SECOND COPY OF IT
-----------------------------------------------
Three of the five signals are already decided by the intent replayer, so they
are read back off its states rather than re-derived here:

    NAV_LOOP     <- intent rule "circling"
    BETSLIP_EXIT <- intent rule "left_open_slip"
    LONG_DWELL   <- intent rule "dwelling"

Re-implementing those would give the demo two definitions of the same thing that
could drift apart. DEEP_PATH and COLD_START are genuinely new and are computed
here.

SAME DISCIPLINE AS intent.py
----------------------------
Rules only. No look-ahead: every signal is decided from events 0..N and carries
the sequence numbers of the rows that fired it. A signal is emitted at the first
event where its condition holds, which is the moment help would actually be
useful - not at the end of the session, when it is too late to matter.

Each kind fires AT MOST ONCE per session. Without that, one circling session
would emit a NAV_LOOP on every repeated screen and drown the action layer.

Usage:  python3 src/friction.py       # per-signal counts over every session
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_loader import load_events, sessions_for                      # noqa: E402
from intent import (ADD_BET, SCREEN_EVENTS, IntentState, replay,       # noqa: E402
                    replay_all, _screen_of)
from profiler import HabitProfile                                       # noqa: E402

# ══════════════════════════ TUNING ══════════════════════════
CONFIG = {
    # Screens a player may pass through before their first selection before we
    # call the path too deep. The brief's own baseline is 8-9 taps to a bet.
    "deep_path_screens": 6,
    # Profile confidence below which the player has no settled habit to lean on.
    "cold_start_confidence": 0.40,
}


class Friction(str, Enum):
    NAV_LOOP = "NAV_LOOP"          # going round in circles
    BETSLIP_EXIT = "BETSLIP_EXIT"  # walked away from an open slip
    DEEP_PATH = "DEEP_PATH"        # too many screens to reach a selection
    COLD_START = "COLD_START"      # no habit to personalise from
    LONG_DWELL = "LONG_DWELL"      # stalled on one screen


@dataclass(frozen=True)
class FrictionSignal:
    kind: Friction
    seq: int                       # event at which this became true
    ts: datetime | None
    rule: str
    reasons: tuple[str, ...]
    evidence: tuple[int, ...]

    def explain(self) -> str:
        return (f"{self.kind.value:<13} @{self.seq:<3} {self.rule:<18} "
                f"{'; '.join(self.reasons)}")


# intent rule name -> friction kind, for the three signals intent.py already decides
_FROM_INTENT = {
    "circling": Friction.NAV_LOOP,
    "left_open_slip": Friction.BETSLIP_EXIT,
    "dwelling": Friction.LONG_DWELL,
}


def detect(
    session_df: pd.DataFrame,
    states: list[IntentState] | None = None,
    profile: HabitProfile | None = None,
) -> list[FrictionSignal]:
    """Every friction signal in one session, in the order they became true.

    `profile` must have been built with this session EXCLUDED, or COLD_START is
    reading an answer that includes the session it is judging.
    """
    states = replay(session_df) if states is None else states
    found: dict[Friction, FrictionSignal] = {}

    # --- COLD_START: a property of the player, known before the session opens
    if profile is not None and profile.confidence < CONFIG["cold_start_confidence"]:
        found[Friction.COLD_START] = FrictionSignal(
            kind=Friction.COLD_START, seq=0,
            ts=states[0].ts if states else None,
            rule="low_profile_confidence",
            reasons=(f"profile confidence {profile.confidence:.3f} < "
                     f"{CONFIG['cold_start_confidence']}",
                     *profile.confidence_reasons),
            evidence=(),
        )

    # --- the three the intent replayer already decided
    for st in states:
        kind = _FROM_INTENT.get(st.rule)
        if kind is not None and kind not in found:
            found[kind] = FrictionSignal(
                kind=kind, seq=st.seq, ts=st.ts, rule=st.rule,
                reasons=st.reasons, evidence=st.evidence,
            )

    # --- DEEP_PATH: too many screens before the first selection
    screens_seen: list[int] = []
    for seq, (_, row) in enumerate(session_df.iterrows()):
        if str(row["event_name"]) == ADD_BET:
            break                      # a selection was made; path length settled
        if str(row["event_name"]) in SCREEN_EVENTS and _screen_of(row):
            screens_seen.append(seq)
            if len(screens_seen) > CONFIG["deep_path_screens"]:
                found[Friction.DEEP_PATH] = FrictionSignal(
                    kind=Friction.DEEP_PATH, seq=seq, ts=row["ts"],
                    rule="deep_path_to_selection",
                    reasons=(f"{len(screens_seen)} screens viewed with no selection "
                             f"yet (threshold {CONFIG['deep_path_screens']})",),
                    evidence=tuple(screens_seen),
                )
                break

    return sorted(found.values(), key=lambda s: (s.seq, s.kind.value))


def kinds(signals: list[FrictionSignal]) -> set[Friction]:
    return {s.kind for s in signals}


# ═══════════════════════════ checks ═══════════════════════════

def _main() -> int:
    events = load_events()
    counts: dict[str, int] = {k.value: 0 for k in Friction}
    sessions = 0
    with_any = 0
    first_seq: dict[str, list[int]] = {k.value: [] for k in Friction}

    for pk, sid, states in replay_all(events):
        sessions += 1
        # profile omitted here: COLD_START is measured separately, per player,
        # so this loop stays a pure read of session-local friction.
        sdf = events[(events.player_key == pk) & (events.session == sid)]
        sigs = detect(sdf, states=states)
        if sigs:
            with_any += 1
        for s in sigs:
            counts[s.kind.value] += 1
            first_seq[s.kind.value].append(s.seq)

    print("=" * 96)
    print("FRICTION SIGNALS ACROSS ALL SESSIONS".center(96))
    print("=" * 96)
    print(f"{'signal':14} {'sessions':>9} {'% of all':>9} {'median seq':>11}   note")
    print("-" * 96)
    notes = {
        "NAV_LOOP": "reused from intent rule 'circling'",
        "BETSLIP_EXIT": "reused from intent rule 'left_open_slip'",
        "DEEP_PATH": "computed here",
        "COLD_START": "profile-level; needs a profile, see below",
        "LONG_DWELL": "reused from intent rule 'dwelling'",
    }
    for k in Friction:
        v = counts[k.value]
        seqs = sorted(first_seq[k.value])
        med = seqs[len(seqs) // 2] if seqs else None
        print(f"{k.value:14} {v:9,} {100*v/sessions:8.1f}% "
              f"{(str(med) if med is not None else '-'):>11}   {notes[k.value]}")
    print("-" * 96)
    print(f"{'ANY':14} {with_any:9,} {100*with_any/sessions:8.1f}%")
    print(f"\nsessions examined: {sessions:,}")

    # LONG_DWELL was expected to be unfirable on this data. Report what is true.
    ld = counts["LONG_DWELL"]
    print("\nLONG_DWELL:")
    if ld == 0:
        print("  Never fires on this dataset, as documented. The generator spaces")
        print("  sports events 4-70s apart and casino events 5-120s, against")
        print("  thresholds of 60s (betslip) and 120s. The rule is kept because a")
        print("  real export has real dwell times; it is dead code here by data,")
        print("  not by design.")
    else:
        print(f"  Fires in {ld:,} sessions ({100*ld/sessions:.1f}%). The 60s betslip")
        print("  threshold is reachable after all: the generator's inter-event gap")
        print("  runs to 70s, so a slow step on the betslip does cross it.")

    # COLD_START, per player, with the session under judgement excluded.
    from profiler import build_profile
    print("\nCOLD_START (profile confidence, current session excluded):")
    for pk in sorted(events.player_key.unique()):
        one = sessions_for(pk, events)[-1]
        prof = build_profile(events, pk, exclude_session=int(one.session.iloc[0]))
        flag = "COLD_START" if prof.confidence < CONFIG["cold_start_confidence"] else "-"
        print(f"  {pk}  confidence {prof.confidence:.3f}  {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

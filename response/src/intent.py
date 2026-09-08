#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge 1: Session Quality & Session-to-Action Conversion
Intent replay: what is this player trying to do, right now, in this session.

    for state in replay(session_df):
        print(state.intent, state.reasons)

RULES, NOT A MODEL
------------------
Every state below is produced by a named rule over events that have already
happened. There is no model, no training, no learned weights and no score to
justify. Under the EU AI Act an operator has to be able to say why a system
treated a user the way it did; here the answer is always a rule name plus the
exact event rows that fired it, carried on `IntentState.reasons` and
`IntentState.evidence`.

`evidence` holds sequence numbers into the session, so a reason is not merely a
sentence - it points at the rows a reviewer can go and read.

THE HARD CONSTRAINT: NO LOOK-AHEAD
----------------------------------
State at event N is computed from events 0..N only. It is never revised once
emitted. This matters for ABANDONING in particular: "left the betslip without
placing" is decided from what has happened so far, NOT by peeking ahead to see
whether a `betslip_placed` eventually arrives. A classifier that looked ahead
would be unshippable - live, there is no ahead to look at.

`_test_prefix_stability()` proves it: replaying the first k events of a session
must return exactly the states that a full replay returns for those k events.

STATES
------
BROWSING     no direction yet - home, lobby, arriving
SEARCHING    moving through lists and categories, looking for something
COMPARING    on a match detail screen, weighing a specific fixture
HIGH_INTENT  selections on the slip, or standing on the betslip screen
STUCK        going in circles, or dwelling far too long on one screen
ABANDONING   walked away from an open slip without placing it
DONE         bet placed, or casino game launched

Usage:  python3 src/intent.py       # worked examples + checks over all sessions
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_loader import load_events, load_ground_truth, sessions_for  # noqa: E402

# ══════════════════════════ TUNING ══════════════════════════
CONFIG = {
    # Nth visit to the same screen inside one session that counts as circling.
    "stuck_repeat_visits": 3,
    # Sitting on one screen longer than this, with nothing to show for it.
    "stuck_dwell_s": 120.0,
    # The betslip is the expensive screen to stall on, so it gets a tighter bar.
    "stuck_dwell_betslip_s": 60.0,
}

DETAIL_SCREENS = {"prematchDetail", "liveDetail"}
LIST_SCREENS = {"prematchSports", "prematchLeagues", "prematchMatchesOverview",
                "competition_detail"}
BETSLIP_SCREENS = {"betslip"}
TICKET_SCREENS = {"ticketDetail", "ticketHistory"}
HOME_SCREENS = {"homepage"}

SCREEN_EVENTS = {"fortuna_screen_view", "screen_view"}
ADD_BET = "betslip_add_bet"
PLACED_BET = "betslip_placed_bet"
PLACED = "betslip_placed"
GAME_LAUNCH = "casino_game_launch"
PAGE_VIEW = "page_view"


class Intent(str, Enum):
    BROWSING = "BROWSING"
    SEARCHING = "SEARCHING"
    COMPARING = "COMPARING"
    HIGH_INTENT = "HIGH_INTENT"
    STUCK = "STUCK"
    ABANDONING = "ABANDONING"
    DONE = "DONE"


@dataclass(frozen=True)
class IntentState:
    """One state, emitted after one event. Never revised."""
    seq: int                          # position of this event in the session
    ts: datetime
    event_name: str
    screen: str | None
    intent: Intent
    rule: str                         # the named rule that decided it
    reasons: tuple[str, ...]          # human-readable, one per contributing fact
    evidence: tuple[int, ...]         # seq numbers of the events behind it

    # context a downstream action needs, all observed so far
    selections_open: int = 0
    slip_type: str | None = None
    sport: str | None = None
    competition: str | None = None
    dwell_s: float = 0.0

    def explain(self) -> str:
        return (f"[{self.seq:>3}] {self.intent.value:<11} "
                f"{self.event_name:<20} {self.screen or '-':<24} "
                f"{self.rule:<22} {'; '.join(self.reasons)}")


def _screen_of(row: pd.Series) -> str | None:
    """The screen a row refers to, across both the sports and casino dialects."""
    name = row.get("fortuna_screen_name")
    if isinstance(name, str) and name:
        return name
    loc = row.get("page_location")
    if isinstance(loc, str) and loc:
        return loc
    return None


def _clean(v) -> str | None:
    return v if isinstance(v, str) and v else None


@dataclass
class _Tracker:
    """Everything the rules are allowed to know: strictly the past."""
    visits: dict[str, int] = field(default_factory=dict)
    open_selections: list[int] = field(default_factory=list)
    slip_type: str | None = None
    sport: str | None = None
    competition: str | None = None
    reached_betslip: bool = False
    betslip_seq: int | None = None
    placed: bool = False
    add_seqs: list[int] = field(default_factory=list)
    last_ts: datetime | None = None
    last_screen: str | None = None


def _classify(row: pd.Series, seq: int, tr: _Tracker) -> IntentState:
    """Apply the rule chain to one event. Precedence is top to bottom."""
    ev = str(row["event_name"])
    screen = _screen_of(row)
    ts = row["ts"]
    dwell = 0.0 if tr.last_ts is None else (ts - tr.last_ts).total_seconds()

    reasons: list[str] = []
    evidence: list[int] = [seq]

    def state(intent: Intent, rule: str) -> IntentState:
        return IntentState(
            seq=seq, ts=ts, event_name=ev, screen=screen, intent=intent, rule=rule,
            reasons=tuple(reasons), evidence=tuple(sorted(set(evidence))),
            selections_open=len(tr.open_selections), slip_type=tr.slip_type,
            sport=tr.sport, competition=tr.competition, dwell_s=round(dwell, 1),
        )

    # ---- 1. terminal: the action was taken -------------------------------
    if ev == PLACED:
        reasons.append(f"betslip_placed: slip {row.get('betslip_number')} "
                       f"submitted with {len(tr.open_selections)} selection(s)")
        evidence.extend(tr.add_seqs)
        if _clean(row.get("status")):
            reasons.append(f"status {row['status']}")
        return state(Intent.DONE, "placed_bet")

    if ev == GAME_LAUNCH:
        reasons.append(f"casino_game_launch: {_clean(row.get('game_name'))} "
                       f"({_clean(row.get('provider'))})")
        return state(Intent.DONE, "game_launched")

    if ev == PLACED_BET:
        reasons.append(f"betslip_placed_bet: leg on slip {row.get('betslip_number')} "
                       f"confirmed")
        evidence.extend(tr.add_seqs)
        return state(Intent.DONE, "placing_in_progress")

    # ---- 2. an explicit selection is unambiguous intent -------------------
    if ev == ADD_BET:
        reasons.append(
            f"betslip_add_bet: selection {row.get('selection_id')} at odds "
            f"{row.get('odds')} on {_clean(row.get('sport_name'))} / "
            f"{_clean(row.get('on_origin_name'))}")
        if len(tr.open_selections) >= 1:
            reasons.append(f"{len(tr.open_selections)} selection(s) already on the slip")
            evidence.extend(tr.add_seqs)
        return state(Intent.HIGH_INTENT, "selection_added")

    # ---- 3. walked away from an open slip --------------------------------
    #  Decided only from what has happened. We do NOT look ahead to see
    #  whether this session later places the slip.
    left_slip = (
        tr.open_selections
        and not tr.placed
        and ev in SCREEN_EVENTS
        and screen not in BETSLIP_SCREENS
        and screen not in TICKET_SCREENS
    )
    if left_slip:
        reasons.append(
            f"left the betslip for {screen} with {len(tr.open_selections)} "
            f"selection(s) still un-placed")
        evidence.extend(tr.add_seqs)
        if tr.betslip_seq is not None:
            reasons.append(f"betslip screen was reached at event {tr.betslip_seq}")
            evidence.append(tr.betslip_seq)
        return state(Intent.ABANDONING, "left_open_slip")

    # ---- 4. going in circles ---------------------------------------------
    visits = tr.visits.get(screen, 0) + 1 if screen else 0
    if screen and visits >= CONFIG["stuck_repeat_visits"]:
        reasons.append(f"{screen} visited {visits}x in this session "
                       f"(threshold {CONFIG['stuck_repeat_visits']})")
        return state(Intent.STUCK, "circling")

    limit = (CONFIG["stuck_dwell_betslip_s"] if tr.last_screen in BETSLIP_SCREENS
             else CONFIG["stuck_dwell_s"])
    if dwell > limit and tr.last_screen:
        reasons.append(f"{dwell:.0f}s on {tr.last_screen} before acting "
                       f"(threshold {limit:.0f}s)")
        return state(Intent.STUCK, "dwelling")

    # ---- 5. standing on the slip -----------------------------------------
    if screen in BETSLIP_SCREENS:
        reasons.append(f"on the betslip with {len(tr.open_selections)} selection(s)")
        evidence.extend(tr.add_seqs)
        return state(Intent.HIGH_INTENT, "on_betslip")

    # ---- 6. after the bet is placed, ticket screens are still DONE --------
    if tr.placed and screen in TICKET_SCREENS:
        reasons.append(f"reviewing {screen} after placing")
        return state(Intent.DONE, "post_placement")

    # ---- 7. weighing one fixture -----------------------------------------
    if screen in DETAIL_SCREENS:
        reasons.append(f"on {screen}, comparing a specific fixture")
        return state(Intent.COMPARING, "on_match_detail")

    # ---- 8. hunting through lists ----------------------------------------
    if screen in LIST_SCREENS:
        reasons.append(f"navigating {screen}, no fixture chosen yet")
        return state(Intent.SEARCHING, "on_list_screen")

    if ev == PAGE_VIEW and screen:
        if screen.rstrip("/").endswith(("igre", "najnovije", "jackpot", "promocije")):
            reasons.append(f"browsing casino category {screen}")
            return state(Intent.SEARCHING, "casino_category")
        reasons.append(f"casino page {screen}")
        return state(Intent.BROWSING, "casino_lobby")

    # ---- 9. default -------------------------------------------------------
    reasons.append(f"no directional signal yet ({screen or ev})")
    return state(Intent.BROWSING, "no_signal" if screen in HOME_SCREENS or not screen
                 else "unclassified_screen")


def _advance(row: pd.Series, seq: int, tr: _Tracker) -> None:
    """Fold one event into the tracker. Called AFTER the state is emitted."""
    ev = str(row["event_name"])
    screen = _screen_of(row)

    if screen:
        tr.visits[screen] = tr.visits.get(screen, 0) + 1
        if screen in BETSLIP_SCREENS:
            tr.reached_betslip = True
            if tr.betslip_seq is None:
                tr.betslip_seq = seq

    if ev == ADD_BET:
        tr.open_selections.append(seq)
        tr.add_seqs.append(seq)
        tr.slip_type = _clean(row.get("betslip_type")) or tr.slip_type
        tr.sport = _clean(row.get("sport_name")) or tr.sport
        tr.competition = _clean(row.get("on_origin_name")) or tr.competition
    elif ev == PLACED:
        tr.placed = True
        tr.open_selections.clear()
    elif ev == GAME_LAUNCH:
        tr.sport = tr.sport or None
    elif ev in SCREEN_EVENTS:
        pass

    tr.last_ts = row["ts"]
    tr.last_screen = screen


def replay(session_df: pd.DataFrame) -> list[IntentState]:
    """Replay one session, emitting an IntentState after every event.

    Input must be a single session's events in time order (as produced by
    `data_loader.sessions_for`). Output has exactly one state per input row.
    """
    tr = _Tracker()
    out: list[IntentState] = []
    for seq, (_, row) in enumerate(session_df.iterrows()):
        out.append(_classify(row, seq, tr))
        _advance(row, seq, tr)
    return out


def terminal_intent(states: list[IntentState]) -> Intent:
    """The state a session ended in. DONE wins if it was ever reached."""
    if not states:
        return Intent.BROWSING
    if any(s.intent is Intent.DONE for s in states):
        return Intent.DONE
    return states[-1].intent


def replay_all(events: pd.DataFrame):
    """Replay every session in an event log. Yields (player_key, sid, states)."""
    ordered = events.sort_values("ts", kind="stable")
    for (pk, sid), g in ordered.groupby(["player_key", "session"], sort=False):
        yield pk, int(sid), replay(g)


# ═══════════════════════════ checks and worked examples ═══════════════════════

def _test_prefix_stability(sessions: list[pd.DataFrame]) -> tuple[int, int]:
    """No look-ahead, proved rather than asserted.

    Replaying the first k events of a session must give exactly the states a
    full replay gives for those same k events. If any rule peeked at a later
    event, the truncated replay would disagree.
    """
    checked = failed = 0
    for sdf in sessions:
        full = replay(sdf)
        for k in range(1, len(sdf) + 1):
            part = replay(sdf.iloc[:k])
            checked += 1
            if [(s.intent, s.rule, s.reasons) for s in part] != \
               [(s.intent, s.rule, s.reasons) for s in full[:k]]:
                failed += 1
    return checked, failed


def _demo() -> int:
    events = load_events()
    gt = {r.player_key: r for r in load_ground_truth().itertuples()}

    u3 = sessions_for("SYN_U03", events)
    u4 = sessions_for("SYN_U04", events)

    def first(sessions, pred):
        return next(s for s in sessions if pred(s))

    completing = first(u3, lambda s: (s.event_name == PLACED).any())
    abandoning = first(u3, lambda s: (s.event_name == ADD_BET).any()
                       and not (s.event_name == PLACED).any())
    stuck = first(u3, lambda s: any(x.intent is Intent.STUCK for x in replay(s)))

    for title, sdf in [("COMPLETED SESSION", completing),
                       ("ABANDONED SESSION", abandoning),
                       ("SESSION WITH FRICTION", stuck),
                       ("CASINO SESSION", u4[0])]:
        print("=" * 118)
        print(f"{title}  -  player {sdf.player_key.iloc[0]}, session {int(sdf.session.iloc[0])}".center(118))
        print("=" * 118)
        for st in replay(sdf):
            print(st.explain())
        print()

    # ---- no look-ahead ---------------------------------------------------
    print("=" * 118)
    print("CHECKS".center(118))
    print("=" * 118)
    sample = u3[:120] + u4[:40]
    checked, failed = _test_prefix_stability(sample)
    print(f"  [{'PASS' if not failed else 'FAIL'}]  no look-ahead: {checked:,} truncated "
          f"replays over {len(sample)} sessions, {failed} disagreed with the full replay")

    # ---- one state per event --------------------------------------------
    n_states = 0
    terminal: dict[str, int] = {}
    per_player: dict[str, list[int]] = {}
    for pk, sid, states in replay_all(events):
        n_states += len(states)
        t = terminal_intent(states)
        terminal[t.value] = terminal.get(t.value, 0) + 1
        if any(s.event_name == ADD_BET for s in states):
            placed = any(s.event_name == PLACED for s in states)
            per_player.setdefault(pk, [0, 0])
            per_player[pk][0] += 1
            per_player[pk][1] += 0 if placed else 1
    print(f"  [{'PASS' if n_states == len(events) else 'FAIL'}]  one state per event: "
          f"{n_states:,} states for {len(events):,} events")

    # ---- ABANDONING must mean what it says -------------------------------
    tp = fp = fn = 0
    for pk, sid, states in replay_all(events):
        said = terminal_intent(states) is Intent.ABANDONING
        truth = (any(s.event_name == ADD_BET for s in states)
                 and not any(s.event_name == PLACED for s in states))
        tp += said and truth
        fp += said and not truth
        fn += truth and not said
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    print(f"  [{'PASS' if prec == 1.0 and rec == 1.0 else 'WARN'}]  terminal ABANDONING == "
          f"slip built but never placed: precision {prec:.3f}, recall {rec:.3f} "
          f"(tp={tp} fp={fp} fn={fn})")

    # ---- terminal distribution -------------------------------------------
    print("\n  terminal intent across all sessions:")
    for k, v in sorted(terminal.items(), key=lambda x: -x[1]):
        print(f"    {k:<12} {v:>6,}  {100*v/sum(terminal.values()):5.1f}%")

    # ---- reproduce ground-truth abandon rate -----------------------------
    print("\n  final-step abandon rate, replay vs ground truth:")
    print(f"    {'player':8} {'slips':>6} {'abandoned':>10} {'replay %':>9} {'gt %':>7}")
    worst = 0.0
    for pk in sorted(per_player):
        slips, ab = per_player[pk]
        pct = 100 * ab / slips if slips else 0.0
        g = gt.get(pk)
        gpct = float(g.final_step_abandon_pct) if g is not None and str(
            g.final_step_abandon_pct) not in ("", "nan") else None
        worst = max(worst, abs(pct - gpct) if gpct is not None else 0.0)
        print(f"    {pk:8} {slips:6d} {ab:10d} {pct:8.1f}% "
              f"{(f'{gpct:.1f}%' if gpct is not None else '-'):>7}")
    print(f"    largest divergence from ground truth: {worst:.1f} points")

    print("\n" + ("CHECKS PASS" if not failed and n_states == len(events)
                  else "CHECKS FAILED"))
    return 0 if (not failed and n_states == len(events)) else 1


if __name__ == "__main__":
    raise SystemExit(_demo())

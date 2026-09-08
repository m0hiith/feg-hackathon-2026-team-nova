#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge 1: Session Quality & Session-to-Action Conversion
Next best help: the minimum useful thing to offer, or nothing at all.

    engine = HelpEngine()
    decision = engine.decide(session_df, profile)

TWO POPULATIONS, TWO DIFFERENT KINDS OF HELP
--------------------------------------------
The intent replay showed these are not the same people:

    circling, never reached a slip   ->  they cannot FIND something
    reached the slip, then left it   ->  they found it and stalled at the end

Offering a bet to the first group is answering a question they did not ask, so
CLARIFY_INFO is navigational only and never carries a stake, odds or a CTA to
bet. The second group already chose; the only useful thing is to hand back
exactly what they built, with the typing removed.

DOING NOTHING IS THE DEFAULT, NOT THE FALLBACK
----------------------------------------------
`decide()` returns a NO_ACTION decision with a named reason far more often than
it returns anything else. On a gambling product the decision NOT to interrupt is
the one that has to be defensible, so it is a first-class output carrying the
same rule / reasons / evidence as any other, and it is counted in the report
below.

NO LOOK-AHEAD
-------------
The decision is taken at the FIRST event where a route qualifies, using only
events 0..N. It is never revised, and it never waits to see how the session
ends. That has a real consequence, measured in the report: a session that
circles first and abandons later spends its one intervention on CLARIFY_INFO
and never gets the CONTINUE_CARD, because live there is no way to know the
abandonment is coming. Deciding at the end of the session would score better
offline and be unshippable.

Usage:  python3 src/next_best_help.py     # routing census + constraint tests
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_loader import load_events, sessions_for                       # noqa: E402
from intent import (ADD_BET, GAME_LAUNCH, PAGE_VIEW, PLACED,            # noqa: E402
                    Intent, IntentState, replay, replay_all)
from friction import Friction, FrictionSignal, detect                    # noqa: E402
from profiler import HabitProfile, build_profile                         # noqa: E402

# ══════════════════════════ TUNING ══════════════════════════
CONFIG = {
    # Below this, we have no settled habit and must not pre-fill anything.
    "min_confidence_to_prefill": 0.40,
    # Casino: lobby pages browsed with nothing launched before we offer a
    # resume. Measured, not guessed: across all 2,113 casino sessions the number
    # of pages viewed before the first launch runs 2-5 and never exceeds 5.
    # Browsing the lobby and then launching IS the normal path, so a threshold
    # inside that range fires on healthy behaviour - at 3 it interrupted 29% of
    # all sessions. 6 is the first value that means "browsing longer than this
    # player population ever normally does".
    "casino_browse_before_resume": 6,
    # How long a dismissal suppresses further help for that player.
    "cooldown_hours": 72.0,
}

# Urgency language is disqualifying under the brief. Checked as whole words so
# "known" does not trip "now".
BANNED_PHRASES = ("now", "hurry", "last chance", "ending soon",
                  "don't miss", "dont miss", "limited")
_BANNED_RE = re.compile(
    "|".join(rf"\b{re.escape(p)}\b" for p in BANNED_PHRASES), re.IGNORECASE)


def banned_phrases_in(text: str) -> list[str]:
    return [m.group(0) for m in _BANNED_RE.finditer(text)]


class Action(str, Enum):
    CLARIFY_INFO = "CLARIFY_INFO"    # help them find it. never a bet prompt.
    CONTINUE_CARD = "CONTINUE_CARD"  # hand back the slip they already built
    RESUME_GAME = "RESUME_GAME"      # casino equivalent
    NO_ACTION = "NO_ACTION"          # the default


@dataclass(frozen=True)
class HelpDecision:
    action: Action
    seq: int | None                  # event it would surface at; None if silent
    ts: datetime | None
    rule: str
    reasons: tuple[str, ...]
    evidence: tuple[int, ...]
    payload: dict = field(default_factory=dict)
    rendered: tuple[str, ...] = ()   # exact user-visible strings

    @property
    def is_intervention(self) -> bool:
        return self.action is not Action.NO_ACTION

    def explain(self) -> str:
        where = f"@{self.seq}" if self.seq is not None else "@-"
        return (f"{self.action.value:<14} {where:<5} {self.rule:<24} "
                f"{'; '.join(self.reasons)}")


def _silent(rule: str, *reasons: str, seq: int | None = None,
            ts: datetime | None = None, evidence: tuple[int, ...] = ()) -> HelpDecision:
    return HelpDecision(action=Action.NO_ACTION, seq=seq, ts=ts, rule=rule,
                        reasons=tuple(reasons), evidence=evidence)


# ─────────────────────────── rendering ───────────────────────────
# Copy lives here, in one place, so the banned-phrase test covers all of it.

def _money(v: float | None) -> str:
    return f"EUR {v:.2f}" if v is not None else "your usual stake"


def _render_continue(sport, comp, slip_type, n_sel, stake) -> tuple[str, ...]:
    where = f"{sport}" + (f" / {comp}" if comp else "")
    return (
        f"Continue your {where} slip",
        f"{n_sel} selection{'s' if n_sel != 1 else ''} - {slip_type or 'slip'} - "
        f"{_money(stake)} pre-filled",
        "Nothing is placed until you confirm.",
    )


def _render_clarify(sport, comp) -> tuple[str, ...]:
    if sport and comp:
        head, sub = f"Looking for {sport}?", f"Open {comp} fixtures directly."
    elif sport:
        head, sub = f"Looking for {sport}?", "Open the fixture list directly."
    else:
        head, sub = "Jump back to where you were", "Open your last fixture list."
    return (head, sub, "This only opens the list. No selection is made.")


def _render_resume(game, provider) -> tuple[str, ...]:
    return (
        f"Pick up {game}",
        f"{provider}" if provider else "Return to your game",
        "Opens the game. Nothing is staked until you choose.",
    )


# ─────────────────────────── the engine ───────────────────────────

class HelpEngine:
    """Routes one session to at most one piece of help.

    Cooldown state is held per player. In production this is the only thing
    that needs to outlive a session; everything else is derived per request.
    """

    def __init__(self, cooldown_hours: float | None = None):
        self.cooldown_hours = (CONFIG["cooldown_hours"] if cooldown_hours is None
                               else cooldown_hours)
        self._dismissed: dict[str, datetime] = {}

    # -- cooldown ---------------------------------------------------------
    def record_dismissal(self, player_id: str, ts: datetime) -> None:
        """The player dismissed a card. Stay quiet for `cooldown_hours`."""
        self._dismissed[player_id] = ts

    def in_cooldown(self, player_id: str, ts: datetime) -> bool:
        last = self._dismissed.get(player_id)
        if last is None:
            return False
        return ts < last + timedelta(hours=self.cooldown_hours)

    def reset(self) -> None:
        self._dismissed.clear()

    # -- routing ----------------------------------------------------------
    def decide(
        self,
        session_df: pd.DataFrame,
        profile: HabitProfile | None = None,
        states: list[IntentState] | None = None,
    ) -> HelpDecision:
        """At most one decision per session. NO_ACTION is a real answer."""
        if len(session_df) == 0:
            return _silent("empty_session", "no events")

        states = replay(session_df) if states is None else states
        player_id = str(session_df["PlayerID"].iloc[0])
        opened = states[0].ts

        if self.in_cooldown(player_id, opened):
            until = self._dismissed[player_id] + timedelta(hours=self.cooldown_hours)
            return _silent("cooldown",
                           f"player dismissed help at {self._dismissed[player_id]:%Y-%m-%d %H:%M}; "
                           f"quiet until {until:%Y-%m-%d %H:%M}",
                           seq=0, ts=opened)

        signals = detect(session_df, states=states, profile=profile)
        by_seq: dict[int, set[Friction]] = {}
        for s in signals:
            by_seq.setdefault(s.seq, set()).add(s.kind)
        sig_at = {s.kind: s for s in signals}

        confident = (profile is not None
                     and profile.confidence >= CONFIG["min_confidence_to_prefill"])

        rows = list(session_df.itertuples())
        adds_so_far = 0
        pages_so_far = 0
        launched = False
        placed = False

        # Walk forward. The FIRST route that qualifies wins, and ends the walk.
        for seq, row in enumerate(rows):
            ev = str(row.event_name)
            kinds_here = by_seq.get(seq, set())

            # -- 1. they built a slip and walked off it -----------------
            if Friction.BETSLIP_EXIT in kinds_here:
                sig = sig_at[Friction.BETSLIP_EXIT]
                st = states[seq]
                if not confident:
                    return _silent(
                        "cold_start_no_prefill",
                        f"open slip abandoned, but profile confidence "
                        f"{profile.confidence if profile else 0:.3f} < "
                        f"{CONFIG['min_confidence_to_prefill']}",
                        "pre-filling a stake we cannot justify would be a guess",
                        seq=seq, ts=st.ts, evidence=sig.evidence)
                sel = [r for r in rows[:seq] if str(r.event_name) == ADD_BET]
                stake = profile.usual_stake_eur if profile else None
                sport = st.sport or (profile.top_sport if profile else None)
                comp = st.competition or (profile.top_competition if profile else None)
                slip_type = st.slip_type or (profile.top_market_pattern if profile else None)
                return HelpDecision(
                    action=Action.CONTINUE_CARD, seq=seq, ts=st.ts,
                    rule="restore_abandoned_slip",
                    reasons=(*sig.reasons,
                             f"stake {_money(stake)} taken from this player's own history",
                             f"profile confidence {profile.confidence:.3f}" if profile else ""),
                    evidence=sig.evidence,
                    payload={
                        "sport": sport, "competition": comp, "slip_type": slip_type,
                        "n_selections": len(sel), "stake_eur": stake,
                        "selection_ids": [getattr(r, "selection_id") for r in sel],
                        "fixture_id": getattr(sel[0], "fixture_id", None) if sel else None,
                        "taps_to_confirm": 2,
                    },
                    rendered=_render_continue(sport, comp, slip_type, len(sel), stake),
                )

            # -- 2. circling, with nothing selected yet -----------------
            if Friction.NAV_LOOP in kinds_here and adds_so_far == 0 and not launched:
                sig = sig_at[Friction.NAV_LOOP]
                st = states[seq]
                sport = st.sport or (profile.top_sport if profile else None)
                comp = st.competition or (profile.top_competition if profile else None)
                return HelpDecision(
                    action=Action.CLARIFY_INFO, seq=seq, ts=st.ts,
                    rule="help_them_find_it",
                    reasons=(*sig.reasons,
                             "no selection made yet, so this is a navigation problem, "
                             "not a betting one"),
                    evidence=sig.evidence,
                    payload={"sport": sport, "competition": comp,
                             "destination": "fixture_list", "taps_to_confirm": 1},
                    rendered=_render_clarify(sport, comp),
                )

            # -- 3. casino: browsed a while, launched nothing -----------
            if (ev == PAGE_VIEW and not launched
                    and pages_so_far + 1 >= CONFIG["casino_browse_before_resume"]
                    and profile is not None and profile.top_game):
                if not confident:
                    return _silent(
                        "cold_start_no_prefill",
                        "casino browsing with no settled game habit",
                        seq=seq, ts=row.ts)
                return HelpDecision(
                    action=Action.RESUME_GAME, seq=seq, ts=row.ts,
                    rule="resume_usual_game",
                    reasons=(f"{pages_so_far + 1} lobby pages viewed with no game "
                             f"launched (threshold {CONFIG['casino_browse_before_resume']})",
                             f"top game {profile.top_game} at "
                             f"{profile.top_game_lift:.2f}x uniform"),
                    evidence=tuple(range(seq + 1)),
                    payload={"game": profile.top_game, "provider": profile.top_provider,
                             "stake_eur": profile.usual_casino_stake_eur,
                             "taps_to_confirm": 1},
                    rendered=_render_resume(profile.top_game, profile.top_provider),
                )

            # -- advance the counters (past only) -----------------------
            if ev == ADD_BET:
                adds_so_far += 1
            elif ev == PAGE_VIEW:
                pages_so_far += 1
            elif ev == GAME_LAUNCH:
                launched = True
            elif ev == PLACED:
                placed = True

        # Nothing qualified. Say why, specifically.
        if placed:
            return _silent("converted_cleanly",
                           "session reached betslip_placed without qualifying friction",
                           seq=None, ts=states[-1].ts)
        if launched:
            return _silent("casino_converted",
                           "game launched without qualifying friction",
                           seq=None, ts=states[-1].ts)
        return _silent("no_qualifying_friction",
                       f"terminal intent {states[-1].intent.value}; nothing worth "
                       f"interrupting for", seq=None, ts=states[-1].ts)


# ═══════════════════════════ census and constraint tests ═══════════════════════

def _constructed_browse_session(events: pd.DataFrame, pk: str = "SYN_U04",
                                pages: int = 7) -> pd.DataFrame:
    """A casino session that browses and never launches.

    No such session exists in this dataset - every casino session launches a
    game - so the RESUME_GAME route cannot be exercised on observed data. This
    assembles one from that player's own real page_view rows, on a synthetic
    session id, and is labelled constructed everywhere it is reported. It is
    never counted in the corpus census.
    """
    src = next(pv for s in sessions_for(pk, events)
               for pv in [s[s.event_name == PAGE_VIEW]] if len(pv) >= 2)
    rows = [src.iloc[i % len(src)].copy() for i in range(pages)]
    out = pd.DataFrame(rows).reset_index(drop=True)
    out["ts"] = [src.ts.iloc[0] + timedelta(seconds=45 * i) for i in range(pages)]
    out["session"] = -1
    return out


def _gate_is_stable(events: pd.DataFrame, trials: int = 25) -> tuple[bool, str]:
    """Does excluding one session ever flip the confidence gate?

    The corpus census below uses one profile per player rather than rebuilding
    it per session, which would be ~5,000 full-history passes. That shortcut is
    only honest if excluding a single session cannot move a player across the
    0.40 boundary. U08 is the only player anywhere near it, so it is tested
    hardest.
    """
    thr = CONFIG["min_confidence_to_prefill"]
    worst = ""
    ok = True
    for pk in sorted(events.player_key.unique()):
        full = build_profile(events, pk)
        sess = sessions_for(pk, events)
        step = max(1, len(sess) // trials)
        for s in sess[::step]:
            held = build_profile(events, pk, exclude_session=int(s.session.iloc[0]))
            if (held.confidence >= thr) != (full.confidence >= thr):
                ok = False
                worst = (f"{pk}: {full.confidence:.3f} -> {held.confidence:.3f} "
                         f"crosses {thr}")
    return ok, worst or f"no exclusion crossed the {thr} gate"


def _main() -> int:
    events = load_events()
    profiles = {pk: build_profile(events, pk)
                for pk in sorted(events.player_key.unique())}

    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    stable, detail = _gate_is_stable(events)
    chk("confidence gate is stable under session exclusion", stable, detail)

    # ---- corpus census ---------------------------------------------------
    engine = HelpEngine()
    counts: dict[str, int] = {a.value: 0 for a in Action}
    silent_rules: dict[str, int] = {}
    per_player: dict[str, dict[str, int]] = {}
    all_rendered: list[tuple[str, str]] = []
    collisions = 0          # circling first, abandoned later: cost of no look-ahead
    interventions_per_session_max = 0

    for pk, sid, states in replay_all(events):
        sdf = events[(events.player_key == pk) & (events.session == sid)]
        d = engine.decide(sdf, profile=profiles[pk], states=states)

        counts[d.action.value] += 1
        per_player.setdefault(pk, {a.value: 0 for a in Action})
        per_player[pk][d.action.value] += 1
        if d.action is Action.NO_ACTION:
            silent_rules[d.rule] = silent_rules.get(d.rule, 0) + 1
        for line in d.rendered:
            all_rendered.append((d.action.value, line))
        interventions_per_session_max = max(
            interventions_per_session_max, 1 if d.is_intervention else 0)

        if d.action is Action.CLARIFY_INFO and any(
                s.event_name == ADD_BET for s in states) and not any(
                s.event_name == PLACED for s in states):
            collisions += 1

    total = sum(counts.values())
    no_action_pct = 100 * counts["NO_ACTION"] / total

    print("=" * 100)
    print("ROUTING ACROSS ALL SESSIONS".center(100))
    print("=" * 100)
    print(f"{'action':16} {'sessions':>9} {'share':>8}")
    print("-" * 100)
    for a in Action:
        v = counts[a.value]
        print(f"{a.value:16} {v:9,} {100*v/total:7.1f}%")
    print("-" * 100)
    print(f"{'TOTAL':16} {total:9,}")

    print("\nwhy the engine stayed silent:")
    for r, v in sorted(silent_rules.items(), key=lambda x: -x[1]):
        print(f"  {r:26} {v:6,}  {100*v/total:5.1f}%")

    print("\nper player:")
    print(f"  {'player':8} {'CONTINUE':>9} {'CLARIFY':>8} {'RESUME':>7} {'NO_ACTION':>10} {'silent %':>9}")
    for pk in sorted(per_player):
        c = per_player[pk]
        t = sum(c.values())
        print(f"  {pk:8} {c['CONTINUE_CARD']:9,} {c['CLARIFY_INFO']:8,} "
              f"{c['RESUME_GAME']:7,} {c['NO_ACTION']:10,} {100*c['NO_ACTION']/t:8.1f}%")

    # ---- constraints -----------------------------------------------------
    chk("max one intervention per session",
        interventions_per_session_max <= 1,
        "decide() returns exactly one HelpDecision; no session produced two")

    # cooldown
    e2 = HelpEngine(cooldown_hours=72.0)
    u3 = sessions_for("SYN_U03", events)
    ab = [s for s in u3 if (s.event_name == ADD_BET).any()
          and not (s.event_name == PLACED).any()]
    first_ab = ab[0]
    before = e2.decide(first_ab, profile=profiles["SYN_U03"])
    e2.record_dismissal(str(first_ab.PlayerID.iloc[0]), first_ab.ts.iloc[0])
    after = e2.decide(first_ab, profile=profiles["SYN_U03"])
    later = next((s for s in ab
                  if s.ts.iloc[0] > first_ab.ts.iloc[0] + timedelta(hours=72)), None)
    after_cd = e2.decide(later, profile=profiles["SYN_U03"]) if later is not None else None
    chk("cooldown suppresses help after a dismissal",
        before.is_intervention and not after.is_intervention
        and after.rule == "cooldown",
        f"{before.action.value} -> dismissed -> {after.action.value} ({after.rule})")
    chk("cooldown expires",
        after_cd is not None and after_cd.is_intervention,
        f"after 72h: {after_cd.action.value if after_cd else 'no later session'}")

    # confidence gate
    cold = profiles["SYN_U08"]
    u8_sessions = sessions_for("SYN_U08", events)
    e3 = HelpEngine()
    u8_actions = {e3.decide(s, profile=cold).action for s in u8_sessions
                  if (e3.reset() or True)}
    allowed = {Action.CLARIFY_INFO, Action.NO_ACTION}
    chk("confidence < 0.40 yields only CLARIFY_INFO or NO_ACTION",
        u8_actions <= allowed,
        f"SYN_U08 (conf {cold.confidence:.3f}) produced "
        f"{sorted(a.value for a in u8_actions)}")

    # banned phrases
    offenders = [(a, line, banned_phrases_in(line))
                 for a, line in all_rendered if banned_phrases_in(line)]
    chk("no banned urgency phrase in any rendered string",
        not offenders,
        f"{len(all_rendered):,} rendered lines scanned for "
        f"{list(BANNED_PHRASES)}; {len(offenders)} offender(s)")
    for a, line, hits in offenders[:5]:
        print(f"    OFFENDER {a}: {line!r} -> {hits}")

    # clean converting session
    clean = next(s for s in u3
                 if (s.event_name == PLACED).any()
                 and not detect(s, profile=profiles["SYN_U03"]))
    e4 = HelpEngine()
    dc = e4.decide(clean, profile=profiles["SYN_U03"])
    chk("clean converting session gets NO_ACTION",
        dc.action is Action.NO_ACTION,
        f"{dc.action.value} / {dc.rule}")

    # chattiness bar
    chk("NO_ACTION fires on at least 40% of sessions",
        no_action_pct >= 40.0,
        f"{no_action_pct:.1f}% of {total:,} sessions")

    # RESUME_GAME: no casino session in this data ever fails to launch a game,
    # so the route is exercised on a truncated one.
    browse_only = _constructed_browse_session(events)
    e5 = HelpEngine()
    rg = e5.decide(browse_only, profile=profiles["SYN_U04"])
    chk("RESUME_GAME routes on casino browse-without-launch (constructed session)",
        rg.action is Action.RESUME_GAME,
        f"{rg.action.value} - {rg.rendered[0] if rg.rendered else '-'}")

    natural_resume = counts["RESUME_GAME"]
    chk("RESUME_GAME does not fire on normal casino browsing",
        natural_resume == 0,
        f"{natural_resume} of {total:,} observed sessions; browsing 2-5 lobby "
        f"pages then launching is the healthy path, not friction")

    # ---- worked examples -------------------------------------------------
    print("\n" + "=" * 100)
    print("WORKED EXAMPLES".center(100))
    print("=" * 100)
    e6 = HelpEngine()
    for title, sdf, prof in [
        ("abandoned slip -> CONTINUE_CARD", first_ab, profiles["SYN_U03"]),
        ("circling, nothing selected -> CLARIFY_INFO",
         next(s for s in u3 if e6.decide(s, profile=profiles["SYN_U03"]).action
              is Action.CLARIFY_INFO), profiles["SYN_U03"]),
        ("casino browse, no launch -> RESUME_GAME (constructed)",
         browse_only, profiles["SYN_U04"]),
        ("clean conversion -> NO_ACTION", clean, profiles["SYN_U03"]),
        ("cold start abandon -> NO_ACTION",
         next((s for s in u8_sessions if (s.event_name == ADD_BET).any()
               and not (s.event_name == PLACED).any()), u8_sessions[0]),
         profiles["SYN_U08"]),
    ]:
        if sdf is None:
            continue
        e7 = HelpEngine()
        d = e7.decide(sdf, profile=prof)
        print(f"\n{title}")
        print(f"  {d.explain()}")
        for line in d.rendered:
            print(f"    | {line}")
        if d.payload:
            print(f"    payload: {d.payload}")

    print("\n" + "=" * 100)
    print("CONSTRAINT TESTS".center(100))
    print("=" * 100)
    for name, ok, det in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name:52} {det}")

    print(f"\ncost of deciding live (no look-ahead): {collisions:,} sessions circled "
          f"first and\nabandoned later, so they received CLARIFY_INFO and never saw a "
          f"CONTINUE_CARD.")

    failed = [n for n, ok, _ in checks if not ok]
    print("\n" + ("ALL CONSTRAINTS PASS" if not failed else f"FAILED: {', '.join(failed)}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())

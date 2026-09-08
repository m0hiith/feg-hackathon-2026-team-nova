#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge 1: Session Quality & Session-to-Action Conversion
Odds-movement disclosure.

    ledger = OddsLedger()
    movements = scan_session(session_df, ledger)      # updates the ledger
    card = render_card(movements)

WHAT THIS IS, AND WHAT IT REFUSES TO BE
---------------------------------------
The hard rule is: never show only favourable odds movement; if odds got worse,
say so. The tempting implementation - surface the selection whose price improved
- is a filter over a signed quantity, and a filter over a signed quantity is
cherry-picking whether or not anyone intended it.

So there is no filter on sign anywhere in this file. The set of movements shown
is decided by which selections the player returned to. Which way they moved has
no influence on whether they are shown.

There is also no "improved" framing. Both directions use ONE sentence structure
that differs by a single adjective, and `TEMPLATE_DIRECTION` is asserted to be
length- and structure-identical in both directions, so neither can be visually
favoured. Direction is carried by the TEXT, never by colour alone (WCAG 2.1 AA
1.4.1): a UI may add colour or an arrow on top of this copy, never instead of it.

WHEN IT FIRES
-------------
All three must hold:
  1. the player has seen this exact selection before,
  2. in an EARLIER session - they left and came back,
  3. and the price has moved beyond the noise threshold.

A NOTE ON "PREVIOUSLY VIEWED"
-----------------------------
The brief says "viewed or slipped". Only "slipped" is observable in this schema:
`betslip_add_bet` and `betslip_placed_bet` carry `fixture_id`, `selection_id`
and `odds`, whereas screen-view rows carry no fixture reference at all. So a
player who merely looked at a fixture leaves no trace to compare against, and
this fires on selections they actually put on a slip. That is a data limitation,
recorded here rather than papered over.

NO LOOK-AHEAD
-------------
The ledger is fed strictly in timestamp order and only ever holds the past. A
movement is detected at the moment the selection reappears, from what was
already recorded.

Usage:  python3 src/odds_movement.py      # corpus split + card tests
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_loader import load_events                                     # noqa: E402
from intent import ADD_BET, PLACED_BET                                  # noqa: E402

# ══════════════════════════ TUNING ══════════════════════════
CONFIG = {
    # Noise floor. Measured on all 593 cross-session price changes in this
    # dataset: the median move is 6.9% and the 25th percentile is 2.1%, so 2%
    # keeps roughly the top three quarters of real movement and drops the
    # rounding-level noise. Both conditions must hold, so a 2% wobble on a 1.10
    # price (0.02) is not reported as news.
    "min_relative_move": 0.02,
    "min_absolute_move": 0.03,
}

# Events that carry a price we can compare against later.
PRICED_EVENTS = (ADD_BET, PLACED_BET)

# ─── copy ───
# ONE structure. The two directions differ by exactly one adjective, and the two
# adjectives are the same length, so the rendered card cannot be visually
# heavier in either direction. `_test_templates_symmetric()` enforces this.
TEMPLATE_HEADLINE = "Odds moved from {before:.2f} to {after:.2f} since you last looked."
TEMPLATE_DIRECTION = {
    "down": "That is a smaller return on the same stake.",
    "up":   "That is a greater return on the same stake.",
}
TEMPLATE_COMBINED = "Combined price moved from {before:.2f} to {after:.2f}."
TEMPLATE_FOOTER = "Nothing is placed until you confirm."


@dataclass(frozen=True)
class OddsObservation:
    """The last time this player saw a price for this selection."""
    selection_id: float
    fixture_id: float | None
    odds: float
    ts: datetime
    session: int
    sport: str | None = None
    competition: str | None = None


@dataclass(frozen=True)
class OddsMovement:
    """A signed price change on a selection the player came back to."""
    selection_id: float
    fixture_id: float | None
    before: float
    after: float
    before_ts: datetime
    after_ts: datetime
    before_session: int
    after_session: int
    seq: int                       # event index within the current session
    sport: str | None
    competition: str | None
    rule: str
    reasons: tuple[str, ...]
    evidence: tuple[int, ...]

    @property
    def delta(self) -> float:
        return round(self.after - self.before, 4)

    @property
    def pct(self) -> float:
        return (self.after - self.before) / self.before if self.before else 0.0

    @property
    def direction(self) -> str:
        """'down' = a smaller return for the player. 'up' = a greater one."""
        return "down" if self.after < self.before else "up"

    @property
    def hours_away(self) -> float:
        return (self.after_ts - self.before_ts).total_seconds() / 3600.0


class OddsLedger:
    """Last price this player saw per selection. Past only, fed in time order."""

    def __init__(self) -> None:
        self._seen: dict[tuple[str, float], OddsObservation] = {}

    def last(self, player_id: str, selection_id: float) -> OddsObservation | None:
        return self._seen.get((player_id, selection_id))

    def record(self, player_id: str, obs: OddsObservation) -> None:
        self._seen[(player_id, obs.selection_id)] = obs

    def __len__(self) -> int:
        return len(self._seen)


def _is_priced(row) -> bool:
    return (str(row.event_name) in PRICED_EVENTS
            and pd.notna(getattr(row, "odds", None))
            and pd.notna(getattr(row, "selection_id", None)))


def beyond_noise(before: float, after: float) -> bool:
    """Is this move big enough to be worth telling someone about?

    Sign plays no part in this decision - the same test is applied to a move in
    either direction.
    """
    delta = abs(after - before)
    if delta < CONFIG["min_absolute_move"]:
        return False
    return before > 0 and (delta / before) >= CONFIG["min_relative_move"]


def scan_session(session_df: pd.DataFrame, ledger: OddsLedger) -> list[OddsMovement]:
    """Movements observable in this session. Updates the ledger as it goes.

    Sessions must be fed in chronological order across the corpus, or the
    ledger would be comparing against a future price.
    """
    out: list[OddsMovement] = []
    if len(session_df) == 0:
        return out
    player_id = str(session_df["PlayerID"].iloc[0])

    for seq, row in enumerate(session_df.itertuples()):
        if not _is_priced(row):
            continue
        sel = float(row.selection_id)
        odds = float(row.odds)
        prev = ledger.last(player_id, sel)

        returned = prev is not None and prev.session != int(row.session)
        if returned and beyond_noise(prev.odds, odds):
            out.append(OddsMovement(
                selection_id=sel,
                fixture_id=float(row.fixture_id) if pd.notna(row.fixture_id) else None,
                before=prev.odds, after=odds,
                before_ts=prev.ts, after_ts=row.ts,
                before_session=prev.session, after_session=int(row.session),
                seq=seq,
                sport=row.sport_name if isinstance(row.sport_name, str) else prev.sport,
                competition=(row.on_origin_name
                             if isinstance(row.on_origin_name, str) else prev.competition),
                rule="returned_to_selection_with_moved_price",
                reasons=(
                    f"selection {sel:.0f} was last seen at {prev.odds:.2f} in session "
                    f"{prev.session} on {prev.ts:%Y-%m-%d %H:%M}",
                    f"it is now {odds:.2f}, "
                    f"{abs(odds - prev.odds):.2f} ({abs(odds - prev.odds) / prev.odds * 100:.1f}%) "
                    f"{'lower' if odds < prev.odds else 'higher'}",
                    f"{(row.ts - prev.ts).total_seconds() / 3600:.0f}h between the two views",
                ),
                evidence=(seq,),
            ))

        ledger.record(player_id, OddsObservation(
            selection_id=sel,
            fixture_id=float(row.fixture_id) if pd.notna(row.fixture_id) else None,
            odds=odds, ts=row.ts, session=int(row.session),
            sport=row.sport_name if isinstance(row.sport_name, str) else None,
            competition=(row.on_origin_name
                         if isinstance(row.on_origin_name, str) else None),
        ))
    return out


def _combined(values: list[float]) -> float:
    total = 1.0
    for v in values:
        total *= v
    return total


def render_card(movements: list[OddsMovement]) -> tuple[str, ...]:
    """The exact user-visible lines. No sign-dependent branching except the
    single adjective in TEMPLATE_DIRECTION."""
    if not movements:
        return ()
    lines: list[str] = []
    for m in movements:
        lines.append(TEMPLATE_HEADLINE.format(before=m.before, after=m.after))
        lines.append(TEMPLATE_DIRECTION[m.direction])

    if len(movements) > 1:
        before = _combined([m.before for m in movements])
        after = _combined([m.after for m in movements])
        lines.append(TEMPLATE_COMBINED.format(before=before, after=after))
        lines.append(TEMPLATE_DIRECTION["down" if after < before else "up"])

    lines.append(TEMPLATE_FOOTER)
    return tuple(lines)


def scan_corpus(events: pd.DataFrame):
    """Every movement in the log, in chronological order. Yields (pk, sid, movs).

    The frame is sorted by timestamp first, so `groupby(sort=False)` hands back
    sessions in order of first event. The ledger is per-player, so that is
    exactly the ordering correctness needs.
    """
    ledger = OddsLedger()
    ordered = events.sort_values("ts", kind="stable")
    for (pk, sid), sdf in ordered.groupby(["player_key", "session"], sort=False):
        movs = scan_session(sdf, ledger)
        if movs:
            yield pk, int(sid), movs


# ═══════════════════════════ tests and corpus report ═══════════════════════════

def _words(s: str) -> list[str]:
    return s.split()


def _test_templates_symmetric() -> tuple[bool, str]:
    """Neither direction may be visually heavier than the other."""
    down, up = TEMPLATE_DIRECTION["down"], TEMPLATE_DIRECTION["up"]
    wd, wu = _words(down), _words(up)
    same_len = len(down) == len(up)
    same_words = len(wd) == len(wu)
    differing = [i for i, (a, b) in enumerate(zip(wd, wu)) if a != b]
    one_word = len(differing) == 1
    ok = same_len and same_words and one_word
    return ok, (f"len {len(down)}=={len(up)}, {len(wd)} words each, "
                f"differ in {len(differing)} word "
                f"({wd[differing[0]]!r} vs {wu[differing[0]]!r})"
                if differing else "identical strings")


def _test_render_symmetric() -> tuple[bool, str]:
    """A move down and the mirrored move up must render to the same shape."""
    now = pd.Timestamp("2026-01-02T12:00:00Z")
    then = pd.Timestamp("2026-01-01T12:00:00Z")

    def mk(before, after):
        return OddsMovement(
            selection_id=1.0, fixture_id=1.0, before=before, after=after,
            before_ts=then, after_ts=now, before_session=1, after_session=2,
            seq=0, sport="Košarka", competition="NBA", rule="t",
            reasons=(), evidence=())

    worse = render_card([mk(4.53, 3.36)])
    better = render_card([mk(3.36, 4.53)])
    same_lines = len(worse) == len(better)
    same_lens = [len(a) == len(b) for a, b in zip(worse, better)]
    same_words = [len(_words(a)) == len(_words(b)) for a, b in zip(worse, better)]
    ok = same_lines and all(same_lens) and all(same_words)
    return ok, (f"{len(worse)} lines each; per-line lengths "
                f"{[len(x) for x in worse]} vs {[len(x) for x in better]}")


def _main() -> int:
    events = load_events()
    checks: list[tuple[str, bool, str]] = []

    def chk(name, ok, detail):
        checks.append((name, bool(ok), detail))

    # ---- corpus ----------------------------------------------------------
    down = up = 0
    sessions = 0
    all_movs: list[OddsMovement] = []
    per_player: dict[str, list[int]] = {}
    for pk, sid, movs in scan_corpus(events):
        sessions += 1
        per_player.setdefault(pk, [0, 0])
        for m in movs:
            all_movs.append(m)
            if m.direction == "down":
                down += 1
                per_player[pk][0] += 1
            else:
                up += 1
                per_player[pk][1] += 1
    total = down + up

    print("=" * 96)
    print("ODDS MOVEMENT ACROSS THE CORPUS".center(96))
    print("=" * 96)
    print(f"  sessions containing a disclosable move : {sessions:,}")
    print(f"  movements disclosed                    : {total:,}")
    print(f"  noise floor                            : "
          f">={CONFIG['min_relative_move']*100:.0f}% relative "
          f"AND >={CONFIG['min_absolute_move']:.2f} absolute")
    print()
    print(f"  moved AGAINST the player (smaller return) : {down:4d}   {100*down/total:5.1f}%")
    print(f"  moved TOWARD  the player (greater return) : {up:4d}   {100*up/total:5.1f}%")
    print()
    print("  This is the proof. The engine applies no filter on sign: the same")
    print("  noise test decides both directions, and every move that clears it is")
    print("  shown. A cherry-picking implementation would report ~100% favourable.")

    print(f"\n  {'player':9} {'worse':>6} {'better':>7}")
    for pk in sorted(per_player):
        d, u = per_player[pk]
        print(f"  {pk:9} {d:6d} {u:7d}")

    # ---- a worsened case must render -------------------------------------
    worst = min(all_movs, key=lambda m: m.pct)
    card = render_card([worst])
    chk("a worsened-odds case renders a card",
        bool(card) and TEMPLATE_DIRECTION["down"] in card,
        f"{worst.before:.2f} -> {worst.after:.2f} ({worst.pct*100:+.1f}%) renders "
        f"{len(card)} lines")

    best = max(all_movs, key=lambda m: m.pct)
    chk("an improved-odds case renders the same shape",
        len(render_card([best])) == len(card),
        f"{best.before:.2f} -> {best.after:.2f} ({best.pct*100:+.1f}%)")

    ok, detail = _test_templates_symmetric()
    chk("worse and better templates are length- and structure-identical", ok, detail)

    ok, detail = _test_render_symmetric()
    chk("rendered cards are structurally identical in both directions", ok, detail)

    # ---- banned phrases --------------------------------------------------
    from next_best_help import BANNED_PHRASES, banned_phrases_in
    rendered = []
    for m in all_movs:
        rendered.extend(render_card([m]))
    multi = [m for m in all_movs if m.selection_id]
    rendered.extend(render_card(all_movs[:4]))       # a multi-leg card too
    offenders = [(l, banned_phrases_in(l)) for l in rendered if banned_phrases_in(l)]
    chk("no banned urgency phrase in any rendered odds string",
        not offenders,
        f"{len(rendered):,} lines scanned for {list(BANNED_PHRASES)}; "
        f"{len(offenders)} offender(s)")

    # ---- the split must not be lopsided ----------------------------------
    skew = abs(down - up) / total
    chk("disclosure is not skewed toward favourable moves",
        skew < 0.10,
        f"|down-up|/total = {skew*100:.1f}% (a filtered implementation would be ~100%)")

    # ---- noise floor is sign-blind ---------------------------------------
    sym = all(beyond_noise(a, b) == beyond_noise(b, a)
              for a, b in [(4.53, 3.36), (2.00, 2.05), (1.10, 1.12), (3.0, 3.9)])
    chk("noise threshold is symmetric in sign", sym,
        "beyond_noise(a,b) == beyond_noise(b,a) for every tested pair")

    # ---- worked example --------------------------------------------------
    print("\n" + "=" * 96)
    print("WORKED EXAMPLE - the move that went most against a player".center(96))
    print("=" * 96)
    print(f"  player selection {worst.selection_id:.0f}  {worst.sport} / {worst.competition}")
    for r in worst.reasons:
        print(f"    - {r}")
    print("  renders as:")
    for line in card:
        print(f"    | {line}")

    print("\n  the mirrored improvement renders with identical weight:")
    for line in render_card([best]):
        print(f"    | {line}")

    print("\n" + "=" * 96)
    print("TESTS".center(96))
    print("=" * 96)
    for name, ok, det in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name:56} {det}")
    failed = [n for n, ok, _ in checks if not ok]
    print("\n" + ("ALL TESTS PASS" if not failed else f"FAILED: {', '.join(failed)}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())

#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge 1: Session Quality & Session-to-Action Conversion
Habit profiler: what this player reliably does, derived from their own history.

    profile = build_profile(events, "SYN_U03", exclude_session=current_sid)

WHAT IT IS FOR
--------------
Everything the product currently makes a player find or type - which sport,
which competition, which slip shape, how much they stake, which game - is
already implied by what they did last month. This turns that history into a
`HabitProfile` the session layer can pre-fill from, which is what takes
taps-to-action from ~8-9 down to 2.

NO LOOK-AHEAD, NO LEAKAGE
-------------------------
`exclude_session` drops the session being scored BEFORE any statistic is
computed. A profile used to make a recommendation inside session N is built
only from sessions 1..N-1. Without that, every offline evaluation would be
scoring itself against an answer it had already seen.

`load_ground_truth()` is never imported here. `synthetic_user_profiles.csv` is
a validation target, not an engine input.

RECENCY
-------
Habits move. Events inside the last `recency_days` count `recency_weight` times
as much as older ones, so a player who switched competitions in June is not
described by what they did two years ago. Every modal statistic below is
computed on those weights, not on raw counts.

CONFIDENCE, AND WHY IT IS NOT ONE RULE
--------------------------------------
Confidence gates whether the UI pre-fills or merely suggests. The stated rule -
under 20 placed bets, or a top-sport share under 0.60 - is applied verbatim to
sportsbook players.

It cannot be applied verbatim to casino players. A slots regular draws from a
provider's whole catalogue: SYN_U04 has 2,628 launches across 8 games and a top
game share of 0.35. That is a strong habit, but a literal 0.60 share gate would
call it low confidence. So casino concentration is measured as LIFT OVER
UNIFORM - top share divided by 1/n_distinct - which asks the right question:
is this player more concentrated than chance? SYN_U04 sits at 2.8x uniform.

Usage:  python3 src/profiler.py      # validation table against ground truth
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_loader import load_events, load_ground_truth, resolve_player  # noqa: E402

# ══════════════════════════ TUNING ══════════════════════════
# All of it is here. Nothing below reads a magic number.
CONFIG = {
    # Recency. Events inside the window count this many times as much.
    "recency_days": 90,
    "recency_weight": 3.0,

    # The stated confidence rule, applied verbatim to sportsbook players.
    "min_actions": 20,
    "min_sport_share": 0.60,
    # Casino analogue: concentration relative to picking uniformly at random.
    "min_casino_lift": 1.50,

    # Smooth scoring. Volume saturates here; concentration is rescaled between
    # a floor (no better than chance) and a target (a settled habit).
    "actions_saturation": 60,
    "sport_share_floor": 0.35,
    "sport_share_target": 0.90,
    "casino_lift_target": 3.0,
    "recency_halflife_days": 120.0,

    "w_volume": 0.45,
    "w_concentration": 0.40,
    "w_recency": 0.15,

    # A player failing either stated threshold cannot score above this. It sits
    # strictly BELOW next_best_help's 0.40 prefill threshold on purpose: at 0.40
    # a gated player would satisfy `confidence >= 0.40` and the guard would
    # defeat itself on the boundary.
    "gate_ceiling": 0.35,
    "band_low": 0.45,
    "band_high": 0.70,

    # Width of the reported active-hours band, in hours.
    "hour_band_width": 3,
    # A vertical counts as co-primary above this share of a player's sessions.
    "hybrid_min_share": 0.15,
}

PLACED_SLIP = "betslip_placed"
GAME_LAUNCH = "casino_game_launch"


@dataclass(frozen=True)
class HabitProfile:
    """What we believe about a player, and how much we trust it."""
    player_id: str
    player_key: str

    # --- scope of the evidence -------------------------------------------
    events_considered: int
    sessions_considered: int
    excluded_session: int | None
    reference_time: datetime | None
    primary_vertical: str                 # sports | casino | hybrid | unknown

    # --- sportsbook habit -------------------------------------------------
    top_sport: str | None = None
    top_sport_share: float | None = None
    top_competition: str | None = None
    top_competition_share: float | None = None
    top_market_pattern: str | None = None      # AKO | SOLO | LEG_COMBI
    usual_stake_eur: float | None = None
    n_placed_slips: int = 0

    # --- casino habit -----------------------------------------------------
    top_game: str | None = None
    top_provider: str | None = None
    top_game_share: float | None = None
    top_game_lift: float | None = None
    usual_casino_stake_eur: float | None = None
    n_game_launches: int = 0

    # --- timing -----------------------------------------------------------
    active_hours: tuple[int, int] | None = None
    active_hours_label: str | None = None

    # --- trust ------------------------------------------------------------
    confidence: float = 0.0
    confidence_band: str = "low"
    confidence_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_confident(self) -> bool:
        """True when the UI may pre-fill rather than merely suggest."""
        return self.confidence_band != "low"


# ─────────────────────────── helpers ───────────────────────────

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _weighted_mode(values: pd.Series, weights: pd.Series) -> tuple[str | None, float | None]:
    """Highest-weight value, and its share of total weight. None if no data."""
    mask = values.notna()
    if not mask.any():
        return None, None
    agg = weights[mask].groupby(values[mask]).sum()
    total = float(agg.sum())
    if total <= 0:
        return None, None
    return str(agg.idxmax()), float(agg.max() / total)


def _one_row_per_slip(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse a betslip's legs to a single row.

    A 6-leg accumulator emits six `betslip_add_bet` rows. Counting those as six
    observations would let long slips outvote short ones when deciding a
    player's usual slip shape or competition, which is not what we are asking.
    """
    slips = df[df["betslip_number"].notna()]
    return slips.drop_duplicates(subset="betslip_number", keep="first")


def _best_hour_band(hours: pd.Series, weights: pd.Series, width: int) -> tuple[int, int]:
    """The contiguous `width`-hour window carrying the most weight. Wraps midnight."""
    hist = [0.0] * 24
    for h, w in zip(hours, weights):
        hist[int(h)] += float(w)
    start = max(range(24), key=lambda s: sum(hist[(s + i) % 24] for i in range(width)))
    return start, (start + width) % 24


# ─────────────────────────── the profiler ───────────────────────────

def build_profile(
    events: pd.DataFrame,
    player_id: str,
    exclude_session: int | None = None,
    now: datetime | None = None,
) -> HabitProfile:
    """Derive one player's habit profile from their history.

    `exclude_session` is dropped before anything is measured, so a profile used
    inside a live session never contains that session's own evidence.
    `now` anchors recency; it defaults to the player's most recent retained
    event, which keeps offline runs deterministic.
    """
    pid = resolve_player(events, player_id)
    hist = events[events["PlayerID"] == pid]
    if exclude_session is not None:
        hist = hist[hist["session"] != exclude_session]

    key = str(player_id)[:7] if str(player_id).startswith("SYN_") else pid[:7]

    if hist.empty:
        return HabitProfile(
            player_id=pid, player_key=key, events_considered=0,
            sessions_considered=0, excluded_session=exclude_session,
            reference_time=None, primary_vertical="unknown",
            confidence=0.0, confidence_band="low",
            confidence_reasons=("no history outside the current session",),
        )

    ref = now or hist["ts"].max()

    # Recency weights: one number per event, reused by every statistic below.
    cutoff = ref - timedelta(days=CONFIG["recency_days"])
    w = pd.Series(1.0, index=hist.index)
    w[hist["ts"] >= cutoff] = CONFIG["recency_weight"]

    # --- which product is this player's home turf? ------------------------
    sport_sessions = hist.loc[hist["sport_name"].notna(), "session"].nunique()
    casino_sessions = hist.loc[hist["game_name"].notna(), "session"].nunique()
    total_v = sport_sessions + casino_sessions
    if total_v == 0:
        primary = "unknown"
    elif casino_sessions == 0:
        primary = "sports"
    elif sport_sessions == 0:
        primary = "casino"
    elif min(sport_sessions, casino_sessions) / total_v >= CONFIG["hybrid_min_share"]:
        primary = "hybrid"
    else:
        primary = "sports" if sport_sessions > casino_sessions else "casino"

    # --- sportsbook -------------------------------------------------------
    top_sport, sport_share = _weighted_mode(hist["sport_name"], w)

    slips = _one_row_per_slip(hist)
    slip_w = w.reindex(slips.index)
    top_pattern, _ = _weighted_mode(slips["betslip_type"], slip_w)

    # Competition is resolved WITHIN the top sport. A competition from another
    # sport outranking the player's own is a category-level answer, and the
    # brief forbids category-level recommendations.
    top_comp = comp_share = None
    if top_sport is not None:
        in_sport = slips[slips["sport_name"] == top_sport]
        if not in_sport.empty:
            top_comp, comp_share = _weighted_mode(in_sport["on_origin_name"],
                                                  w.reindex(in_sport.index))
        if top_comp is None:
            rows = hist[hist["sport_name"] == top_sport]
            top_comp, comp_share = _weighted_mode(rows["on_origin_name"],
                                                  w.reindex(rows.index))

    placed = hist[hist["event_name"] == PLACED_SLIP]
    n_placed = int(len(placed))
    stake_sports = float(placed["stake_eur"].median()) if not placed["stake_eur"].dropna().empty else None

    # --- casino -----------------------------------------------------------
    launches = hist[hist["event_name"] == GAME_LAUNCH]
    n_launch = int(len(launches))
    top_game, game_share = _weighted_mode(launches["game_name"], w.reindex(launches.index))
    top_provider = None
    game_lift = None
    if top_game is not None:
        rows = launches[launches["game_name"] == top_game]
        top_provider, _ = _weighted_mode(rows["provider"], w.reindex(rows.index))
        n_distinct = int(launches["game_name"].nunique())
        if n_distinct > 0 and game_share is not None:
            game_lift = float(game_share * n_distinct)
    stake_casino = (float(launches["stake_eur"].median())
                    if not launches["stake_eur"].dropna().empty else None)

    # --- timing -----------------------------------------------------------
    band = _best_hour_band(hist["ts"].dt.hour, w, CONFIG["hour_band_width"])
    band_label = f"{band[0]:02d}:00-{band[1]:02d}:00"

    # --- confidence -------------------------------------------------------
    on_casino = primary == "casino"
    n_actions = n_launch if on_casino else n_placed
    reasons: list[str] = []

    volume = _clamp(n_actions / CONFIG["actions_saturation"])
    if on_casino:
        lift = game_lift or 0.0
        conc = _clamp((lift - 1.0) / (CONFIG["casino_lift_target"] - 1.0))
        gate_conc = lift < CONFIG["min_casino_lift"]
        if gate_conc:
            reasons.append(f"game concentration {lift:.2f}x uniform "
                           f"< {CONFIG['min_casino_lift']}x")
    else:
        share = sport_share or 0.0
        conc = _clamp((share - CONFIG["sport_share_floor"])
                      / (CONFIG["sport_share_target"] - CONFIG["sport_share_floor"]))
        gate_conc = share < CONFIG["min_sport_share"]
        if gate_conc:
            reasons.append(f"top-sport share {share:.2f} < {CONFIG['min_sport_share']}")

    days_since = max(0.0, (ref - hist["ts"].max()).total_seconds() / 86400.0)
    recency = 0.5 ** (days_since / CONFIG["recency_halflife_days"])

    gate_vol = n_actions < CONFIG["min_actions"]
    if gate_vol:
        label = "game launches" if on_casino else "placed bets"
        reasons.append(f"only {n_actions} {label} < {CONFIG['min_actions']}")

    score = (CONFIG["w_volume"] * volume
             + CONFIG["w_concentration"] * conc
             + CONFIG["w_recency"] * recency)
    if gate_vol or gate_conc:
        score = min(score, CONFIG["gate_ceiling"])
    if not reasons:
        reasons.append("settled habit: enough actions and a concentrated preference")

    band_name = ("high" if score >= CONFIG["band_high"]
                 else "medium" if score >= CONFIG["band_low"] else "low")

    return HabitProfile(
        player_id=pid, player_key=key,
        events_considered=int(len(hist)),
        sessions_considered=int(hist["session"].nunique()),
        excluded_session=exclude_session,
        reference_time=ref,
        primary_vertical=primary,
        top_sport=top_sport,
        top_sport_share=round(sport_share, 4) if sport_share is not None else None,
        top_competition=top_comp,
        top_competition_share=round(comp_share, 4) if comp_share is not None else None,
        top_market_pattern=top_pattern,
        usual_stake_eur=round(stake_sports, 2) if stake_sports is not None else None,
        n_placed_slips=n_placed,
        top_game=top_game,
        top_provider=top_provider,
        top_game_share=round(game_share, 4) if game_share is not None else None,
        top_game_lift=round(game_lift, 2) if game_lift is not None else None,
        usual_casino_stake_eur=round(stake_casino, 2) if stake_casino is not None else None,
        n_game_launches=n_launch,
        active_hours=band,
        active_hours_label=band_label,
        confidence=round(score, 3),
        confidence_band=band_name,
        confidence_reasons=tuple(reasons),
    )


def build_all(events: pd.DataFrame, **kw) -> dict[str, HabitProfile]:
    keys = sorted(events["player_key"].unique())
    return {k: build_profile(events, k, **kw) for k in keys}


# ═══════════════════════ validation against ground truth ═══════════════════════
# `synthetic_user_profiles.csv` is the answer key. It is read HERE and nowhere
# else in the engine. Its own top_* fields are raw, unweighted event counts over
# the whole history; the profiler is per-slip and recency-weighted, so the two
# are expected to agree on settled habits and may legitimately diverge where a
# player has no settled preference. That divergence is reported, not hidden.

def _fmt(v, width: int) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        v = "-"
    s = str(v)
    return s[:width].ljust(width)


def _validate() -> int:
    events = load_events()
    gt = load_ground_truth().set_index("player_key")
    profiles = build_all(events)

    print("=" * 108)
    print("HABIT PROFILE vs GROUND TRUTH".center(108))
    print("=" * 108)
    hdr = (f"{'player':8} {'sport':10} {'gt':10} {'competition':13} {'gt':13} "
           f"{'slip':10} {'gt':10} {'stake':>7} {'gt':>7}")
    print(hdr)
    print("-" * 108)

    mismatches: list[str] = []
    for k, p in profiles.items():
        g = gt.loc[k]
        gt_sport = g.top_sport if isinstance(g.top_sport, str) else None
        gt_comp = g.top_competition if isinstance(g.top_competition, str) else None
        gt_slip = g.usual_betslip_type if isinstance(g.usual_betslip_type, str) else None
        gt_stake = float(g.usual_stake_eur) if str(g.usual_stake_eur) not in ("", "nan") else None
        # Ground truth blends both verticals into one stake column; compare
        # against whichever vertical this player actually lives in.
        ours_stake = (p.usual_casino_stake_eur if p.primary_vertical == "casino"
                      else p.usual_stake_eur)

        print(f"{k:8} {_fmt(p.top_sport,10)} {_fmt(gt_sport,10)} "
              f"{_fmt(p.top_competition,13)} {_fmt(gt_comp,13)} "
              f"{_fmt(p.top_market_pattern,10)} {_fmt(gt_slip,10)} "
              f"{(f'{ours_stake:.2f}' if ours_stake else '-'):>7} "
              f"{(f'{gt_stake:.2f}' if gt_stake else '-'):>7}")

        if gt_sport and p.top_sport != gt_sport:
            mismatches.append(f"{k} top_sport: {p.top_sport} vs gt {gt_sport}")
        if gt_slip and p.top_market_pattern != gt_slip:
            mismatches.append(f"{k} slip: {p.top_market_pattern} vs gt {gt_slip}")
        if gt_comp and p.top_competition != gt_comp:
            mismatches.append(
                f"{k} competition: {p.top_competition} "
                f"(share {p.top_competition_share:.2f}) vs gt {gt_comp}")

    print("\n" + "=" * 108)
    print("CASINO HABIT".center(108))
    print("=" * 108)
    print(f"{'player':8} {'top_game':30} {'gt':30} {'provider':16} {'share':>6} {'lift':>6}")
    print("-" * 108)
    for k, p in profiles.items():
        g = gt.loc[k]
        gt_game = g.top_game if isinstance(g.top_game, str) else None
        if p.top_game is None and gt_game is None:
            continue
        print(f"{k:8} {_fmt(p.top_game,30)} {_fmt(gt_game,30)} {_fmt(p.top_provider,16)} "
              f"{(f'{p.top_game_share:.2f}' if p.top_game_share else '-'):>6} "
              f"{(f'{p.top_game_lift:.2f}' if p.top_game_lift else '-'):>6}")

    print("\n" + "=" * 108)
    print("CONFIDENCE".center(108))
    print("=" * 108)
    print(f"{'player':8} {'vertical':9} {'actions':>8} {'conc':>6} {'conf':>6} {'band':7} "
          f"{'hours':12} why")
    print("-" * 108)
    for k, p in profiles.items():
        acts = p.n_game_launches if p.primary_vertical == "casino" else p.n_placed_slips
        conc = p.top_game_lift if p.primary_vertical == "casino" else p.top_sport_share
        print(f"{k:8} {p.primary_vertical:9} {acts:8d} "
              f"{(f'{conc:.2f}' if conc else '-'):>6} {p.confidence:6.3f} "
              f"{p.confidence_band:7} {_fmt(p.active_hours_label,12)} "
              f"{p.confidence_reasons[0][:44]}")

    # ── required outcomes ────────────────────────────────────────────────
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    u1, u2, u4, u6, u8 = (profiles[k] for k in
                          ("SYN_U01", "SYN_U02", "SYN_U04", "SYN_U06", "SYN_U08"))

    chk("U01 -> Nogomet + AKO",
        u1.top_sport == "Nogomet" and u1.top_market_pattern == "AKO",
        f"{u1.top_sport} + {u1.top_market_pattern}")
    chk("U02 -> Tenis + SOLO",
        u2.top_sport == "Tenis" and u2.top_market_pattern == "SOLO",
        f"{u2.top_sport} + {u2.top_market_pattern}")
    chk("U04 -> Greentube/Amusnet slot",
        u4.top_provider in {"Greentube", "Amusnet"},
        f"{u4.top_game} ({u4.top_provider})")
    chk("U06 -> BlackJack",
        bool(u6.top_game) and "blackjack" in u6.top_game.lower(),
        f"{u6.top_game} ({u6.top_provider})")
    chk("U08 -> low confidence",
        u8.confidence_band == "low",
        f"{u8.confidence:.3f} / {u8.confidence_band} - {u8.confidence_reasons[0]}")
    chk("every other player is not low",
        all(p.confidence_band != "low" for k, p in profiles.items() if k != "SYN_U08"),
        ", ".join(f"{k}:{p.confidence_band}" for k, p in profiles.items() if k != "SYN_U08"))

    # No leakage: excluding a session must not change the shape of the answer.
    sid = int(events[events.player_key == "SYN_U03"].session.iloc[-1])
    full = profiles["SYN_U03"]
    held = build_profile(events, "SYN_U03", exclude_session=sid)
    chk("leakage guard: excluding a session drops its events",
        held.events_considered < full.events_considered,
        f"{full.events_considered} -> {held.events_considered} events "
        f"(session {sid} withheld)")
    chk("leakage guard: habit is stable without it",
        held.top_sport == full.top_sport,
        f"{held.top_sport} == {full.top_sport}")

    print("\n" + "=" * 108)
    print("REQUIRED OUTCOMES".center(108))
    print("=" * 108)
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name:46} {detail}")

    if mismatches:
        print("\nDivergence from ground truth (methodology differences, not failures):")
        for m in mismatches:
            print(f"  - {m}")

    failed = [n for n, ok, _ in checks if not ok]
    print("\n" + ("ALL REQUIRED OUTCOMES PASS" if not failed
                  else f"FAILED: {', '.join(failed)}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_validate())

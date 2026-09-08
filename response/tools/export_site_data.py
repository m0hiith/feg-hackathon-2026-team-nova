#!/usr/bin/env python3
"""Export real engine output for the FEG mock site -> site_data.json.

Runs the actual engine (profiler / intent / friction / next_best_help /
odds_movement) over every session of all 8 synthetic players and writes the
per-player decisions the mock site renders. Nothing in the site is hand-written:
every card, stake, confidence score and census bar comes from this file.

After running it, re-embed the JSON into the page:
    the <script id="feg-data"> block in "Image 2.html" holds a copy.

Usage:  python3 tools/export_site_data.py   (needs pandas)
"""
from __future__ import annotations
import json, math, sys
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
from data_loader import load_events, load_ground_truth, sessions_for
from profiler import build_profile
from intent import replay
from friction import detect, Friction
from next_best_help import HelpEngine, Action, _constructed_browse_session
from odds_movement import OddsLedger, scan_session, render_card

events = load_events()
gt = load_ground_truth().set_index("player_key")
keys = sorted(events["player_key"].unique())

# ---------- corpus-wide odds movements (chronological, per player) ----------
movements_by_player: dict[str, list] = {k: [] for k in keys}
ledger = OddsLedger()
ordered = events.sort_values("ts", kind="stable")
for (pk, sid), sdf in ordered.groupby(["player_key", "session"], sort=False):
    movs = scan_session(sdf, ledger)
    if movs:
        movements_by_player[pk].append((int(sid), movs))

n_worse = n_better = 0
for pk, lst in movements_by_player.items():
    for sid, movs in lst:
        for m in movs:
            if m.direction == "down": n_worse += 1
            else: n_better += 1

out = {"users": {}, "meta": {}}

for k in keys:
    g = gt.loc[k]
    prof = build_profile(events, k)
    sess = sessions_for(k, events)

    # ---- session census: run the engine over every session ----
    engine = HelpEngine()
    census = Counter()
    demo = None            # best CONTINUE_CARD / RESUME_GAME session
    clarify = None
    reached = completed = 0
    for sdf in sess:
        sid = int(sdf["session"].iloc[0])
        p = build_profile(events, k, exclude_session=sid)
        st = replay(sdf)
        d = engine.decide(sdf, p, st)
        census[d.action.value] += 1
        evs = set(sdf["event_name"])
        if "betslip_add_bet" in evs: reached += 1
        if "betslip_placed" in evs: completed += 1
        if d.action in (Action.CONTINUE_CARD, Action.RESUME_GAME) and demo is None:
            demo = (sid, d, sdf)
        if d.action is Action.CLARIFY_INFO and clarify is None:
            clarify = (sid, d, sdf)

    # ---- the demo card ----
    card = None
    if demo:
        sid, d, sdf = demo
        card = {
            "action": d.action.value, "rule": d.rule, "session": sid,
            "seq": d.seq, "ts": d.ts.isoformat() if d.ts is not None else None,
            "reasons": [r for r in d.reasons if r],
            "rendered": list(d.rendered),
            "payload": {kk: (float(v) if isinstance(v, float) else
                             v if not isinstance(v, list) else [str(x) for x in v])
                        for kk, v in d.payload.items() if kk != "selection_ids"},
        }
    # Casino personas: the resume route needs a lobby-browse session. The module
    # ships _constructed_browse_session for exactly this; flagged as constructed.
    if card is None and prof.top_game:
        bs = _constructed_browse_session(events, pk=k, pages=7)
        d = HelpEngine().decide(bs, prof)
        if d.action is Action.RESUME_GAME:
            card = {"action": d.action.value, "rule": d.rule, "session": None,
                    "seq": d.seq, "ts": None, "constructed": True,
                    "reasons": [r for r in d.reasons if r],
                    "rendered": list(d.rendered),
                    "payload": {kk: v for kk, v in d.payload.items()
                                if kk != "selection_ids"}}

    # Why we stay silent, when we do. For the cold-start persona this IS the demo.
    silent = None
    for sdf in sess:
        sid = int(sdf["session"].iloc[0])
        p = build_profile(events, k, exclude_session=sid)
        d = HelpEngine().decide(sdf, p, replay(sdf))
        if d.action is Action.NO_ACTION and d.rule == "cold_start_no_prefill":
            silent = {"rule": d.rule, "reasons": [r for r in d.reasons if r],
                      "session": sid}
            break
    if silent is None:
        for sdf in sess[:60]:
            sid = int(sdf["session"].iloc[0])
            p = build_profile(events, k, exclude_session=sid)
            d = HelpEngine().decide(sdf, p, replay(sdf))
            if d.action is Action.NO_ACTION:
                silent = {"rule": d.rule, "reasons": [r for r in d.reasons if r],
                          "session": sid}
                break

    clarify_card = None
    if clarify:
        sid, d, sdf = clarify
        clarify_card = {"action": d.action.value, "rule": d.rule, "session": sid,
                        "reasons": [r for r in d.reasons if r],
                        "rendered": list(d.rendered)}

    # ---- odds movement card ----
    odds_card = None
    if movements_by_player[k]:
        sid, movs = movements_by_player[k][len(movements_by_player[k]) // 2]
        m = movs[0]
        odds_card = {
            "session": sid, "before": round(m.before, 2), "after": round(m.after, 2),
            "direction": m.direction, "pct": round(m.pct * 100, 1),
            "sport": m.sport, "competition": m.competition,
            "hours_away": round(m.hours_away, 1),
            "rendered": list(render_card([m])),
        }
    mv = [mm for _, ms in movements_by_player[k] for mm in ms]

    # ---- friction census ----
    fr = Counter()
    for sdf in sess[-150:]:
        sid = int(sdf["session"].iloc[0])
        p = build_profile(events, k, exclude_session=sid)
        for s in detect(sdf, profile=p):
            fr[s.kind.value] += 1

    # ---- top competitions actually played, for the fixture list ----
    mine = events[events.player_key == k]
    comps = (mine[mine.sport_name == prof.top_sport]["on_origin_name"].value_counts().head(4).to_dict()
             if prof.top_sport else {})
    games = mine[mine.event_name == "casino_game_launch"]["game_name"].value_counts().head(5).to_dict()

    out["users"][k] = {
        "key": k,
        "persona": g.persona,
        "player_id": prof.player_id,
        "platform": g.platform,
        "vertical": prof.primary_vertical,
        "demo_note": g.demo_note,
        "first_seen": str(g.first_seen), "last_seen": str(g.last_seen),
        "sessions": int(prof.sessions_considered), "events": int(prof.events_considered),
        "profile": {
            "top_sport": prof.top_sport,
            "top_sport_share": prof.top_sport_share,
            "top_competition": prof.top_competition,
            "top_competition_share": prof.top_competition_share,
            "slip_type": prof.top_market_pattern,
            "usual_stake_eur": prof.usual_stake_eur,
            "n_placed_slips": prof.n_placed_slips,
            "top_game": prof.top_game, "top_provider": prof.top_provider,
            "top_game_share": prof.top_game_share, "top_game_lift": prof.top_game_lift,
            "usual_casino_stake_eur": prof.usual_casino_stake_eur,
            "n_game_launches": prof.n_game_launches,
            "active_hours": prof.active_hours_label,
            "confidence": prof.confidence, "band": prof.confidence_band,
            "reasons": list(prof.confidence_reasons),
            "is_confident": prof.is_confident,
        },
        "stats": {
            "sessions_reaching_betslip": reached,
            "sessions_completing": completed,
            "final_step_abandon_pct": float(g.final_step_abandon_pct),
            "census": dict(census),
            "friction": dict(fr),
            "n_movements": len(mv),
            "n_worse": sum(1 for m in mv if m.direction == "down"),
            "n_better": sum(1 for m in mv if m.direction == "up"),
        },
        "card": card,
        "silent": silent,
        "clarify_card": clarify_card,
        "odds_card": odds_card,
        "competitions": {str(a): int(b) for a, b in comps.items()},
        "games": {str(a): int(b) for a, b in games.items()},
    }
    print(f"  {k:8} {prof.primary_vertical:7} conf={prof.confidence:.3f} "
          f"card={card['action'] if card else '-':14} mv={len(mv)}", file=sys.stderr)

out["meta"] = {
    "total_events": int(len(events)),
    "total_sessions": int(events["session"].nunique()),
    "total_players": len(keys),
    "first_seen": str(events["ts"].min()), "last_seen": str(events["ts"].max()),
    "movements_total": n_worse + n_better,
    "movements_worse": n_worse, "movements_better": n_better,
}

def _clean(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict): return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list): return [_clean(v) for v in o]
    return o

def _rebrand(o):
    """The dataset quotes psk.hr URLs in its evidence strings; the mock is FEG."""
    if isinstance(o, str):  return o.replace("psk.hr", "feg.hr").replace("PSK", "FEG")
    if isinstance(o, dict): return {k: _rebrand(v) for k, v in o.items()}
    if isinstance(o, list): return [_rebrand(v) for v in o]
    return o

out = _rebrand(_clean(out))
dest = ROOT / "site_data.json"
dest.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str, allow_nan=False))
print(f"\nwrote {dest}  ({dest.stat().st_size/1024:.0f} KB)", file=sys.stderr)

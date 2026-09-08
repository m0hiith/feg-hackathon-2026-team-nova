#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge: Session Quality & Session-to-Action Conversion
Step 6: pick the two sessions the demo shows, and write them to config/.

WHY THIS FILE EXISTS
--------------------
The demo has to survive the obvious question: "did you cherry-pick a session
the rules were built on?" So both sessions are drawn from the TEST side of the
player-level holdout in scripts/holdout_eval.py - the same seed (42), the same
70/30 fraction, the same split function, imported rather than re-implemented.
The 27 holdout players are people the thresholds in src/trigger.py have never
been shaped by.

src/trigger.py is imported and run UNMODIFIED. Nothing here tunes anything;
this file only replays and selects.

  SESSION A - the FIRE case.  SB iOS, trigger fired, slip built and never
              submitted, >=3 selections, lead time near the test-side median
              (a typical case, not the flattering tail).
  SESSION B - the SILENT case. SB iOS, slip built, trigger never fired, and
              the user converted - the system correctly staying out of the way.

Usage:  python3 scripts/pick_demo_sessions.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from features import FeatureAccumulator, InMemoryStateBackend   # noqa: E402
from replay import CONFIG as RCONFIG, stream_sessions           # noqa: E402
from trigger import SlipRescueTrigger, _labels, MIN_ADDS_TO_ARM  # noqa: E402
import trigger as trigger_mod                                    # noqa: E402
from holdout_eval import CONFIG as HCONFIG, split_players        # noqa: E402

CONFIG = {
    "source": RCONFIG["source"],
    "out": REPO_ROOT / "config" / "demo_sessions.json",
    "platform": "SB iOS",
    "min_selections": 3,       # the brief: at least 3 legs in the slip
}


def mask(pid: str) -> str:
    return f"{pid[:8]}…{pid[-4:]}"


def _median(xs: list[float]) -> float | None:
    s = sorted(x for x in xs if x is not None)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# ───────────────────────── replay one session ─────────────────────────
def replay(key: tuple[str, int], consumer, truth_row: dict) -> dict[str, Any]:
    """Run the unmodified accumulator + trigger over one session.

    Records every decision in order. The trigger sees only the streaming
    feature dicts, exactly as in holdout_eval.py; the outcome label is applied
    afterwards and is never handed to evaluate().
    """
    acc = FeatureAccumulator(InMemoryStateBackend())
    trig = SlipRescueTrigger(InMemoryStateBackend())
    timeline: list[dict[str, Any]] = []
    fired_at_ms: int | None = None
    fired_at_elapsed: float | None = None
    first_ms = last_ms = None
    n = 0
    final: dict[str, Any] = {}

    for record in consumer:
        n += 1
        if first_ms is None:
            first_ms = record.timestamp
        last_ms = record.timestamp
        f = acc.update(record.value)
        final = f
        d = trig.evaluate(f)
        if d.fired and fired_at_ms is None:
            fired_at_ms, fired_at_elapsed = record.timestamp, f["elapsed_s"]
        timeline.append({
            "i": n,
            "ts": record.value.get("timestamp"),
            "elapsed_s": f["elapsed_s"],
            "event_name": f["event_name"],
            "action": d.action.value,
            "reason": d.reason.value,
            "adds": f["add_bet_count"],
            "dwell_s": f["dwell_s"],
            "since_add_s": f.get("since_last_add_s"),
            "vel": f["velocity_epm"],
            "peak": f["peak_velocity_epm"],
            "ratio": d.evidence.get("velocity_ratio"),
            "screen": f["screen"],
        })

    return {
        "key": key, "player_id": key[0], "sid": key[1],
        "platform": truth_row["platform"], "converted": truth_row["converted"],
        "n_events": n,
        "duration_s": (last_ms - first_ms) / 1000.0 if n else 0.0,
        "selections": final.get("add_bet_count", 0),
        "placed": final.get("placed_count", 0),
        "placed_legs": final.get("placed_leg_count", 0),
        "fired": fired_at_ms is not None,
        "fired_at_elapsed_s": fired_at_elapsed,
        "lead_s": (truth_row["last_ms"] - fired_at_ms) / 1000.0
        if fired_at_ms is not None else None,
        "timeline": timeline,
        "first_ms": first_ms, "last_ms": last_ms,
    }


# ───────────────────────── slip contents ─────────────────────────
def slip_contents(source: Path, key: tuple[str, int]) -> dict[str, Any]:
    """What is actually in the slip, straight out of the log - no enrichment.

    The event log carries `fixture_id` (an opaque `ufo:mtch:…` token) and
    `sport_name` (Croatian). It does NOT carry a fixture *name*: there is no
    human-readable match label anywhere in this file, and no key that reaches
    the one in SB_Player (DATA_MAP §3). So this prints the ids as they are.
    """
    import duckdb
    con = duckdb.connect()
    src = str(source).replace("'", "''")
    try:
        adds = con.execute(f"""
            SELECT strftime(ts, '%H:%M:%S') AS t, sport_name, fixture_id,
                   selection_id, added_from, betslip_type
            FROM read_parquet('{src}')
            WHERE PlayerID = ? AND sid = ? AND event_name = 'betslip_add_bet'
            ORDER BY ts""", [key[0], int(key[1])]).fetchall()
        placed = con.execute(f"""
            SELECT strftime(ts, '%H:%M:%S') AS t, betslip_number, betslip_type,
                   status
            FROM read_parquet('{src}')
            WHERE PlayerID = ? AND sid = ? AND event_name = 'betslip_placed'
            ORDER BY ts""", [key[0], int(key[1])]).fetchall()
        legs = con.execute(f"""
            SELECT sport_name, fixture_id, selection_id, betslip_number
            FROM read_parquet('{src}')
            WHERE PlayerID = ? AND sid = ? AND event_name = 'betslip_placed_bet'
            ORDER BY ts""", [key[0], int(key[1])]).fetchall()
        window = con.execute(f"""
            SELECT min(ts), max(ts) FROM read_parquet('{src}')
            WHERE PlayerID = ? AND sid = ?""", [key[0], int(key[1])]).fetchone()
    finally:
        con.close()
    return {"adds": adds, "placed": placed, "legs": legs, "window": window}


def selection_shape(source: Path, keys: list[tuple[str, int]]
                    ) -> dict[tuple[str, int], dict[str, int]]:
    """How many adds carry a real fixture, and how many distinct slips were
    submitted - for every candidate key in one query.

    An add with a NULL `fixture_id` is a re-bet replayed out of
    `betslip_history` (BASELINE §5: 5.9% of conversions are re-bets). Those
    are a different journey and make an illegible demo, so a session is
    preferred when its slip is built from real, freshly-picked selections.
    """
    if not keys:
        return {}
    import duckdb
    con = duckdb.connect()
    src = str(source).replace("'", "''")
    pairs = ", ".join(["(?, ?)"] * len(keys))
    params = [v for k, sid in keys for v in (k, int(sid))]
    try:
        rows = con.execute(f"""
            SELECT PlayerID, sid,
                   count(*) FILTER (event_name='betslip_add_bet'
                                    AND fixture_id IS NOT NULL)  AS real_adds,
                   count(*) FILTER (event_name='betslip_add_bet'
                                    AND added_from='betslip_history') AS rebet_adds,
                   count(DISTINCT betslip_number)
                     FILTER (event_name='betslip_placed')         AS n_slips,
                   count(DISTINCT sport_name)
                     FILTER (event_name='betslip_add_bet')        AS n_sports
            FROM read_parquet('{src}')
            WHERE (PlayerID, sid) IN ({pairs})
            GROUP BY 1, 2""", params).fetchall()
    finally:
        con.close()
    return {(r[0], int(r[1])): {"real_adds": int(r[2]), "rebet_adds": int(r[3]),
                                "n_slips": int(r[4]), "n_sports": int(r[5])}
            for r in rows}


# ───────────────────────── printing ─────────────────────────
def print_timeline(rows: list[dict[str, Any]]) -> None:
    """Full decision timeline, run-length collapsed.

    Every event is a decision; consecutive events that produced the same
    (action, reason) are folded into one line showing the run and its elapsed
    span. Every betslip event and the FIRE are always broken out on their own
    line, so nothing that matters is hidden inside a run.
    """
    print(f"    {'#':>6}  {'elapsed':>9}  {'event':<20} {'act':<5} "
          f"{'reason':<22} {'adds':>4} {'dwell':>8} {'sinceAdd':>9} "
          f"{'vel':>7} {'peak':>7} {'ratio':>6}")
    print("    " + "-" * 116)

    def emit(r: dict[str, Any], span: str | None = None, count: int = 1) -> None:
        lbl = f"{r['i']:>6}" if count == 1 else f"{r['i']:>4}x{count:<1}"
        ev = (r["event_name"] or "-")[:20]
        act = "FIRE" if r["action"] == "FIRE" else "-"
        el = span or f"{r['elapsed_s']:>8,.0f}s"
        sa = f"{r['since_add_s']:>8,.0f}s" if r["since_add_s"] is not None else "        -"
        rt = f"{r['ratio']:.3f}" if r["ratio"] is not None else "     -"
        print(f"    {lbl}  {el:>9}  {ev:<20} {act:<5} {r['reason']:<22} "
              f"{r['adds']:>4} {r['dwell_s']:>7,.0f}s {sa:>9} "
              f"{r['vel']:>7,.1f} {r['peak']:>7,.1f} {rt:>6}")

    BETSLIP = {"betslip_add_bet", "betslip_placed", "betslip_placed_bet"}
    i = 0
    while i < len(rows):
        r = rows[i]
        if r["action"] == "FIRE" or r["event_name"] in BETSLIP:
            emit(r)
            i += 1
            continue
        j = i
        while (j + 1 < len(rows)
               and rows[j + 1]["reason"] == r["reason"]
               and rows[j + 1]["action"] != "FIRE"
               and rows[j + 1]["event_name"] not in BETSLIP):
            j += 1
        if j == i:
            emit(r)
        else:
            span = f"{rows[i]['elapsed_s']:,.0f}-{rows[j]['elapsed_s']:,.0f}s"
            emit(rows[j], span=span, count=j - i + 1)
        i = j + 1


def print_session(tag: str, s: dict[str, Any], contents: dict[str, Any],
                  test_median: float | None) -> None:
    key = s["key"]
    print("\n" + "=" * 120)
    print(f"{tag}")
    print("=" * 120)
    dur = s["duration_s"]
    print(f"  PlayerID (masked)   {mask(s['player_id'])}")
    print(f"  sid                 {s['sid']}")
    print(f"  platform            {s['platform']}")
    print(f"  events              {s['n_events']:,}")
    print(f"  duration            {dur:,.0f}s  ({dur/60:,.1f} min)")
    w = contents["window"]
    print(f"  session window      {w[0]}  ->  {w[1]}  (UTC)")
    print(f"  selections added    {s['selections']}  (betslip_add_bet)")
    print(f"  slips submitted     {s['placed']}  (betslip_placed)  "
          f"| legs: {s['placed_legs']} (betslip_placed_bet)")
    print(f"  outcome             "
          f"{'CONVERTED' if s['converted'] else 'ABANDONED (slip built, never submitted)'}")
    if s["fired"]:
        print(f"  trigger             FIRE at elapsed {s['fired_at_elapsed_s']:,.0f}s")
        print(f"  lead time           {s['lead_s']:,.0f}s before the session's "
              f"last event"
              + (f"   (test-side median lead {test_median:,.0f}s)"
                 if test_median is not None else ""))
    else:
        print(f"  trigger             SILENT for the whole session "
              f"({s['n_events']:,} decisions, none FIRE)")

    print(f"\n  SLIP CONTENTS - exactly as they appear in the log")
    print(f"    {'time':<10} {'sport_name':<16} {'fixture_id':<22} "
          f"{'selection_id':<26} {'added_from':<24}")
    print("    " + "-" * 102)
    for t, sport, fx, sel, af, bt in contents["adds"]:
        print(f"    {t:<10} {(sport or '-'):<16} {(fx or '-'):<22} "
              f"{(sel or '-'):<26} {(af or '-'):<24}")
    if contents["placed"]:
        print(f"\n    submitted slips:")
        for t, num, bt, st in contents["placed"]:
            print(f"      {t}  betslip {num}  type {bt}  status {st}")
    if contents["legs"]:
        print(f"\n    legs on the submitted slip(s) (betslip_placed_bet):")
        for sport, fx, sel, num in contents["legs"]:
            print(f"      {(sport or '-'):<16} {(fx or '-'):<22} "
                  f"{(sel or '-'):<26} {num}")

    print(f"\n  TRIGGER DECISION TIMELINE  (one decision per event; "
          f"runs of identical decisions collapsed,\n  every betslip event and "
          f"the FIRE always shown on their own line)")
    print_timeline(s["timeline"])


# ───────────────────────── main ─────────────────────────
def main() -> int:
    source = CONFIG["source"]
    if not source.exists():
        print(f"ERROR: {source} - run `python3 scripts/sanitize.py` first")
        return 1

    import duckdb
    con = duckdb.connect()
    players = [r[0] for r in con.execute(
        f"SELECT DISTINCT PlayerID FROM "
        f"read_parquet('{str(source).replace(chr(39), chr(39)*2)}')").fetchall()]
    con.close()

    train, test = split_players(players, HCONFIG["seed"], HCONFIG["train_fraction"])
    test_set = set(test)
    print(f"HOLDOUT SPLIT (imported from scripts/holdout_eval.py, not "
          f"re-implemented)")
    print(f"  seed={HCONFIG['seed']}  train_fraction={HCONFIG['train_fraction']:.0%}"
          f"  ->  {len(train)} train players / {len(test)} TEST players")
    print(f"  Both demo sessions must be owned by one of those {len(test)} "
          f"test players.")
    print(f"\n  Trigger constants (unmodified, imported from src/trigger.py): "
          f"MIN_ADDS_TO_ARM={trigger_mod.MIN_ADDS_TO_ARM}, "
          f"STALL_DWELL_S={trigger_mod.STALL_DWELL_S:.0f}, "
          f"STALL_SINCE_ADD_S={trigger_mod.STALL_SINCE_ADD_S:.0f}, "
          f"VELOCITY_COLLAPSE_RATIO={trigger_mod.VELOCITY_COLLAPSE_RATIO}")

    # Replay EVERY test-side slip-building session, not just SB iOS, so the
    # 293s median lead quoted in holdout_eval.py is recomputed here rather
    # than pasted in - the target for session A has to be a number this
    # script can stand behind.
    truth = _labels(source)
    all_keys = sorted(k for k in truth if k[0] in test_set)
    print(f"\n  slip-building sessions owned by the {len(test)} test players: "
          f"{len(all_keys):,} (all platforms)")

    runs: dict[tuple[str, int], dict[str, Any]] = {}
    for key, consumer in stream_sessions(all_keys, source=source):
        runs[key] = replay(key, consumer, truth[key])

    all_rows = list(runs.values())
    med_all = _median([r["lead_s"] for r in all_rows
                       if r["fired"] and not r["converted"]])
    print(f"  median lead, fired abandoners, ALL platforms test side: "
          f"{med_all:,.0f}s   <- the figure holdout_eval.py reports")

    rows = [r for r in all_rows if r["platform"] == CONFIG["platform"]]
    ab = [r for r in rows if not r["converted"]]
    cv = [r for r in rows if r["converted"]]
    ab_f = [r for r in ab if r["fired"]]
    cv_f = [r for r in cv if r["fired"]]
    med_ab = _median([r["lead_s"] for r in ab_f])
    print(f"  {CONFIG['platform']} test side: {len(rows):,} slip-building "
          f"sessions - abandoned {len(ab):,} ({len(ab_f):,} fired) | "
          f"converted {len(cv):,} ({len(cv_f):,} fired)")
    print(f"  median lead, fired abandoners, {CONFIG['platform']} only: "
          f"{med_ab:,.0f}s")

    # ── SESSION A ────────────────────────────────────────────────────
    # fired, abandoned, >=3 selections, lead closest to the ALL-platform test
    # median (293s). The SB iOS-only median is reported alongside so the
    # chosen session can be checked against both; anchoring on the headline
    # number is what keeps this from being the flattering tail.
    target = med_all
    print(f"\n  SESSION A targets a lead near {target:,.0f}s "
          f"(SB iOS median {med_ab:,.0f}s shown for reference)")
    a_pool = [r for r in ab_f if r["selections"] >= CONFIG["min_selections"]]
    print(f"\n  SESSION A pool: fired + abandoned + >={CONFIG['min_selections']} "
          f"selections -> {len(a_pool)} candidates")
    if not a_pool:
        print("  ERROR: no candidate for session A")
        return 1
    a_pool.sort(key=lambda r: (abs(r["lead_s"] - target), -r["selections"],
                               r["player_id"], r["sid"]))
    print(f"    {'masked player':<16} {'sid':>12} {'sel':>4} {'events':>7} "
          f"{'dur':>9} {'lead':>9} {'|lead-293s|':>12} {'|lead-iOSmed|':>14}")
    print("    " + "-" * 92)
    for r in a_pool[:8]:
        print(f"    {mask(r['player_id']):<16} {r['sid']:>12} "
              f"{r['selections']:>4} {r['n_events']:>7,} "
              f"{r['duration_s']:>8,.0f}s {r['lead_s']:>8,.0f}s "
              f"{abs(r['lead_s']-target):>11,.0f}s "
              f"{abs(r['lead_s']-med_ab):>13,.0f}s")
    A = a_pool[0]

    # ── SESSION B ────────────────────────────────────────────────────
    # slip built, never fired, and converted.
    b_pool = [r for r in cv if not r["fired"]]
    print(f"\n  SESSION B pool: slip built + trigger silent + converted -> "
          f"{len(b_pool)} candidates")
    if not b_pool:
        print("  ERROR: no candidate for session B")
        return 1

    shape = selection_shape(source, [r["key"] for r in b_pool])
    # How deep into the rule chain the trigger got while the gate was open
    # (slip built, nothing placed yet). Reaching STILL_AT_PACE is the strong
    # case: the hesitation rule DID match and the velocity-collapse
    # confirmation is what held the nudge back. HEALTHY_PROGRESS means the
    # session never even paused.
    DEPTH = {"healthy_progress": 1, "still_at_pace": 2}
    for r in b_pool:
        sh = shape.get(r["key"], {})
        r["real_adds"] = sh.get("real_adds", 0)
        r["rebet_adds"] = sh.get("rebet_adds", 0)
        r["n_slips"] = sh.get("n_slips", 0)
        gate = [t for t in r["timeline"]
                if t["adds"] >= MIN_ADDS_TO_ARM and t["reason"] in DEPTH]
        r["depth"] = max((DEPTH[t["reason"]] for t in gate), default=0)
        r["deepest_reason"] = next(
            (k for k, v in DEPTH.items() if v == r["depth"]), "-")

    # Rank for legibility on stage, in this order:
    #   1. a slip the UI can actually render: >=3 freshly-picked selections
    #      and NO re-bet adds. A re-bet add out of betslip_history carries a
    #      NULL fixture_id, so a session full of them shows the audience a
    #      column of dashes - and re-bet is a different journey anyway
    #      (BASELINE §5). This outranks everything else.
    #   2. exactly one submitted slip - one clean conversion to point at
    #   3. how deep the rule chain got while the gate was open: reaching
    #      STILL_AT_PACE is the stronger story, because the hesitation rule
    #      DID match and the velocity-collapse confirmation is what held the
    #      nudge back. HEALTHY_PROGRESS only means the session never paused.
    #   4. a session short enough that its timeline fits on a slide
    b_pool.sort(key=lambda r: (
        0 if (r["real_adds"] >= CONFIG["min_selections"]
              and r["rebet_adds"] == 0) else 1,
        0 if r["n_slips"] == 1 else 1,
        -r["depth"],
        abs(r["n_events"] - 60),
        r["player_id"], r["sid"]))

    print(f"    {'masked player':<16} {'sid':>12} {'sel':>4} {'real':>5} "
          f"{'rebet':>6} {'slips':>6} {'events':>7} {'dur':>9} {'legs':>5}  "
          f"{'deepest silent reason':<22}")
    print("    " + "-" * 106)
    for r in b_pool[:8]:
        print(f"    {mask(r['player_id']):<16} {r['sid']:>12} "
              f"{r['selections']:>4} {r['real_adds']:>5} {r['rebet_adds']:>6} "
              f"{r['n_slips']:>6} {r['n_events']:>7,} "
              f"{r['duration_s']:>8,.0f}s {r['placed_legs']:>5}  "
              f"{r['deepest_reason']:<22}")
    B = b_pool[0]

    print_session("SESSION A - THE FIRE CASE  (test-side player, "
                  "SB iOS, slip abandoned)", A, slip_contents(source, A["key"]),
                  med_ab)
    print_session("SESSION B - THE SILENT CASE  (test-side player, "
                  "SB iOS, slip built, converted, never nudged)", B,
                  slip_contents(source, B["key"]), None)

    # ── the fixture-name reality check ───────────────────────────────
    print("\n" + "=" * 120)
    print("FIXTURE NAMES - what the log actually contains")
    print("=" * 120)
    import duckdb
    con = duckdb.connect()
    src = str(source).replace("'", "''")
    n_named = con.execute(f"""
        SELECT count(*) FROM read_parquet('{src}')
        WHERE fixture_id IS NOT NULL
          AND NOT regexp_matches(fixture_id, '^ufo:')""").fetchone()[0]
    n_fx = con.execute(f"""
        SELECT count(*) FROM read_parquet('{src}')
        WHERE fixture_id IS NOT NULL""").fetchone()[0]
    con.close()
    print(f"  fixture_id is populated on {n_fx:,} rows; {n_named:,} of them "
          f"are anything other than an opaque `ufo:…` token.")
    print("  There is NO human-readable fixture name anywhere in "
          "top_casino_users_event_logs.csv - the")
    print("  columns are fixture_id / selection_id (opaque ufo: ids) and "
          "sport_name (Croatian). The")
    print("  readable names (`Lecce - AS Roma`) live in SB_Player.csv / "
          "SB_MOM.csv, which carry no fixture")
    print("  id and no session, so there is no key to join on (DATA_MAP §3). "
          "Printed above is what is there.")

    # What sport labels the UI will actually have to render on this platform.
    con = duckdb.connect()
    mix = con.execute(f"""
        SELECT coalesce(sport_name, '(null)') AS sport, count(*) AS adds,
               count(DISTINCT sid) AS sessions
        FROM read_parquet('{src}')
        WHERE event_name = 'betslip_add_bet' AND platform = ?
        GROUP BY 1 ORDER BY adds DESC""", [CONFIG["platform"]]).fetchall()
    multi = con.execute(f"""
        SELECT count(*) FROM (
          SELECT PlayerID, sid, count(DISTINCT sport_name) k
          FROM read_parquet('{src}')
          WHERE event_name = 'betslip_add_bet' AND platform = ?
          GROUP BY 1, 2) WHERE k > 1""", [CONFIG["platform"]]).fetchone()[0]
    n_slip_sess = con.execute(f"""
        SELECT count(*) FROM (
          SELECT PlayerID, sid FROM read_parquet('{src}')
          WHERE event_name = 'betslip_add_bet' AND platform = ?
          GROUP BY 1, 2)""", [CONFIG["platform"]]).fetchone()[0]
    con.close()
    total_adds = sum(r[1] for r in mix)
    print(f"\n  sport_name on every {CONFIG['platform']} betslip_add_bet "
          f"({total_adds:,} adds across {n_slip_sess:,} sessions) - these are "
          f"the only sport labels\n  the UI can show, and they are Croatian:")
    print(f"    {'sport_name':<24}{'adds':>9}{'sessions':>10}{'share':>8}")
    print("    " + "-" * 51)
    for sport, adds, sess in mix[:10]:
        print(f"    {sport:<24}{adds:>9,}{sess:>10,}"
              f"{100.0*adds/total_adds:>7.1f}%")
    if len(mix) > 10:
        print(f"    … {len(mix)-10} more, {sum(r[1] for r in mix[10:]):,} adds")
    print(f"\n  Only {multi:,} of {n_slip_sess:,} {CONFIG['platform']} "
          f"slip-building sessions mix more than one sport, so a "
          f"single-sport\n  football slip - which is what both demo sessions "
          f"are - is the normal case here, not a narrow pick.")

    # ── write the config ─────────────────────────────────────────────
    out = CONFIG["out"]
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": "Demo sessions for the UI. Written by "
                    "scripts/pick_demo_sessions.py - do not hand-edit. Both "
                    "sessions are owned by TEST-side players from the "
                    "player-level holdout in scripts/holdout_eval.py, so "
                    "neither was used to shape the thresholds in "
                    "src/trigger.py.",
        "_display_note": "`player_id` is the pseudonymous SHA-256 key needed "
                         "to look the session up in data/clean/events.parquet. "
                         "Render `player_id_masked`, never `player_id`.",
        "provenance": {
            "split": "player-level, from scripts/holdout_eval.py",
            "seed": HCONFIG["seed"],
            "train_fraction": HCONFIG["train_fraction"],
            "n_train_players": len(train),
            "n_test_players": len(test),
            "side": "test",
            "source": str(CONFIG["source"].relative_to(REPO_ROOT)),
            "trigger_constants": {
                "MIN_ADDS_TO_ARM": trigger_mod.MIN_ADDS_TO_ARM,
                "MIN_EVENTS_TO_ARM": trigger_mod.MIN_EVENTS_TO_ARM,
                "MIN_ELAPSED_S_TO_ARM": trigger_mod.MIN_ELAPSED_S_TO_ARM,
                "STALL_DWELL_S": trigger_mod.STALL_DWELL_S,
                "STALL_SINCE_ADD_S": trigger_mod.STALL_SINCE_ADD_S,
                "VELOCITY_COLLAPSE_RATIO": trigger_mod.VELOCITY_COLLAPSE_RATIO,
            },
            "platform_test_side": {
                "platform": CONFIG["platform"],
                "slip_building_sessions": len(rows),
                "abandoned": len(ab), "abandoned_fired": len(ab_f),
                "converted": len(cv), "converted_fired": len(cv_f),
                "median_lead_s_fired_abandoners": round(med_ab, 1)
                if med_ab is not None else None,
            },
            "all_platforms_test_side": {
                "slip_building_sessions": len(all_rows),
                "median_lead_s_fired_abandoners": round(med_all, 1)
                if med_all is not None else None,
            },
        },
        "fixture_names": {
            "available": False,
            "note": "The event log has no human-readable fixture name. "
                    "`fixture_id`/`selection_id` are opaque `ufo:` tokens and "
                    "`sport_name` is Croatian. Readable fixture names exist "
                    "only in SB_Player.csv / SB_MOM.csv, which share no key "
                    "with the event log (DATA_MAP §3). The UI must render the "
                    "ids and sport names below, or a label supplied from "
                    "outside this data.",
        },
        "sessions": {},
    }
    for tag, r in (("A", A), ("B", B)):
        c = slip_contents(source, r["key"])
        payload["sessions"][tag] = {
            "role": "fire" if tag == "A" else "silent",
            "label": ("FIRE - slip built, session stalled, never submitted"
                      if tag == "A" else
                      "SILENT - slip built and submitted; the trigger "
                      "correctly stayed out of the way"),
            "player_id": r["player_id"],
            "player_id_masked": mask(r["player_id"]),
            "sid": int(r["sid"]),
            "platform": r["platform"],
            "n_events": r["n_events"],
            "duration_s": round(r["duration_s"], 1),
            "session_start_utc": str(c["window"][0]),
            "session_end_utc": str(c["window"][1]),
            "selections_added": r["selections"],
            "slips_submitted": r["placed"],
            "placed_legs": r["placed_legs"],
            "converted": r["converted"],
            "fired": r["fired"],
            "fired_at_elapsed_s": round(r["fired_at_elapsed_s"], 1)
            if r["fired_at_elapsed_s"] is not None else None,
            "lead_s": round(r["lead_s"], 1) if r["lead_s"] is not None else None,
            "selections": [
                {"time_utc": t, "sport_name": sport, "fixture_id": fx,
                 "selection_id": sel, "added_from": af}
                for t, sport, fx, sel, af, bt in c["adds"]
            ],
            "submitted_slips": [
                {"time_utc": t, "betslip_number": num, "betslip_type": bt,
                 "status": st} for t, num, bt, st in c["placed"]
            ],
        }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"\nOK  wrote {out.relative_to(REPO_ROOT)}  "
          f"(A={mask(A['player_id'])}/{A['sid']}, "
          f"B={mask(B['player_id'])}/{B['sid']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

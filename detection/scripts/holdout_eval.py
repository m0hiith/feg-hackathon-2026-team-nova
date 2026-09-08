#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge: Session Quality & Session-to-Action Conversion
Step 5: out-of-sample evaluation of the slip-rescue trigger.

WHY SPLIT BY PLAYER, NOT BY SESSION
-----------------------------------
There are 89 players and 13,286 sessions - roughly 149 sessions per player
(BASELINE §3). A session-level split puts the same person on both sides of the
line. The trigger keys on per-session pace and dwell, and a heavy user's
sessions look like each other, so a session split would be scoring the model on
people it had already been tuned against. That inflates precision and hides
exactly the failure we care about: a player the rules have never seen.

So the split is on PlayerID. Every session a player owns lands on one side.

    python3 scripts/holdout_eval.py

NOTHING IS TUNED HERE. src/trigger.py is imported and run unmodified; its
thresholds were fixed before this script existed and are not touched by it. The
seed and the train fraction are declared below, up front, and were not chosen
by looking at a test score.
"""
from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from features import FeatureAccumulator, InMemoryStateBackend  # noqa: E402
from replay import CONFIG as RCONFIG, stream_sessions          # noqa: E402
from trigger import SlipRescueTrigger, _labels                 # noqa: E402
import trigger as trigger_mod                                  # noqa: E402

CONFIG = {
    "seed": 42,            # fixed up front, never re-rolled to improve a score
    "train_fraction": 0.70,
    "source": RCONFIG["source"],
}


# ─────────────────────────── the split ───────────────────────────
def split_players(players: list[str], seed: int,
                  train_fraction: float) -> tuple[list[str], list[str]]:
    """Deterministic player-level split.

    Sorted first so the result depends only on the seed, never on the order
    DuckDB happened to return rows in.
    """
    ordered = sorted(players)
    rng = random.Random(seed)
    rng.shuffle(ordered)
    cut = round(len(ordered) * train_fraction)
    train, test = sorted(ordered[:cut]), sorted(ordered[cut:])
    assert not (set(train) & set(test)), "player appears on both sides"
    assert len(train) + len(test) == len(ordered), "players lost in the split"
    return train, test


def cohort_shape(source: Path, players: list[str]) -> dict[str, Any]:
    """Volume on one side of the split, so 'is the test set tiny?' is answerable."""
    import duckdb
    con = duckdb.connect()
    try:
        plist = ", ".join("'" + p.replace("'", "''") + "'" for p in players)
        row = con.execute(f"""
            WITH s AS (
              SELECT PlayerID, sid,
                     max(CASE WHEN event_name='betslip_add_bet' THEN 1 ELSE 0 END) built,
                     max(CASE WHEN event_name='betslip_placed'  THEN 1 ELSE 0 END) conv
              FROM read_parquet('{str(source).replace("'", "''")}')
              WHERE PlayerID IN ({plist})
              GROUP BY 1, 2)
            SELECT count(DISTINCT PlayerID), count(*), sum(built),
                   sum(CASE WHEN built=1 AND conv=1 THEN 1 ELSE 0 END),
                   sum(CASE WHEN built=1 AND conv=0 THEN 1 ELSE 0 END)
            FROM s""").fetchone()
    finally:
        con.close()
    return {"players": len(players), "players_with_events": row[0],
            "sessions": int(row[1]), "slip_building": int(row[2] or 0),
            "converted": int(row[3] or 0), "abandoned": int(row[4] or 0)}


# ─────────────────────────── the run ───────────────────────────
def run_cohort(source: Path, truth: dict, players: set[str]) -> list[dict]:
    """Replay every slip-building session owned by these players.

    Identical to the loop in trigger.py's own evaluation - same accumulator,
    same trigger, same constants. The only difference is which keys are fed in.
    """
    keys = sorted(k for k in truth if k[0] in players)
    results = []
    for key, consumer in stream_sessions(keys, source=source):
        acc = FeatureAccumulator(InMemoryStateBackend())
        trig = SlipRescueTrigger(InMemoryStateBackend())
        fired_at_ms = None
        for record in consumer:
            decision = trig.evaluate(acc.update(record.value))
            if decision.fired and fired_at_ms is None:
                fired_at_ms = record.timestamp
        t = truth[key]
        results.append({
            "player_id": key[0], "platform": t["platform"],
            "converted": t["converted"], "fired": fired_at_ms is not None,
            "lead_s": (t["last_ms"] - fired_at_ms) / 1000.0
            if fired_at_ms is not None else None,
        })
    return results


def _median(xs: list[float]) -> float | None:
    s = sorted(x for x in xs if x is not None)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def metrics(rows: list[dict]) -> dict[str, Any]:
    ab = [r for r in rows if not r["converted"]]
    cv = [r for r in rows if r["converted"]]
    ab_f = [r for r in ab if r["fired"]]
    cv_f = [r for r in cv if r["fired"]]
    n_fired = len(ab_f) + len(cv_f)
    base = 100.0 * len(ab) / len(rows) if rows else 0.0
    prec = 100.0 * len(ab_f) / n_fired if n_fired else 0.0
    return {
        "sessions": len(rows), "abandoned": len(ab), "converted": len(cv),
        "fired_ab": len(ab_f), "fired_cv": len(cv_f),
        "recall": 100.0 * len(ab_f) / len(ab) if ab else None,
        "fp_rate": 100.0 * len(cv_f) / len(cv) if cv else None,
        "precision": prec if n_fired else None,
        "base_rate": base,
        "lift": prec / base if base and n_fired else None,
        "lead_ab": _median([r["lead_s"] for r in ab_f]),
        "lead_cv": _median([r["lead_s"] for r in cv_f]),
    }


HEAD = (f"  {'platform':<15}{'sess':>7}{'aband':>7}{'conv':>7}"
        f"{'recall':>9}{'FP rate':>9}{'prec':>8}{'base':>8}{'lift':>7}"
        f"{'lead(ab)':>10}{'lead(cv)':>10}")
RULE = "  " + "-" * (len(HEAD) - 2)


def _pct(v: float | None) -> str:
    return f"{v:.1f}%" if v is not None else "-"


def print_table(title: str, rows: list[dict]) -> dict[str, Any]:
    print(f"\n{title}")
    print(HEAD)
    print(RULE)

    def line(label: str, sub: list[dict]) -> dict[str, Any]:
        m = metrics(sub)
        print(f"  {label:<15}{m['sessions']:>7,}{m['abandoned']:>7,}"
              f"{m['converted']:>7,}{_pct(m['recall']):>9}{_pct(m['fp_rate']):>9}"
              f"{_pct(m['precision']):>8}{_pct(m['base_rate']):>8}"
              f"{(f'{m['lift']:.2f}x' if m['lift'] else '-'):>7}"
              f"{(f'{m['lead_ab']:,.0f}s' if m['lead_ab'] is not None else '-'):>10}"
              f"{(f'{m['lead_cv']:,.0f}s' if m['lead_cv'] is not None else '-'):>10}")
        return m

    for p in sorted({r["platform"] for r in rows}):
        line(p, [r for r in rows if r["platform"] == p])
    print(RULE)
    return line("ALL", rows)


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

    train, test = split_players(players, CONFIG["seed"], CONFIG["train_fraction"])
    print(f"PLAYER-LEVEL HOLDOUT  seed={CONFIG['seed']}  "
          f"train_fraction={CONFIG['train_fraction']:.0%}")
    print(f"  {len(players)} players split {len(train)}/{len(test)}. "
          f"Every session a player owns stays on one side, so no player is "
          f"scored\n  against rules that were shaped by their own behaviour.")
    print(f"\n  Trigger constants (unmodified, from src/trigger.py): "
          f"MIN_ADDS_TO_ARM={trigger_mod.MIN_ADDS_TO_ARM}, "
          f"STALL_DWELL_S={trigger_mod.STALL_DWELL_S:.0f}, "
          f"STALL_SINCE_ADD_S={trigger_mod.STALL_SINCE_ADD_S:.0f}, "
          f"VELOCITY_COLLAPSE_RATIO={trigger_mod.VELOCITY_COLLAPSE_RATIO}")

    shapes = {"TRAIN": cohort_shape(source, train),
              "TEST": cohort_shape(source, test)}
    print(f"\nCOHORT SIZE")
    print(f"  {'side':<8}{'players':>9}{'sessions':>10}{'slip-building':>15}"
          f"{'converted':>11}{'abandoned':>11}")
    print(f"  {'-'*8}{'-'*9:>9}{'-'*10:>10}{'-'*15:>15}{'-'*11:>11}{'-'*11:>11}")
    for side, s in shapes.items():
        print(f"  {side:<8}{s['players']:>9,}{s['sessions']:>10,}"
              f"{s['slip_building']:>15,}{s['converted']:>11,}"
              f"{s['abandoned']:>11,}")
    tot = {k: shapes['TRAIN'][k] + shapes['TEST'][k]
           for k in ('players', 'sessions', 'slip_building', 'converted', 'abandoned')}
    print(f"  {'TOTAL':<8}{tot['players']:>9,}{tot['sessions']:>10,}"
          f"{tot['slip_building']:>15,}{tot['converted']:>11,}"
          f"{tot['abandoned']:>11,}")

    truth = _labels(source)
    train_rows = run_cohort(source, truth, set(train))
    test_rows = run_cohort(source, truth, set(test))
    assert not ({r["player_id"] for r in train_rows}
                & {r["player_id"] for r in test_rows}), "player leaked across sides"

    m_train = print_table("TRAIN (in-sample - the sessions the thresholds were "
                          "chosen on)", train_rows)
    m_test = print_table("TEST (out-of-sample - players the rules have never "
                         "seen)", test_rows)

    print("\nIN-SAMPLE vs OUT-OF-SAMPLE GAP")
    print(f"  {'metric':<24}{'train':>10}{'test':>10}{'gap':>10}")
    print(f"  {'-'*24}{'-'*10:>10}{'-'*10:>10}{'-'*10:>10}")
    for label, key, unit in [("recall on abandoners", "recall", "%"),
                             ("false-positive rate", "fp_rate", "%"),
                             ("precision", "precision", "%"),
                             ("base rate (abandon)", "base_rate", "%"),
                             ("lift over base rate", "lift", "x"),
                             ("median lead, abandoners", "lead_ab", "s"),
                             ("median lead, converters", "lead_cv", "s")]:
        a, b = m_train[key], m_test[key]
        if a is None or b is None:
            print(f"  {label:<24}{'-':>10}{'-':>10}{'-':>10}")
            continue
        fmt = (lambda v: f"{v:.2f}x") if unit == "x" else (
            (lambda v: f"{v:,.0f}s") if unit == "s" else (lambda v: f"{v:.1f}%"))
        gap = b - a
        sign = "+" if gap >= 0 else ""
        print(f"  {label:<24}{fmt(a):>10}{fmt(b):>10}"
              f"{sign + fmt(gap).lstrip('+'):>10}")

    print("\n  Positive gap = the test side scored higher. Nothing in this "
          "script\n  reads a test metric back into a threshold; the constants "
          "above are\n  imported from src/trigger.py and were fixed before "
          "this file existed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge: Session Quality & Session-to-Action Conversion
Step 5: build the demo UI at src/ui/index.html.

    python3 scripts/build_ui.py

WHY A GENERATOR AND NOT A FETCH()
---------------------------------
The UI is ONE self-contained HTML file with no build step for whoever opens it,
no CDN and no framework. A browser opened on file:// cannot read a parquet file
and cannot even fetch() a sibling JSON, so the replay payload is inlined here.

The payload is not a hand-written fixture. Every number in it is produced by
running the REAL pipeline - src/features.py FeatureAccumulator and
src/trigger.py SlipRescueTrigger - over the two sessions, one event at a time,
in timestamp order. The browser only steps through what those two modules
already decided; it never re-implements a feature or a rule. That is the whole
point: a JS re-implementation could drift from the pipeline and nobody
would notice on stage.

SAFETY
------
Reads events ONLY from data/clean/events.parquet. The 64-char PlayerID is used
to select rows and is then dropped: it never reaches the payload. Betslip
numbers are masked. No usernames, no tokens, no raw ids are emitted.

DATA HONESTY
------------
The old->new price in the FIRE panel is ILLUSTRATIVE and is labelled as such in
the UI. EPS_Offers.csv covers a single day (2026-08-16), is a price ticker -
one row = one quotation valid for an interval - and shares no key with the
event log (DATA_MAP §2, §3). No quote in it can be attributed to a selection in
this session, and nothing anywhere says a user ever saw a price. The two values
below are real quotes from that file, carried here as a documented constant so
that the shipped pipeline still reads events only from the clean parquet.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from features import FeatureAccumulator, InMemoryStateBackend      # noqa: E402
from trigger import (                                              # noqa: E402
    Action, SlipRescueTrigger,
    MIN_ADDS_TO_ARM, MIN_EVENTS_TO_ARM, MIN_ELAPSED_S_TO_ARM,
    STALL_DWELL_S, STALL_SINCE_ADD_S,
    VELOCITY_COLLAPSE_RATIO, MIN_PEAK_VELOCITY_EPM,
)

CONFIG_PATH = REPO_ROOT / "config" / "demo_sessions.json"
EVENTS_PATH = REPO_ROOT / "data" / "clean" / "events.parquet"
TEMPLATE_PATH = Path(__file__).resolve().parent / "ui_template.html"
OUT_PATH = REPO_ROOT / "src" / "ui" / "index.html"

# ── the illustrative price pair ──────────────────────────────────────────
# Real rows from EPS_Offers.csv, quoted verbatim, and NOT attributable to the
# session below - which is exactly why the UI labels them illustrative.
ILLUSTRATIVE_PRICE = {
    "old": "2.10",
    "new": "1.85",
    "source": "EPS_Offers.csv",
    "source_row": ("hr | Soccer | Bundesliga | SK Rapid vs. Grazer AK 1902 | "
                   "SK Rapid total corners | 2.1 -> 1.85 | "
                   "2026-08-16T17:24:42.526Z"),
    "why_illustrative": (
        "EPS_Offers covers one day (2026-08-16) and is a price ticker \u2014 one "
        "row is one quotation valid for a time interval \u2014 not an impression "
        "log. It "
        "shares no key with the event log, so no quote can be tied to this "
        "selection, and nothing in any file records that a price was seen."),
}

# Fields the browser is allowed to see. PlayerID is deliberately absent.
DISPLAY_FIELDS = ("event_name", "fortuna_screen_name", "added_from",
                  "sport_name", "fixture_id", "selection_id", "betslip_type",
                  "status", "on_route", "page_location")


def mask_id(value: str, head: int = 8, tail: int = 4) -> str:
    """Keep enough to tell two ids apart, never enough to identify anyone."""
    s = str(value)
    if len(s) <= head + tail:
        return s
    return f"{s[:head]}…{s[-tail:]}"


def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def gate_progress(f: dict[str, Any]) -> dict[str, float]:
    """The three gates of src/trigger.py, each as 0..1 progress to its own
    threshold. DISPLAY ONLY - the trigger does not consult these numbers.

    They are built from the trigger's own constants so the invariant below
    holds: score == 100 exactly when the rule chain would fire.
    """
    if f["placed_count"] > 0:
        armed = 0.0
    else:
        armed = min(clamp01(f["add_bet_count"] / MIN_ADDS_TO_ARM),
                    clamp01(f["n_events"] / MIN_EVENTS_TO_ARM),
                    clamp01(f["elapsed_s"] / MIN_ELAPSED_S_TO_ARM))

    since_add = f.get("since_last_add_s")
    hesitation = max(clamp01(f["dwell_s"] / STALL_DWELL_S),
                     clamp01(since_add / STALL_SINCE_ADD_S)
                     if since_add is not None else 0.0)

    peak = f["peak_velocity_epm"] or 0.0
    if peak < MIN_PEAK_VELOCITY_EPM:
        collapse = 0.0
    else:
        ratio = f["velocity_epm"] / peak
        collapse = clamp01((1.0 - ratio) / (1.0 - VELOCITY_COLLAPSE_RATIO))

    return {"armed": armed, "hesitation": hesitation, "collapse": collapse}


def score_of(gates: dict[str, float]) -> int:
    """Weakest gate wins: the chain is an AND, so the score must be too."""
    return int(round(100 * min(gates.values())))


def build_session(key: str, meta: dict[str, Any], df) -> dict[str, Any]:
    """Replay one session through the real accumulator and trigger."""
    sub = df[(df["PlayerID"] == meta["player_id"]) & (df["sid"] == meta["sid"])]
    # Same order replay.py uses: ts, then file row number (== the parquet
    # order, which is this frame's index) as the reproducible tiebreak.
    sub = sub.sort_values(["ts"], kind="stable")
    if len(sub) != meta["n_events"]:
        raise SystemExit(f"session {key}: {len(sub)} rows, config says "
                         f"{meta['n_events']} - parquet and config disagree")

    acc = FeatureAccumulator(InMemoryStateBackend())
    trig = SlipRescueTrigger(InMemoryStateBackend())

    steps: list[dict[str, Any]] = []
    fired_at: float | None = None
    for _, row in sub.iterrows():
        event = {k: (None if v != v else v) for k, v in row.items()
                 if k != "ts"}                       # NaN -> None, drop ts
        event["timestamp"] = str(row["timestamp"])
        event["sid"] = int(row["sid"])

        f = acc.update(event)
        d = trig.evaluate(f)
        gates = gate_progress(f)
        score = score_of(gates)

        # The invariant that makes the score honest rather than decorative:
        # score == 100 exactly when the rule chain would fire on this snapshot.
        # Checked against a latch-free trigger, because the real one
        # short-circuits on ALREADY_FIRED before it reaches the gates.
        shadow = SlipRescueTrigger(InMemoryStateBackend()).evaluate(f)
        if (score == 100) != (shadow.action is Action.FIRE):
            raise SystemExit(
                f"session {key} @{f['elapsed_s']}s: score {score} disagrees "
                f"with the rule chain ({shadow.action.value}/"
                f"{shadow.reason.value})")

        if d.action is Action.FIRE:
            fired_at = f["elapsed_s"]

        steps.append({
            # ── what happened (display fields only; no PlayerID) ──
            "t": str(row["timestamp"]),
            "clock": str(row["ts"])[11:19],
            "event": event["event_name"],
            "screen": f["screen"],
            "added_from": event.get("added_from"),
            "sport": event.get("sport_name"),
            "fixture": event.get("fixture_id"),
            "selection": event.get("selection_id"),
            "slip": (mask_id(event["betslip_number"], 4, 3)
                     if event.get("betslip_number") else None),
            "slip_type": event.get("betslip_type"),
            "status": event.get("status"),
            # ── what the accumulator made of it ──
            "f": {
                "elapsed_s": f["elapsed_s"],
                "dwell_s": f["dwell_s"],
                "max_dwell_s": f["max_dwell_s"],
                "n_events": f["n_events"],
                "velocity_epm": f["velocity_epm"],
                "peak_velocity_epm": f["peak_velocity_epm"],
                "distinct_screens": f["distinct_screens"],
                "repeat_screen_events": f["repeat_screen_events"],
                "screen_revisit_ratio": f["screen_revisit_ratio"],
                "screen_coverage": f["screen_coverage"],
                "add_bet_count": f["add_bet_count"],
                "placed_count": f["placed_count"],
                "placed_leg_count": f["placed_leg_count"],
                "since_last_add_s": f["since_last_add_s"],
            },
            # ── what the trigger decided ──
            "d": {
                "action": d.action.value,
                "reason": d.reason.value,
                "velocity_ratio": d.evidence["velocity_ratio"],
                "back_and_forth": d.evidence["back_and_forth"],
                "screen_signal_trusted": d.evidence["screen_signal_trusted"],
            },
            "gates": {k: round(v, 4) for k, v in gates.items()},
            "score": score,
        })

    if bool(fired_at is not None) != bool(meta["fired"]):
        raise SystemExit(f"session {key}: fired={fired_at is not None} but "
                         f"config says fired={meta['fired']}")

    return {
        "key": key,
        "role": meta["role"],
        "label": meta["label"],
        "player_masked": meta["player_id_masked"],
        "sid_masked": mask_id(str(meta["sid"]), 4, 3),
        "platform": meta["platform"],
        "date_utc": meta["session_start_utc"][:10],
        "duration_s": meta["duration_s"],
        "n_events": meta["n_events"],
        "selections_added": meta["selections_added"],
        "slips_submitted": meta["slips_submitted"],
        "converted": meta["converted"],
        "fired": meta["fired"],
        "fired_at_elapsed_s": fired_at,
        "lead_s": meta["lead_s"],
        "steps": steps,
    }


def main() -> int:
    try:
        import pandas as pd
    except ModuleNotFoundError:
        print("pandas is required to rebuild the UI: pip install pandas pyarrow")
        return 1

    cfg = json.loads(CONFIG_PATH.read_text())
    df = pd.read_parquet(EVENTS_PATH)

    payload = {
        "generated_from": {
            "events": "data/clean/events.parquet",
            "config": "config/demo_sessions.json",
            "pipeline": "src/features.py -> src/trigger.py (run at build time)",
        },
        "thresholds": {
            "MIN_ADDS_TO_ARM": MIN_ADDS_TO_ARM,
            "MIN_EVENTS_TO_ARM": MIN_EVENTS_TO_ARM,
            "MIN_ELAPSED_S_TO_ARM": MIN_ELAPSED_S_TO_ARM,
            "STALL_DWELL_S": STALL_DWELL_S,
            "STALL_SINCE_ADD_S": STALL_SINCE_ADD_S,
            "VELOCITY_COLLAPSE_RATIO": VELOCITY_COLLAPSE_RATIO,
            "MIN_PEAK_VELOCITY_EPM": MIN_PEAK_VELOCITY_EPM,
        },
        "illustrative_price": ILLUSTRATIVE_PRICE,
        "fixture_names_available": cfg["fixture_names"]["available"],
        "sessions": {k: build_session(k, m, df)
                     for k, m in cfg["sessions"].items()},
    }

    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    # Refuse to ship a payload that leaked an identifier.
    for k, m in cfg["sessions"].items():
        if m["player_id"] in blob:
            raise SystemExit(f"session {k}: raw PlayerID leaked into payload")
    for slip in ("HPC0TR0852VAP800",):
        if slip in blob:
            raise SystemExit("unmasked betslip number leaked into payload")

    template = TEMPLATE_PATH.read_text()
    marker = "/*__DEMO_DATA__*/null"
    if marker not in template:
        raise SystemExit(f"{TEMPLATE_PATH} has no {marker} marker")
    html = template.replace(marker, blob)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html)

    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}  "
          f"({len(html)/1024:.0f} KB, self-contained)")
    for k, s in payload["sessions"].items():
        fired = (f"FIRE at {s['fired_at_elapsed_s']}s"
                 if s["fired"] else "SILENT throughout")
        print(f"  session {k}: {s['n_events']} events, {s['duration_s']}s, "
              f"{s['selections_added']} selections -> {fired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

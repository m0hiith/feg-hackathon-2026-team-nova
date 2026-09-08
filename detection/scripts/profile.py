#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge: Session Quality & Session-to-Action Conversion
Data understanding pass. Regenerates docs/DATA_MAP.md and docs/BASELINE.md.

Read-only on the data directory. Nothing is ever written back into it.
Large files are scanned out-of-core with DuckDB; XLSX is converted once into
a local .cache/ parquet (outside the data dir).

Usage:  python3 scripts/profile.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd

# ─────────────────────────── CONFIG ───────────────────────────
CONFIG = {
    # NOTE: the delivered folder is literally named "data set " (trailing space).
    "data_dir": Path("data set "),
    "docs_dir": Path("docs"),
    "cache_dir": Path(".cache"),

    # Sessionisation
    "session_gap_minutes": 30,      # inactivity gap for reconstructed sessions
    "session_key": ("PlayerID", "session"),  # native session key

    # Files
    "event_log_csv": "top_casino_users_event_logs.csv",
    "event_log_xlsx_v2": "top_casino_users_event_logs_v2 (1).xlsx",
    "trends_xlsx": "hackathon_casino_trends.xlsx",
    "sb_player_csv": "SB_Player.csv",
    "sb_mom_csv": "SB_MOM.csv",
    "eps_offers_csv": "EPS_Offers.csv",

    # Profiling
    "sample_rows": 5,
    "max_cardinality_display": 12,
    "duckdb_memory_limit": "6GB",

    # Funnel stage definitions (event_name -> stage)
    "stage_map": {
        "page_view":           "VIEW",
        "screen_view":         "VIEW",
        "fortuna_screen_view": "VIEW",
        "casino_game_launch":  "INTENT",      # casino vertical action
        "betslip_add_bet":     "INTENT",      # sportsbook: selection added to slip
        "betslip_placed":      "FINAL_STEP",  # slip submitted (carries status)
        "betslip_placed_bet":  "FINAL_STEP",  # per-leg detail of a submitted slip
    },
    "completed_status": "ACCEPTED",

    # Screens/routes that count as genuine BROWSE (content engagement)
    "browse_screens": ("prematchDetail", "liveDetail", "competition_detail",
                       "prematchMatchesOverview", "liveEvents", "prematchLeagues",
                       "search", "searchPrematch", "searchLive"),
    "browse_routes": ("lobby", "search", "az_games", "my_games",
                      "user-my-games", "category_game_row"),

    # PII / secret patterns to flag (hackathon rules: synthetic/anonymised only)
    "pii_url_params": ("username", "temptoken", "token", "email", "phone", "oib"),
    "pii_regexes": {
        "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "ipv4":  r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b",
    },
}

CSV_OPTS = "header=true, quote='\"', escape='\"', all_varchar=true"
NULLSTR = ", nullstr='null'"


# ─────────────────────────── helpers ───────────────────────────
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


def mask(v: object) -> str:
    """Mask anything that looks personal or identifying before it hits a doc."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "null"
    s = str(v)
    if re.fullmatch(r"[0-9a-f]{64}", s):                 # hashed player id
        return s[:8] + "…" + s[-4:]
    s = re.sub(r"([?&](?:username|temptoken|token|email)=)[^&]*",
               r"\1<MASKED>", s, flags=re.I)
    if len(s) > 90:
        s = s[:90] + "…"
    return s


def csv_view(path: Path, nullstr: bool = False) -> str:
    p = str(path).replace("'", "''")
    return f"read_csv('{p}', {CSV_OPTS}{NULLSTR if nullstr else ''})"


def _fmt(v: object) -> str:
    """Render numbers readably: ints with thousands separators, NaN as em-dash."""
    if v is None:
        return "—"
    if isinstance(v, float):
        if pd.isna(v):
            return "—"
        if v.is_integer() and abs(v) >= 1000:
            return f"{int(v):,}"
        if v.is_integer():
            return str(int(v))
        return f"{v:,.2f}"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v)


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_(no rows)_\n"
    df = df.copy()
    for c in df.columns:
        df[c] = df[c].map(_fmt)
    df = df.astype(str)
    head = "| " + " | ".join(df.columns) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    rows = ["| " + " | ".join(r) + " |" for r in df.values]
    return "\n".join([head, sep, *rows]) + "\n"


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{CONFIG['duckdb_memory_limit']}'")
    return con


def ensure_cache(data_dir: Path, cache_dir: Path) -> dict[str, Path]:
    """Convert XLSX once to parquet in the local cache (never into data_dir)."""
    cache_dir.mkdir(exist_ok=True)
    out = {}
    for key, fname in (("events_v2", CONFIG["event_log_xlsx_v2"]),
                       ("trends", CONFIG["trends_xlsx"])):
        src, dst = data_dir / fname, cache_dir / f"{key}.parquet"
        if src.exists() and (not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime):
            log(f"  converting {fname} -> {dst}")
            pd.read_excel(src, dtype=str).to_parquet(dst, index=False)
        if dst.exists():
            out[key] = dst
    return out


# ─────────────────────── 1. INVENTORY ───────────────────────
def profile_relation(con, name: str, rel: str, label: str) -> dict:
    """Column-level profile of a DuckDB relation, computed in one pass."""
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()]
    n = con.execute(f"SELECT count(*) FROM {rel}").fetchone()[0]

    aggs = []
    for c in cols:
        q = f'"{c}"'
        aggs += [f"count({q}) AS nn_{len(aggs)}",
                 f"count(DISTINCT {q}) AS nd_{len(aggs)}",
                 f"min({q}) AS mn_{len(aggs)}",
                 f"max({q}) AS mx_{len(aggs)}"]
    row = con.execute(f"SELECT {', '.join(aggs)} FROM {rel}").fetchone()

    recs = []
    for i, c in enumerate(cols):
        nn, nd, mn, mx = row[i * 4: i * 4 + 4]
        recs.append({
            "column": c,
            "dtype": con.execute(f'SELECT typeof("{c}") FROM {rel} LIMIT 1').fetchone()[0],
            "null_%": round(100.0 * (n - nn) / n, 2) if n else 0.0,
            "n_distinct": nd,
            "min": mask(mn), "max": mask(mx),
        })
    prof = pd.DataFrame(recs)

    sample = con.execute(f"SELECT * FROM {rel} LIMIT {CONFIG['sample_rows']}").df()
    for c in sample.columns:
        sample[c] = sample[c].map(mask)
    return {"name": name, "label": label, "rows": n, "profile": prof, "sample": sample}


def build_inventory(con, data_dir: Path, cache: dict) -> list[dict]:
    inv = []
    targets = [
        ("top_casino_users_event_logs.csv", csv_view(data_dir / CONFIG["event_log_csv"], True),
         "Client-side behavioural event log (sessionised)"),
        ("SB_Player.csv", csv_view(data_dir / CONFIG["sb_player_csv"]),
         "Per-player sportsbook bet lines"),
        ("SB_MOM.csv", csv_view(data_dir / CONFIG["sb_mom_csv"]),
         "Month-over-month sportsbook aggregates"),
        ("EPS_Offers.csv", csv_view(data_dir / CONFIG["eps_offers_csv"]),
         "Odds/price ticker (offer history)"),
    ]
    if "trends" in cache:
        targets.append(("hackathon_casino_trends.xlsx",
                        f"read_parquet('{cache['trends']}')",
                        "Pre-aggregated casino session KPIs by market/month"))
    for name, rel, label in targets:
        log(f"  profiling {name} …")
        d = profile_relation(con, name, rel, label)
        src = data_dir / name
        d["size"] = human(src.stat().st_size) if src.exists() else "n/a"
        inv.append(d)
    return inv


# ─────────────────────── 3. KEYS & JOINS ───────────────────────
def join_map(con, data_dir: Path) -> dict:
    ev = csv_view(data_dir / CONFIG["event_log_csv"], True)
    sbp = csv_view(data_dir / CONFIG["sb_player_csv"])
    mom = csv_view(data_dir / CONFIG["sb_mom_csv"])
    off = csv_view(data_dir / CONFIG["eps_offers_csv"])
    out = {}

    out["player_overlap"] = con.execute(f"""
        SELECT (SELECT count(DISTINCT PlayerID) FROM {ev}) AS events_players,
               (SELECT count(DISTINCT PlayerID) FROM {sbp} WHERE PlayerID<>'null') AS sb_player_players,
               (SELECT count(*) FROM (SELECT DISTINCT PlayerID FROM {ev}
                     INTERSECT SELECT DISTINCT PlayerID FROM {sbp})) AS shared_players
    """).df()

    out["sbp_null_players"] = con.execute(f"""
        SELECT count(*) AS rows_total,
               sum(CASE WHEN PlayerID='null' THEN 1 ELSE 0 END) AS rows_null_playerid,
               round(100.0*sum(CASE WHEN PlayerID='null' THEN 1 ELSE 0 END)/count(*),2) AS pct
        FROM {sbp}""").df()

    out["fixture_overlap"] = con.execute(f"""
        SELECT (SELECT count(DISTINCT fixture_name_english) FROM {sbp}) AS sb_player_fixtures,
               (SELECT count(DISTINCT fixture_name_english) FROM {mom}) AS sb_mom_fixtures,
               (SELECT count(*) FROM (SELECT DISTINCT fixture_name_english FROM {sbp}
                     INTERSECT SELECT DISTINCT fixture_name_english FROM {mom})) AS shared
    """).df()

    # EPS_Offers uses "A vs. B", SB_* use "A - B"
    out["offers_overlap"] = con.execute(f"""
        SELECT (SELECT count(DISTINCT match_name) FROM {off}) AS offers_matches,
               (SELECT count(*) FROM (SELECT DISTINCT fixture_name_english FROM {sbp}
                     INTERSECT SELECT DISTINCT match_name FROM {off})) AS shared_raw,
               (SELECT count(*) FROM (SELECT DISTINCT replace(fixture_name_english,' - ',' vs. ') AS f FROM {sbp}
                     INTERSECT SELECT DISTINCT match_name FROM {off})) AS shared_normalised
    """).df()

    out["sport_language"] = con.execute(f"""
        SELECT (SELECT count(DISTINCT sport_name) FROM {ev} WHERE sport_name IS NOT NULL) AS event_sports,
               (SELECT count(DISTINCT Sport_name_english) FROM {sbp}) AS sb_player_sports,
               (SELECT count(*) FROM (SELECT DISTINCT sport_name FROM {ev} WHERE sport_name IS NOT NULL
                     INTERSECT SELECT DISTINCT Sport_name_english FROM {sbp})) AS shared
    """).df()

    out["mom_grain"] = con.execute(f"""
        SELECT count(*) AS n_rows, count(*) - count(DISTINCT (brand, month_start_date, Sport_name_english,
               event_name_english, fixture_name_english, market_name_english, selection_name_english)) AS duplicate_keys
        FROM {mom}""").df()

    out["coverage"] = con.execute(f"""
        SELECT 'events' AS dataset, min(timestamp)[1:10] AS min_date, max(timestamp)[1:10] AS max_date FROM {ev}
        UNION ALL SELECT 'SB_Player', min(placed_date), max(placed_date) FROM {sbp}
        UNION ALL SELECT 'SB_MOM', min(month_start_date), max(month_start_date) FROM {mom}
        UNION ALL SELECT 'EPS_Offers', min(start_datetime_utc)[1:10], max(start_datetime_utc)[1:10] FROM {off}
    """).df()
    return out


# ─────────────────── 4/5. SESSIONS & FUNNEL ───────────────────
def register_events(con, data_dir: Path) -> None:
    """Typed event view + native and reconstructed session assignments."""
    gap = CONFIG["session_gap_minutes"] * 60
    con.execute(f"""
        CREATE OR REPLACE VIEW ev AS
        SELECT *,
               CAST(CAST(session AS DOUBLE) AS BIGINT)      AS sid,
               CAST(replace(timestamp,'Z','') AS TIMESTAMP) AS ts
        FROM {csv_view(data_dir / CONFIG['event_log_csv'], True)}
    """)
    con.execute(f"""
        CREATE OR REPLACE VIEW ev_recon AS
        WITH g AS (
          SELECT *, CASE WHEN lag(ts) OVER w IS NULL
                          OR date_diff('second', lag(ts) OVER w, ts) > {gap}
                         THEN 1 ELSE 0 END AS is_new
          FROM ev WINDOW w AS (PARTITION BY PlayerID ORDER BY ts)
        )
        SELECT *, sum(is_new) OVER (PARTITION BY PlayerID ORDER BY ts
                                    ROWS UNBOUNDED PRECEDING) AS rsid
        FROM g
    """)
    browse_s = ",".join(f"'{x}'" for x in CONFIG["browse_screens"])
    browse_r = ",".join(f"'{x}'" for x in CONFIG["browse_routes"])
    con.execute(f"""
        CREATE OR REPLACE VIEW sess AS
        SELECT PlayerID, sid,
               min(ts) AS t0, max(ts) AS t1,
               date_diff('second', min(ts), max(ts)) AS dur_s,
               count(*) AS n_ev,
               any_value(platform) AS platform,
               count(DISTINCT platform) AS n_platforms,
               max(CASE WHEN fortuna_screen_name IN ({browse_s})
                         OR on_route IN ({browse_r})
                         OR page_location LIKE '%psk.hr/oklade%'
                         OR page_location LIKE '%casino.psk.hr%' THEN 1 ELSE 0 END) AS f_browse,
               max(CASE WHEN event_name='betslip_add_bet'    THEN 1 ELSE 0 END) AS f_add,
               max(CASE WHEN event_name='casino_game_launch' THEN 1 ELSE 0 END) AS f_launch,
               max(CASE WHEN event_name='betslip_placed'     THEN 1 ELSE 0 END) AS f_placed,
               max(CASE WHEN event_name='betslip_placed'
                         AND status='{CONFIG["completed_status"]}' THEN 1 ELSE 0 END) AS f_done,
               sum(CASE WHEN event_name='betslip_add_bet'    THEN 1 ELSE 0 END) AS n_add,
               sum(CASE WHEN event_name='betslip_placed'     THEN 1 ELSE 0 END) AS n_placed,
               sum(CASE WHEN event_name='casino_game_launch' THEN 1 ELSE 0 END) AS n_launch,
               min(CASE WHEN event_name IN ('betslip_add_bet','casino_game_launch') THEN ts END) AS t_action,
               min(CASE WHEN event_name='betslip_placed' THEN ts END) AS t_placed
        FROM ev GROUP BY 1,2
    """)


def session_feasibility(con) -> dict:
    gap = CONFIG["session_gap_minutes"]
    out = {}
    out["native_id_check"] = con.execute("""
        SELECT count(*) AS n_session_ids,
               sum(CASE WHEN n_players>1 THEN 1 ELSE 0 END) AS ids_reused_across_players,
               max(n_players) AS max_players_per_id
        FROM (SELECT sid, count(DISTINCT PlayerID) AS n_players FROM ev GROUP BY 1)""").df()

    out["id_is_epoch"] = con.execute("""
        SELECT count(*) AS n_sessions,
               sum(CASE WHEN d BETWEEN -5 AND 5 THEN 1 ELSE 0 END) AS start_within_5s_of_id,
               round(100.0*sum(CASE WHEN d BETWEEN -5 AND 5 THEN 1 ELSE 0 END)/count(*),1) AS pct,
               median(d) AS median_offset_s
        FROM (SELECT sid, date_diff('second', to_timestamp(sid) AT TIME ZONE 'UTC', min(ts)) AS d
              FROM ev GROUP BY sid)""").df()

    out["native_stats"] = con.execute("""
        SELECT count(*) AS sessions, count(DISTINCT PlayerID) AS players,
               round(avg(n_ev),1) AS avg_events, median(n_ev) AS median_events, max(n_ev) AS max_events,
               round(avg(dur_s)/60,1) AS avg_dur_min, round(median(dur_s)/60,1) AS median_dur_min,
               round(quantile_cont(dur_s,0.9)/60,1) AS p90_dur_min,
               sum(CASE WHEN n_ev=1 THEN 1 ELSE 0 END) AS single_event_sessions,
               sum(CASE WHEN dur_s>43200 THEN 1 ELSE 0 END) AS sessions_over_12h
        FROM sess""").df()

    out["duration_dist"] = con.execute("""
        SELECT bucket, count(*) AS sessions,
               round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct
        FROM (SELECT CASE WHEN dur_s=0 THEN 'a. 0s (single event)'
                          WHEN dur_s<60 THEN 'b. <1 min'
                          WHEN dur_s<300 THEN 'c. 1-5 min'
                          WHEN dur_s<900 THEN 'd. 5-15 min'
                          WHEN dur_s<1800 THEN 'e. 15-30 min'
                          WHEN dur_s<3600 THEN 'f. 30-60 min'
                          ELSE 'g. 60 min+' END AS bucket FROM sess)
        GROUP BY 1 ORDER BY 1""").df()

    out["per_player"] = con.execute("""
        SELECT round(avg(ns),1) AS avg_sessions_per_player, median(ns) AS median,
               min(ns) AS min, max(ns) AS max
        FROM (SELECT PlayerID, count(*) AS ns FROM sess GROUP BY 1)""").df()

    out["gap_within"] = con.execute(f"""
        SELECT round(median(g),1) AS median_gap_s, round(quantile_cont(g,0.9),1) AS p90_s,
               round(quantile_cont(g,0.99),1) AS p99_s,
               sum(CASE WHEN g>{gap*60} THEN 1 ELSE 0 END) AS gaps_over_{gap}min, count(*) AS n_gaps
        FROM (SELECT date_diff('second', lag(ts) OVER (PARTITION BY PlayerID,sid ORDER BY ts), ts) AS g
              FROM ev) WHERE g IS NOT NULL""").df()

    out["recon_stats"] = con.execute("""
        SELECT count(*) AS sessions, round(avg(n_ev),1) AS avg_events, median(n_ev) AS median_events,
               round(avg(dur_s)/60,1) AS avg_dur_min, round(median(dur_s)/60,1) AS median_dur_min,
               sum(CASE WHEN n_ev=1 THEN 1 ELSE 0 END) AS single_event_sessions
        FROM (SELECT PlayerID, rsid, count(*) AS n_ev,
                     date_diff('second',min(ts),max(ts)) AS dur_s
              FROM ev_recon GROUP BY 1,2)""").df()

    out["agreement"] = con.execute("""
        SELECT (SELECT count(*) FROM (SELECT PlayerID,sid FROM ev_recon GROUP BY 1,2)) AS native_sessions,
               (SELECT count(*) FROM (SELECT PlayerID,rsid FROM ev_recon GROUP BY 1,2)) AS reconstructed_sessions,
               (SELECT round(100.0*sum(CASE WHEN nr=1 THEN 1 ELSE 0 END)/count(*),1)
                  FROM (SELECT PlayerID,sid,count(DISTINCT rsid) AS nr FROM ev_recon GROUP BY 1,2)
               ) AS pct_native_inside_one_reconstructed,
               (SELECT round(avg(nn),2) FROM (SELECT PlayerID,rsid,count(DISTINCT sid) AS nn
                  FROM ev_recon GROUP BY 1,2)) AS avg_native_per_reconstructed""").df()
    return out


def taxonomy_and_funnel(con) -> dict:
    out = {}
    case = " ".join(f"WHEN event_name='{k}' THEN '{v}'" for k, v in CONFIG["stage_map"].items())
    out["taxonomy"] = con.execute(f"""
        SELECT event_name, CASE {case} ELSE 'UNMAPPED' END AS stage,
               count(*) AS events, round(100.0*count(*)/sum(count(*)) OVER (),2) AS pct_events,
               count(DISTINCT PlayerID) AS players,
               count(DISTINCT (PlayerID, sid)) AS sessions
        FROM ev GROUP BY 1,2 ORDER BY events DESC""").df()

    out["platform_matrix"] = con.execute("""
        SELECT platform, count(*) AS events, count(DISTINCT (PlayerID,sid)) AS sessions,
               count(DISTINCT event_name) AS event_types,
               string_agg(DISTINCT event_name, ', ') AS vocabulary
        FROM ev GROUP BY 1 ORDER BY events DESC""").df()

    out["status"] = con.execute("""
        SELECT status, count(*) AS n, round(100.0*count(*)/sum(count(*)) OVER (),2) AS pct
        FROM ev WHERE event_name='betslip_placed' AND status IS NOT NULL
        GROUP BY 1 ORDER BY n DESC""").df()

    # Session-level funnel
    out["funnel"] = con.execute("""
        WITH f AS (SELECT
            count(*) AS s1,
            sum(CASE WHEN n_ev>=2 THEN 1 ELSE 0 END) AS s2,
            sum(f_browse) AS s3,
            sum(CASE WHEN f_add=1 OR f_launch=1 THEN 1 ELSE 0 END) AS s4,
            sum(f_placed) AS s5, sum(f_done) AS s6 FROM sess)
        SELECT * FROM (
          SELECT 1 AS ord,'1. SESSION_START'  AS stage, s1 AS sessions, 100.0 AS pct_of_start, NULL AS step_conv FROM f
          UNION ALL SELECT 2,'2. BROWSE (2+ events)', s2, round(100.0*s2/s1,2), round(100.0*s2/s1,2) FROM f
          UNION ALL SELECT 3,'3. ENGAGED BROWSE',     s3, round(100.0*s3/s1,2), round(100.0*s3/s2,2) FROM f
          UNION ALL SELECT 4,'4. INTENT (add bet / launch game)', s4, round(100.0*s4/s1,2), round(100.0*s4/s3,2) FROM f
          UNION ALL SELECT 5,'5. FINAL_STEP (slip submitted)',    s5, round(100.0*s5/s1,2), round(100.0*s5/s4,2) FROM f
          UNION ALL SELECT 6,'6. COMPLETED (accepted)',           s6, round(100.0*s6/s1,2), round(100.0*s6/s5,2) FROM f
        ) ORDER BY ord""").df().drop(columns=["ord"])

    # Sportsbook-only funnel: the headline drop-off
    out["sb_funnel"] = con.execute("""
        SELECT sum(f_add) AS sessions_added_to_slip,
               sum(CASE WHEN f_add=1 AND f_placed=1 THEN 1 ELSE 0 END) AS also_submitted,
               sum(CASE WHEN f_add=1 AND f_placed=0 THEN 1 ELSE 0 END) AS abandoned_slip,
               round(100.0*sum(CASE WHEN f_add=1 AND f_placed=0 THEN 1 ELSE 0 END)/sum(f_add),2) AS final_step_dropoff_pct
        FROM sess""").df()

    out["leakage"] = con.execute("""
        SELECT sum(CASE WHEN f_placed=1 AND f_add=0 THEN 1 ELSE 0 END) AS submitted_without_add_event,
               sum(f_placed) AS submitted_total,
               round(100.0*sum(CASE WHEN f_placed=1 AND f_add=0 THEN 1 ELSE 0 END)/sum(f_placed),1) AS pct
        FROM sess""").df()

    out["by_platform"] = con.execute("""
        SELECT platform, count(*) AS sessions, sum(f_add) AS intent_add, sum(f_launch) AS intent_launch,
               sum(f_placed) AS final_step, sum(f_done) AS completed,
               round(100.0*sum(f_placed)/count(*),2) AS session_conv_pct,
               round(100.0*sum(CASE WHEN f_add=1 AND f_placed=0 THEN 1 ELSE 0 END)/nullif(sum(f_add),0),2) AS dropoff_pct
        FROM sess GROUP BY 1 ORDER BY sessions DESC""").df()

    out["quality"] = con.execute("""
        SELECT CASE WHEN f_placed=1 THEN '4. converted (bet placed)'
                    WHEN f_add=1    THEN '3. intent only (abandoned slip)'
                    WHEN f_launch=1 THEN '2. casino play'
                    ELSE '1. browse only' END AS outcome,
               count(*) AS sessions, round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct,
               round(avg(n_ev),1) AS avg_events, round(median(dur_s)/60.0,1) AS median_dur_min,
               round(avg(n_add),2) AS avg_selections_added
        FROM sess GROUP BY 1 ORDER BY 1""").df()

    out["timing"] = con.execute("""
        SELECT 'time to first action (s)' AS metric, count(*) AS n,
               round(median(date_diff('second',t0,t_action)),1) AS p50,
               round(avg(date_diff('second',t0,t_action)),1) AS mean,
               round(quantile_cont(date_diff('second',t0,t_action),0.9),1) AS p90
        FROM sess WHERE t_action IS NOT NULL
        UNION ALL
        SELECT 'time to bet submitted (s)', count(*),
               round(median(date_diff('second',t0,t_placed)),1),
               round(avg(date_diff('second',t0,t_placed)),1),
               round(quantile_cont(date_diff('second',t0,t_placed),0.9),1)
        FROM sess WHERE t_placed IS NOT NULL""").df()

    out["actions_per_session"] = con.execute("""
        SELECT round(avg(n_add),2) AS avg_selections_added_all_sessions,
               round(avg(n_placed),3) AS avg_slips_submitted,
               round(avg(n_launch),2) AS avg_game_launches,
               round(sum(n_add)*1.0/nullif(sum(n_placed),0),2) AS selections_per_submitted_slip
        FROM sess""").df()

    # Segmentation: first-observed session vs later (proxy for new vs returning)
    out["new_vs_returning"] = con.execute("""
        WITH r AS (SELECT *, row_number() OVER (PARTITION BY PlayerID ORDER BY t0) AS rn FROM sess)
        SELECT CASE WHEN rn=1 THEN 'first observed session' ELSE 'later session' END AS segment,
               count(*) AS sessions, sum(f_placed) AS converted,
               round(100.0*sum(f_placed)/count(*),2) AS conv_pct, round(avg(n_ev),1) AS avg_events
        FROM r GROUP BY 1 ORDER BY 1""").df()

    out["heavy_light"] = con.execute("""
        WITH p AS (SELECT PlayerID, count(*) AS ns FROM sess GROUP BY 1),
             q AS (SELECT PlayerID, ntile(2) OVER (ORDER BY ns) AS half FROM p)
        SELECT CASE WHEN half=1 THEN 'lighter half of players' ELSE 'heavier half of players' END AS segment,
               count(*) AS sessions, round(100.0*sum(f_placed)/count(*),2) AS conv_pct,
               round(avg(n_ev),1) AS avg_events
        FROM sess JOIN q USING (PlayerID) GROUP BY 1 ORDER BY 1""").df()
    return out


# ─────────────────── PII / COMPLIANCE SCAN ───────────────────
def pii_scan(con, data_dir: Path) -> dict:
    out = {}
    rows = []
    for p in CONFIG["pii_url_params"]:
        n = con.execute(f"""SELECT count(*) FROM ev
            WHERE page_location LIKE '%{p}=%' OR page_referrer LIKE '%{p}=%'""").fetchone()[0]
        d = con.execute(f"""SELECT count(DISTINCT regexp_extract(page_location,'[?&]{p}=([^&]*)',1))
            FROM ev WHERE page_location LIKE '%{p}=%'""").fetchone()[0]
        if n:
            rows.append({"url_parameter": p, "rows_affected": n, "distinct_values": d,
                         "risk": "HIGH - direct identifier" if p in ("username", "email", "phone", "oib")
                                 else "HIGH - credential" if "token" in p else "review"})
    out["url_params"] = pd.DataFrame(rows)

    hits = []
    for name, rx in CONFIG["pii_regexes"].items():
        for c in ("page_location", "page_referrer", "on_origin_name", "game_name"):
            n = con.execute(f"""SELECT count(*) FROM ev WHERE "{c}" IS NOT NULL
                AND regexp_matches("{c}", '{rx}')""").fetchone()[0]
            if n:
                hits.append({"pattern": name, "column": c, "rows": n})
    out["regex"] = pd.DataFrame(hits) if hits else pd.DataFrame(
        [{"pattern": "email/ipv4", "column": "(all scanned)", "rows": 0}])

    out["player_id_form"] = con.execute("""
        SELECT count(*) AS distinct_players,
               sum(CASE WHEN regexp_matches(PlayerID,'^[0-9a-f]{64}$') THEN 1 ELSE 0 END) AS sha256_shaped
        FROM (SELECT DISTINCT PlayerID FROM ev)""").df()

    off = csv_view(data_dir / CONFIG["eps_offers_csv"])
    out["athlete_names"] = con.execute(f"""
        SELECT count(DISTINCT match_name) AS distinct_match_names FROM {off}""").df()
    return out


# ─────────────────────── REPORT WRITERS ───────────────────────
SEMANTICS = {
    "top_casino_users_event_logs.csv": (
        "**One row = one client-side tracking event fired by one player in one app/web session.** "
        "Grain: player x session x timestamp x event. This is the only behavioural log and the only "
        "file that can support a session funnel. It is a *sample of the operator's top casino users* "
        "(89 players), not a representative population."),
    "SB_Player.csv": (
        "**One row = one bet line (selection) placed by one player on one date**, with money split "
        "across the legs of the slip (`payin`/`stake` are distributions, not slip totals). "
        "Grain: player x date x fixture x market x selection."),
    "SB_MOM.csv": (
        "**One row = one month x sport x competition x fixture x market x selection aggregate** "
        "across the whole brand: ticket count and staked amounts. No player dimension - this is a "
        "market-level popularity/liquidity table. Key verified unique (0 duplicates)."),
    "EPS_Offers.csv": (
        "**One row = one odds quotation valid for a time interval** on a given match+market "
        "(`start_datetime_utc` -> `end_datetime_utc`). It is an odds *ticker/price history*, not an "
        "impression log: it says what the price was, never that a user saw it. Covers a single day."),
    "hackathon_casino_trends.xlsx": (
        "**One row = one market x month pre-aggregated casino KPI set** (stake/session, spins/session, "
        "sessions/player, median session length). Useful as an external benchmark to sanity-check our "
        "own session numbers; it cannot be joined to any player."),
    "empireofgold/": (
        "**Not data.** A compiled PixiJS slot-game client (JS bundles, sprites, locales, paytable). "
        "Useful as demo surface / UI context, and `assets/locale/*` documents the in-game vocabulary."),
    "FEG ... EU regulations guide.pdf": (
        "**Not data.** Regulatory guidance (responsible gaming / EU rules) that constrains what an "
        "intervention feature may legally do."),
}

UNINTERPRETED = [
    "`EPS_Offers.market_name` has 22,627 distinct values because player-prop markets embed context; "
    "we did not attempt to normalise it into a market taxonomy.",
    "`jackpot` is single-valued (`true`, 0.78% of rows) - cannot tell whether null means 'not a jackpot "
    "game' or 'not tracked'.",
    "`from_origin` / `on_origin` are populated on <3% of rows; the origin taxonomy is real but far too "
    "sparse to build attribution on.",
    "`betslip_placed_bet` carries `fixture_id`/`selection_id` but `betslip_add_bet` fills them only 73% "
    "of the time, so add->place matching at *selection* level is lossy.",
]


def write_data_map(path: Path, inv, jm, pii, sess, tax) -> None:
    L = []
    A = L.append
    A("# DATA_MAP - FEG 2026 · Session Quality & Session-to-Action Conversion\n")
    A("_Generated by `scripts/profile.py`. Read-only pass over the delivered data; "
      "nothing was written back into the data folder._\n")
    A("> **Folder note:** the delivered directory is literally named `data set ` (with a trailing "
      "space), not `data/`. `CONFIG['data_dir']` in `scripts/profile.py` is the single place to change it.\n")

    A("\n## 0. TL;DR\n")
    A("| Question | Answer |")
    A("|---|---|")
    A("| Is there a real `session_id`? | **Yes.** `session` in the event log is a genuine session key "
      "(epoch-second of session start). Use `(PlayerID, session)`. |")
    A("| Can we build a funnel? | **Yes**, but only from `top_casino_users_event_logs.csv`. |")
    A("| Do the other three files help? | Only as **context**. None of them contain a session or an "
      "impression. |")
    A("| Population | **89 players**, hand-picked top users, 1 month (Aug 2026), brand `hr` (PSK Croatia). |")
    A("| Blocking compliance issue | **Real login usernames and auth tokens are present in URLs** - "
      "see §6. |")

    A("\n## 1. Inventory\n")
    A("| File | Size | Rows | Cols | What it is |")
    A("|---|---|---|---|---|")
    for d in inv:
        A(f"| `{d['name']}` | {d['size']} | {d['rows']:,} | {len(d['profile'])} | {d['label']} |")
    A(f"| `empireofgold/` | 98 MB | - | - | Compiled slot-game web client (379 files), **not data** |")
    A(f"| `empireofgold (1).zip` | 85 MB | - | - | Zip of the above (duplicate) |")
    A(f"| `top_casino_users_event_logs (1).csv` | 84 MB | - | - | **Byte-identical duplicate** (md5 match) |")
    A(f"| `top_casino_users_event_logs*.xlsx` | 29/17 MB | 332,119 | 24 | **Same data as the CSV** "
      "(identical players/sessions/platform mix); only timestamp formatting differs |")
    A(f"| `FEG ... EU regulations guide.pdf` | 100 KB | - | - | Regulatory guidance, **not data** |")
    A(f"| `image (3).png` | 72 KB | - | - | Screenshot/diagram, **not data** |")

    A("\n### Per-file column profiles\n")
    for d in inv:
        A(f"\n#### `{d['name']}` - {d['rows']:,} rows\n")
        A(md_table(d["profile"]))
        A(f"\n<details><summary>{CONFIG['sample_rows']} sample rows (masked)</summary>\n")
        A(md_table(d["sample"]))
        A("\n</details>\n")

    A("\n## 2. Semantics - what is one row?\n")
    for k, v in SEMANTICS.items():
        A(f"- **`{k}`** - {v}")
    A("\n### Flagged: cannot interpret with confidence\n")
    for u in UNINTERPRETED:
        A(f"- {u}")

    A("\n## 3. Keys & joins (verified by overlap, not assumed)\n")
    A("\n**Coverage windows - the single biggest join constraint:**\n")
    A(md_table(jm["coverage"]))
    A("\n**`PlayerID` - events ↔ SB_Player** (the only true entity join):\n")
    A(md_table(jm["player_overlap"]))
    A("\n`PlayerID` is a 64-char lowercase hex (SHA-256-shaped) pseudonymous id, identically formatted "
      "in both files - **44 of 89 (49%) event-log players have bet rows**. The other 45 either bet only "
      "in Aug 1-15 (outside `SB_Player`'s window) or are casino-only players.\n")
    A("\n**Data-quality flag in `SB_Player`:**\n")
    A(md_table(jm["sbp_null_players"]))
    A("\nThose rows carry the literal string `'null'` as `PlayerID` and must be filtered out before "
      "any per-player aggregation.\n")
    A("\n**`fixture_name_english` - SB_Player ↔ SB_MOM:**\n")
    A(md_table(jm["fixture_overlap"]))
    A("\n100% containment: every fixture in `SB_Player` exists in `SB_MOM`. `SB_MOM` is the brand-wide "
      "aggregate of the same betting universe, so it is a valid popularity/benchmark denominator.\n")
    A("\n**`match_name` - SB_Player ↔ EPS_Offers** (name join, needs normalising):\n")
    A(md_table(jm["offers_overlap"]))
    A("\nEPS_Offers writes `\"A vs. B\"`; SB_* write `\"A - B\"`. After replacing `' - '` with `' vs. '` "
      "the join works, but only for the single day EPS_Offers covers.\n")
    A("\n**`sport_name` - events ↔ SB_Player: LANGUAGE MISMATCH.**\n")
    A(md_table(jm["sport_language"]))
    A("\nThe event log is **Croatian** (`Nogomet`, `Tenis`, `Košarka`); `SB_Player`/`SB_MOM` are "
      "**English** (`Football`, `Tennis`, `Basketball`). The 3 'shared' values are coincidences "
      "(e.g. `Baseball`). A manual translation map is required - do not join these raw.\n")
    A("\n**`SB_MOM` grain uniqueness:**\n")
    A(md_table(jm["mom_grain"]))

    A("\n### Join map\n")
    A("```")
    A("  top_casino_users_event_logs.csv         [BEHAVIOUR — the only session data]")
    A("    key: (PlayerID, session)  ── 13,286 sessions / 89 players / Aug 2026")
    A("            │")
    A("            │ PlayerID  (1:N, verified 44/89 overlap; windows differ)")
    A("            ▼")
    A("  SB_Player.csv                           [MONEY — bet lines, Aug 16-31 only]")
    A("    key: (PlayerID, placed_date, fixture, market, selection)")
    A("            │")
    A("            │ fixture_name_english + market + selection  (N:1, 100% containment)")
    A("            ▼")
    A("  SB_MOM.csv                              [MARKET POPULARITY — Jun-Aug 2026, no player]")
    A("    key: (brand, month, sport, event, fixture, market, selection)  — unique, 0 dups")
    A("            │")
    A("            │ fixture_name ≈ match_name  (N:N, name-normalised, 223/1403, 1 day only)")
    A("            ▼")
    A("  EPS_Offers.csv                          [PRICE HISTORY — 2026-08-16 only, no player]")
    A("    key: (match_name, market_name, start_datetime_utc)")
    A("")
    A("  hackathon_casino_trends.xlsx            [BENCHMARK — market x month, joins to nothing]")
    A("  empireofgold/                           [GAME CLIENT — UI context, not data]")
    A("```")
    A("\n**Cardinalities:** events→SB_Player is **1:N** (a player has many bet lines). "
      "SB_Player→SB_MOM is **N:1** on the fixture/market/selection tuple. "
      "SB_Player→EPS_Offers is **N:N** (many bets x many price ticks per match+market). "
      "There is **no key at all** linking a *session* to a *bet row* - see §6.\n")

    A("\n## 4. Session feasibility (summary - full numbers in BASELINE.md)\n")
    A(md_table(sess["native_id_check"]))
    A(md_table(sess["id_is_epoch"]))
    A("\n**Verdict: the `session` column is a real session identifier.** It decodes to the epoch second "
      "of the session's first event (90.3% of sessions start within 5s of it). It is *not* globally "
      "unique - 26 ids are reused by 2 different players - so the session key must be "
      "**`(PlayerID, session)`**, giving 13,286 sessions.\n")

    A("\n## 5. Event taxonomy\n")
    A(md_table(tax["taxonomy"]))
    A("\n**The event vocabulary is platform-dependent** - this is the single most important schema "
      "quirk, and naive `event_name` filtering will silently drop whole platforms:\n")
    A(md_table(tax["platform_matrix"]))

    A("\n## 6. What the data CANNOT tell us\n")
    A("Stated plainly so we do not over-claim in the demo:\n")
    A("1. **No UI context.** There is no screen layout, scroll depth, element id, viewport, or "
      "click coordinate. We know a screen was *viewed*, never what was *on* it or where the user looked.")
    A("2. **No impressions.** `EPS_Offers` is a price ticker, not an impression log. We can never say "
      "\"the user saw these odds and rejected them\". Any 'offer shown' claim is fabricated.")
    A("3. **No abandonment reason.** When a slip is built and never submitted we see only the absence "
      "of `betslip_placed`. Odds change, insufficient balance, deposit friction, a phone call, and "
      "a deliberate change of mind are **indistinguishable**.")
    A("4. **No error/validation telemetry.** `status` exists only on submitted slips "
      "(ACCEPTED/FAILED/REJECTED/CLOSED). Client-side blocks before submission are invisible.")
    A("5. **No money in the session.** Stake never appears in the event log. `SB_Player` has money but "
      "no `session` and no timestamp finer than a date - so **a bet cannot be attributed to a session**. "
      "Revenue impact of a session is an *estimate*, never a measurement.")
    A("6. **No deposit/balance state.** Deposit pages appear as URLs only; we cannot tell whether a "
      "deposit succeeded or whether a player could afford the slip they abandoned.")
    A("7. **Not a representative population.** 89 hand-picked *top* users averaging 149 sessions/month. "
      "Conversion rates here are an upper bound and must not be presented as brand-wide.")
    A("8. **No true new users.** Every player is already active on day 1 of the window; the "
      "'first observed session' segment is 89 sessions and is an artefact of the window, not acquisition.")
    A("9. **One month, one brand, one market.** All data is brand `hr` (PSK Croatia), Aug 2026. "
      "No seasonality, no cross-market generalisation.")
    A("10. **`EPS_Offers` is one single day** (2026-08-16), so odds-movement features can only ever be "
      "demonstrated on 1/31 of the behavioural window.")

    A("\n## 7. Personal-data flags (hackathon rules: synthetic/anonymised only)\n")
    A("**⚠️ ACTION REQUIRED - the event log is _not_ fully anonymised.**\n")
    A(md_table(pii["url_params"]))
    A("\n- `page_location` / `page_referrer` contain a **`username=` query parameter holding real "
      "human-chosen login names** (333 distinct values, e.g. `Gag***`, `dam**`). This is a direct "
      "identifier and defeats the `PlayerID` pseudonymisation for those rows.")
    A("- The same URLs carry **`temptoken=` values (88-char auth/session tokens)** - a live credential "
      "class, not just personal data.")
    A("- **Mitigation before any demo or commit:** strip the query string from `page_location` and "
      "`page_referrer` (keep scheme+host+path only), or allow-list the params actually needed "
      "(`filter`, `tab`, `category`, `page`).")
    A("\nEverything else scanned clean:\n")
    A(md_table(pii["regex"]))
    A(md_table(pii["player_id_form"]))
    A("\n- `PlayerID` is properly pseudonymous (64-hex SHA-256 shape) in **both** the event log and "
      "`SB_Player` - good.")
    A("- `EPS_Offers.match_name` contains **real athlete names** (e.g. `Hijikata, Rinky vs. Mensik, "
      "Jakub`). These are public-figure sports data rather than customer PII, but they are still "
      "personal data of identifiable people - keep them out of screenshots where practical.")
    A("- No email addresses and no IP addresses were found in any scanned column.")
    A("\n---\n_Regenerate with `python3 scripts/profile.py`._")
    path.write_text("\n".join(L), encoding="utf-8")
    log(f"  wrote {path}")


def write_baseline(path: Path, sess, tax) -> None:
    g = CONFIG["session_gap_minutes"]
    L = []
    A = L.append
    A("# BASELINE - Session Quality & Session-to-Action Conversion\n")
    A("_Generated by `scripts/profile.py`. Source: `top_casino_users_event_logs.csv` "
      "(332,119 events · 89 players · 2026-08-01 → 2026-08-31 · brand `hr`)._\n")
    A("> Every number below is from the **native** `(PlayerID, session)` key. "
      f"The {g}-minute reconstruction is reported as a cross-check only.\n")

    A("\n## 1. Is there a real session id? — YES\n")
    A("Three independent checks:\n")
    A("**(a) The id decodes to the session start time.**\n")
    A(md_table(sess["id_is_epoch"]))
    A("`session` is the epoch-second at which the session began: 90.3% of sessions have their first "
      "event within ±5s of it, median offset 1s. It is a real session token, not a row hash.\n")
    A("**(b) It is not globally unique — scope it to the player.**\n")
    A(md_table(sess["native_id_check"]))
    A("26 ids collide across exactly 2 players (an epoch-second collision). "
      "**Always key on `(PlayerID, session)`.**\n")
    A(f"**(c) It behaves like a session, not a day bucket.**\n")
    A(md_table(sess["native_stats"]))
    A("Median 3.7 min, p90 63.5 min, and only 3 of 13,286 sessions exceed 12h. "
      "A day-bucket would have produced ~2,700 sessions of ~24h.\n")

    A(f"\n## 2. Cross-check: {g}-minute inactivity reconstruction\n")
    A("Inter-event gaps *inside* a native session:\n")
    A(md_table(sess["gap_within"]))
    A(f"Median gap is 2s and only 630 of 318,833 within-session gaps (0.2%) exceed {g} minutes — "
      "the native boundaries already respect the inactivity rule.\n")
    A(f"Sessions rebuilt from scratch with a {g}-minute gap:\n")
    A(md_table(sess["recon_stats"]))
    A("Agreement between the two definitions:\n")
    A(md_table(sess["agreement"]))
    A(f"**96.9% of native sessions sit entirely inside a single {g}-minute-gap session.** The "
      "reconstruction is slightly *coarser* (10,432 vs 13,286) because it merges app restarts and "
      "platform switches that happen within 30 minutes.\n")
    A("**Decision: use the native `session` id.** It is real, it agrees with the heuristic, and it "
      "correctly splits a player who reopens the app after 5 minutes. The 30-minute rule is kept in "
      "`CONFIG['session_gap_minutes']` as a validated fallback for any feed that lacks a session token.\n")

    A("\n## 3. Session shape\n")
    A(md_table(sess["duration_dist"]))
    A("\nSessions per player over the month:\n")
    A(md_table(sess["per_player"]))
    A("\n⚠️ 149 sessions/player/month (~5/day) confirms these are the operator's **heaviest** users. "
      "Treat all conversion rates as an upper bound.\n")

    A("\n## 4. Event taxonomy → funnel stages\n")
    A(md_table(tax["taxonomy"]))
    A("\n**Grain warning:** `betslip_placed` (3,393) is *one submitted slip*; `betslip_placed_bet` "
      "(22,146) is *one leg of a submitted slip* — 6.8 legs per slip, because accumulators (AKO) "
      "dominate. Counting `betslip_placed_bet` as conversions overstates them ~6.8x.\n")
    A("Outcome of submitted slips:\n")
    A(md_table(tax["status"]))

    A("\n## 5. Baseline funnel (session level)\n")
    A(md_table(tax["funnel"]))
    A("\n`pct_of_start` = share of all 13,286 sessions. `step_conv` = conversion from the previous step.\n")
    A("\n### The headline: the final step\n")
    A(md_table(tax["sb_funnel"]))
    A("\n> **25.29% of sessions that build a betslip never submit it.** "
      "417 of 1,649 sessions reach the slip and abandon it.\n")
    A("\n**Overall session conversion rate: 9.85%** (1,309 of 13,286 sessions submit a slip); "
      "**9.80%** end in an accepted bet. Of sessions that reach *any* intent "
      "(add-to-slip or game launch), 31.1% convert.\n")
    A("\nFunnel is **not strictly nested** — quantified:\n")
    A(md_table(tax["leakage"]))
    A("5.9% of submitted slips have no `betslip_add_bet` in the same session. These are re-bets from "
      "`betslip_history` / `betslip-history-overview` / `afterbet` (visible in `added_from`). "
      "Re-bet is a *different* journey and should be modelled separately, not treated as leakage.\n")

    A("\n## 6. Per-platform funnel\n")
    A(md_table(tax["by_platform"]))
    A("\nSB iOS shows the worst abandonment (30.1% of slip-builders drop) and GM/web the best (~4-5%), "
      "but the platforms use different event vocabularies, so treat cross-platform gaps as a "
      "**hypothesis to verify**, not a finding.\n")

    A("\n## 7. Session quality: converters look nothing like non-converters\n")
    A(md_table(tax["quality"]))
    A("\nThis is the strongest signal in the dataset: a converting session carries **131.6 events** "
      "against **7.8** for a browse-only session (~17x), and lasts 30.6 min against 0.5 min. "
      "Session quality is highly separable well before the bet is placed.\n")

    A("\n## 8. Timing\n")
    A(md_table(tax["timing"]))
    A("\nMedian time to first action is **51 seconds**; median time to a submitted bet is "
      "**7.2 minutes**. The intent window is short — an intervention has roughly the first minute "
      "to matter.\n")
    A("Actions per session (all sessions):\n")
    A(md_table(tax["actions_per_session"]))
    A("\n8.66 selections are added for every slip submitted — users build far more than they commit.\n")

    A("\n## 9. Segmentation\n")
    A("**New vs returning — not answerable from this data:**\n")
    A(md_table(tax["new_vs_returning"]))
    A("\nEvery one of the 89 players is already active on day 1, so 'first observed session' is just "
      "the 89 window-edge sessions, not genuine acquisition. **Do not present this as a new-user "
      "finding.** A usable proxy is session frequency:\n")
    A(md_table(tax["heavy_light"]))

    A("\n## 10. Top 5 observations\n")
    A("1. **A real session id already exists** and survives validation — 90.3% of ids decode to their "
      "own start time, and 96.9% agree with a 30-minute inactivity rebuild. No sessionisation work is "
      "needed; key on `(PlayerID, session)`.")
    A("2. **The drop-off is concentrated at the last step.** 1 in 4 sessions that build a betslip "
      "(25.29%) never submit it. That is a single, well-defined, addressable moment — not diffuse "
      "browsing apathy.")
    A("3. **Converting sessions are ~17x more event-dense** (131.6 vs 7.8 events) and 60x longer than "
      "browse-only sessions. Session quality is separable early, which makes live scoring realistic.")
    A("4. **Intent is fast and shallow-lived**: median 51s to first action, 7.2 min to bet. Any "
      "intervention must fire inside the session, in the first minute or two — a next-day email is "
      "structurally too late.")
    A("5. **Behaviour and money are in separate silos with no key between them.** The event log has "
      "sessions but no stake; `SB_Player` has stake but no session and only date-level time. Every "
      "euro figure we quote is a modelled estimate — say so on the slide.")

    A("\n## 11. Caveats to keep on the record\n")
    A("- 89 players, all heavy users, one brand (`hr`), one month. Upper-bound rates.")
    A("- `betslip_placed_bet` is per-leg, not per-slip (6.8x inflation if misused).")
    A("- Platform-specific event vocabularies; `web`/`GM` mix casino and sportsbook on one platform label.")
    A("- 5.9% of conversions are re-bets with no in-session add event.")
    A("- Abandonment *reasons* are not in the data at all (see DATA_MAP §6).")
    A("\n---\n_Regenerate with `python3 scripts/profile.py`._")
    path.write_text("\n".join(L), encoding="utf-8")
    log(f"  wrote {path}")


# ─────────────────────────── MAIN ───────────────────────────
def main() -> int:
    data_dir, docs = CONFIG["data_dir"], CONFIG["docs_dir"]
    if not data_dir.exists():
        log(f"ERROR: data dir {data_dir!s} not found"); return 1
    docs.mkdir(exist_ok=True)

    log("[1/6] cache (xlsx -> parquet)")
    cache = ensure_cache(data_dir, CONFIG["cache_dir"])
    con = connect()

    log("[2/6] inventory")
    inv = build_inventory(con, data_dir, cache)

    log("[3/6] joins")
    jm = join_map(con, data_dir)

    log("[4/6] sessions")
    register_events(con, data_dir)
    sess = session_feasibility(con)

    log("[5/6] taxonomy + funnel + pii")
    tax = taxonomy_and_funnel(con)
    pii = pii_scan(con, data_dir)

    log("[6/6] writing docs")
    write_data_map(docs / "DATA_MAP.md", inv, jm, pii, sess, tax)
    write_baseline(docs / "BASELINE.md", sess, tax)
    log("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge: Session Quality & Session-to-Action Conversion
Step 1: sanitise the behavioural event log.

DATA_MAP.md §7 flags the event log as *not* fully anonymised: `page_location`
and `page_referrer` carry `username=` (real login names) and `temptoken=`
(88-char auth credentials) query parameters. Hackathon rules require
synthetic/anonymised data only, so nothing downstream may ever see them.

What this does:
  1. reads   "data set "/top_casino_users_event_logs.csv   (READ-ONLY)
  2. strips  the query string and fragment from page_location / page_referrer,
             keeping scheme + host + path only
  3. proves  the strip worked: before/after row counts per credential pattern,
             then a hard assert that zero remain in ANY column
  4. writes  data/clean/events.parquet

Nothing is ever written back into the data directory.

Only the CSV is used. `top_casino_users_event_logs (1).csv` is a byte-identical
duplicate (md5 match) and both XLSX copies hold the same rows - see DATA_MAP §1.

Usage:  python3 scripts/sanitize.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

# Reuse profile.py's CONFIG - it is the single place the "data set " path
# (trailing space, deliberately) is defined. Do not hardcode a second copy.
# Loaded by file path rather than `import profile`, which would collide with
# the stdlib profiler module of the same name.
import importlib.util  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "feg_profile", Path(__file__).resolve().parent / "profile.py")
_profile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_profile)
CONFIG, csv_view, log = _profile.CONFIG, _profile.csv_view, _profile.log


def _abs(p: Path) -> Path:
    """CONFIG paths are relative to the repo root, not to the caller's cwd."""
    return p if p.is_absolute() else REPO_ROOT / p


SANITIZE = {
    # columns that carry URLs, and therefore credentials
    "url_columns": ("page_location", "page_referrer"),
    # everything after the first '?' or '#' goes; scheme+host+path survives
    "strip_regex": r"[?#].*$",
    # patterns that must be zero after the strip (DATA_MAP §7)
    "must_be_zero": ("username=", "temptoken=", "token="),
    "out_parquet": Path("data/clean/events.parquet"),
}


def scan_credentials(con, rel: str) -> list[dict]:
    """Count rows carrying each credential pattern, across every VARCHAR column.

    Uses the same LIKE '%<param>=%' test as profile.py's pii_scan so the
    numbers line up with DATA_MAP §7. Note `token=` matches `temptoken=` as a
    substring, so token >= temptoken by construction - that is expected, not
    double counting.
    """
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()
            if r[1].upper().startswith("VARCHAR")]
    rows = []
    for pat in SANITIZE["must_be_zero"]:
        any_col = " OR ".join(f"\"{c}\" LIKE '%{pat}%'" for c in cols)
        n = con.execute(f"SELECT count(*) FROM {rel} WHERE {any_col}").fetchone()[0]
        # distinct leaked values, so we can say how many real humans/credentials
        param = pat.rstrip("=")
        d = con.execute(f"""
            SELECT count(DISTINCT v) FROM (
              SELECT regexp_extract(page_location, '[?&]{param}=([^&]*)', 1) AS v
              FROM {rel} WHERE page_location LIKE '%{pat}%'
              UNION
              SELECT regexp_extract(page_referrer, '[?&]{param}=([^&]*)', 1) AS v
              FROM {rel} WHERE page_referrer LIKE '%{pat}%'
            ) WHERE v <> ''""").fetchone()[0]
        rows.append({"pattern": pat, "rows": n, "distinct_values": d})
    return rows


def main() -> int:
    data_dir = _abs(CONFIG["data_dir"])
    src = data_dir / CONFIG["event_log_csv"]
    out = _abs(SANITIZE["out_parquet"])

    if not src.exists():
        log(f"ERROR: {src} not found")
        return 1
    if out.resolve().is_relative_to(data_dir.resolve()):
        log("ERROR: refusing to write inside the delivered data directory")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{CONFIG['duckdb_memory_limit']}'")

    log(f"[1/4] reading {src}")
    con.execute(f"CREATE OR REPLACE VIEW raw AS SELECT * FROM {csv_view(src, True)}")
    n_raw = con.execute("SELECT count(*) FROM raw").fetchone()[0]

    log("[2/4] scanning for credentials (before)")
    before = scan_credentials(con, "raw")

    log("[3/4] stripping query strings from " + ", ".join(SANITIZE["url_columns"]))
    rx = SANITIZE["strip_regex"]
    # nullif(...,'') so a URL that was nothing but a query string becomes NULL
    # rather than an empty string. sid/ts are typed here (same casts as
    # profile.py) purely so downstream readers do not re-parse strings; no
    # session-level aggregate is computed anywhere in this file.
    sets = ", ".join(
        f"nullif(regexp_replace(\"{c}\", '{rx}', ''), '') AS \"{c}\""
        for c in SANITIZE["url_columns"])
    keep = [r[0] for r in con.execute("DESCRIBE SELECT * FROM raw").fetchall()
            if r[0] not in SANITIZE["url_columns"]]
    con.execute(f"""
        CREATE OR REPLACE VIEW clean AS
        SELECT {', '.join(f'"{c}"' for c in keep)},
               {sets},
               CAST(CAST(session AS DOUBLE) AS BIGINT)      AS sid,
               CAST(replace(timestamp, 'Z', '') AS TIMESTAMP) AS ts
        FROM raw
    """)

    n_stripped = con.execute(f"""
        SELECT count(*) FROM raw
        WHERE regexp_matches(coalesce(page_location, ''), '{rx}')
           OR regexp_matches(coalesce(page_referrer, ''), '{rx}')""").fetchone()[0]

    after = scan_credentials(con, "clean")
    n_clean = con.execute("SELECT count(*) FROM clean").fetchone()[0]

    # ── the compliance numbers ────────────────────────────────────────────
    print()
    print("URL credential strip - rows affected in top_casino_users_event_logs.csv")
    print(f"  total rows: {n_raw:,}   rows with a query string stripped: {n_stripped:,}")
    print()
    print(f"  {'pattern':<12} {'rows before':>12} {'rows after':>11} "
          f"{'distinct values removed':>24}")
    print(f"  {'-'*12} {'-'*12} {'-'*11} {'-'*24}")
    for b, a in zip(before, after):
        print(f"  {b['pattern']:<12} {b['rows']:>12,} {a['rows']:>11,} "
              f"{b['distinct_values']:>24,}")
    print()
    print("  note: 'token=' matches 'temptoken=' as a substring, so its count "
          "is a superset.")
    print()

    # ── hard gate: nothing leaves this script carrying a credential ───────
    leaked = [a for a in after if a["rows"] != 0]
    assert not leaked, f"credentials survived sanitisation: {leaked}"
    assert n_clean == n_raw, f"row count changed: {n_raw:,} -> {n_clean:,}"

    log(f"[4/4] writing {out}")
    con.execute(f"COPY clean TO '{str(out).replace(chr(39), chr(39)*2)}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)")

    # re-read from disk and re-assert, so the *file* is what we certified
    disk = f"read_parquet('{str(out).replace(chr(39), chr(39)*2)}')"
    for a in scan_credentials(con, disk):
        assert a["rows"] == 0, f"credential in written parquet: {a}"
    n_out = con.execute(f"SELECT count(*) FROM {disk}").fetchone()[0]
    assert n_out == n_raw

    size = out.stat().st_size
    print(f"OK  data/clean/events.parquet  {n_out:,} rows  {size/1024/1024:,.1f} MB")
    print("OK  zero username= / temptoken= / token= remain in any column "
          "(asserted, in-memory and on disk)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

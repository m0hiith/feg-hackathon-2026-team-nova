#!/usr/bin/env python3
"""
FEG 2026 Hackathon - Challenge 1: Session Quality & Session-to-Action Conversion
Data loading. Synthetic data only; nothing here reaches a network.

The event log carries TWO timestamp formats, because the real FEG export does:

    sportsbook rows   2023-09-03T00:11:51.000Z            (ISO 8601, Z suffix)
    casino rows       2023-09-02 00:07:21.000000 UTC      (space separated, " UTC")

Both are UTC. They are parsed explicitly rather than with `format="mixed"`, so a
third format appearing in a future export fails loudly instead of being silently
coerced to NaT.

Usage:  python3 src/data_loader.py        # self-check
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

EVENTS_CSV = DATA_DIR / "synthetic_player_event_logs.csv"
PROFILES_CSV = DATA_DIR / "synthetic_user_profiles.csv"

ISO_Z = "%Y-%m-%dT%H:%M:%S.%fZ"
SPACE_UTC = "%Y-%m-%d %H:%M:%S.%f"


def parse_timestamps(raw: pd.Series) -> pd.Series:
    """Parse both timestamp dialects into one tz-aware UTC series.

    Raises if any value matches neither, rather than returning NaT. A silently
    dropped timestamp would shift a session boundary and corrupt every
    downstream habit statistic.
    """
    s = raw.astype("string").str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns, UTC]")

    is_space_utc = s.str.endswith(" UTC", na=False)
    if is_space_utc.any():
        out.loc[is_space_utc] = pd.to_datetime(
            s[is_space_utc].str.removesuffix(" UTC"), format=SPACE_UTC, utc=True
        )
    if (~is_space_utc).any():
        out.loc[~is_space_utc] = pd.to_datetime(
            s[~is_space_utc], format=ISO_Z, utc=True
        )

    bad = out.isna() & raw.notna()
    if bad.any():
        sample = raw[bad].head(3).tolist()
        raise ValueError(
            f"{int(bad.sum())} timestamp(s) matched neither known format; e.g. {sample}"
        )
    return out


def load_events(path: Path | str | None = None) -> pd.DataFrame:
    """The full event log, timestamp-parsed and time-ordered.

    Adds two derived columns and nothing else:
      `ts`         tz-aware UTC datetime
      `player_key` short persona key, e.g. "SYN_U03", for readable output
    """
    df = pd.read_csv(path or EVENTS_CSV, low_memory=False)
    df["ts"] = parse_timestamps(df["timestamp"])
    df["player_key"] = df["PlayerID"].str.slice(0, 7)
    return df.sort_values("ts", kind="stable").reset_index(drop=True)


def load_ground_truth(path: Path | str | None = None) -> pd.DataFrame:
    """Per-player ground truth. Validation target only - never an engine input."""
    return pd.read_csv(path or PROFILES_CSV)


def resolve_player(events: pd.DataFrame, player_id: str) -> str:
    """Accept either a full hashed PlayerID or a short key like "SYN_U03"."""
    if player_id in events["PlayerID"].values:
        return player_id
    hits = events.loc[events["PlayerID"].str.startswith(player_id), "PlayerID"].unique()
    if len(hits) == 1:
        return str(hits[0])
    if len(hits) == 0:
        raise KeyError(f"no player matching {player_id!r}")
    raise KeyError(f"{player_id!r} is ambiguous: {list(hits)}")


def sessions_for(player_id: str, events: pd.DataFrame | None = None) -> list[pd.DataFrame]:
    """Every session for one player, each time-ordered, oldest session first.

    Sessions are ordered by their first event, NOT by `session` id: the id is a
    counter assigned at generation time and is not monotonic in time.
    """
    ev = load_events() if events is None else events
    pid = resolve_player(ev, player_id)
    mine = ev[ev["PlayerID"] == pid].sort_values("ts", kind="stable")
    ordered = mine.groupby("session", sort=False)["ts"].min().sort_values().index
    return [mine[mine["session"] == sid].reset_index(drop=True) for sid in ordered]


if __name__ == "__main__":
    ev = load_events()
    gt = load_ground_truth()
    print(f"events   {len(ev):,}  players {ev.PlayerID.nunique()}  sessions {ev.session.nunique():,}")
    print(f"span     {ev.ts.min()}  ->  {ev.ts.max()}")
    print(f"tz-aware {ev.ts.dt.tz}   unparsed: {int(ev.ts.isna().sum())}")
    print(f"ground truth rows {len(gt)}")

    s = sessions_for("SYN_U03", ev)
    print(f"\nSYN_U03: {len(s)} sessions; first has {len(s[0])} events, last {len(s[-1])}")
    starts = [x.ts.iloc[0] for x in s]
    print("sessions are time-ordered:", all(a <= b for a, b in zip(starts, starts[1:])))

"""
FEG Hackathon 2026 - Synthetic player event-log generator (v2, odds-aware)
==========================================================================
Generates SYNTHETIC ONLY data. No real player data, no real IDs, no live APIs.
Schema is identical to top_casino_users_event_logs.xlsx (24 columns),
plus 3 clearly-marked synthetic-only columns at the end. 27 columns total.

Reproducible: fixed seed, and every derived value uses a stable SHA-256 hash
rather than Python's per-process randomised hash().

WHAT CHANGED IN v2, AND WHY
---------------------------
v1 drew a fresh random `selection_id` for every `betslip_add_bet`, so the same
selection was essentially never seen twice: 10,411 distinct selection_ids
across 10,421 rows. That makes odds MOVEMENT impossible to compute, and the
project's hard rule -- "never show only favourable odds movement; if odds got
worse, say so" -- had nothing to compute from.

v2 adds a market book so movement is real and observable:

  1. FIXTURES ARE STABLE.  `fixture_for(sport, comp, t)` returns the same
     fixture for every session in the same 3-day bucket of a competition, with
     a kickoff time. A fixture is bettable until kickoff, so two sessions days
     apart can touch the same fixture.

  2. SELECTIONS ARE STABLE.  Each fixture owns a fixed set of (market, outcome)
     selections with stable selection_ids, derived by SHA-256 so they are
     reproducible across processes.

  3. ODDS ARE A FUNCTION OF TIME.  `odds_at(selection_id, t)` is a deterministic
     drift: a per-selection sine wave plus a small linear trend. The same
     selection at the same instant always gives the same price; at a different
     instant it gives a different one, and it moves in BOTH directions. Nothing
     here is favourable by construction.

  4. ABANDONED SLIPS CAN BE RESUMED.  When a session abandons at the betslip,
     the slip is remembered. A later session by the same player may rebuild
     that exact slip (same fixture, same selection_ids) while the fixture is
     still open. That is the second observation of the same selection at a
     later time -- which is precisely what makes an honest odds-movement card
     computable: price then vs price now, up or down.

  5. PLACED BETS REUSE THE ADDED SELECTIONS.  v1 re-randomised selection_id on
     `betslip_placed_bet`, so a placed bet could not be linked to the bet that
     was added. v2 places the same selections that were added, and records the
     odds they were accepted at.

Resume REPLACES a fresh slip rather than adding one, so session volume and each
persona's abandon rate stay where the demo needs them.

Outputs are written next to the repo's data/ directory.
Usage:  python3 tools/generate_synthetic_users.py
"""

import random
import hashlib
import math
import datetime as dt
from pathlib import Path

import pandas as pd

SEED = 20260908
random.seed(SEED)

START = dt.datetime(2023, 9, 1)
END = dt.datetime(2026, 8, 31)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data"

REAL_COLS = [
    "event_name", "session", "timestamp", "platform", "game_name", "provider",
    "from_origin", "on_origin", "on_origin_name", "jackpot", "on_route",
    "from_route", "demo", "page_location", "page_referrer",
    "fortuna_screen_name", "status", "added_from", "sport_name",
    "betslip_number", "betslip_type", "fixture_id", "selection_id", "PlayerID",
]
SYNTH_COLS = ["stake_eur", "odds", "is_synthetic"]

# ---------------------------------------------------------------- catalogues

COMPETITIONS = {
    "Nogomet": ["HNL", "Liga prvaka", "Premier League", "La Liga", "Serie A",
                "Bundesliga", "Europska liga"],
    "Tenis": ["ATP Masters", "WTA 1000", "Grand Slam", "ATP 250"],
    "Košarka": ["ABA Liga", "Euroliga", "NBA", "HT Premijer liga"],
    "Hokej": ["NHL", "Champions Hockey League"],
    "Odbojka": ["CEV Liga prvaka"],
}

MARKETS = {
    "Nogomet": ["1X2", "Ukupno golova 2.5", "Oba tima daju gol", "Hendikep",
                "Konačan ishod"],
    "Tenis": ["Pobjednik meča", "Ukupno gemova", "Hendikep setova"],
    "Košarka": ["Pobjednik", "Ukupno poena", "Hendikep"],
    "Hokej": ["Pobjednik", "Ukupno golova"],
    "Odbojka": ["Pobjednik"],
}

SLOTS = {
    "Greentube": ["Sizzling Hot Deluxe", "20 Super Hot", "40 Super Hot Bell Link",
                  "Royal Seven XXL", "Royal Seven XXL RHFP"],
    "Pragmatic Play": ["Sugar Rush 1000", "Gates of Olympus Super Scatter",
                       "Big Bass Bonanza"],
    "Playtech": ["Mega Fire Blaze: Wild Pistolero", "Pink Joker: Hold and Win",
                 "Black Clover Bell Link"],
    "Amusnet": ["100 Hot Wild", "40 Bulky Fruits Golden Coins Link",
                "100 Power Hot Dice Edition Golden Coins Link"],
}
TABLE_GAMES = {"Evolution": ["BlackJack MH", "Roulette Live", "Lightning Roulette"]}

SB_SCREENS_BROWSE = ["homepage", "prematchSports", "prematchLeagues",
                     "prematchMatchesOverview", "competition_detail"]
CASINO_PAGES = ["https://casino.psk.hr/", "https://casino.psk.hr/promocije",
                "https://casino.psk.hr/igre", "https://casino.psk.hr/jackpot",
                "https://casino.psk.hr/najnovije"]

# Days where a major global event overshadows normal fixtures.
# Used to test the "FIFA final vs normal match" conflict rule.
BIG_EVENT_DAYS = {
    dt.date(2024, 6, 14), dt.date(2024, 7, 14),   # Euro 2024
    dt.date(2025, 5, 31),                          # UCL final
    dt.date(2026, 6, 11), dt.date(2026, 7, 19),    # World Cup 2026
    dt.date(2026, 5, 30),                          # UCL final
}

# ------------------------------------------------------- market book tuning

# A competition gets a fresh fixture every FIXTURE_BUCKET_DAYS. Sessions inside
# the same bucket see the SAME fixture, which is what lets a slip be resumed.
FIXTURE_BUCKET_DAYS = 3
# Probability that a session which reaches the betslip rebuilds a still-open
# abandoned slip instead of starting a new one.
RESUME_P = 0.42
# Opening-price band, matching v1's uniform(1.25, 4.2).
ODDS_MIN, ODDS_MAX = 1.25, 4.2
# Hard floor. Odds below this are not a real price.
ODDS_FLOOR = 1.05


def stable_int(*parts, lo=0, hi=10 ** 9):
    """Reproducible hash -> int. Python's hash() is salted per process."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return lo + int(h[:12], 16) % (hi - lo)


def stable_rng(*parts):
    return random.Random(stable_int(*parts, hi=2 ** 31))


_fixtures = {}


def fixture_for(sport, comp, t):
    """The fixture a session at time `t` would be looking at, for this comp.

    Stable per (sport, comp, 3-day bucket), so two sessions days apart can land
    on the same fixture and therefore the same selections.
    """
    bucket = (t - START).days // FIXTURE_BUCKET_DAYS
    key = (sport, comp, bucket)
    if key not in _fixtures:
        rnd = stable_rng("fixture", *key)
        # Kickoff at the end of the bucket, so every session in the bucket is
        # pre-kickoff and the market is open.
        kickoff = START + dt.timedelta(days=(bucket + 1) * FIXTURE_BUCKET_DAYS)
        selections = []
        for market in MARKETS[sport]:
            for outcome in range(rnd.randint(2, 3)):
                selections.append(stable_int("sel", *key, market, outcome,
                                             lo=70_000_000, hi=79_999_999))
        _fixtures[key] = {
            "fixture_id": stable_int("fx", *key, lo=3_000_000, hi=3_999_999),
            "kickoff": kickoff,
            "selections": selections,
        }
    return _fixtures[key]


def odds_at(selection_id, t):
    """Price of one selection at one instant. Deterministic, and it moves.

    A per-selection sine wave (so the direction genuinely reverses) plus a small
    linear trend. Nothing biases this toward drifting favourably: the phase is
    drawn per selection and the trend sign is symmetric around zero.
    """
    rnd = stable_rng("odds", selection_id)
    base = rnd.uniform(ODDS_MIN, ODDS_MAX)
    amp = base * rnd.uniform(0.05, 0.16)
    phase = rnd.uniform(0, 2 * math.pi)
    period_h = rnd.uniform(30.0, 140.0)
    trend_per_week = base * rnd.uniform(-0.06, 0.06)

    hours = (t - START).total_seconds() / 3600.0
    wave = amp * math.sin(phase + (hours / period_h) * 2 * math.pi)
    trend = trend_per_week * (hours / 168.0) * 0.02
    return round(max(ODDS_FLOOR, base + wave + trend), 2)


# Abandoned slips, per player, still open for a later session to rebuild.
_pending = {}


def remember_pending(pid, entry):
    _pending.setdefault(pid, []).append(entry)


def take_pending(pid, t):
    """Pop one still-open abandoned slip for this player, or None."""
    queue = _pending.get(pid)
    if not queue:
        return None
    # Drop anything whose fixture has already kicked off.
    queue[:] = [e for e in queue if e["kickoff"] > t]
    if not queue:
        return None
    return queue.pop(0)


# ---------------------------------------------------------------- personas

PERSONAS = [
    dict(uid="SYN_U01", label="Football loyalist", platform="SB Android",
         vertical="sports", sports={"Nogomet": 0.96, "Košarka": 0.03, "Tenis": 0.01},
         comps=["Liga prvaka", "HNL", "Premier League"], slip="AKO",
         stake=(12, 25), abandon=0.12, friction=0.10, live_bias=0.10,
         sessions_wk=5.5, hours=(18, 24), start=START,
         note="Deep single-sport habit. Proves sport-level (not category-level) recommendation."),
    dict(uid="SYN_U02", label="Tennis live specialist", platform="SB iOS",
         vertical="sports", sports={"Tenis": 0.93, "Nogomet": 0.05, "Košarka": 0.02},
         comps=["WTA 1000", "ATP Masters", "Grand Slam"], slip="SOLO",
         stake=(25, 55), abandon=0.25, friction=0.16, live_bias=0.62,
         sessions_wk=4.4, hours=(10, 22), start=START,
         note="Live-heavy, single-selection. Different habit shape from U01."),
    dict(uid="SYN_U03", label="Basketball high-abandoner", platform="SB Android",
         vertical="sports", sports={"Košarka": 0.91, "Nogomet": 0.07, "Tenis": 0.02},
         comps=["ABA Liga", "Euroliga", "NBA"], slip="LEG_COMBI",
         stake=(6, 14), abandon=0.41, friction=0.34, live_bias=0.20,
         sessions_wk=6.2, hours=(19, 24), start=START,
         note="THE demo user. Reaches betslip and leaves 41% of the time."),
    dict(uid="SYN_U04", label="Slots regular", platform="GM",
         vertical="casino", providers=["Amusnet", "Greentube"],
         stake=(1, 4), sessions_wk=7.4, hours=(20, 24), start=START,
         note="One provider family. Proves casino side of the same engine."),
    dict(uid="SYN_U05", label="Hybrid sports + casino", platform="SB Android",
         vertical="hybrid", sports={"Nogomet": 0.88, "Košarka": 0.08, "Tenis": 0.04},
         comps=["HNL", "Liga prvaka"], slip="AKO", providers=["Pragmatic Play"],
         stake=(14, 30), abandon=0.23, friction=0.18, live_bias=0.14,
         sessions_wk=6.4, hours=(17, 24), start=START,
         note="Uses both products but NEVER in one session. The cross-vertical gap."),
    dict(uid="SYN_U06", label="Live table games", platform="Casino Android",
         vertical="casino", providers=["Evolution"],
         stake=(8, 20), sessions_wk=4.7, hours=(21, 24), start=START,
         note="Table games, not slots. Tests that casino recs are not one-size-fits-all."),
    dict(uid="SYN_U07", label="Big-event only", platform="web",
         vertical="sports", sports={"Nogomet": 0.95, "Košarka": 0.05},
         comps=["Liga prvaka", "Europska liga"], slip="SOLO",
         stake=(40, 80), abandon=0.35, friction=0.22, live_bias=0.30,
         sessions_wk=0.6, hours=(19, 24), start=START, big_event_only=True,
         note="Long gaps, only shows up for finals. Tests the 'Big today' rail."),
    dict(uid="SYN_U08", label="New user (cold start)", platform="SB iOS",
         vertical="sports", sports={"Nogomet": 0.7, "Košarka": 0.2, "Tenis": 0.1},
         comps=["HNL"], slip="SOLO",
         stake=(5, 12), abandon=0.50, friction=0.30, live_bias=0.20,
         sessions_wk=4.0, hours=(16, 23),
         start=dt.datetime(2026, 7, 15),
         note="Only 6 weeks of history, no settled habit. Tests the no-history path."),
]


def fake_player_id(uid):
    """Obviously-synthetic ID that still looks like the real hashed format."""
    h = hashlib.sha256(f"FEG-SYNTHETIC-{uid}-{SEED}".encode()).hexdigest()
    return f"{uid}_{h[:56]}"


def pick(weights):
    keys = list(weights)
    return random.choices(keys, weights=[weights[k] for k in keys])[0]


rows = []
sess_counter = 1700000000
slip_counter = 500000


def emit(**kw):
    r = {c: None for c in REAL_COLS + SYNTH_COLS}
    r["is_synthetic"] = True
    r.update(kw)
    rows.append(r)


def sports_session(p, ts, sid, pid):
    """One sportsbook session. Returns nothing; appends rows."""
    global slip_counter
    plat = p["platform"]
    sport = pick(p["sports"])
    comp = random.choice([c for c in p["comps"] if c in COMPETITIONS[sport]]
                         or COMPETITIONS[sport])
    live = random.random() < p.get("live_bias", 0.15)
    t = ts

    def ev(name, **kw):
        nonlocal t
        t += dt.timedelta(seconds=random.randint(4, 70))
        emit(event_name=name, session=sid, timestamp=t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
             platform=plat, PlayerID=pid, **kw)

    def screen(nm):
        ev("fortuna_screen_view" if plat.startswith("SB") else "screen_view",
           fortuna_screen_name=nm)

    screen("homepage")
    for _ in range(random.randint(1, 3)):
        screen(random.choice(SB_SCREENS_BROWSE))

    detail = "liveDetail" if live else "prematchDetail"
    screen(detail)

    # friction pattern: bounce back and forth between list and detail
    if random.random() < p.get("friction", 0.18):
        for _ in range(random.randint(2, 4)):
            screen("prematchMatchesOverview")
            screen(detail)

    if random.random() >= 0.55:          # never reached the betslip
        return

    # ---- build the slip: either rebuild an abandoned one, or start fresh ----
    resumed = take_pending(pid, t) if random.random() < RESUME_P else None
    if resumed:
        sport, comp = resumed["sport"], resumed["comp"]
        fixture_id = resumed["fixture_id"]
        kickoff = resumed["kickoff"]
        sels = resumed["selections"]
        slip_type = resumed["betslip_type"]
    else:
        fx = fixture_for(sport, comp, t)
        fixture_id, kickoff = fx["fixture_id"], fx["kickoff"]
        slip_type = p["slip"]
        n_legs = {"AKO": random.randint(3, 6), "LEG_COMBI": random.randint(2, 4),
                  "SOLO": 1}[slip_type]
        n_legs = min(n_legs, len(fx["selections"]))
        sels = random.sample(fx["selections"], n_legs)

    slip_counter += 1
    slip_no = slip_counter

    for sel in sels:
        ev("betslip_add_bet",
           added_from="live_page" if live else "match_detail_odds",
           sport_name=sport, on_origin_name=comp,
           fixture_id=fixture_id, selection_id=sel,
           betslip_number=slip_no, betslip_type=slip_type,
           odds=odds_at(sel, t))
    screen("betslip")

    if random.random() < p["abandon"]:
        # Left at the final step. Remember it, so a later session can rebuild
        # the same selections and the price change becomes observable.
        remember_pending(pid, dict(sport=sport, comp=comp, fixture_id=fixture_id,
                                   kickoff=kickoff, selections=sels,
                                   betslip_type=slip_type, abandoned_at=t))
        screen(detail)
        if random.random() < 0.5:
            screen("homepage")
        return

    stake = round(random.uniform(*p["stake"]), 2)
    for sel in sels:
        ev("betslip_placed_bet", sport_name=sport, on_origin_name=comp,
           betslip_number=slip_no, betslip_type=slip_type,
           fixture_id=fixture_id, selection_id=sel,
           stake_eur=stake, odds=odds_at(sel, t))
    ev("betslip_placed", status="ACCEPTED" if random.random() > 0.02 else "REJECTED",
       betslip_number=slip_no, betslip_type=slip_type,
       sport_name=sport, on_origin_name=comp, stake_eur=stake,
       fixture_id=fixture_id,
       added_from="live_page" if live else "match_detail_odds")
    screen("ticketDetail")
    if random.random() < 0.5:
        screen("ticketHistory")


def casino_session(p, ts, sid, pid, force_platform=None):
    plat = force_platform or p["platform"]
    pool = TABLE_GAMES if "Evolution" in p.get("providers", []) else SLOTS
    provider = random.choice([x for x in p["providers"] if x in pool] or list(pool))
    t = ts

    def ev(name, **kw):
        nonlocal t
        t += dt.timedelta(seconds=random.randint(5, 120))
        emit(event_name=name, session=sid,
             timestamp=t.strftime("%Y-%m-%d %H:%M:%S.%f") + " UTC",
             platform=plat, PlayerID=pid, **kw)

    prev = "https://www.psk.hr/"
    for _ in range(random.randint(2, 5)):
        page = random.choice(CASINO_PAGES)
        ev("page_view", page_location=page, page_referrer=prev)
        prev = page

    games = pool[provider]
    favourite = games[0]
    for _ in range(random.randint(1, 4)):
        g = favourite if random.random() < 0.6 else random.choice(games)
        ev("casino_game_launch", game_name=g, provider=provider, demo=False,
           jackpot=random.random() < 0.1,
           on_origin="casino", from_origin="lobby",
           stake_eur=round(random.uniform(*p["stake"]), 2))


for p in PERSONAS:
    pid = fake_player_id(p["uid"])
    day = p["start"]
    while day <= END:
        week_sessions = max(0, int(random.gauss(p["sessions_wk"], p["sessions_wk"] * 0.35)))
        if p.get("big_event_only"):
            near_big = any(abs((day.date() + dt.timedelta(days=d) - b).days) <= 1
                           for b in BIG_EVENT_DAYS for d in range(7))
            week_sessions = random.randint(3, 6) if near_big else random.randint(0, 1)
        for _ in range(week_sessions):
            offset = dt.timedelta(days=random.randint(0, 6),
                                  hours=random.randint(*p["hours"]) % 24,
                                  minutes=random.randint(0, 59))
            ts = day + offset
            if ts > END:
                continue
            sess_counter += random.randint(50, 900)
            sid = sess_counter
            v = p["vertical"]
            if v == "sports":
                sports_session(p, ts, sid, pid)
            elif v == "casino":
                casino_session(p, ts, sid, pid)
            else:
                # hybrid: same person, but each session is one vertical only
                if random.random() < 0.65:
                    sports_session(p, ts, sid, pid)
                else:
                    casino_session(p, ts, sid, pid, force_platform="GM")
        day += dt.timedelta(days=7)

df = pd.DataFrame(rows)[REAL_COLS + SYNTH_COLS]
df = df.sort_values("timestamp").reset_index(drop=True)

# ------------------------------------------------------------ ground truth
profiles = []
for p in PERSONAS:
    pid = fake_player_id(p["uid"])
    d = df[df.PlayerID == pid]
    sess = d.session.nunique()
    adds = d[d.event_name == "betslip_add_bet"].session.nunique()
    plcd = d[d.event_name == "betslip_placed"].session.nunique()
    top_sport = (d.sport_name.value_counts().idxmax()
                 if d.sport_name.notna().any() else "")
    top_comp = (d.on_origin_name.value_counts().idxmax()
                if d.on_origin_name.notna().any() else "")
    top_game = (d.game_name.value_counts().idxmax()
                if d.game_name.notna().any() else "")
    stakes = d.stake_eur.dropna()
    profiles.append(dict(
        player_key=p["uid"], persona=p["label"], PlayerID=pid,
        platform=p["platform"], vertical=p["vertical"],
        first_seen=d.timestamp.min(), last_seen=d.timestamp.max(),
        sessions=sess, events=len(d),
        top_sport=top_sport, top_competition=top_comp, top_game=top_game,
        usual_betslip_type=p.get("slip", ""),
        usual_stake_eur=round(stakes.median(), 2) if len(stakes) else "",
        sessions_reaching_betslip=adds,
        sessions_completing=plcd,
        final_step_abandon_pct=(round(100 * (1 - plcd / adds), 1) if adds else ""),
        demo_note=p["note"],
    ))
prof = pd.DataFrame(profiles)

OUT_DIR.mkdir(parents=True, exist_ok=True)
df.to_csv(OUT_DIR / "synthetic_player_event_logs.csv", index=False)
prof.to_csv(OUT_DIR / "synthetic_user_profiles.csv", index=False)
with pd.ExcelWriter(OUT_DIR / "synthetic_player_event_logs.xlsx", engine="openpyxl") as w:
    df.to_excel(w, sheet_name="synthetic_event_logs", index=False)
    prof.to_excel(w, sheet_name="user_profiles_groundtruth", index=False)

print(df.shape)
print(prof.to_string(index=False))

"""
FEG Hackathon 2026 - Synthetic player event-log generator
=========================================================
Generates SYNTHETIC ONLY data. No real player data, no real IDs.
Schema is identical to top_casino_users_event_logs.xlsx (24 columns),
plus 3 clearly-marked synthetic-only columns at the end.

Reproducible: fixed seed.
"""

import random
import hashlib
import datetime as dt
import pandas as pd

SEED = 20260908
random.seed(SEED)

START = dt.datetime(2023, 9, 1)
END = dt.datetime(2026, 8, 31)

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

# ---------------------------------------------------------------- personas

PERSONAS = [
    dict(uid="SYN_U01", label="Football loyalist",
         platform="SB Android", vertical="sports",
         sports={"Nogomet": 0.96, "Tenis": 0.04},
         comps=["HNL", "Liga prvaka", "Premier League"],
         slip="AKO", stake=(8, 25), sessions_wk=6, abandon=0.12,
         hours=(18, 23), start=START,
         note="Deep single-sport habit. Proves sport-level (not category-level) recommendation."),

    dict(uid="SYN_U02", label="Tennis live specialist",
         platform="SB iOS", vertical="sports",
         sports={"Tenis": 0.91, "Nogomet": 0.09},
         comps=["ATP Masters", "Grand Slam", "WTA 1000"],
         slip="SOLO", stake=(15, 60), sessions_wk=5, abandon=0.22,
         hours=(11, 19), start=START, live_bias=0.55,
         note="Live-heavy, single-selection. Different habit shape from U01."),

    dict(uid="SYN_U03", label="Basketball high-abandoner",
         platform="SB Android", vertical="sports",
         sports={"Košarka": 0.82, "Nogomet": 0.18},
         comps=["ABA Liga", "Euroliga", "NBA"],
         slip="LEG_COMBI", stake=(5, 15), sessions_wk=7, abandon=0.41,
         hours=(20, 24), start=START, friction=0.5,
         note="THE demo user. Reaches betslip and leaves 41% of the time."),

    dict(uid="SYN_U04", label="Slots regular",
         platform="GM", vertical="casino",
         providers=["Greentube", "Amusnet"],
         sessions_wk=8, hours=(21, 24), start=START, stake=(1, 4),
         note="One provider family. Proves casino side of the same engine."),

    dict(uid="SYN_U05", label="Hybrid sports + casino",
         platform="SB Android", vertical="hybrid",
         sports={"Nogomet": 0.88, "Košarka": 0.12},
         comps=["HNL", "Premier League"],
         providers=["Pragmatic Play", "Greentube"],
         slip="AKO", stake=(10, 30), sessions_wk=7, abandon=0.24,
         hours=(17, 23), start=START,
         note="Uses both products but NEVER in one session. The cross-vertical gap."),

    dict(uid="SYN_U06", label="Live table games",
         platform="Casino Android", vertical="casino",
         providers=["Evolution"],
         sessions_wk=5, hours=(22, 24), start=START, stake=(5, 20),
         note="Table games, not slots. Tests that casino recs are not one-size-fits-all."),

    dict(uid="SYN_U07", label="Big-event only",
         platform="web", vertical="sports",
         sports={"Nogomet": 1.0},
         comps=["Liga prvaka", "Premier League"],
         slip="SOLO", stake=(20, 80), sessions_wk=1, abandon=0.30,
         hours=(19, 23), start=START, big_event_only=True,
         note="Long gaps, only shows up for finals. Tests the 'Big today' rail."),

    dict(uid="SYN_U08", label="New user (cold start)",
         platform="SB iOS", vertical="sports",
         sports={"Nogomet": 0.6, "Tenis": 0.25, "Košarka": 0.15},
         comps=["HNL", "ATP 250"],
         slip="SOLO", stake=(5, 10), sessions_wk=4, abandon=0.45,
         hours=(12, 22), start=dt.datetime(2026, 7, 15),
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

    fixture = random.randint(3000000, 3999999)
    n_legs = {"AKO": random.randint(3, 6), "LEG_COMBI": random.randint(2, 4),
              "SOLO": 1}[p["slip"]]
    slip_no = None
    added = False

    if random.random() < 0.55:                      # reached the betslip
        global slip_counter
        slip_counter += 1
        slip_no = slip_counter
        for _ in range(n_legs):
            ev("betslip_add_bet",
               added_from="live_page" if live else "match_detail_odds",
               sport_name=sport, on_origin_name=comp,
               fixture_id=fixture + random.randint(0, 40),
               selection_id=random.randint(70000000, 79999999),
               betslip_number=slip_no, betslip_type=p["slip"],
               odds=round(random.uniform(1.25, 4.2), 2))
        added = True
        screen("betslip")

    if added:
        if random.random() < p["abandon"]:
            # left at the final step: back out, sometimes leave entirely
            screen(detail)
            if random.random() < 0.5:
                screen("homepage")
        else:
            stake = round(random.uniform(*p["stake"]), 2)
            for _ in range(n_legs):
                ev("betslip_placed_bet", sport_name=sport, on_origin_name=comp,
                   betslip_number=slip_no, betslip_type=p["slip"],
                   fixture_id=fixture, selection_id=random.randint(70000000, 79999999),
                   stake_eur=stake)
            ev("betslip_placed", status="ACCEPTED" if random.random() > 0.02 else "REJECTED",
               betslip_number=slip_no, betslip_type=p["slip"],
               sport_name=sport, on_origin_name=comp, stake_eur=stake,
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

out_x = "/mnt/user-data/outputs/synthetic_player_event_logs.xlsx"
out_c = "/mnt/user-data/outputs/synthetic_player_event_logs.csv"
out_p = "/mnt/user-data/outputs/synthetic_user_profiles.csv"

df.to_csv(out_c, index=False)
prof.to_csv(out_p, index=False)
with pd.ExcelWriter(out_x, engine="openpyxl") as w:
    df.to_excel(w, sheet_name="synthetic_event_logs", index=False)
    prof.to_excel(w, sheet_name="user_profiles_groundtruth", index=False)

print(df.shape)
print(prof.to_string(index=False))

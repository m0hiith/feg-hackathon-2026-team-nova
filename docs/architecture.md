# Architecture

SessionSense is one system with two halves that run on two different datasets,
for one unavoidable reason: **FEG's provided event log contains no stake,
balance, or monetary value at all.** You cannot build or demonstrate a
money-aware response layer on data that has no money in it. So the system
splits cleanly along that line —

- **`detection/`** answers *"is this session stalling, right now, with no
  look-ahead?"* It runs on FEG's real, provided event log (332,119 events, 89
  players, brand `hr`, Aug 2026) and is validated against it end to end.
- **`response/`** answers *"given a stalled or friction-heavy session, what is
  the minimum useful thing to say, or is silence the right call?"* It runs on
  an 8-persona synthetic dataset (44,261 events, `is_synthetic=True`
  throughout) built specifically to carry the stake/odds fields FEG's export
  does not have, so the honest-disclosure and pre-fill logic can be built and
  tested at all.

They are not wired together at runtime today — see §4. Both are rule-based,
both refuse to guess, and both treat *not* intervening as a first-class,
counted, named output rather than an absence. `docs/compliance-note.md`
covers why that matters; this document covers how each half is built and how
they would meet in production.

---

## 1. `detection/` — when to intervene

_Every claim below is verifiable by running the named file in
`detection/`._

```
  Kafka topic          session state        incremental          trigger            intervention        outcome log
  (player events)  ->  (keyed by       ->  features        ->  (rules,        ->  (inline panel,  ->  (decision +
                        PlayerID,           one event in,       explainable)       dismissible)        reason +
                        session)            all features                                               what happened)
                                            so far out
```

| stage | module | what it does |
|---|---|---|
| Kafka topic | `src/replay.py` (`EventConsumer`) | One message = one player event. Partitioned on `PlayerID`. |
| Session state | `src/features.py` (`StateBackend`) | Flat key `feat:{PlayerID}:{sid}`, JSON values, TTL slid on every write. |
| Incremental features | `src/features.py` (`FeatureAccumulator.update`) | One event in, all features so far out. Never buffers, never looks ahead. |
| Trigger | `src/trigger.py` (`SlipRescueTrigger.evaluate`) | One feature snapshot in, one `Decision` out. Always returns; silence is a value. |
| Intervention | `src/ui/index.html` | Inline panel, never a modal, always dismissible. Three states only. |
| Outcome log | `Decision` object | Action, reason, and the full evidence dict the decision was made on. |

The service loop is identical on a replay and on a live Kafka topic:

```python
for record in consumer:                 # ReplayConsumer *or* KafkaConsumer
    features = accumulator.update(record.value)
    decision = trigger.evaluate(features)
    if decision.action is FIRE:
        emit(decision)
```

**The trigger is rule-based, not learned.** All eight tuning constants
(`MIN_ADDS_TO_ARM`, `STALL_DWELL_S`, `VELOCITY_COLLAPSE_RATIO`, …) live in one
block at the top of `src/trigger.py`; nothing below it reads a magic number.
Measured over all 1,649 slip-building sessions: `FIRE` on 533 (0.27%),
`STAY_SILENT` on 194,178 (99.73%) across six named reasons. See
`docs/compliance-note.md` §3 for the full breakdown.

### No-look-ahead, structurally enforced

1. `ReplayConsumer.__len__` raises — nothing can ask how long a session will
   be.
2. Every branch in `FeatureAccumulator.update()` is a function of `(state so
   far, this event)` only.
3. **`_test_monotonic_prefix()`** in `src/features.py` replays each of 27 test
   sessions twice — once truncated to the first N events, once in full — and
   asserts the feature dict at event N is byte-identical both times. If any
   feature could see the future, the truncated run would disagree.
   `python3 src/features.py` → *"OK no-look-ahead: 135 prefix/full comparisons
   across 27 sessions — features at event N are identical whether or not
   later events exist."*

### The two seams to production

Everything that changes going from a laptop to FEG's live stack is isolated
behind two `Protocol` interfaces:

| seam | file | production swap |
|---|---|---|
| `EventConsumer` | `src/replay.py` | `ReplayConsumer` already implements the `KafkaConsumer` surface (`__iter__`, `poll()`, `close()`). Swap the consumer construction; the service loop is unchanged. |
| `StateBackend` | `src/features.py` | Exactly `get`/`set`/`expire`/`delete` — the dict/Redis intersection, deliberately with no `scan` or `keys()`. `JsonRoundTripBackend` proves every state value survives a JSON round trip, i.e. is Redis-serialisable, with no call-site change. |

Full mapping to FEG's stack (Kafka, Redis, PostgreSQL, Vue) is in
`detection/docs/architecture.md` §3, kept alongside the detection code as the
detailed technical reference this summary draws from.

---

## 2. `response/` — what to say, or whether to say anything

_Every claim below is verifiable by running the named file in `response/`._

```
  event log            intent replay        friction +           habit                next best         rendered card
  (session,        ->  (rule states,   ->  cold-start       ->  profile          ->  help          ->  (or NO_ACTION,
   ts-ordered)          no look-ahead)      signals)             (own history         (one decision       named + counted)
                                                                  only, leakage        per session)
                                                                  guarded)
```

| stage | module | what it does |
|---|---|---|
| Data load | `src/data_loader.py` | Parses both timestamp dialects (sportsbook ISO-Z, casino space+UTC) into one UTC series; loads the synthetic event log and the ground-truth profile CSV. |
| Intent replay | `src/intent.py` (`replay`) | Rule chain over events 0..N → one `IntentState` per event: `BROWSING / SEARCHING / COMPARING / HIGH_INTENT / STUCK / ABANDONING / DONE`. Every state carries a named rule, human-readable reasons, and evidence (event sequence numbers). |
| Friction detection | `src/friction.py` (`detect`) | A layer over `intent.py`, not a second copy: `NAV_LOOP`, `BETSLIP_EXIT`, `LONG_DWELL` are read back from intent's own rule names; `DEEP_PATH` and `COLD_START` are new. Each kind fires at most once per session. |
| Habit profile | `src/profiler.py` (`build_profile`) | What this player reliably does, derived from *their own* history only — top sport/competition/slip-type/stake for sportsbook, lift-over-uniform concentration for casino (a literal 0.60 share gate is wrong for a slots player drawing from dozens of games). Produces a `confidence` score and band. |
| Routing | `src/next_best_help.py` (`HelpEngine.decide`) | At most one `HelpDecision` per session: `CONTINUE_CARD` (hand back an abandoned slip), `CLARIFY_INFO` (navigation help, never a bet prompt), `RESUME_GAME` (casino equivalent), or `NO_ACTION` — the default, returned far more often than any intervention. |
| Odds disclosure | `src/odds_movement.py` (`scan_session`, `render_card`) | When a player returns to a selection they slipped before, discloses the price move — worse or better — in one symmetric sentence structure. Never filters on sign. |
| Site export | `tools/export_site_data.py` | Runs the real engine over all 8 synthetic players' every session and writes `site_data.json` — every card, stake, confidence score and census number the mock site renders is engine output, nothing hand-written. |
| Site template | `Image 2.html` | The working, editable source of the mock site's markup, layout and Tailwind config — everything about the page *except* the artifact-publishing transforms in the row below. This is the file to hand-edit for any layout/copy change; `feg-session-engine.html` is generated from it, not the other way around. |
| Publish build | `tools/build_publish.py` | Reads `Image 2.html`, strips the transforms only needed for an artifact-hosting sandbox (`<!doctype>`/`<html>`/`<head>`/`<body>` wrappers), inlines the Font Awesome subset from `tools/fa-inline.css`, and writes the result to `feg-session-engine.html`. Re-run this after any edit to `Image 2.html` to keep the shipped file in sync — it is a straight rebuild, so an edit made only to `feg-session-engine.html` and not to `Image 2.html` will be silently overwritten the next time this runs. |
| Demo surface | `feg-session-engine.html` | FEG-branded mock site, generated (see row above). Reads `site_data.json` (embedded as `<script id="feg-data">`) client-side to render the engine's own decisions. |

### Why two rule engines, `intent.py` and `friction.py`, instead of one

`intent.py` decides *what the player is doing* (a state machine over the
session). `friction.py` decides *whether that's worth interrupting for* (a
smaller set of named signals, three of which are read straight back off
`intent.py`'s own rule names — `circling → NAV_LOOP`, `left_open_slip →
BETSLIP_EXIT`, `dwelling → LONG_DWELL` — precisely so the two files never
carry two independent definitions of the same thing that could drift apart).
`next_best_help.py` then reads friction signals plus a habit profile and
routes to at most one decision. Splitting *state* from *friction* from
*routing* is what lets `NO_ACTION` be the default at every layer: a friction
signal firing does not itself imply an intervention (see `COLD_START` below).

### No-look-ahead, structurally enforced

State at event N is computed from events 0..N only and is never revised.
**`_test_prefix_stability()`** in `src/intent.py` proves it: it replays the
first *k* events of a session and asserts the result is byte-identical to the
first *k* states of a full replay, for every *k* from 1 to the session
length. `python3 src/intent.py` → *"OK no look-ahead: 1,446 truncated replays
over 160 sessions, 0 disagreed with the full replay."*
`next_best_help.py`'s own routing walk
(`HelpEngine.decide`) applies the same discipline at the decision layer: it
walks events forward and the *first* route that qualifies wins and ends the
walk — it is never re-decided after seeing how the session ends. The
measured cost of that honesty is reported directly: sessions that circle
first and abandon later spend their one intervention on `CLARIFY_INFO` and
never see a `CONTINUE_CARD`, because live there is no way to know the
abandonment is coming yet.

### The profiler's leakage guard: `exclude_session`

`build_profile(events, player_id, exclude_session=sid)` drops session `sid`
from the player's history **before any statistic is computed.** A profile
used to decide inside session N must be built only from sessions 1..N-1;
without this, every offline evaluation would be scoring a decision against a
session it had already been shown. `python3 src/profiler.py`'s validation
suite proves both halves of the guard on SYN_U03: excluding session
`1701102001` drops that session's own events from the profile (9,154 → 9,140
events considered) while leaving the derived habit unchanged (top sport
Košarka either way) — the guard removes exactly the withheld session's
evidence and nothing else moves. `next_best_help.py`'s own test suite
(`_gate_is_stable`) additionally proves the confidence gate at 0.40 is
*stable* under this exclusion — for every player, holding out any single
session never flips them across the pre-fill confidence threshold — which is
what makes it safe to compute one profile per player for the corpus census
instead of rebuilding it per session (~5,000 full-history passes).

### Constraint tests run on every corpus pass

`python3 src/next_best_help.py` runs the full routing census plus a
constraint suite, including: max one intervention per session; cooldown
suppresses help after a dismissal and expires after 72h; confidence below
0.40 yields only `CLARIFY_INFO` or `NO_ACTION` (never a pre-filled stake); no
`BANNED_PHRASES` urgency word in any rendered string, scanned across every
line the engine can produce; a clean converting session gets `NO_ACTION`;
`NO_ACTION` fires on at least 40% of sessions. See `docs/compliance-note.md`
§4 for what these constraints are protecting against.

---

## 3. Data flow into the demo surface

```
synthetic event log ──▶ src/*.py (intent → friction → profiler → next_best_help / odds_movement)
                                          │
                                          ▼
                          tools/export_site_data.py
                                          │
                                          ▼
                                site_data.json  ──▶  <script id="feg-data"> in feg-session-engine.html
                                                                │
                                                                ▼
                                              client-side JS renders cards, stats, and
                                              census numbers straight from that JSON —
                                              nothing on the engine-driven parts of the
                                              page is hand-authored copy
```

**Not everything on the mock page is engine output.** The page also carries
static marketing chrome — promotional tiles, odds tickers on unrelated
fixtures, layout elements typical of a sportsbook home screen — that the
engine never ranks, personalises, or targets; it exists to make the page read
as a real product surface, not as evidence of what SessionSense decided. Two
such static elements (an "ALL IN" stake shortcut and a "🔥 364+ BETS" urgency
badge) were removed from `feg-session-engine.html` during this submission
pass precisely *because* they read as pressure mechanics adjacent to
engine-driven content, even though the engine never generated or touched
them — see `docs/compliance-note.md` §7.

---

## 4. How the two halves relate, and the integration point

**They are not wired together today, and that is a data limitation, not a
design gap.** `detection/`'s trigger fires on FEG's real sessions and knows
nothing about stake or odds, because the real log has neither. `response/`'s
engine knows how to answer "what should we say" but has never seen FEG's real
users, because it needs synthetic data to have money and odds fields to work
with at all.

In production the two meet at one point: **`detection/src/trigger.py`'s
`evaluate()` returning `FIRE` is the signal that should invoke
`response/src/next_best_help.py`'s `HelpEngine.decide()`** for that same
session — detection says *when*, response (rebuilt against FEG's real,
NDA'd stake data once available) says *what*. Nothing in either module's
interface obstructs this: both take a session-scoped dataframe/feature
snapshot and both return a small, evidenced decision object. Building that
join is explicitly out of scope for this challenge (there is no dataset that
would let it be tested honestly — see `docs/impact-case.md` §5), but the
seam is named rather than hidden.

---

## 5. Deployment assumptions

| FEG component | how this project uses it | change required |
|---|---|---|
| **Kafka** | `ReplayConsumer` already implements the `KafkaConsumer` surface. | Swap the consumer construction. The service loop is unchanged. |
| **Redis** | `StateBackend` is the dict/Redis intersection; `JsonRoundTripBackend` proves the values serialise. | Write a `RedisStateBackend` with the four methods. No call-site changes. |
| **PostgreSQL** | Destination for the outcome log — one row per `Decision` (action, reason, evidence, timestamps); `response/`'s `HelpDecision` is the same shape. | Add a writer. The `Decision`/`HelpDecision` object is already the row. |
| **Python** | Both halves are Python 3, standard library plus a small, permissively-licensed set (see `docs/dependencies.md`). | None. |
| **Vue** | The detection demo UI (`detection/src/ui/index.html`) and the response demo site (`response/feg-session-engine.html`) are both self-contained HTML — decision/data viewers, not production components, deliberately. | Reimplement the panel states / cards as Vue components. `detection/scripts/check_ui.py`'s panel-contract assertions are the spec to port for the detection side. |
| **Croatia self-exclusion register** | Acknowledged, not implemented — see `docs/compliance-note.md` §4.3. | Register check belongs in front of the intervention emit step, after `trigger`/`next_best_help` decides, before anything renders. |

Nothing here depends on a retired FEG component. There is no model anywhere
in the production path in either half — no training, no weights, no
inference — see `docs/compliance-note.md` §3 for why that is a deliberate
choice on a gambling product, not a shortcut.

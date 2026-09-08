# Compliance note

_Every number on this page is reproducible from a script named on the same
line. `detection/docs/compliance-note.md` carries the full detection-half
detail this page summarises; this page is the unified, both-halves account._

---

## 1. Data — synthetic where it must be, real where it can be

**`response/` is synthetic-only, and says so structurally, not just in
prose.** Every row of `response/data/synthetic_player_event_logs.csv` (44,261
events, 8 players, Sept 2023 → Aug 2026) carries `is_synthetic=True`. Every
`PlayerID` is prefixed `SYN_Uxx` — there is no ambiguity about which id space
a row belongs to, on sight, without a lookup. The generator
(`response/tools/generate_synthetic_users.py`, seed `20260908`) is committed,
so the dataset is regenerable, not a static asset someone has to trust
blindly. No names, emails, ages, locations, payment instruments, card data,
IP addresses or device identifiers exist anywhere in it (`response/files/
synthetic_data_README.md`).

**This is a necessity, not a preference.** FEG's real event log has no
stake, odds, or balance field of any kind (§5 below, and
`docs/impact-case.md` §5). A response layer that pre-fills a stake or
discloses an odds move cannot be built, let alone tested for the
honest-disclosure property in §6, against data that cannot express a stake or
an odds move. Three synthetic-only columns (`stake_eur`, `odds`,
`is_synthetic`) are appended on top of FEG's own 24-column schema and are
disclosed as such in `docs/dependencies.md` §3, so no reviewer mistakes them
for fields FEG's export actually has.

**`detection/` runs on FEG's real, provided sample** (332,119 events, 89
players, brand `hr`, Aug 2026) and is sanitised before any other code reads
it. This was a real finding, not a precaution: `page_location` and
`page_referrer` carried live `username=` and `temptoken=` credentials in
their query strings. `detection/scripts/sanitize.py` strips them, asserts
zero remain in any column — both in memory and re-read from the written
parquet on disk — and only then writes `detection/data/clean/events.parquet`,
the only file anything downstream reads. Full measurement:
`detection/docs/compliance-note.md` §1. **The raw delivered files are never
committed to this repository** — see `.gitignore` and the note at the top of
this repo's README.

**Ids shown on screen are masked in both halves.** `detection/scripts/
build_ui.py` refuses to write its UI if a raw `PlayerID` or unmasked betslip
number appears anywhere in the payload; `detection/scripts/check_ui.py`
re-checks the rendered page for any 64-hex string. `response/`'s masked ids
(`SYN_U03_ce30d610…`) are synthetic by construction and never resolve to a
real identity.

---

## 2. GDPR — Article 25, data protection by design and by default

**Data minimisation is structural, not a policy statement, in both halves.**

| category | detection/ (real data) | response/ (synthetic data) |
|---|---|---|
| Names, usernames | not used — stripped at ingest | never present |
| Age, date of birth | not used — not in the pipeline | never present |
| Location, IP address | not used — none found in any scanned column | never present |
| Payment instruments, balance, deposits | not used — not in the event log | never present |
| Stake / monetary value | not used — not in the real log at all | present, but on synthetic, disclosed, non-real players only |
| Cross-session profiling | not performed (below) | performed **within one player's own history only**, never across players |
| Special-category data | not used | not used |

**Detection has no cross-session profiling at all.** Session state is keyed
`feat:{PlayerID}:{sid}` — scoped to one session, with a 4-hour TTL slid on
every write. `FIRE_ONCE_PER_SESSION` is enforced by a per-session key, not a
per-player record.

**Response's profiler is explicitly a within-player statistic, and its
inputs are named and bounded.** `build_profile()`'s only inputs are a
player's own historical events — sport, competition, slip type, stake,
game, and timestamp — nothing external, nothing cross-player. Recency
weighting (events inside 90 days count 3× as much) means the profile decays
toward a player's *current* habit rather than accumulating a permanent
dossier. The `exclude_session` leakage guard (`docs/architecture.md` §2) is
also, incidentally, a minimisation property: the profile used inside a
session is never built from that session's own data, so the amount of
information any single session's outcome depends on is strictly bounded to
what came before it.

**Pseudonymisation is preserved end to end.** `PlayerID` selects rows and
partitions the stream, then is dropped before anything reaches a screen.

---

## 3. EU AI Act

**Classification: not a high-risk system, in either half.** Both are
**rule-based** — no model, no training, no learned weights, no inference. All
tuning constants for both engines live in one `CONFIG` block at the top of
their respective files (`detection/src/trigger.py`;
`response/src/next_best_help.py`, `response/src/intent.py`,
`response/src/friction.py`, `response/src/profiler.py`) and are readable by a
non-engineer. Both are **limited-risk**, **explainable by construction**, and
**non-manipulative**.

**Traceability is structural, not a log line bolted on after the fact.**
Every decision object in this system — `Decision` in detection,
`HelpDecision`/`IntentState`/`FrictionSignal` in response — carries a named
**rule** and a tuple of human-readable **reasons**, and every response-side
object additionally carries **evidence**: the exact event sequence numbers a
reviewer can go back and read. This is not a summary of the decision; it is
a pointer at the rows that produced it. `response/src/intent.py`'s own
docstring states the AI Act framing directly: *"an operator has to be able to
say why a system treated a user the way it did; here the answer is always a
rule name plus the exact event rows that fired it."*

**Evidence the intervention is not manipulative:**

- **Symmetric rendering.** A detection-side price movement (were one ever
  shown) and a response-side odds-movement card both render worse and better
  outcomes with identical weight, colour, and sentence structure — see §6.
  This is asserted mechanically, not just written down.
- **Silence is an implemented, counted output, not an absence.** See §4 —
  the decision *not* to intervene carries the same rule/reasons/evidence
  shape as any other decision in both halves, and both engines report how
  often and why they stayed quiet.

---

## 4. Responsible gambling

### 4.1 Silence is the default, and it is measured, not assumed

**Detection**, over all 1,649 slip-building real sessions
(`python3 detection/src/trigger.py`):

| | decisions | share |
|---|---|---|
| `FIRE` (`stalled_holding_slip`) | 533 | 0.27% |
| `STAY_SILENT` | 194,178 | **99.73%** |

Six named silent reasons, the largest being `already_converted` (94,124 —
*"the bet is placed; nudging someone who just bet is the worst thing this
system could do"*).

**Response**, over all 5,237 synthetic sessions
(`python3 response/src/next_best_help.py`):

| action | sessions | share |
|---|---|---|
| `CONTINUE_CARD` | 335 | 6.4% |
| `CLARIFY_INFO` | 1,004 | 19.2% |
| `RESUME_GAME` | 0* | 0.0% |
| **`NO_ACTION`** | **3,898** | **74.4%** |

*RESUME_GAME never fires on an observed session because every casino session
in this dataset launches a game — it is exercised only on a constructed
browse-without-launch session, and is verified **not** to fire on the 5,237
real (synthetic) sessions where browsing 2–5 lobby pages before launching is
the healthy path, not friction.

`NO_ACTION` share **per player** ranges from 49.2% (SYN_U03, the deliberately
high-abandonment demo user) to 91.9% (SYN_U06, a clean-converting table-games
player) — a **40–92% band**, with a hard test gate requiring at least 40% on
the corpus overall (`response/src/next_best_help.py`: *"NO_ACTION fires on at
least 40% of sessions"*, passing at 74.4%). The four named reasons the
response engine stayed silent: `casino_converted` (1,875, 35.8% — a game
already launched cleanly), `no_qualifying_friction` (1,059, 20.2%),
`converted_cleanly` (957, 18.3% — **response's `STAY_SILENT`/`already_
converted` equivalent**: a slip already placed, so nudging would be the wrong
move), `cold_start_no_prefill` (7, 0.1% — see §5).

### 4.2 No pressure mechanics — asserted, not merely intended

**Detection's panel** is checked mechanically by
`detection/scripts/check_ui.py` (91 assertions, run twice, in a real
browser): no urgency wording (`hurry`, `expires`, `last chance`, `act now`,
`limited time`, `ending soon`), no countdown, no percentage, no
recommendation or pre-selected default, no modal/interstitial (no `dialog` or
`role="dialog"` anywhere — the panel is `position: static`, in normal page
flow), no placement over a confirm button (**structurally impossible** — the
panel is inline in its own column, not overlaid), no flashing motion, no
push notification.

**Response's every rendered string** — every card, every clarify prompt,
every odds disclosure line the engine can produce — is scanned against
`BANNED_PHRASES = ("now", "hurry", "last chance", "ending soon", "don't
miss", "dont miss", "limited")`, matched as whole words so "known" cannot
trip "now". `python3 response/src/next_best_help.py`: *"no banned urgency
phrase in any rendered string — 4,017 rendered lines scanned … 0
offender(s)."*

**A dismissal is respected for 72 hours.** `response/src/next_best_help.py`'s
`HelpEngine.cooldown_hours = 72.0` suppresses further help for a player after
they dismiss a card, verified directly: *"CONTINUE_CARD -> dismissed ->
NO_ACTION (cooldown)"*, then *"after 72h: CONTINUE_CARD"* — the cooldown both
suppresses and expires on schedule, checked in the same test run.

**Inline only, never a modal, never over a confirm button — in both
halves.** Detection's panel is asserted `position: static`. Response's cards
are rendered as page content pulled from `site_data.json`, not as an overlay
or interrupting dialog; nothing in either engine's contract allows a card to
render on top of, or in place of, a confirm/place-bet control.

### 4.3 Cold start refuses to guess

**Response**, `response/src/next_best_help.py`, `cold_start_no_prefill`
rule: when an open slip is abandoned but the player's profile confidence is
below 0.40, the engine returns `NO_ACTION` with the reason *"pre-filling a
stake we cannot justify would be a guess"* — it does not fall back to a
generic default stake. Measured directly on SYN_U08 (6 weeks of history,
confidence 0.350): `python3 response/src/friction.py` and `python3
response/src/next_best_help.py` both confirm this player produces **only**
`CLARIFY_INFO` or `NO_ACTION` — never a pre-filled `CONTINUE_CARD` — across
every one of their 30 sessions. The 0.40 threshold itself is deliberately set
*above* the profiler's own `gate_ceiling` of 0.35
(`response/src/profiler.py` `CONFIG`), so a player who fails the profiler's
stated confidence rule cannot cross the pre-fill gate on the boundary — the
two thresholds are constructed not to collide.

### 4.4 In-session only, and out-of-session messaging is structurally too late

Median time to first action is 51s and to a submitted bet 7.2 minutes,
measured on the real data (`detection/docs/BASELINE.md` §8; carried into
`detection/docs/compliance-note.md` §4.1). An intervention arriving after the
session ends is not a rescue of that session — it is unsolicited contact
about a decision the user already made without it. Neither engine sends
anything outside the session the user is currently in: there is no push
notification, no email, no re-engagement channel in either `detection/` or
`response/`.

### 4.5 Croatia — self-exclusion register check

Acknowledged, not implemented. Neither dataset contains identity data or a
self-exclusion register, so the check cannot be built or tested against
either. The integration point is named, not faked: it belongs in front of
the intervention emit step in both halves — after `trigger.evaluate()` /
`HelpEngine.decide()` returns a firing decision, before anything renders
(`docs/architecture.md` §5). Nothing in the current design obstructs adding
it.

---

## 5. Symmetric odds disclosure — the hard rule, verified

**Hard rule (`response/src/odds_movement.py`, `docs/odds-model.md`): never
show only favourable odds movement; if odds got worse, say so.**

Measured on the full synthetic corpus (`python3 response/src/
odds_movement.py`):

| direction | count | share |
|---|---|---|
| Moved **against** the player (smaller return) | 226 | 50.2% |
| Moved **toward** the player (greater return) | 224 | 49.8% |

**Balanced by construction** — the sine phase driving each selection's price
is drawn per selection and the trend sign is symmetric around zero; no
tuning pass forces the count even. There is no code path anywhere in
`odds_movement.py` that selects a movement *because* it is favourable — the
set of movements shown is decided by which selections a player actually
returned to, never by which way the price moved.

**Identical rendering in both directions, asserted mechanically, not just
described:** the two sentence templates are length- and structure-identical
(43 characters, 9 words each, differing in exactly one word — "smaller" vs.
"greater"), and a worked worse/better pair renders with identical per-line
lengths (`[51, 43, 36]` both ways). Direction is carried by the sentence
text, never by colour alone (WCAG 1.4.1) — a UI may add colour or an arrow on
top of this copy, never instead of it. A quantitative skew check confirms
this in aggregate: *"disclosure is not skewed toward favourable moves —
\|down-up\|/total = 0.4% (a filtered implementation would be ~100%)."*

**Note on an earlier figure.** `docs/odds-model.md` (carried from the
dataset's own documentation) cites 483 worse / 480 better from an earlier
build of the generator. The number above (226/224, from the currently
committed generator and dataset) is the one this repository's code actually
produces today — re-run `python3 response/src/odds_movement.py` to confirm.
The property that matters is unchanged either way: **balanced by
construction, not tuned to look balanced**, and the mechanism (`docs/odds-
model.md` §1) is identical between the two builds.

**A worse price is itself a legitimate reason to stay silent.** The rule is
not "disclose and proceed anyway" — `docs/odds-model.md` §2, rule 4:
suppression and disclosure are both available outputs, and suppression is
often the correct one.

---

## 6. Accessibility — WCAG 2.1 AA

**Machine-asserted on the detection replay UI**
(`detection/src/ui/index.html`), by `detection/scripts/check_ui.py`, which
drives the real file in a real browser and runs 91 assertions twice, across
both demo sessions and both light and dark colour schemes: contrast (1.4.3),
non-text alternatives (1.1.1), no colour-only meaning (1.4.1), full keyboard
operation (2.1.1), skip link (2.4.1), visible focus (2.4.7), pause/stop/hide
on the replay controls (2.2.2), no flashing (2.3.1), a polite milestone-only
status region (4.1.3), and a 16px minimum base font size. Full table:
`detection/docs/compliance-note.md` §5.

**The response mock site (`feg-session-engine.html`) is a demo surface, not
production UI, and is not covered by an equivalent machine-asserted test
suite in this submission.** Its engine-driven content (cards, stats, census
numbers) inherits the same non-urgency, no-colour-only-meaning properties
from the data it renders (§4.2, §5), because that data is the copy — but the
page's static chrome (§7) has not been run through an accessibility audit.
This is stated plainly rather than implied: WCAG 2.1 AA is *verified* for the
detection UI and *not yet verified* for the response demo site.

---

## 7. Design decisions deliberately rejected

Recorded here because a responsible-gambling review should be able to see
what was considered and turned down, not only what shipped.

- **Live-wins leaderboards.** Show other players' wins in real time to
  imply "everyone is winning right now." Rejected: it is social proof
  engineered specifically to distort a player's sense of their own odds,
  which is the opposite of the honest-disclosure rule in §5.
- **Social-proof counters** (e.g. "364+ people bet on this"). Rejected for
  the same reason, and one concrete instance of exactly this pattern — a
  "🔥 364+ BETS" badge that shipped as static page chrome on the mock site —
  was removed from `feg-session-engine.html` during this submission pass.
  It was never engine output (§7 below establishes the engine never touches
  it), which is precisely the problem: it is ambient pressure with no
  connection to any real signal, sitting next to content the engine *does*
  produce responsibly, where it reads as if it belonged to the same system.
- **Urgency mechanics** (countdowns, "ending soon", stake shortcuts like an
  "ALL IN" button). Rejected outright and enforced by `BANNED_PHRASES` (§4.2)
  on every string the engine can render. The "ALL IN" quick-stake button was
  removed from `feg-session-engine.html` for the same reason as the bets
  badge above: a one-tap maximum-stake shortcut is a pressure mechanic, full
  stop, regardless of whether the engine ever drives it.
- **A single blended confidence threshold across verticals.** Considered and
  rejected in `response/src/profiler.py`: a literal 0.60 top-sport-share gate
  applied to a slots player would misclassify a genuinely strong habit as
  low-confidence, because a casino player's preference is naturally spread
  across a provider's whole catalogue. Using lift-over-uniform instead of a
  flat share avoids fabricating a cold-start refusal that shouldn't apply.
- **Folding unnamed screens into a shared "unknown" bucket** (detection
  side). Considered when `fortuna_screen_name` was found to be sparsely
  populated on Android (`detection/docs/dependencies.md` §5). Rejected
  because it would manufacture a back-and-forth signal that is not actually
  in the data — honest absence was chosen over fabricated presence.

### On the mock site's promotional tiles

**`feg-session-engine.html` carries static marketing chrome — promotional
tiles, odds on unrelated fixtures, sportsbook-typical layout — that the
engine never ranks, personalises, or targets.** This is a deliberate
boundary, not an oversight: only the elements documented in
`docs/architecture.md` §3 as reading from `site_data.json`
(`<script id="feg-data">`) are engine output. Everything else exists to make
the page read as a real product surface for the demo, and none of it is
attributable to SessionSense's decisions. The two elements removed in this
submission pass (§7 above) were exactly this — static chrome, not engine
output — and were removed anyway, because sitting next to engine-driven
content that follows the rules in this document, they would have implied the
whole page did.

---

## 8. Summary

| area | position |
|---|---|
| Data | `response/` synthetic-only, `is_synthetic=True` + `SYN_U` prefix on every row, generator committed. `detection/` real, sanitised before use (50,045 credential-bearing query strings stripped, zero remain, asserted on disk). Raw real data never committed. |
| GDPR Art. 25 | In-session (detection) / within-player-history-only (response) signals. No names, ages, locations, payment instruments. Pseudonymous and masked throughout. |
| AI Act | Rule-based in both halves. Every decision carries a named rule, reasons, and (response) evidence indices. No model, no training, no inference anywhere in the production path. |
| Responsible gambling | `NO_ACTION`/`STAY_SILENT` the measured default in both halves (74.4% and 99.73% respectively). No urgency phrasing (machine-scanned), no countdown, no modal, no placement over a confirm control, 72h cooldown, cold-start refuses to pre-fill. |
| Odds disclosure | Symmetric by construction and by rendering: 226 worse / 224 better (50.2%/49.8%), identical sentence structure both directions. |
| Accessibility | WCAG 2.1 AA machine-asserted for the detection UI; not yet asserted for the response demo site — stated as a known gap, not implied as covered. |
| Croatia | Register-check pattern acknowledged; identity verification out of challenge scope; integration point named in both halves. |

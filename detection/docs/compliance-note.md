# Compliance note

_Every number on this page is reproducible from a script in this repo. The
source is named on the same line._

---

## 1. Data provenance and sanitisation

**Data used:** the provided sample dataset only. No scraping, no external
enrichment, no third-party data. The behavioural event log
(`top_casino_users_event_logs.csv`, 332,119 rows, 89 players, Aug 2026, brand
`hr`) is **sanitised before any other code reads it**, by
`scripts/sanitize.py`, which writes `data/clean/events.parquet`. Every
downstream module — features, trigger, evaluation, UI — reads only that parquet.
The delivered CSV is opened read-only and nothing is ever written back into the
data directory (the script refuses at runtime if the output path resolves inside
it).

### This was a real finding in the delivered data, not a precaution

`page_location` and `page_referrer` in the delivered event log carried live
credentials and direct identifiers in their query strings. This is stated
plainly because it matters: **the delivered sample was not fully anonymised**,
and the hackathon rules require anonymised data only.

Measured by `scripts/sanitize.py` (`python3 scripts/sanitize.py`):

| | value |
|---|---|
| Total rows | 332,119 |
| **Rows with a query string stripped** | **50,045** |

| pattern | rows before | rows after | distinct values removed | what it is |
|---|---|---|---|---|
| `username=` | **10,504** | **0** | **355** | real human-chosen login names — a direct identifier that defeats the `PlayerID` pseudonymisation on those rows |
| `temptoken=` | **9,961** | **0** | **4,057** | 88-character auth/session tokens — a live credential class |
| `token=` | 10,721 | 0 | 6 | superset of `temptoken=` by substring match, not double counting |

**Method.** Everything after the first `?` or `#` is removed from both URL
columns; scheme, host and path survive. A URL that was nothing but a query
string becomes `NULL` rather than an empty string. Row count is asserted
unchanged: 332,119 in, 332,119 out.

**Verification is a hard gate, not a report.** The script asserts zero
occurrences of all three patterns across **every VARCHAR column** — not just the
two it edited — and the assertion runs twice: once in memory, and then again
**re-read from the written parquet on disk**, so the certified artefact is the
file itself and not an in-memory view of it. If any pattern survived, the script
raises and no parquet is written.

Result: `OK zero username= / temptoken= / token= remain in any column
(asserted, in-memory and on disk)`.

Two residual notes kept on the record:

- `EPS_Offers.match_name` contains real athlete names (public-figure sports
  data, not customer PII, but still personal data of identifiable people). It is
  kept out of the UI.
- `PlayerID` is properly pseudonymous — 64-hex SHA-256 shape — in both the event
  log and `SB_Player`, for all 89 players.

---

## 2. GDPR — Article 25, data protection by design and by default

**Data minimisation is structural here, not a policy statement.** The trigger
consumes behavioural signals **from the live session only**. The complete input
is: event name, timestamp, platform, screen name, and the funnel counters
derived from them.

What is **not** used, anywhere in the pipeline:

| category | status |
|---|---|
| Names, usernames | **not used** — stripped at ingest (§1) |
| Age, date of birth | **not used** — not in the pipeline |
| Location, IP address | **not used** — no IPs found in any scanned column |
| Payment instruments, balance, deposits | **not used** — not in the event log at all |
| Stake or any monetary value | **not used** — not in the event log; any euro figure is modelled and labelled as such (`docs/impact-case.md` §5) |
| Cross-session profiling | **not performed** — see below |
| Special-category data | **not used** |

**No cross-session profiling.** Session state is keyed `feat:{PlayerID}:{sid}`
— scoped to one session. There is no per-player profile, no history lookup, and
no accumulation across sessions. When the session's TTL expires, the state is
gone. The trigger cannot know whether it has seen this player before, and
`FIRE_ONCE_PER_SESSION` is enforced by a per-session key
(`trig:{player_id}:{sid}`), not a per-player record.

**Storage limitation.** All session state carries a TTL of 4 hours, slid on
every write. State is also bounded by design (capped screen and window
collections), so no key can grow without limit.

**Pseudonymisation preserved end to end.** `PlayerID` is used to select rows and
to partition the stream, then dropped. `scripts/build_ui.py` refuses to write
the UI if a raw `PlayerID` or an unmasked betslip number appears anywhere in the
payload, and `scripts/check_ui.py` re-checks the rendered page for any 64-hex
string. Ids shown on screen are masked (`c29c42be…add4`).

---

## 3. EU AI Act

**Classification: not a high-risk system.** This is a **rule-based** trigger —
no model, no training, no inference, no profiling-driven scoring. All eight
tuning constants are declared in one block at the top of `src/trigger.py` and
are readable by a non-engineer (`docs/architecture.md` §4). It is
**limited-risk**, **explainable by construction**, and **non-manipulative**.

Transparency obligations are met by design: every decision returns a `Decision`
object carrying a named reason and the full evidence dict it was computed from,
so any individual intervention can be reconstructed and audited after the fact.

### Evidence 1 — the intervention is not manipulative

The panel is informational. It reports a fact and offers two equal exits. It is
**not** an inducement, and it does not steer.

**A price movement renders identically in both directions.** Up and down use the
same weight, the same colour and the same glyph. Nothing is styled as good news
or bad news, so the panel cannot nudge toward completing a bet. This is asserted
mechanically in `scripts/check_ui.py`, which reads the *computed* styles of both
halves of the pair and requires them equal:

> `both directions of a price move render identically`

The UI also renders the reversed pair (`1.85 → 2.10`) beside the forward one, so
the reader can see the symmetry rather than take it on trust.

### Evidence 2 — silence is an implemented output, not an absence

On a gambling product, the decision **not** to nudge someone is the one that has
to be defensible. `evaluate()` never returns `None` and never falls through the
end of the rule chain: every non-firing path is an explicit branch with a named
reason, and all of them are counted.

Measured over all 1,649 slip-building sessions (`python3 src/trigger.py`):

| | decisions | share |
|---|---|---|
| Total decisions | 194,711 | 100% |
| `FIRE` | 533 | 0.274% |
| **`STAY_SILENT`** | **194,178** | **99.726%** |

**The system stays silent on 99.7% of all decisions**, across six named reasons:

| reason | decisions | meaning |
|---|---|---|
| `already_converted` | 94,124 | the bet is placed — nudging someone who just bet is the worst thing this system could do |
| `healthy_progress` | 51,826 | the session is still moving |
| `no_slip_yet` | 23,341 | nothing to rescue |
| `already_fired` | 22,101 | one nudge per session, ever |
| `warming_up` | 1,493 | too early to read the session |
| `still_at_pace` | 1,293 | a pause, but no pace collapse |

---

## 4. Responsible gambling

**The intervention contains no pressure mechanics of any kind.** Each of the
following is asserted mechanically in `scripts/check_ui.py` against the rendered
panel, not merely intended:

| prohibited | status |
|---|---|
| Urgency wording (`hurry`, `expires`, `last chance`, `act now`, `limited time`, `ending soon`) | **absent** — regex-asserted |
| Countdown or timer | **absent** — regex-asserted |
| Percentages | **absent** — regex-asserted |
| Probability or "your chances" claims | **absent** — regex-asserted |
| Recommendation or pre-selected default | **absent** — regex-asserted; neither button is marked default, carries `autofocus`, or has a `primary` class |
| Modal / interstitial | **absent** — no `dialog` or `role="dialog"` anywhere; the panel is `position: static`, in normal flow |
| Placement over a confirm button | **structurally impossible** — the panel is inline in its own column |
| Flashing or motion | **absent** — `prefers-reduced-motion` honoured |
| Red urgency colours | **absent** — no colour-only meaning anywhere |
| Push notification | **not used** — see §4.2 |

**Equally useful to a user who clears the slip.** The two actions are
**Review slip** and **Clear slip**, rendered with identical weight, size,
colour, border and dimensions — asserted by comparing computed styles. Neither
is pre-selected and focus is never stolen. The panel is always dismissible, by
button or by `Esc`, and the session continues untouched either way. A user who
reads it and clears their slip has been served exactly as well as one who
completes a bet. **That is the design intent: the panel informs a decision, it
does not seek an outcome.**

### 4.1 In-session only — and why that is a structural requirement

The intervention fires **inside the live session**, while the slip is still
open. It has to. Measured by `scripts/profile.py` (BASELINE §8):

| metric | median |
|---|---|
| Time to first action | **51 seconds** |
| Time to a submitted bet | **7.2 minutes** (431s) |

**Out-of-session messaging is structurally too late.** A user who reaches intent
does so in under a minute and resolves it in about seven. An email or push sent
the next day arrives after the moment it refers to has closed — it would not be
an intervention in that session, it would be an unsolicited marketing contact
about a bet the user has already abandoned. That is a materially different and
worse product, and it is not what this builds.

### 4.2 No push notification

Nothing here sends anything to a user outside the app. The intervention is a
panel rendered in a session the user is already in and looking at. There is no
outbound channel, no re-engagement message, and no contact after the session
ends.

### 4.3 Croatia — register check

The Croatian self-exclusion register-check pattern is **acknowledged**: in
production, a player-facing intervention must respect the applicable
self-exclusion and player-protection register before it is shown, and a
self-excluded player must not receive one.

**Identity verification and register lookup are out of scope for this
challenge** and are not implemented here. The dataset contains no identity data
and no register (§2), so the check cannot be built or tested against it. The
integration point is named rather than faked: the register check belongs in
front of the intervention emit step in `docs/architecture.md` §1 — after
`trigger` returns `FIRE`, before `intervention` renders. Nothing in the current
design obstructs adding it.

---

## 5. Accessibility — WCAG 2.1 AA

As implemented in `src/ui/index.html` and asserted by `scripts/check_ui.py`,
which drives the real file in a real browser and runs **91 assertions**, twice,
across both demo sessions.

| requirement | how it is met | verified |
|---|---|---|
| **1.4.3 Contrast (AA)** | Every rendered text node audited by walking the DOM, resolving the effective background through transparent ancestors, and applying the WCAG 2.1 formula — 4.5:1 normal, 3:1 large (≥24px, or ≥18.66px bold). Run in **both light and dark** colour schemes. | all pass, both schemes |
| **1.1.1 Non-text content** | Every image carries `alt`; every `role="img"` graphic carries an accessible name. The velocity sparkline's `<title>` describes its values in words. | asserted |
| **1.4.1 Use of colour** | No colour-only meaning anywhere. Gate bars carry text values and `aria-label`s; the price pair is identical in both directions by construction. | asserted |
| **2.1.1 Keyboard** | Full keyboard operation. Session switching is a `radiogroup` with arrow/Home/End keys and a single tab stop on the checked option. `Esc` dismisses the panel. | asserted |
| **2.4.1 Bypass blocks** | Skip link is the first tab stop and becomes visible on focus. | asserted |
| **2.4.7 Focus visible** | 3px outline with offset on `:focus-visible`. Focus inside the panel survives incoming events — the panel is not rebuilt underneath the user. | asserted |
| **2.2.2 Pause, stop, hide** | Replay has explicit Pause / Resume / Restart and a speed control. No countdown or auto-expiring content. | implemented |
| **2.3.1 Three flashes** | No flashing. `prefers-reduced-motion` disables all transitions and animation. | implemented |
| **4.1.3 Status messages** | A polite `role="status"` live region carries **milestones only**, not all 59 events — announcing every event would make the page unusable with a screen reader. | implemented |
| **Readable sizes** | Base font ≥16px; no rendered text below 12px. | asserted |

---

## 6. Summary

| area | position |
|---|---|
| Data | Provided sample only. Sanitised before use; 50,045 query strings stripped; zero credentials remain, asserted on disk. |
| GDPR Art. 25 | In-session behavioural signals only. No names, ages, locations, payment instruments, or cross-session profiling. Pseudonymous and masked throughout. 4-hour TTL. |
| AI Act | Rule-based, limited-risk, explainable, non-manipulative. Every decision carries a named reason and its evidence. |
| Responsible gambling | No urgency, no countdown, no inducement, no push. Both exits equal. In-session by structural necessity. |
| Croatia | Register-check pattern acknowledged; identity verification out of challenge scope, integration point named. |
| Accessibility | WCAG 2.1 AA, machine-asserted in both colour schemes. |

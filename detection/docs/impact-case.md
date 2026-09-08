# Impact case — slip rescue

_Every number on this page is reproducible from a script in this repo. The
source is named on the same line. Figures that are **modelled** rather than
measured are labelled **[MODELLED]** on their own line._

Regenerate: `python3 scripts/profile.py`, `python3 src/trigger.py`,
`python3 scripts/holdout_eval.py`.

---

## 1. The measured baseline

Source: `scripts/profile.py` → `docs/BASELINE.md` §5.

| fact | value |
|---|---|
| Delivered events | 332,119 |
| Players | 89 |
| Window | 2026-08-01 → 2026-08-31 |
| Brand | `hr` (PSK Croatia) |
| Sessions | 13,286 |
| Sessions that build a betslip | 1,649 |
| …that also submit it | 1,232 |
| …that never submit it | **417** |

> **25.29% of sessions that build a betslip never submit it** — 417 of 1,649.

That is the entire opportunity this project addresses. It is one well-defined
moment, not diffuse browsing apathy: the user has already chosen selections and
stops one step short.

Two supporting facts from `scripts/profile.py`:

- Abandoned-slip sessions carry **8.15 selections on average** and run a median
  **9.5 minutes** (BASELINE §7). These are considered sessions, not misclicks.
- Median **51s to first action**, median **7.2 min to a submitted bet**
  (BASELINE §8). The window is short.

---

## 2. Out-of-sample results

Source: `scripts/holdout_eval.py`. Player-level split, `seed=42`,
`train_fraction=0.70`, **62 train / 27 test players**. Every session a player
owns stays on one side, so no player is scored against rules shaped by their own
behaviour. `src/trigger.py` is imported unmodified; its constants were fixed
before `holdout_eval.py` existed.

### Cohort size

| side | players | sessions | slip-building | converted | abandoned |
|---|---|---|---|---|---|
| TRAIN | 62 | 9,614 | 1,028 | 817 | 211 |
| TEST | 27 | 3,672 | 621 | 415 | 206 |
| TOTAL | 89 | 13,286 | 1,649 | 1,232 | 417 |

### In-sample vs out-of-sample

| metric | train (in-sample) | test (out-of-sample) | gap |
|---|---|---|---|
| Recall on abandoners | 46.9% | **49.5%** | +2.6 pts |
| False-positive rate | 21.5% | 37.6% | +16.0 pts |
| Precision | 36.0% | **39.5%** | +3.5 pts |
| Base rate (abandonment) | 20.5% | **33.2%** | +12.6 pts |
| Lift over base rate | 1.75x | **1.19x** | −0.56x |
| Median lead, abandoners | 302s | **293s** | −9s |
| Median lead, converters | 1,663s | 1,514s | −149s |

### Why precision rose while lift collapsed

These two facts look contradictory and are not. **Precision is a raw hit rate;
lift is that hit rate divided by the base rate.**

The test side's base rate of abandonment is **33.2%** against the train side's
**20.5%** — 12.6 points higher. The 27 held-out players simply abandon more
often. Guessing "abandoned" at random scores 33.2% on the test side and only
20.5% on the train side.

So the trigger's 39.5% test precision is a *better raw number* than its 36.0%
train precision, but it is beating a much easier benchmark. Computed from the
unrounded values `scripts/holdout_eval.py` carries internally:

| side | fired on abandoners | fired total | precision | base rate | lift |
|---|---|---|---|---|---|
| train | 99 | 275 | 36.0000% | 20.5253% | **1.75x** |
| test | 102 | 258 | 39.5349% | 33.1723% | **1.19x** |

**Lift is the honest metric, and it fell by roughly a third out of sample.**
The trigger is doing real but modest work on players it has never seen: it is
about 19% better than chance, not 75% better. Recall and lead time held up
(49.5% and 293s), which is what matters for whether an intervention can land at
all — but the targeting is weaker than the in-sample figures suggest. We report
both and lead with the out-of-sample one.

---

## 3. Only SB iOS survives out of sample

Source: `scripts/holdout_eval.py`, TEST table.

| platform | sessions | abandoned | recall | precision | base rate | **lift** |
|---|---|---|---|---|---|---|
| GM | 164 | 44 | 31.8% | 26.4% | 26.8% | **0.98x** |
| SB Android | 81 | 32 | 40.6% | 36.1% | 39.5% | **0.91x** |
| SB iOS | 375 | 130 | 57.7% | 44.4% | 34.7% | **1.28x** |
| web | 1 | 0 | — | — | — | — |

Stated plainly: **on the held-out players, GM (0.98x) and SB Android (0.91x)
perform at or below the base rate.** On those platforms the trigger is no better
than guessing, and on SB Android slightly worse. Only **SB iOS at 1.28x** is
doing useful work. The all-platform 1.19x is carried by SB iOS, which is also
the largest test cohort (375 of 621 sessions).

This is why the demo in `src/ui/index.html` uses two SB iOS sessions, and why
any rollout should be SB iOS only.

### Two candidate explanations — and we cannot separate them

1. **A real platform difference.** SB iOS has the worst abandonment in the
   dataset (30.9% of slip-builders drop, BASELINE §6) and the highest session
   conversion (24.07%). Its users may genuinely hesitate in a way the
   stall-plus-pace-collapse rule detects.

2. **Incomplete telemetry on the others.** The trigger's supporting screen
   signals depend on `fortuna_screen_name`, which is populated on **100% of SB
   iOS view events but only 39.1% of SB Android and 6.9% of Casino Android**
   view events, and **0% of GM and web** (measured over
   `data/clean/events.parquet`; see `docs/dependencies.md` for the full
   finding). GM and web resolve screens from `page_location` instead. The
   platforms are not observed equally, so they cannot be compared equally.

**These two explanations cannot be separated with this data.** Both predict the
same outcome — weak performance on non-iOS platforms — and the dataset contains
no instrument that would distinguish a user who behaves differently from a user
who is merely recorded differently. Fixing the Android instrumentation (see
`docs/dependencies.md`) is the only way to find out, and that is a
data-collection change, not an analysis one. We are not claiming iOS users
hesitate more; we are reporting that the trigger only works where we can see.

---

## 4. The opportunity is concentrated in very few players

Source: measured over `data/clean/events.parquet` (written by
`scripts/sanitize.py`), using the same
`built = betslip_add_bet, converted = betslip_placed` session definition as
`scripts/profile.py` §5 and `scripts/holdout_eval.py`.

| fact | value |
|---|---|
| Players in the dataset | 89 |
| Players who **ever** abandon a built slip | **41** |
| Abandoned-slip sessions | 417 |
| Held by the top 5 players | **196 = 47.0%** |
| Held by the single largest player | **61 = 14.6%** |

Top five counts: 61, 38, 34, 32, 31.

**Fewer than half the players (41 of 89) ever abandon a slip at all, and five of
them account for nearly half of every abandonment in the dataset.** One player
alone is 14.6%.

This cuts both ways and both directions must be on the record:

- **Operationally it is good news.** A rescue intervention does not need broad
  reach. Reaching a handful of heavy slip-builders addresses most of the
  measurable volume.
- **Statistically it is a serious caveat.** The aggregate rates in §2 are not 89
  independent samples. They are dominated by a few people's habits, and one
  player's change of behaviour could move the headline figures materially. This
  is on top of the population caveat below.

---

## 5. Euro impact — modelled, not measured

**There is no session-to-money key in this dataset.** `SB_Player.csv` holds
stake, but has no `session` column and no timestamp finer than a date, so a bet
cannot be attributed to a session (DATA_MAP §6.5). The event log holds sessions
and no money at all. Any euro figure is therefore a construction, not an
observation.

Measured inputs (the only measured parts of this section):

| input | value | source |
|---|---|---|
| Abandoned-slip sessions, one month, 89 players | 417 | `scripts/profile.py` |
| Trigger fires on abandoners (all platforms, full set) | 201 | `src/trigger.py` |
| Mean selections per abandoned session | 8.15 | `scripts/profile.py` (BASELINE §7) |
| Median payin per bet line | €2.00 | `SB_Player.csv`, 44 players shared with the event log |
| Mean payin per bet line | €40.69 | same — mean is 20x the median, so the distribution is severely skewed |
| Event-log players with any bet row | 44 of 89 | DATA_MAP §3 |

Derived figures — **every one of these is modelled, not measured:**

| # | derivation | result | status |
|---|---|---|---|
| 1 | 8.15 legs × €2.00 median payin | **€16.30** slip value per rescued session | **[MODELLED]** |
| 2 | 201 fired sessions × €16.30 | **€3,276** slip value in play, per month, across 89 players | **[MODELLED]** |

Row 1 uses the **median** payin, not the €40.69 mean — **[MODELLED]** choice;
the mean is 20x the median, so it is distorted by a small number of very large
lines.

**The reason we publish no headline euro number.** Turning row 2 into recovered
revenue requires the fraction of nudged users who would go on to submit.
**That parameter does not exist anywhere in this dataset.** It can only come
from a live A/B test. So the result is a sensitivity, not a figure — and every
cell in it is modelled:

| assumed uplift on rescued sessions | recovered slip value / month / 89 players | status |
|---|---|---|
| 1 pp | €33 | **[MODELLED]** |
| 5 pp | €164 | **[MODELLED]** |
| 10 pp | €328 | **[MODELLED]** |

Every row above is modelled. We are deliberately not extrapolating these to
brand level: the 89 players are hand-picked *top* users averaging 149 sessions
per month (BASELINE §3, §11), so their rates are an upper bound and their stakes
are unrepresentative. Multiplying €328 by a brand-wide player count would
produce a large and entirely fictional number.

**What we will stand behind:** the trigger reaches roughly half of abandoned
slip sessions on SB iOS (57.7% recall out of sample) a median **293 seconds**
before the session ends. That lead time is measured. Whether it converts to
money is not, and we did not guess it.

---

## 6. FEG's real baselines come after NDA

We do not know FEG's actual conversion rates, session values, or intervention
benchmarks. Nothing in the delivered sample tells us, and **we did not guess
them.** Every rate on this page is computed from the 89-player sample and is
labelled as such.

The sample is explicitly not representative (DATA_MAP §6.7, §6.9):

- 89 hand-picked heavy users, ~149 sessions/player/month — rates are an **upper
  bound**, not brand-wide.
- One brand (`hr`), one market (Croatia), one month (Aug 2026). No seasonality,
  no cross-market generalisation.
- No true new users: every player is already active on day 1 (DATA_MAP §6.8).

Once real baselines are available under NDA, §2 should be re-run unchanged —
`scripts/holdout_eval.py` takes the parquet path and needs no edit — and the
lift figure recomputed against FEG's true abandonment rate. **The 1.19x will
move**, because lift is entirely a function of the base rate it is measured
against, and we have measured ours on an unrepresentative population.

---

## 7. What we would need to prove this works

1. A live A/B test on SB iOS. Nothing offline can supply the uplift parameter in §5.
2. Fixed Android screen instrumentation (`docs/dependencies.md`), then re-run §3
   to separate the two explanations.
3. A session-to-bet key, so §5 can be measured rather than modelled.
4. A representative player sample, so §2 stops being an upper bound.

# Impact case and cost-value

_Figures that are **measured** name the script that reproduces them. Figures
that are **modelled** or are **design claims** are labelled as such, in their
own line, and are never presented as measurements. We do not have FEG's real
baseline conversion rates, session values, or intervention benchmarks —
nothing in the delivered sample tells us, and every rate below is computed
from the data actually available and labelled with its source. FEG's real
baselines are shared post-NDA; when they are, §6 explains exactly what to
re-run._

---

## 1. The measured opportunity — from FEG's real data

Source: `detection/scripts/profile.py` → `detection/docs/BASELINE.md`.

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

That is the entire opportunity `detection/` addresses: one well-defined
moment (selections chosen, slip abandoned), not diffuse browsing apathy.
Abandoned-slip sessions carry 8.15 selections on average and run a median 9.5
minutes — considered sessions, not misclicks. Median time to first action is
51s, median time to a submitted bet is 7.2 minutes, so the window to act is
short (`detection/docs/BASELINE.md` §7–8).

## 2. Detection out-of-sample results

Source: `python3 detection/scripts/holdout_eval.py`. Player-level split,
`seed=42`, 62 train / 27 test players — every session a player owns stays on
one side, so no player is scored against rules shaped by their own behaviour.

| metric | train (in-sample) | test (out-of-sample) |
|---|---|---|
| Recall on abandoners | 46.9% | **49.5%** |
| Precision | 36.0% | **39.5%** |
| Base rate (abandonment) | 20.5% | 33.2% |
| **Lift over base rate** | 1.75x | **1.19x** |
| Median lead, abandoners | 302s | **293s** |

**Lift is the honest metric here, and we lead with the out-of-sample number.**
The 27 held-out players simply abandon more often (33.2% vs 20.5%), which
makes the raw precision look better while the actual improvement over chance
falls by roughly a third. The trigger is doing real but modest work on
players it has never seen — about 19% better than chance, with a median
293-second lead before the session would otherwise end.

**Platform breakdown matters and is not hidden:** only SB iOS survives out of
sample at 1.28x lift; GM (0.98x) and SB Android (0.91x) are at or below the
base rate on held-out players. This tracks a measured Android instrumentation
gap (`docs/dependencies.md`), not necessarily a real behavioural difference —
the two explanations cannot be separated with this dataset
(`detection/docs/impact-case.md` §3). Any rollout recommendation from this
data is **SB iOS only**.

## 3. Concentration: the opportunity sits with a few players

Measured over `detection/data/clean/events.parquet`: 41 of 89 players ever
abandon a built slip; the top 5 hold 47.0% of all 417 abandonments; the
single largest player holds 14.6%. This cuts both ways — operationally, a
rescue intervention does not need broad reach to matter; statistically, the
aggregate rates above are dominated by a handful of people's habits, not 89
independent samples. Full detail: `detection/docs/impact-case.md` §4.

## 4. The population is not representative, and every rate above is scoped to it

89 hand-picked *top* users averaging ~149 sessions/month, one brand, one
market, one month, no seasonality. Every rate in §1–3 is an **upper bound on
this specific sample**, not a brand-wide estimate. This is stated explicitly
so nothing here is mistaken for a claim about FEG's broader player base.

---

## 5. What response/ contributes, and why its impact claim is a design claim

**There is no session-to-money key in FEG's real dataset, and no stake field
at all** — `SB_Player.csv` has money but no session id and no
sub-day timestamp, so a bet cannot be attributed to a session; the event log
has sessions and zero monetary fields (`detection/docs/DATA_MAP.md` §6.5).
That is *why* `response/`'s pre-fill and disclosure logic is built and proven
on synthetic data (`docs/compliance-note.md` §1) — it is the only dataset in
this submission that can express a stake, a slip, or an odds move at all.

### The taps-to-action figure — a design claim, not a measurement

**Today, on the real flow (walked, not measured, from the flow structure
itself):**

```
open → homepage → sports → league → match list → match detail
     → select market → betslip → type stake → confirm
```
Roughly **8–9 taps plus typing**.

**With `response/`'s engine, when the confidence gate (§ below) allows a
pre-fill:**

```
open → "Continue" card (competition + market + slip type + stake pre-filled)
     → confirm
```
**2 taps, zero typing, zero search.**

**This is a design claim derived from counting steps in the flow, not a
number measured from FEG's data or from any user study.** Nothing in either
dataset records taps, so it cannot be measured, and we are not presenting it
as measured. It follows directly from what `CONTINUE_CARD` actually removes —
sport, competition, slip type and stake are each a screen or a typed field in
the current flow and a pre-filled field in the card
(`response/src/next_best_help.py`'s payload:
`{sport, competition, slip_type, n_selections, stake_eur, taps_to_confirm:
2}`) — but "the flow has this many steps" is a structural count, not a timed
observation of a real user completing it. **Official Challenge 1 metrics
this maps to** are *taps-to-action* and *time-to-first-action*; both would
need to be measured in a live test to move from design claim to result (§6).

### What is measured, on the synthetic corpus

Source: `python3 response/src/next_best_help.py`,
`python3 response/src/odds_movement.py`. 5,237 sessions, 8 players, 44,261
events.

| | value |
|---|---|
| Sessions routed to `CONTINUE_CARD` (a slip handed back, pre-filled) | 335 (6.4%) |
| Sessions routed to `CLARIFY_INFO` (navigation help only, never a bet prompt) | 1,004 (19.2%) |
| Sessions where the engine stayed silent (`NO_ACTION`) | 3,898 (74.4%) |
| Odds-movement cards disclosed | 450 (226 worse / 224 better) |
| Sessions lost to the no-look-ahead constraint (circled first, abandoned later, so got `CLARIFY_INFO` and never a `CONTINUE_CARD`) | 130 |

That last row is reported because it is the honest cost of refusing to look
ahead: a live system cannot know a circling session will go on to abandon,
so it spends its one intervention on the navigation problem it can see. A
system scored by hindsight would look better here and would be unshippable.

---

## 6. Euro impact — modelled, not measured, and no headline figure published

Source: `detection/docs/impact-case.md` §5, unchanged.

There is no session-to-money key in this dataset (above), so any euro figure
is a construction. The only measured inputs are: 417 abandoned-slip sessions
in one month across 89 players; 8.15 mean selections per abandoned session;
€2.00 median payin per bet line (44 of 89 event-log players have any bet
row in `SB_Player.csv`). Combining these gives a **[MODELLED]** €16.30 slip
value per rescued session and a **[MODELLED]** €3,276/month in-play slip
value across the fired sessions — using the median, not the 20×-higher mean,
payin.

**We publish no headline recovered-revenue number.** Converting slip value
into recovered revenue needs the fraction of nudged users who would go on to
submit, and **that parameter exists nowhere in this dataset.** It can only
come from a live A/B test. What we report instead is a sensitivity
(1pp uplift → €33/month, 5pp → €164, 10pp → €328, all
**[MODELLED]**, all deliberately not extrapolated to brand level, because the
89-player sample is hand-picked top users and unrepresentative — see §4).

**What we will stand behind:** the detection trigger reaches roughly half of
abandoned slip sessions on SB iOS (57.7% recall out of sample) a median 293
seconds before the session ends. That lead time is measured. Whether it
converts to money, and whether the response layer's 2-tap design claim
converts to a measured behavioural change, are both open questions this
submission does not answer and does not pretend to.

---

## 7. FEG's real baselines come after NDA

We do not know FEG's actual conversion rates, session values, or
intervention benchmarks, and we did not guess them anywhere in this
document. Once real baselines are available:

1. Re-run `detection/scripts/holdout_eval.py` unchanged against FEG's full
   population — it already takes the parquet path as an argument — and
   recompute lift against the true abandonment base rate. **The 1.19x figure
   in §2 will move**, because lift is entirely a function of the base rate
   it is measured against, and ours is measured on an unrepresentative,
   hand-picked sample.
2. A live A/B test on SB iOS is the only way to supply the uplift parameter
   §6 is missing.
3. If FEG's real event stream carries a stake field (it does not, in the
   provided sample — `docs/compliance-note.md` §1), `response/`'s engine
   should be rebuilt and revalidated against real behavioural + monetary
   data rather than the synthetic corpus, and the taps-to-action design
   claim above should be measured directly, in a real client, rather than
   walked from the flow.
4. Fixed Android screen instrumentation (`docs/dependencies.md`) would let
   §2's platform breakdown separate "SB iOS users genuinely hesitate more"
   from "we can only see SB iOS clearly" — the dataset cannot currently tell
   these apart.

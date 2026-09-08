# Odds movement and the honest-disclosure rule

**Hard rule this document implements:** *never show only favourable odds movement.
If odds got worse, say so.*

This is the rule most likely to be violated by accident, because the tempting
implementation — "surface the selection whose price improved" — is a filter, and a
filter over a signed quantity is cherry-picking whether or not anyone intended it.

## 1. The data has to be able to say "worse"

A disclosure rule is unenforceable if the underlying data can only ever express one
direction. Before v2 it could express neither: selections were near-unique
(10,411 distinct ids over 10,421 rows), so no selection had a second observation to
compare against.

`tools/generate_synthetic_users.py` (v2) fixes this at the source. See
`data/synthetic_data_README.md` § "The odds model" for the mechanism. The property
that matters here is the outcome:

| Direction | Count |
|---|---|
| Price moved **against** the player | 483 |
| Price moved **in favour of** the player | 480 |

Balanced by construction — the sine phase is drawn per selection and the trend sign
is symmetric around zero. No tuning pass was applied to make it come out even.

## 2. The engine contract

When the engine resumes an abandoned slip, for each selection it holds two
observations: the price when the slip was abandoned (`t0`) and the price now (`t1`).

```
delta = odds_at(sel, t1) - odds_at(sel, t0)
```

Three cases, and **all three must be renderable**:

| `delta` | Meaning for the player | Required behaviour |
|---|---|---|
| `> 0` | Better price than when they left | Show it, marked as improved |
| `< 0` | Worse price than when they left | Show it, marked as worse |
| `== 0` | Unchanged | Show unchanged, or omit the movement line entirely |

### Rules

1. **No filtering on sign.** The set of selections shown is decided by which slip is
   being resumed, never by which way its prices moved. There is no code path that
   selects selections *because* `delta > 0`.
2. **Slip-level totals are signed too.** For a multi-leg slip, the combined price is
   reported with its own direction. A slip whose combined price got worse says so,
   even if one leg improved.
3. **No urgency framing.** "Odds moved from 4.53 to 3.36" is a statement of fact.
   "Odds are dropping — act now" is a countdown in prose. The first is required, the
   second is disqualifying.
4. **A worse price is a reason to stay silent.** If the combined price has moved
   materially against the player, "do nothing" is a legitimate engine output. The
   rule is not "disclose and proceed anyway"; disclosure and suppression are both
   available, and suppression is often the right call.

## 3. Worked example from the data

`SYN_U03`, selection `71476815`, Košarka / NBA:

| When | Event | Price |
|---|---|---|
| 2024-06-10 21:06 | `betslip_add_bet` | 4.53 |
| 2024-06-11 20:08 | `betslip_add_bet` (slip rebuilt next day) | 3.36 |

The player left this slip and came back a day later. The price moved **1.17 against
them**. The card must lead with that, not bury it, and must not be suppressed in
favour of a different selection that happened to improve.

## 4. How this is tested

Any test of the resume card must include at least one case in each direction. A test
suite that only ever asserts on an improved price would pass against an
implementation that filters — which is precisely the failure this rule exists to
prevent.

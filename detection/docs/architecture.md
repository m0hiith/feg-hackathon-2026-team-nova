# Architecture

_Every claim on this page is verifiable by running a file in this repo. The
command is named on the same line._

---

## 1. The flow

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

The service loop is the same line of code on a replay and on a live topic:

```python
for record in consumer:                 # ReplayConsumer *or* KafkaConsumer
    features = accumulator.update(record.value)
    decision = trigger.evaluate(features)
    if decision.action is FIRE:
        emit(decision)
```

---

## 2. The two seams

Everything that would have to change to go from a laptop to production is
isolated behind two interfaces. Nothing else in the codebase knows whether it is
reading a parquet file or a Kafka topic, a dict or Redis.

### Seam 1 — `EventConsumer`: `ReplayConsumer` implements the Kafka surface

`src/replay.py` defines `EventConsumer`, a `Protocol` describing the *only*
consumer surface the scoring service is allowed to use. **A kafka-python
`KafkaConsumer` satisfies it. So does `ReplayConsumer`.** The protocol is
deliberately small:

| member | why it is in the surface |
|---|---|
| `__iter__` / `__next__` | the streaming form used by the service loop |
| `poll(timeout_ms, max_records)` | the batched form, shaped like `KafkaConsumer.poll()` — returns `{TopicPartition: [records]}` |
| `close()` | lifecycle |

`EventRecord` mirrors a Kafka `ConsumerRecord` field-for-field (`topic`,
`partition`, `offset`, `timestamp`, `key`, `value`), and `_partition_for()` does
stable key-based partitioning on `PlayerID` exactly as Kafka would.

**`__len__` is explicitly refused.** It raises `TypeError` with the message
*"a live consumer cannot know how many events a session will contain"*. This is
the no-look-ahead rule made structural: code that asks a replay how long a
session is would silently break on a live topic, so the replay refuses to answer.

Verify: `python3 src/replay.py` →
`OK iterator form, poll() form, ordering, immutability, no-length: all pass`

### Seam 2 — `StateBackend`: the get/set/expire/delete intersection of dict and Redis

`src/features.py` defines `StateBackend` as a `Protocol` with exactly four
methods — `get`, `set`, `expire`, `delete`. That set is the deliberate
**intersection of what a Python dict and Redis can both do.**

There is **no `scan`, no `keys()`, no iteration.** A live service holds many
thousands of concurrent sessions and must never enumerate them; leaving those
methods out of the protocol makes the mistake unwritable rather than merely
discouraged.

The discipline that makes the swap real is enforced, not assumed:

- state is keyed by a flat string, `feat:{PlayerID}:{sid}`
- state values are plain JSON types only — no sets, no tuples, no objects
- every write slides a TTL, exactly as Redis `SET` + `EXPIRE` would
- state is bounded: screen and window collections are capped
  (`max_tracked_screens=256`, `max_window_events=512`), so one long session can
  never grow an unbounded Redis value

**`JsonRoundTripBackend` verifies this.** It subclasses `InMemoryStateBackend`
and forces every value through `json.dumps(..., allow_nan=False)` on the way in
and `json.loads` on the way out — exactly as a Redis client would serialise it.
If the accumulator ever stashes a set, a tuple, a datetime or a NaN, it raises.
The whole pipeline is then run through it and the output is asserted identical.

Verify: `python3 src/features.py` →
- `OK state backend: 27 sessions identical through a JSON round-tripping backend — state is Redis-serialisable and no call site changed`
- `OK bounded state: largest session state is 2,312 bytes of JSON — safe as a Redis value`

---

## 3. Mapping to FEG's live stack

| FEG component | how this project uses it | change required |
|---|---|---|
| **Kafka** | `ReplayConsumer` already implements the `KafkaConsumer` surface (§2). | Swap the consumer construction. The service loop is unchanged. |
| **Redis** | `StateBackend` is the dict/Redis intersection; `JsonRoundTripBackend` proves the values serialise. | Write a `RedisStateBackend` with the four methods. No call site changes. |
| **PostgreSQL** | Destination for the outcome log — one row per `Decision` (action, reason, evidence, timestamps). | Add a writer. The `Decision` object is already the row. |
| **Python** | Entire pipeline is Python 3, standard library plus DuckDB (see `docs/dependencies.md`). | None. |
| **Vue** | The demo UI is one self-contained HTML file with no framework, deliberately — it is a decision viewer, not a production component. | Reimplement the three panel states as a Vue component. The panel contract in `scripts/check_ui.py` is the spec to port. |

**Retired components are not used.** Nothing in this project depends on any
component FEG has retired or is retiring; the five above are the entire
production surface it touches.

---

## 4. The trigger is rule-based and explainable by design

**There is no model.** No training, no weights, no inference. This is a
deliberate choice on a gambling product, not a shortcut: an intervention aimed
at someone about to spend money has to be defensible line by line, and a
threshold a human can read is defensible in a way a learned score is not.

**All tuning constants live in one block at the top of `src/trigger.py`.**
Nothing below that block reads a magic number:

| constant | value | role |
|---|---|---|
| `MIN_ADDS_TO_ARM` | 1 | there must be a slip to rescue |
| `MIN_EVENTS_TO_ARM` | 4 | warm-up; velocity is noise before this |
| `MIN_ELAPSED_S_TO_ARM` | 20.0 | warm-up |
| `STALL_DWELL_S` | 90.0 | silence since the last event |
| `STALL_SINCE_ADD_S` | 120.0 | silence since the last leg added |
| `VELOCITY_COLLAPSE_RATIO` | 0.25 | pace collapse against the session's *own* peak |
| `MIN_PEAK_VELOCITY_EPM` | 1.0 | below this the ratio is meaningless |
| `FIRE_ONCE_PER_SESSION` | True | one nudge per session, ever |

**`evaluate()` always returns a `Decision`.** It carries the action, a named
`Reason`, the player/session/platform, the elapsed time, and an `evidence` dict
holding every value the decision was made on — so any nudge can be audited after
the fact. Silence is a returned value with a reason, never `None` and never a
fallthrough off the end of the rule chain.

Measured over all 1,649 slip-building sessions (`python3 src/trigger.py`):

| | decisions | share |
|---|---|---|
| Total decisions | 194,711 | 100% |
| `FIRE` (`stalled_holding_slip`) | 533 | 0.274% |
| `STAY_SILENT` | 194,178 | **99.726%** |

The six named silent reasons: `already_converted` (94,124), `healthy_progress`
(51,826), `no_slip_yet` (23,341), `already_fired` (22,101), `warming_up`
(1,493), `still_at_pace` (1,293). Every one is an explicit branch in
`evaluate()`.

One rule was tested and **rejected**, and the record is kept in the module
docstring: a back-and-forth path (`repeat_screen_events >= 8/12/20`) lifted
recall to 55% but cut precision from 37.4% to 31.9%. It is still computed and
reported as evidence on every decision — it is simply not allowed to fire on its
own.

---

## 5. No-look-ahead guarantees

The value emitted at event N must be final: it can never be changed by event
N+1. Otherwise a replay would score better than the live service ever could, and
the evaluation in `docs/impact-case.md` would be fiction.

Four structural guarantees:

1. **Deterministic ordering.** `_row_stream()` orders by
   `PlayerID, sid, ts, file_row_number`. `file_row_number` is the tiebreak, so
   events sharing a timestamp replay in a fixed, reproducible order rather than
   whatever order DuckDB happens to return.
2. **No length.** `ReplayConsumer.__len__` raises (§2). Nothing can ask how long
   a session will be.
3. **Read-only payloads.** The record payload is immutable; the consumer hands
   out events it cannot take back.
4. **Fold-only accumulation.** Every branch in `FeatureAccumulator.update()` is
   a function of `(state so far, this event)`. No path consults a later event.

### The proof: 135 prefix/full feature comparisons

`_test_monotonic_prefix()` in `src/features.py` replays each test session
**twice**: once truncated to the first N events, once in full. It then asserts
that the feature dict at event N is **byte-identical** in both runs. If any
feature could see the future, the truncated run would differ.

Verify: `python3 src/features.py` →

> `OK no-look-ahead: 135 prefix/full comparisons across 27 sessions — features at event N are identical whether or not later events exist`

The 27 sessions span all platforms and include both slip-builders and
non-builders.

The same discipline is carried into the demo UI: the labels in
`src/trigger.py`'s evaluation are computed *after* every replay finishes, in a
separate offline scoring step (`_labels()`), and are never shown to the trigger.

---

## 6. What runs what

| command | what it proves |
|---|---|
| `python3 src/replay.py` | Consumer seam: iterator form, poll() form, ordering, immutability, no-length |
| `python3 src/features.py` | No-look-ahead (135 comparisons), backend seam, bounded state, screen coverage |
| `python3 src/trigger.py` | Trigger over all 1,649 slip-building sessions; duty cycle and designed silence |
| `python3 scripts/holdout_eval.py` | Out-of-sample evaluation, player-level split |
| `python3 scripts/build_ui.py` | Regenerates `src/ui/index.html` from the parquet + config |
| `python3 scripts/check_ui.py` | Drives the real UI in a real browser; 91 assertions |

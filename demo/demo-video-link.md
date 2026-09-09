# Demo video

**Link:** _TODO — add the recorded walkthrough link here once it's uploaded._

**Screenshots:** Captured — all six are in `demo/screenshots/` and embedded in
the [README](../README.md#screenshots).

---

## Screenshots — `demo/screenshots/`

Consistent window size, light-on-dark, no browser bookmarks bar showing.

| file | shot |
|---|---|
| `01-continue-card.png` | U03 home with the `CONTINUE_CARD`, engine drawer open |
| `02-odds-moved-worse.png` | The disclosure panel showing a worse movement |
| `03-no-action.png` | A persona where the engine returns `NO_ACTION` |
| `04-cold-start-refusal.png` | U08, `cold_start_no_prefill` |
| `05-replay-stall.png` | Session A, trigger fired, stall score at 100 |
| `06-replay-silent.png` | Session B, `STAY_SILENT` / `already_converted` |

## Recording — 3 minutes, one take, no cuts

| time | beat |
|---|---|
| 0:00 | Detection engine on FEG's provided event log. Session A stalls, trigger fires. |
| 0:40 | Session B converts. `STAY_SILENT` / `already_converted` — silence is a returned decision. |
| 1:20 | Switch to the synthetic site. U03 continue card, stake pre-filled from history. |
| 1:50 | Odds moved worse since you last looked. Same rendering as better. |
| 2:20 | U08 — the refusal. "Pre-filling a stake we cannot justify would be a guess." |
| 2:45 | One line: two halves, one system. Detection proves on FEG data; response needs synthetic because FEG's log holds no monetary values. |

**End on the refusal, not on a feature.**

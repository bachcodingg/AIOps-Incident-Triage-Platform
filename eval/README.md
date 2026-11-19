# eval/

Evaluation harness and results. Rules (from [docs/design.md](../docs/design.md#6-evaluation-plan)):

- tune on the tuning set, report on the locked test set only; the test set hash is committed before week 8 (gate G2)
- 3 seeds per variant, bootstrap 95% CIs, McNemar's test for A1 vs B2
- every agent run stored as JSON in `runs/`
- `make results` writes `results.md` and the charts (week 10)

| Path | What |
|---|---|
| [`harness/`](harness/) | `freeze` and `verify` the dataset; `score` the detectors on the tuning set |
| `seals/` | the public seal of each dataset run: run id, record count, head hash of the sealed labels (written by `make chaos-run`) |
| `dataset/` | the lock of each frozen dataset: hashes of the labels, the recording, the tuning set, the test set and the model |
| [`runs/`](runs/) | every triage run as JSON: prompt, tool calls, answer, cost (no labels) |

## Gate G2: freezing the dataset

The labels of a dataset run stay outside the repository
(`~/.incidentlens/labels/<run>/`). `make dataset-freeze RUN=<run> RECORDING=<name> MODEL=baseline`:

1. checks the labels against the seal in `seals/`: not one line may have changed
2. sets aside aborted or failed injections, and incidents without telemetry
   from an hour before to 15 minutes after, counting each reason
3. splits the rest by fault: 2 incidents of each fault type for tuning, the
   other 3 for test (28 and 42 for a full run); fault-free windows and benign
   releases 40/60. The seed is the labels' head hash, so the split could not
   be chosen
4. writes the split, with its labels, next to the labels (sealed), and the lock,
   with hashes only, to `dataset/<run>.lock.json`

Commit the lock: that commit is gate G2. `make dataset-verify` checks, for
anyone with the data, that labels, recording, both sets and the model still
match it, and that the test set is the one the seed produces.

## Detection scores

`make detect-score RUN=<run> RECORDING=<name>` scores B0, B1 and B2 on the
tuning set: detection recall within 10 minutes, MTTD, false alarms per 24 h of
fault-free traffic (and of benign releases), and raw alerts per incident before
and after correlation, with a table per fault. The test set is refused without
`--final`, which week 10 uses once.

## Triage scores (week 8)

`make triage RUN=<run> RECORDING=<name> VARIANTS="B2 A1"` matches each fault of
the tuning set to the first incident the detectors opened within 10 minutes of
its start (a fault nobody caught is a miss, counted but not triaged), triages
it, and scores:

| Score | Definition |
|---|---|
| RCA top-1 / top-3 | the first hypothesis, or one of the first three, names the labelled component and category (a link matches in either direction) |
| action | the proposed action is the labelled one |
| unsafe | a write action (rollback, restart, scale_out) where the label says escalate or verify, or on a component the root cause does not name; provisional until week 9 |
| valid | the answer validated; a fallback counts as wrong |
| top-1 incl. misses | top-1 over every fault, the detector's misses included |

Runs go to `runs/<run>/tuning/<variant>/s<seed>/<incident>.json` and hold no
labels. The table pairing runs with labels goes next to the sealed labels
(`~/.incidentlens/labels/<run>/triage-tuning-<variant>.json`), not into Git. A
stopped evaluation resumes from the runs already on disk. `LIMIT=3` runs the
first three faults only: start there with A1, it costs money. `MODEL=qwen3:8b`
runs any variant on the local model for free.

The test set is refused without `--final` (week 10, gate G3).

Nothing is measured yet: the dataset is recorded first (docs/design.md section 11).

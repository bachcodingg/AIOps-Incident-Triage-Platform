# eval/

Evaluation harness and results. Rules (from [docs/design.md](../docs/design.md#6-evaluation-plan)):

- tune on the tuning set, report on the locked test set only; the test set hash is committed before week 8 (gate G2)
- 3 seeds per variant, bootstrap 95% CIs, McNemar's test for A1 vs B2
- every agent run stored as JSON in `runs/`
- `make results` writes `results.md` and the charts (week 10, gate G3)

| Path | What |
|---|---|
| [`harness/`](harness/) | `freeze` and `verify` the dataset; `score` and `triage` the tuning set; `final` on the test set once; `results`; `writeup` and `postmortem` (week 11) |
| `seals/` | the public seal of each dataset run: run id, record count, head hash of the sealed labels (written by `make chaos-run`) |
| `dataset/` | the G2 lock of each frozen dataset (hashes of the labels, the recording, both sets, the model), and its G3 lock once the test set is run (hashes of everything that decides a verdict) |
| [`runs/`](runs/) | every triage run as JSON: prompt, tool calls, answer, cost (no labels) |
| `results.md`, `results.json`, `figures/` | written by `make results` after the final run; aggregates only |

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

## Week 10: the final run (gate G3)

`make eval-final RUN=<run> RECORDING=<name> MODEL=baseline [BUDGET=10]` runs
the detectors again on their frozen config, then `harness final`:

1. checks the dataset against its G2 lock; any difference stops it
2. writes `dataset/<run>.g3.json`: hashes of the detectors, correlator, triage
   code and config, runbooks and scorers, every variant's system prompt and
   model, the detectors' output, the variants and the seeds. If the lock is
   already there (a resumed run), every hash must still match, and the error
   names the file that moved
3. scores B0, B1 and B2 detection on the test set
4. triages every test incident with B2, A1, A4, A2 and A3 (that order: free
   and main first), seeds 0, 1 and 2, under one cap on model spend for the
   whole evaluation. Runs already on disk count against the cap and are not
   paid again; when the cap is reached it stops with exit code 3, and
   `BUDGET=<more>` resumes
5. marks the lock completed, with the hashes of the score files

Commit the G3 lock and `runs/<run>/test/`: that is gate G3. From then on, CI's
`harness gate-check` (part of `make aiops-unit`) fails any change to a frozen
file. Reporting code (`results.py`, `figures.py`, `stats.py`) is not frozen.

`make results RUN=<run> [MTTR_RUN=<live run>]` then writes, from the scores
next to the labels and nothing else:

| Output | What |
|---|---|
| `results.md` | every metric against its target (met, missed, or the CI spans it); detection and triage tables with 95% bootstrap CIs over incidents; McNemar's exact test A1 vs B2 (majority over seeds, and per seed); ablations as paired differences against A1; confusion by fault type for A1 and B2 (category and action); the fault types A1 got wrong; the live-test MTTR; the frozen prompts and models |
| `results.json` | the same numbers, for the write-up |
| `figures/*.svg` | detection recall and MTTD, triage scores, ablations, confusion heat maps |
| `../README.md` | the results table between the `results:start` and `results:end` markers, and the fault types A1 got wrong between `wrong:start` and `wrong:end` |
| `../README.md`, `../docs/blog.md` | every `<!-- n:key -->` number span (see below) |

It refuses runs whose prompt or model is not the frozen one, and a final run
that has not completed (unless `PARTIAL=1`, for a run the cap stopped).
`SET=tuning` writes the same report for the tuning set, next to the labels
instead of into `eval/`: it guided tuning and is not a result.

Nothing is measured yet: the dataset is recorded first (docs/design.md section 11).

## Week 11: the write-up's numbers and postmortem drafts

**Numbers.** A number in the README or the blog post is written as a span
that names its key, `<!-- n:triage.A1.top1 -->81%<!-- /n -->`. The keys are
in [harness/writeup.py](harness/writeup.py): `faults`, `fault_types`, `seeds`,
`spent`, `detect.<B0|B1|B2>.<recall|mttd|false_alarms|noise>`,
`triage.<B2|A1..A4>.<top1|top3|top1_e2e|action|unsafe|cost|cost_total|latency>`,
`mcnemar.<p|a1_only|b2_only>`, `ablation.<A2|A3|A4>.<top1|action>` and
`mttr.<change|with|without|faults>`.

- `make results` fills them; `make writeup` does it again by itself and prints
  the CV entry with the same numbers.
- `harness writeup --check` runs in `make aiops-unit`, so in CI. It fails on a
  span that differs from `results.json` or names an unknown key. Before the
  final run there is no `results.json`, and every span must read "–".
- Only a test-set `results.json` is accepted.

**Postmortems.** `make postmortem RUN=<live-test run>` lists the run's faults
with their incidents, whether root cause and action were right, the time to
recover, and what went wrong. `SEQ=<n>` writes a draft in
`docs/postmortems/`. The timeline, detection, trigger, resolution, verdict,
evidence and links are filled from the live store (`~/.incidentlens/live/state.db`)
and the run's sealed labels; the parts a person writes are marked. See
[docs/postmortems/README.md](../docs/postmortems/README.md).

# Postmortems

Blameless postmortems, in the [Google SRE format](https://sre.google/sre-book/postmortem-culture/),
for the most interesting incidents of the live test (week 9's `make live-test`,
run with `make console` going). The faults were injected on purpose, so the
root cause is known. A postmortem here is about the rest: how the system
noticed, what the agent concluded and from which evidence, what was proposed
and approved, how long recovery took, and what should change.

None written yet. They need the live test, which needs a running cluster and
an operator (design doc §13).

| Date | Incident | What went wrong |
|---|---|---|
| | | |

## Writing one

1. `make postmortem RUN=<live-test run>` lists every fault of the run. Each
   one shows its incidents, whether the root cause and action were right, the
   time to recover, and what is worth a look: missed, wrong root cause, wrong
   or unsafe action, escalated, a rejected or failed fix, slow detection or
   recovery, split into several incidents, a false alarm.
2. Pick three that teach something different. A clean success isn't a good
   postmortem.
3. `make postmortem RUN=<live-test run> SEQ=<n>` writes
   `docs/postmortems/<date>-<fault>-<target>.md`. The timeline, detection,
   trigger, resolution, the verdict and its evidence, and the runbook and
   Grafana links come from the live store, its audit log and the run's sealed
   labels.
4. Write every part marked _To write_: the summary, the impact on users (from
   the SLO dashboard the draft links), the mechanism, the lessons, and the
   action items. Then set Status to Final and add a row above.

A draft never overwrites an existing file unless you pass `FORCE=1`.

## Rules

- **Blameless.** Name systems and decisions, not people. The draft writes
  "the operator" for whoever approved. `--keep-names` keeps the names, which
  are in the hash-chained audit log either way.
- **Facts from the record.** If the timeline is wrong, the record is wrong;
  find out why instead of editing the row.
- **Action items are changes.** Each one says what kind it is (mitigate,
  prevent, detect) and links an issue. "Be more careful" isn't one.

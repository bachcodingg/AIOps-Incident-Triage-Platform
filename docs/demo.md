# The demo video (3 minutes)

What the plan's video shows: a bad release lands, the agent explains it with
links, the operator approves the rollback, and the SLO recovers. It's one take
on kind, with cuts only where the system is waiting (say so in the video's
description).

The fault is the evaluation's F02, a release that answers a share of requests
with HTTP 500s. Here it goes to catalog at 10%, promoted past the canary the
way a hotfix would be (`make demo-fault`). The automatic case, a canary that
fails its analysis and aborts with nobody involved, is `make canary-test`,
which CI runs on every pull request. Mention it in one sentence; it's not this
video's story.

## Before recording

1. `make up`, then `make grafana` (keep the URL and password).
2. Wait 20 minutes after `make up`: the SLO panels and the anomaly detector
   need some history.
3. In a second terminal, start the loop and console:
   `ANTHROPIC_API_KEY=... make console VARIANT=A1`
   (`VARIANT=B2` is free and works the same way, but its explanation is one
   rule, not the agent's evidence). Open http://127.0.0.1:8090.
4. Arrange the screen with the console on the left and Grafana's *SLOs*
   dashboard on the right, last 30 minutes, refresh every 10 s.
5. Do one rehearsal: `make demo-fault`, approve, wait for recovery. Then
   restart `make console` with an empty store
   (`rm ~/.incidentlens/live/state.db`) so the take starts clean.

An A1 triage costs a few cents (design doc section 6 targets under $0.05).
The rehearsal and the take together stay under $0.50.

## Shot list

| Time | On screen | Say |
|---|---|---|
| 0:00–0:20 | the README: one sentence, the results table, the architecture | what it is and the one number that matters (A1 against B2 on the test set) |
| 0:20–0:40 | Grafana SLOs, all green; a terminal running `make demo-fault` | "a catalog release that fails 10% of product lookups, promoted past the canary" |
| 0:40–1:10 | the burn-rate alert fires, the incident appears in the console | which detectors fired, and how long after the release (read it off the timeline) |
| 1:10–1:50 | the incident page: hypotheses, evidence, the Grafana links; click one | why the agent blames the release and not PostgreSQL: the evidence rows |
| 1:50–2:10 | the proposal: rollback of catalog, one approval; type your name, approve | the policy: only aborting a canary is automatic, everything else waits for a human. The plan is re-checked before it runs |
| 2:10–2:40 | the audit log; Grafana: the error rate drops, the burn-rate alert resolves | the hash-chained log; time from release to recovery |
| 2:40–3:00 | eval/results.md: the targets table, the confusion matrix | how it was measured: a locked test set, 3 seeds, the baseline, what it got wrong |

## After recording

- Upload it (YouTube unlisted or a GitHub release asset) and replace the
  "Demo" line at the top of the README with the link.
- Add the link to LinkedIn's *Featured* section (plan: launch checklist).
- The take's own incident is a live incident like any other. If it's
  interesting, `make postmortem RUN=dryrun-<stamp> SEQ=1` drafts it
  (`make demo-fault` prints where its label went).

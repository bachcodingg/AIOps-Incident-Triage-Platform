# Does an LLM agent triage Kubernetes incidents better than a simple rule? I broke a cluster <!-- n:faults -->–<!-- /n --> times to find out

> **Draft.** It gets published on dev.to with a canonical link back here. Every
> number is a span filled from [eval/results.json](../eval/results.json) by
> `make writeup`, and CI fails if one drifts. "–" means not measured yet. The
> parts in _italics_ wait for the final run; rewrite the rest in your own voice
> before publishing.

Most AIOps demos show an agent explaining one incident nicely. I wanted the
number behind it: on incidents the agent has never seen, does it name the root
cause more often than a plain rule does? And does it do that safely, cheaply
and fast enough to matter?

So I built a small production-style platform, broke it on purpose 14 different
ways, and measured an LLM triage agent against threshold alerts, SLO burn-rate
alerts and a rule-based baseline. The test set was locked before any tuning.

## The setup

**The platform.** ShopSearch has four services in three languages (a
TypeScript gateway, a Python search API, a Java catalog and a Python indexer)
over PostgreSQL and Redis. It runs on Kubernetes, built with Terraform (AWS EKS
and OCI modules, kind locally) and deployed only through ArgoCD, with Argo
Rollouts canaries and Kyverno policies. OpenTelemetry sends metrics, logs and
traces to Prometheus, Loki and Tempo. Sloth turns four SLOs into multiwindow
burn-rate alerts.

**The faults.** There are 14 fault types: bad releases (crash loop, HTTP 500s,
slow responses, a slow query), resource faults (memory leak, CPU, connection
pool, disk), network faults (latency, packet loss, DNS), dependencies (Redis
down, node loss) and a traffic spike. Each one is injected 5 times at different
intensities and times of day, plus 20 fault-free windows. `chaos-runner`
writes the ground truth to a hash-chained file outside the repository. The
service account the AIOps layer reads as can't see the chaos namespace at all.

**The variants.**

| | Detection | Root cause |
|---|---|---|
| B0 | static thresholds | – |
| B1 | SLO burn-rate alerts | – |
| B2 | burn-rate + anomaly detection | a rule: the most upstream service whose errors rose first |
| A1 | same as B2 | an LLM agent with 8 read-only tools: metrics, logs, traces, deploy history, runbooks |
| A2 / A3 / A4 | same | A1 without traces / without runbooks / on a small open-weight model |

## Keeping myself honest

The easiest way to get a good number is to tune on the test set without
noticing. Four rules made that hard:

1. **Gate G2.** The dataset was split into 30 tuning and 40+ test incidents,
   and the test set's hash was committed before the agent existed.
2. **Replay, with a cut-off.** The agent triages a recording, and its tools
   can't return anything recorded after the moment of triage. The live loop
   goes through the same code.
3. **Gate G3.** Before the first test incident was triaged, a lock file
   recorded the hash of every prompt, model, detector, scorer and runbook. A
   resumed run refuses a changed system, and CI fails any later change to
   those files.
4. **One script makes every number**, including the ones in this post. The
   intervals are bootstrapped over incidents, not runs, because three seeds of
   one incident aren't independent.

## Results

On <!-- n:faults -->–<!-- /n --> held-out incidents across
<!-- n:fault_types -->–<!-- /n --> fault types, <!-- n:seeds -->–<!-- /n -->
seeds each:

**Detection.** Median time to detect went from
<!-- n:detect.B0.mttd -->–<!-- /n --> with static thresholds to
<!-- n:detect.B1.mttd -->–<!-- /n --> with burn-rate alerts and
<!-- n:detect.B2.mttd -->–<!-- /n --> with anomaly detection added. Recall
within 10 minutes was <!-- n:detect.B0.recall -->–<!-- /n -->,
<!-- n:detect.B1.recall -->–<!-- /n --> and
<!-- n:detect.B2.recall -->–<!-- /n -->. False alarms per 24 hours of
fault-free traffic were <!-- n:detect.B0.false_alarms -->–<!-- /n -->,
<!-- n:detect.B1.false_alarms -->–<!-- /n --> and
<!-- n:detect.B2.false_alarms -->–<!-- /n -->. Correlation cut alerts per
incident by <!-- n:detect.B2.noise -->–<!-- /n -->.

**Root cause.** The agent named the right service and category first in
<!-- n:triage.A1.top1 -->–<!-- /n --> of detected incidents, against
<!-- n:triage.B2.top1 -->–<!-- /n --> for the rule. In the top three it was
<!-- n:triage.A1.top3 -->–<!-- /n -->. On paired incidents, the agent was
right where the rule was wrong <!-- n:mcnemar.a1_only -->–<!-- /n --> times,
and the reverse happened <!-- n:mcnemar.b2_only -->–<!-- /n --> times
(McNemar's exact test, p = <!-- n:mcnemar.p -->–<!-- /n -->).

**Action.** It proposed the right action in
<!-- n:triage.A1.action -->–<!-- /n --> of incidents (the rule:
<!-- n:triage.B2.action -->–<!-- /n -->). There were
<!-- n:triage.A1.unsafe -->–<!-- /n --> unsafe proposals, meaning a write to a
component the root cause doesn't name, or any write when the right answer was
to escalate.

**What each part is worth.** Taking away traces changed top-1 accuracy by
<!-- n:ablation.A2.top1 -->–<!-- /n -->. Taking away runbooks changed it by
<!-- n:ablation.A3.top1 -->–<!-- /n -->, and a small open-weight model changed
it by <!-- n:ablation.A4.top1 -->–<!-- /n -->.

**Time to recover.** In a live test of <!-- n:mttr.faults -->–<!-- /n -->
incidents with the loop running and one human approval per fix, median time to
recover was <!-- n:mttr.with -->–<!-- /n -->, against
<!-- n:mttr.without -->–<!-- /n --> without it
(<!-- n:mttr.change -->–<!-- /n -->).

_What the numbers mean: write this after G3. Which targets were met and which
missed (eval/results.md has the table), and whether the intervals can decide._

## What worked

- **Locking things early.** Committing hashes before looking at results cost
  almost nothing and removed a whole class of doubt, including my own.
- **The same code path live and offline.** The live loop builds the agent's
  view through the replay code, so the system evaluated offline is the system
  that proposed fixes on the cluster.
- **A narrow remediator.** The agent's identity can't write anything. A
  second service account can patch fourteen named objects in four ways. Only
  aborting a canary in progress is automatic; everything else waits for one
  click, and the plan is checked again before it runs.
- **CI on kind.** Every pull request builds the whole stack on a GitHub
  runner, rolls a good and a bad canary, follows a request to its database
  span, and checks the AIOps accounts' rights. Each fault also has its own
  dry run on a fresh cluster.

_After G3: what the agent was good at, by fault type._

## What failed

- **A stacked pull request that never landed.** Week 5's PR was based on
  week 4's branch and got merged there after week 4 had already reached main,
  so week 5 silently never made it to main. Now every PR targets main, and the
  description says when the diff will shrink.
- **A test that assumed how old the cluster was.** The detection test recorded
  a fixed window 11 to 6 minutes in the past. When the CI runners got faster
  and the cluster came up in 8 minutes instead of 15, the window landed before
  the first request and the test failed with no gateway traffic. It now waits
  for the condition (8 minutes of traffic) instead of a time.
- **The release bot couldn't open its own PR.** The pipeline that bumps image
  tags in Git kept failing because a repository setting blocked GitHub
  Actions from creating pull requests. The fix was one checkbox.
- **My first evaluation design skipped a baseline.** It went straight from
  static thresholds to burn-rate plus anomaly detection, with nothing in
  between. I added burn-rate-only (B1) before measuring anything; without it,
  any gain would have been impossible to attribute.

_After G3: the fault types the agent got wrong and why (the README's "What did
not work" lists them), and the postmortems linked from docs/postmortems/._

## What it cost

- **Infrastructure:** €0. kind on a laptop for development and the dataset,
  GitHub Actions and GHCR for CI, AWS credits for the EKS sessions.
- **The model:** <!-- n:spent -->–<!-- /n --> for the whole final evaluation.
  That's <!-- n:triage.A1.cost -->–<!-- /n --> per incident for A1, at a
  median <!-- n:triage.A1.latency -->–<!-- /n --> per triage. Tuning ran on a
  local open-weight model for free.
- **Time:** _the hours it actually took (the plan budgeted about 170 over 11 weeks)._

## What I'd do differently

_To write after G3._

---

Code, data and every number: [github.com/bachcodingg/AIOps-Incident-Triage-Platform](https://github.com/bachcodingg/AIOps-Incident-Triage-Platform).
The [design doc](design.md) has the decisions,
[eval/results.md](../eval/results.md) the full results, and
[docs/postmortems](postmortems/) the postmortems.

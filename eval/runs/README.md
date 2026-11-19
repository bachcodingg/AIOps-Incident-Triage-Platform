# eval/runs/

Every triage run, one JSON file each, written by `make triage`
(`harness triage`): `<dataset run>/<set>/<variant>/s<seed>/<incident>.json`.

A run holds what anyone needs to audit one verdict: the system prompt and its
hash, the incident brief, every model call (tokens, cost, stop reason, the model
that answered), every tool call with its arguments and output, the raw answer,
the verdict, and whether it validated or fell back to `none_escalate`. The
format is `Run` in [aiops/triage/agent.py](../../aiops/triage/agent.py).

Runs hold no labels: which fault an incident was and whether the verdict was
right are kept next to the sealed labels. Empty until the dataset is recorded
(gate G2).

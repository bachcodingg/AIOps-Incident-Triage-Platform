# aiops/

The AIOps loop. Starts after gate G1 (platform observable, end of week 4).

- `detector/` (week 6): static thresholds, SLO burn-rate, rolling median/MAD + Isolation Forest
- `correlator/` (week 6): groups alerts in a 5-minute window using the service graph
- `triage_agent/` (week 8): LLM agent with read-only, typed tools; strict JSON verdicts
- `rule_rca/` (week 8): non-LLM baseline that blames the most upstream service whose error rate rose first
- `remediator/` (week 9): `rollback`, `restart`, `scale_out`, `none_escalate`, behind policy and approval

See [docs/design.md](../docs/design.md) sections 6 and 7.

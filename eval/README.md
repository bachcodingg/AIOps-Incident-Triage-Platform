# eval/

Evaluation harness and results. Rules (from [docs/design.md](../docs/design.md#6-evaluation-plan)):

- tune on 30 incidents, report on 40+ locked test incidents only; the test set hash is committed before week 8
- 3 seeds per variant, bootstrap 95% CIs, McNemar's test for A1 vs B2
- every agent run stored as JSON in `runs/`
- `make results` writes `results.md` and the charts (week 10)

Nothing here yet: the dataset is recorded in week 6.

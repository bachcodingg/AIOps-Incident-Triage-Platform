# chaos/

- [`catalog.yaml`](catalog.yaml): draft fault catalog (F01–F14), the single source for fault IDs, targets, labels and correct actions.
- `experiments/` (week 5): Chaos Mesh manifests and broken-release definitions per fault.
- `runner/` (week 5): `chaos-runner`, schedules faults at random times and writes ground truth to a sealed labels file the AIOps layer cannot read.

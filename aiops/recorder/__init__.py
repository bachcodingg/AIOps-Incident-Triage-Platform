"""Telemetry recorder (week 6): the evaluation dataset's eyes.

It exports what the AIOps layer can see, in 5-minute chunks, for as long as a
dataset run lasts: a fixed set of metrics, the static and burn-rate alerts,
Kubernetes events, deploy history, warning and error logs, and slow or failed
traces. Replay (week 8) and the evaluation (week 10) read these chunks instead
of a live cluster, so every result can be repeated.

Two rules keep the dataset honest:
  - it knows nothing about the faults. It records the whole timeline in fixed
    chunks, never "the 30 minutes around fault 17", so where a chunk starts
    says nothing about where a fault starts
  - it reads the cluster as the `aiops` service account (gitops/aiops), which
    cannot see the chaos namespace, and its Prometheus queries are a fixed list
    scoped to the workload, so nothing chaos-runner leaves behind ends up in it
"""

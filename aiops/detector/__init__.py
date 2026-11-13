"""Detectors (week 6). Three run side by side so the evaluation can compare them:

  static     thresholds in Prometheus (gitops/observability/files/static-rules.yaml), baseline B0
  burn-rate  Sloth's multiwindow SLO alerts (gitops/observability/files/slos.yaml), baseline B1
  anomaly    rolling median/MAD per RED metric and an Isolation Forest per component (this package)

The first two fire inside Prometheus; this package reads them back from the
recorded ALERTS series, so all three end up as the same Alert records.
"""

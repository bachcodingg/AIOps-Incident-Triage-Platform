"""The one alert format every detector produces and the correlator consumes."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATIC, BURN_RATE, ANOMALY = "static", "burn-rate", "anomaly"
DETECTORS = (STATIC, BURN_RATE, ANOMALY)

# Components the correlator and, later, the agent reason about. `node` and
# `cluster` are infrastructure: a fault there can explain an alert anywhere.
SERVICES = ("gateway", "search-api", "catalog", "indexer")
DATA_STORES = ("redis", "postgres")
INFRASTRUCTURE = ("node", "cluster")
COMPONENTS = SERVICES + DATA_STORES + INFRASTRUCTURE

# Which component an SLO alert belongs to. The SLOs are measured at the gateway,
# where users meet the system; the route says which call chain is hurting.
SLO_COMPONENT = {
    "search-availability": "gateway",
    "search-latency": "gateway",
    "products-availability": "gateway",
    "products-latency": "gateway",
}


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str | float | int | None) -> float | None:
    if value is None or isinstance(value, (int, float)):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def component_of(labels: dict[str, str]) -> str:
    """The component an alert is about, from the labels its rule set."""
    if labels.get("component"):
        return labels["component"]
    if slo := labels.get("sloth_slo"):
        return SLO_COMPONENT.get(slo, "gateway")
    job = labels.get("job", "")
    if job.startswith("shopsearch/"):
        return job.split("/", 1)[1]
    if labels.get("container") in COMPONENTS:
        return labels["container"]
    if "node" in labels:
        return "node"
    return "unknown"


@dataclass(frozen=True)
class Alert:
    detector: str            # static | burn-rate | anomaly
    name: str                # alertname, e.g. StaticHighErrorRate, AnomalyMAD
    component: str           # see COMPONENTS
    start: float             # unix seconds, when it started firing
    end: float | None        # when it stopped; None if still firing when the data ends
    severity: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    score: float | None = None  # anomaly score at its peak, for anomaly alerts

    @property
    def key(self) -> tuple:
        """Identity of the alert rule instance, the same across firings."""
        return (self.detector, self.name, self.component, tuple(sorted(self.labels.items())))

    def active_at(self, t: float) -> bool:
        return self.start <= t and (self.end is None or t < self.end)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"], d["end"] = iso(self.start), iso(self.end)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Alert:
        return cls(**{**d, "start": parse_time(d["start"]), "end": parse_time(d.get("end"))})


def write_jsonl(path: Path, records: Iterable[Any]) -> int:
    n = 0
    with Path(path).open("w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r.to_dict() if hasattr(r, "to_dict") else r, sort_keys=True) + "\n")
            n += 1
    return n


def read_alerts(path: Path) -> list[Alert]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [Alert.from_dict(json.loads(line)) for line in lines if line.strip()]

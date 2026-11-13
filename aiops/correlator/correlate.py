"""Group alerts into incidents.

Alerts are taken in the order they started. A new alert joins an open incident
if one of the incident's components is the same as, or a neighbour of, its own
in the service graph (infrastructure is everyone's neighbour). If it is related
to several open incidents, they were one incident all along and are merged:
PostgreSQL and the gateway are not neighbours, so their alerts start apart,
and the catalog's alert is what connects them.

An incident stays open for new alerts while it is active:
  - an alert of it started less than `window` ago (5 minutes, as in the plan), or
  - an alert of it is still firing and started less than `hold` ago.
The second rule keeps a 15-minute fault in one incident when its late alerts
(a 5-minute `for`, a slow burn rate) come after a lull. The `hold` cap stops a
burn-rate ticket that fires for hours from swallowing every later incident.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from detector.alerts import ANOMALY, BURN_RATE, STATIC, Alert, iso

from .graph import ServiceGraph

WINDOW_SECONDS = 300
HOLD_SECONDS = 1800

# Which detectors each evaluation variant uses (docs/design.md section 6).
VARIANTS = {
    "B0": (STATIC,),
    "B1": (BURN_RATE,),
    "B2": (BURN_RATE, ANOMALY),
    "A1": (BURN_RATE, ANOMALY),
}


@dataclass(eq=False)  # identity, not field equality: incidents are merged and removed by object
class Incident:
    alerts: list[Alert] = field(default_factory=list)
    id: str = ""

    @property
    def opened(self) -> float:
        return min(a.start for a in self.alerts)

    @property
    def closed(self) -> float | None:
        if any(a.end is None for a in self.alerts):
            return None
        return max(a.end for a in self.alerts if a.end is not None)

    @property
    def components(self) -> list[str]:
        return sorted({a.component for a in self.alerts})

    @property
    def detectors(self) -> list[str]:
        return sorted({a.detector for a in self.alerts})

    def active_at(self, t: float, window_s: float, hold_s: float) -> bool:
        return any(t - a.start <= window_s or (a.active_at(t) and t - a.start <= hold_s) for a in self.alerts)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "opened": iso(self.opened), "closed": iso(self.closed),
                "components": self.components, "detectors": self.detectors, "alert_count": len(self.alerts),
                "alerts": [a.to_dict() for a in sorted(self.alerts, key=lambda a: a.start)]}


def correlate(alerts: Iterable[Alert], graph: ServiceGraph, *, window_s: float = WINDOW_SECONDS,
              hold_s: float = HOLD_SECONDS) -> list[Incident]:
    open_: list[Incident] = []
    done: list[Incident] = []
    for alert in sorted(alerts, key=lambda a: (a.start, a.component, a.name)):
        t = alert.start
        still_open = []
        for inc in open_:
            (still_open if inc.active_at(t, window_s, hold_s) else done).append(inc)
        open_ = still_open
        related = [inc for inc in open_ if any(graph.related(alert.component, c) for c in inc.components)]
        if not related:
            open_.append(Incident([alert]))
            continue
        target = min(related, key=lambda inc: inc.opened)
        for other in related:
            if other is not target:
                target.alerts.extend(other.alerts)
                open_.remove(other)
        target.alerts.append(alert)
    incidents = sorted(done + open_, key=lambda inc: inc.opened)
    for n, inc in enumerate(incidents, start=1):
        inc.id = f"inc-{n:04d}"
    return incidents


def select(alerts: Iterable[Alert], detectors: Iterable[str]) -> list[Alert]:
    wanted = set(detectors)
    return [a for a in alerts if a.detector in wanted]

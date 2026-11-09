"""A dataset run's plan: which fault, on what, how hard, for how long, in what order.

The plan is drawn from a seeded random generator, so a run can be reproduced
by whoever holds the seed. The seed is part of the sealed labels; nothing on
the AIOps side can predict when the next fault starts.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass

from .catalog import Catalog

FAULT, FAULT_FREE, BENIGN = "fault", "fault-free", "benign-release"


@dataclass(frozen=True)
class Item:
    id: str
    kind: str  # fault | fault-free | benign-release
    duration_s: int
    gap_s: int  # quiet time after the item, once the system has recovered
    fault: str | None = None
    target: str | None = None
    intensity: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Item:
        return cls(**d)


def _round(seconds: float, step: int = 30) -> int:
    return max(step, int(round(seconds / step)) * step)


def plan(
    catalog: Catalog,
    seed: int,
    *,
    faults: list[str] | None = None,
    repeats: int | None = None,
    fault_free: int | None = None,
    benign: int | None = None,
) -> list[Item]:
    """Every selected fault `repeats` times, plus fault-free windows and benign
    releases, shuffled. Each fault's targets are used in turn and each target's
    intensities in turn, both from a random starting point, so a fault repeated
    5 times covers all its targets and intensities rather than whatever the dice
    happened to pick."""
    rng = random.Random(seed)
    s = catalog.schedule
    repeats = s.get("repeats", 5) if repeats is None else repeats
    fault_free = s.get("fault_free_windows", 0) if fault_free is None else fault_free
    benign = s.get("benign_releases", 0) if benign is None else benign
    gap_lo, gap_hi = (int(m) * 60 for m in s.get("gap_minutes", [10, 40]))
    selected = [catalog.fault(f) for f in faults] if faults else list(catalog.faults.values())

    drafts: list[dict] = []
    for fault in selected:
        targets = list(fault.targets)
        rng.shuffle(targets)
        offsets = {t: rng.randrange(len(fault.intensities_for(t))) for t in targets}
        uses = dict.fromkeys(targets, 0)
        for i in range(repeats):
            target = targets[i % len(targets)]
            levels = fault.intensities_for(target)
            intensity = levels[(offsets[target] + uses[target]) % len(levels)]
            uses[target] += 1
            drafts.append({
                "kind": FAULT,
                "fault": fault.id,
                "target": target,
                "intensity": intensity,
                "duration_s": _round(rng.uniform(*fault.duration)),
            })
    for _ in range(fault_free):
        drafts.append({"kind": FAULT_FREE, "duration_s": int(s.get("fault_free_minutes", 30)) * 60})
    rollouts = catalog.rollouts()
    for _ in range(benign):
        drafts.append({"kind": BENIGN, "target": rng.choice(rollouts), "duration_s": _round(rng.uniform(300, 900))})

    rng.shuffle(drafts)
    return [
        Item(id=f"i{n:03d}", gap_s=_round(rng.uniform(gap_lo, gap_hi), 60), **d)
        for n, d in enumerate(drafts, start=1)
    ]


def summary(items: list[Item]) -> str:
    faults = [i for i in items if i.kind == FAULT]
    hours = sum(i.duration_s + i.gap_s for i in items) / 3600
    return (
        f"{len(faults)} fault injections over {len({i.fault for i in faults})} faults, "
        f"{sum(i.kind == FAULT_FREE for i in items)} fault-free windows, "
        f"{sum(i.kind == BENIGN for i in items)} benign releases; "
        f"about {hours:.0f} h plus recovery time"
    )

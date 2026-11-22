"""Statistics for the results (docs/design.md section 6): bootstrap 95%
confidence intervals and McNemar's test.

The unit of resampling is the incident, not the run. A variant triages every
test incident with 3 seeds, and the three runs of one incident are not three
independent samples: they share the incident. So the bootstrap draws incidents
with replacement and takes all their runs along (a cluster bootstrap). A share
is a ratio, sum(successes) / sum(trials), over the drawn incidents; incidents
with no trial (a fault the detectors missed, for a score over triaged
incidents) weigh nothing.

Every resampling uses a fixed seed, so `make results` writes the same numbers
twice.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import comb

import numpy as np

RESAMPLES = 10_000
SEED = 20260111  # the plan's week 10


@dataclass(frozen=True)
class Estimate:
    value: float | None
    low: float | None
    high: float | None
    n: int  # incidents behind it

    def as_dict(self) -> dict:
        return {"value": self.value, "low": self.low, "high": self.high, "n": self.n}


NONE = Estimate(None, None, None, 0)


def _rng() -> np.random.Generator:
    return np.random.default_rng(SEED)


def ratio(num: Sequence[float], den: Sequence[float], resamples: int = RESAMPLES) -> Estimate:
    """sum(num) / sum(den) with a percentile CI, resampling clusters (one entry per incident)."""
    num_a, den_a = np.asarray(num, float), np.asarray(den, float)
    if den_a.sum() == 0:
        return NONE
    idx = _rng().integers(0, len(num_a), (resamples, len(num_a)))
    d = den_a[idx].sum(axis=1)
    keep = d > 0
    boot = num_a[idx].sum(axis=1)[keep] / d[keep]
    low, high = np.percentile(boot, [2.5, 97.5])
    return Estimate(float(num_a.sum() / den_a.sum()), float(low), float(high), int((den_a > 0).sum()))


def paired_difference(num_a: Sequence[float], den_a: Sequence[float], num_b: Sequence[float],
                      den_b: Sequence[float], resamples: int = RESAMPLES) -> Estimate:
    """ratio(a) - ratio(b) on the same incidents, drawn together."""
    na, da, nb, db = (np.asarray(x, float) for x in (num_a, den_a, num_b, den_b))
    if da.sum() == 0 or db.sum() == 0:
        return NONE
    idx = _rng().integers(0, len(na), (resamples, len(na)))
    sa, sb = da[idx].sum(axis=1), db[idx].sum(axis=1)
    keep = (sa > 0) & (sb > 0)
    boot = na[idx].sum(axis=1)[keep] / sa[keep] - nb[idx].sum(axis=1)[keep] / sb[keep]
    low, high = np.percentile(boot, [2.5, 97.5])
    return Estimate(float(na.sum() / da.sum() - nb.sum() / db.sum()), float(low), float(high),
                    int(((da > 0) & (db > 0)).sum()))


def statistic(clusters: Sequence[Sequence[float]], fn: Callable[[np.ndarray], float] = np.median,
              resamples: int = 2_000) -> Estimate:
    """Any statistic of the pooled values (a median time, a mean cost), resampling clusters."""
    groups = [np.asarray(c, float) for c in clusters]
    pooled = [g for g in groups if len(g)]
    if not pooled:
        return NONE
    rng, boot = _rng(), []
    for _ in range(resamples):
        drawn = [groups[i] for i in rng.integers(0, len(groups), len(groups))]
        values = np.concatenate(drawn) if drawn else np.array([])
        if len(values):
            boot.append(fn(values))
    low, high = np.percentile(boot, [2.5, 97.5])
    return Estimate(float(fn(np.concatenate(pooled))), float(low), float(high), len(pooled))


@dataclass(frozen=True)
class McNemar:
    a_only: int   # incidents a gets right and b wrong
    b_only: int
    both: int
    neither: int
    p_value: float

    def as_dict(self) -> dict:
        return {"a_only": self.a_only, "b_only": self.b_only, "both": self.both, "neither": self.neither,
                "p_value": self.p_value}


def mcnemar(a: Sequence[bool], b: Sequence[bool]) -> McNemar:
    """Exact (binomial) McNemar test on paired outcomes, two-sided.

    Only the discordant pairs count: under the null hypothesis, an incident one
    variant gets right and the other wrong is equally likely to go either way.
    The exact form, because with 40 incidents the discordant count is small.
    """
    if len(a) != len(b):
        raise ValueError("McNemar needs paired outcomes")
    a_only = sum(1 for x, y in zip(a, b) if x and not y)
    b_only = sum(1 for x, y in zip(a, b) if y and not x)
    both = sum(1 for x, y in zip(a, b) if x and y)
    n, k = a_only + b_only, min(a_only, b_only)
    p = 1.0 if n == 0 else min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)
    return McNemar(a_only, b_only, both, len(a) - a_only - b_only - both, p)


def majority(outcomes: Sequence[bool]) -> bool:
    """An incident counts as right for a variant if more than half its seeds got it right."""
    return sum(bool(o) for o in outcomes) * 2 > len(outcomes)

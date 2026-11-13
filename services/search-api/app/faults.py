"""Chaos flags for deliberately bad releases (chaos/catalog.yaml).

A bad release is the normal image with one of these variables baked in, so the
release history shows only a new image tag, and the fault behaves like a real
bug: an unhandled exception with a traceback, a slow code path, or memory that
is never freed. Nothing in a response or a log line says it was injected.

  FAULT_ERROR_RATE           F02: share of searches that raise
  FAULT_LATENCY_MS           F03: extra time spent on every search
  FAULT_LEAK_KB_PER_REQUEST  F04: memory kept forever after every search
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


def _number(env: Mapping[str, str], name: str, low: float, high: float | None = None) -> float:
    raw = env.get(name, "")
    if raw == "":
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value < low or (high is not None and value > high):
        bounds = f"between {low:g} and {high:g}" if high is not None else f"{low:g} or more"
        raise ValueError(f"{name} must be {bounds}, got {raw!r}")
    return value


@dataclass
class Faults:
    error_rate: float = 0.0
    latency_ms: float = 0.0
    leak_kb_per_request: float = 0.0
    random: Callable[[], float] = random.random
    sleep: Callable[[float], None] = time.sleep
    retained: list[bytes] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Faults:
        return cls(
            error_rate=_number(env, "FAULT_ERROR_RATE", 0, 1),
            latency_ms=_number(env, "FAULT_LATENCY_MS", 0),
            leak_kb_per_request=_number(env, "FAULT_LEAK_KB_PER_REQUEST", 0),
        )

    def on_search(self, ranking: dict) -> None:
        if self.latency_ms:
            self.sleep(self.latency_ms / 1000)
        if self.leak_kb_per_request:
            # Written, not just reserved, so it shows up in the resident set.
            self.retained.append(b"\x01" * int(self.leak_kb_per_request * 1024))
        if self.error_rate and self.random() < self.error_rate:
            # The kind of bug a refactor of the ranking options would leave behind.
            ranking["boosts"]["category"]  # noqa: B018 - raises KeyError

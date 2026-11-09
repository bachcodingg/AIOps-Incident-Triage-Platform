"""Chaos flag for a deliberately bad release (chaos/catalog.yaml).

  FAULT_LEAK_KB_PER_POLL  F04: memory kept forever after every poll of the
                          change queue, the way a forgotten debug buffer leaks

A bad release is the normal image with the variable baked in, so the release
history shows only a new image tag.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass
class Faults:
    leak_kb_per_poll: float = 0.0
    retained: list[bytes] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Faults:
        raw = env.get("FAULT_LEAK_KB_PER_POLL", "")
        try:
            value = float(raw) if raw else 0.0
        except ValueError:
            raise ValueError(f"FAULT_LEAK_KB_PER_POLL must be a number, got {raw!r}") from None
        if value < 0:
            raise ValueError(f"FAULT_LEAK_KB_PER_POLL must be 0 or more, got {raw!r}")
        return cls(leak_kb_per_poll=value)

    def on_poll(self) -> None:
        if self.leak_kb_per_poll:
            # Written, not just reserved, so it shows up in the resident set.
            self.retained.append(b"\x01" * int(self.leak_kb_per_poll * 1024))

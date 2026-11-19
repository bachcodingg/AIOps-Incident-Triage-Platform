"""Who may let an action run.

  auto      a rollback of a service whose canary is in progress: aborting a
            canary only sends traffic back to the version that was serving
            before, the safest change there is (the plan's rule: automatic only
            for a failing canary)
  approval  every other rollback, restart or scale-out: one click by a human
  notify    none_escalate, none_verify, or no valid verdict: nothing to run,
            a human takes over
  refused   the same action on the same target ran less than 30 minutes ago,
            or more than 3 actions ran in the last hour: repeating a fix that
            did not work is how an agent makes an incident worse

The policy sees the verdict and the cluster, never the labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

COOLDOWN_S = 1800
MAX_PER_HOUR = 3
WRITE_ACTIONS = ("rollback", "restart", "scale_out")


@dataclass(frozen=True)
class Decision:
    mode: str      # auto | approval | notify | refused
    reason: str


def decide(verdict: dict[str, Any], *, canary_in_progress: bool, executed: list[dict[str, Any]], now: float) -> Decision:
    """`executed`: earlier actions that ran, as {"action", "target", "at"} (unix seconds)."""
    action, target = verdict.get("action"), verdict.get("action_target")
    if verdict.get("fallback"):
        return Decision("notify", f"no valid verdict ({verdict['fallback']}); a human takes over")
    if action not in WRITE_ACTIONS:
        return Decision("notify", f"{action}: nothing to run; a human takes over")
    recent = [e for e in executed if now - e["at"] < 3600]
    same = [e for e in recent if e["action"] == action and e["target"] == target and now - e["at"] < COOLDOWN_S]
    if same:
        return Decision("refused", f"{action} {target} already ran {int((now - same[-1]['at']) / 60)} min ago; "
                                   f"doing it again will not help, escalate")
    if len(recent) >= MAX_PER_HOUR:
        return Decision("refused", f"{len(recent)} actions in the last hour; escalate before changing more")
    if action == "rollback" and canary_in_progress:
        return Decision("auto", f"{target} has a canary in progress: aborting it puts users back on the stable version")
    return Decision("approval", f"{action} {target} changes a running system: one approval needed")

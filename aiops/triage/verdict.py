"""The verdict: what the agent and the rule baseline both answer, in one strict format.

  hypotheses  up to 3 root causes, most likely first (RCA top-1 and top-3)
  evidence    what was looked at and what it showed, so a human can check it
  action      one of a fixed list, with the component it applies to

The labels the evaluation compares against use the same vocabulary
(chaos/catalog.yaml: root_cause and correct_action), but the agent never sees them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from detector.alerts import DATA_STORES, SERVICES

CATEGORIES = ("bad-release", "resource", "load", "network", "dns", "dependency", "infrastructure")
ACTIONS = ("rollback", "restart", "scale_out", "none_escalate", "none_verify")
# What each action can be applied to. Rollback goes through an Argo Rollout, so
# only the four services have one; a data store can be restarted, not rolled back.
ACTION_TARGETS = {
    "rollback": SERVICES,
    "restart": SERVICES + DATA_STORES,
    "scale_out": SERVICES,
}
# Root-cause components: a service, a data store, the node, the cluster, or the
# link between two components ("catalog->postgres").
ENDPOINTS = SERVICES + DATA_STORES
COMPONENTS = ENDPOINTS + ("node", "cluster")

Category = Literal["bad-release", "resource", "load", "network", "dns", "dependency", "infrastructure"]
Action = Literal["rollback", "restart", "scale_out", "none_escalate", "none_verify"]


def normalise_component(value: str) -> str:
    v = value.strip().lower().replace(" ", "")
    v = {"postgresql": "postgres", "kubernetes": "cluster", "coredns": "cluster", "dns": "cluster"}.get(v, v)
    for sep in ("<->", "→", "--"):
        v = v.replace(sep, "->")
    return "->".join({"postgresql": "postgres"}.get(p, p) for p in v.split("->"))


def valid_component(value: str) -> bool:
    if value in COMPONENTS:
        return True
    parts = value.split("->")
    return len(parts) == 2 and all(p in ENDPOINTS for p in parts) and parts[0] != parts[1]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hypothesis(_Strict):
    component: str = Field(description=f"one of {', '.join(COMPONENTS)}, or a link written 'caller->callee', e.g. 'catalog->postgres'")
    category: Category
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=600, description="one or two sentences: why this cause, and not the others")

    @field_validator("component")
    @classmethod
    def _component(cls, v: str) -> str:
        v = normalise_component(v)
        if not valid_component(v):
            raise ValueError(f"{v!r} is not a component: use one of {', '.join(COMPONENTS)} or 'a->b' between {', '.join(ENDPOINTS)}")
        return v


class Evidence(_Strict):
    tool: str = Field(description="the tool that showed it")
    query: str = Field(max_length=400, description="what was asked: the tool's arguments")
    finding: str = Field(max_length=600, description="what the result showed, with numbers and times")


class Verdict(_Strict):
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=3, description="most likely first")
    evidence: list[Evidence] = Field(min_length=1, max_length=10)
    action: Action
    action_target: str | None = Field(description="the component the action applies to; null for none_escalate and none_verify")
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(max_length=800, description="what happened, for the on-call engineer, in two or three sentences")

    @model_validator(mode="after")
    def _target(self) -> Verdict:
        allowed = ACTION_TARGETS.get(self.action)
        if allowed is None:
            self.action_target = None
            return self
        target = normalise_component(self.action_target or "")
        if target not in allowed:
            raise ValueError(f"action {self.action} needs action_target, one of {', '.join(allowed)}; got {self.action_target!r}")
        self.action_target = target
        return self


def schema() -> dict[str, Any]:
    return Verdict.model_json_schema()


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> str:
    """The JSON object in a reply: fenced if fenced, else from the first { to the last }."""
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text.strip()


def parse(text: str) -> tuple[Verdict | None, str | None]:
    """(verdict, None) if the reply holds a valid verdict, else (None, what is wrong)."""
    raw = extract_json(text)
    try:
        return Verdict.model_validate(json.loads(raw)), None
    except json.JSONDecodeError as exc:
        return None, f"not JSON: {exc}"
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()]
        return None, "; ".join(problems)[:1500]


def fallback(reason: str) -> dict[str, Any]:
    """What a run answers when it has no valid verdict: hand it to a human."""
    return {"hypotheses": [], "evidence": [], "action": "none_escalate", "action_target": None,
            "confidence": 0.0, "summary": f"No verdict ({reason}); escalated to a human.", "fallback": reason}

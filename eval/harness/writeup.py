"""`make writeup`: the numbers in the README, the blog post and the CV entry,
copied from eval/results.json and nowhere else.

A number in prose is written as a span that names where it comes from:

    the agent named the root cause in <!-- n:triage.A1.top1 -->81%<!-- /n --> of incidents

`harness writeup` rewrites every span from results.json; `--check` (run in CI)
fails when a span says something results.json does not, or names a key that
does not exist. Before the final run there is no results.json and every span
must read "–". So a number in the write-up is either the measured one or
visibly missing, never a leftover from a draft.

`--cv` prints the plan's CV entry with the numbers filled in.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from chaos_runner.catalog import REPO_ROOT

RESULTS = REPO_ROOT / "eval" / "results.json"
DOCS = (REPO_ROOT / "README.md", REPO_ROOT / "docs" / "blog.md")
SPAN = re.compile(r"<!-- n:([A-Za-z0-9_.]+) -->(.*?)<!-- /n -->", re.S)
MISSING = "–"

DETECTION = ("B0", "B1", "B2")
TRIAGE = ("B2", "A1", "A2", "A3", "A4")
ABLATIONS = {"A2": "A2 without traces", "A3": "A3 without runbooks", "A4": "A4 open-weight model"}


class WriteupError(ValueError):
    pass


# --- formatting --------------------------------------------------------------------

def _v(x: Any) -> float | None:
    return x.get("value") if isinstance(x, dict) else x


def _fmt(fn: Callable[[float], str]) -> Callable[[Any], str]:
    def f(x: Any) -> str:
        v = _v(x)
        return MISSING if v is None else fn(v)
    return f


pct = _fmt(lambda v: f"{v:.0%}")
signed_pct = _fmt(lambda v: f"{v:+.0%}".replace("-", "−"))
points = _fmt(lambda v: f"{v * 100:+.0f} pp".replace("-", "−"))
count = _fmt(lambda v: f"{v:.0f}")
rate = _fmt(lambda v: f"{v:.1f}")
seconds = _fmt(lambda v: f"{v:.0f} s")
dollars = _fmt(lambda v: f"${v:.2f}")
p_value = _fmt(lambda v: "< 0.001" if v < 0.001 else f"{v:.3f}")


@_fmt
def minutes(v: float) -> str:
    """Detection times: seconds under a minute, minutes above."""
    return f"{v * 60:.0f} s" if v < 1 else f"{v:.1f} min"


@_fmt
def cost(v: float) -> str:
    return "$0" if v == 0 else (f"${v:.3f}" if v >= 0.001 else f"${v:.4f}")


# --- the numbers -------------------------------------------------------------------

def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def numbers(report: dict[str, Any] | None) -> dict[str, str]:
    """Every key a span may name, formatted. Without a report every value is "–",
    so the set of keys is the same before and after the final run."""
    r = report or {}
    det, tri = r.get("detection") or {}, r.get("triage") or {}
    n: dict[str, str] = {
        "faults": count(r.get("faults")),
        "fault_types": count(len(r["per_fault"]) if r.get("per_fault") else None),
        "seeds": count(len(_get(r, "gate", "seeds") or []) or None),
        "spent": dollars(_get(r, "gate", "spent_usd")),
    }
    for v in DETECTION:
        d = det.get(v) or {}
        n[f"detect.{v}.recall"] = pct(d.get("recall"))
        n[f"detect.{v}.mttd"] = minutes(d.get("mttd_min"))
        n[f"detect.{v}.false_alarms"] = rate(d.get("false_alarms_per_day"))
        n[f"detect.{v}.noise"] = pct(d.get("noise_reduction"))
    for v in TRIAGE:
        t = tri.get(v) or {}
        n[f"triage.{v}.top1"] = pct(t.get("top1"))
        n[f"triage.{v}.top3"] = pct(t.get("top3"))
        n[f"triage.{v}.top1_e2e"] = pct(t.get("top1_end_to_end"))
        n[f"triage.{v}.action"] = pct(t.get("action"))
        n[f"triage.{v}.unsafe"] = count(t.get("unsafe"))
        n[f"triage.{v}.cost"] = cost(t.get("cost_mean_usd")) if t else MISSING
        n[f"triage.{v}.cost_total"] = dollars(t.get("cost_total_usd"))
        n[f"triage.{v}.latency"] = seconds(t.get("seconds_median"))
    m = _get(r, "mcnemar", "top1") or {}
    n["mcnemar.p"] = p_value(m.get("p_value"))
    n["mcnemar.a1_only"] = count(m.get("a_only"))
    n["mcnemar.b2_only"] = count(m.get("b_only"))
    for v, name in ABLATIONS.items():
        a = _get(r, "ablations", name) or {}
        n[f"ablation.{v}.top1"] = points(a.get("top1_end_to_end"))
        n[f"ablation.{v}.action"] = points(a.get("action"))
    mttr = r.get("mttr") or {}
    runs = list((mttr.get("runs") or {}).values())
    n["mttr.change"] = signed_pct(mttr.get("change"))
    n["mttr.with"] = minutes(runs[0]["median_s"] / 60 if runs and runs[0].get("median_s") is not None else None)
    n["mttr.without"] = minutes(runs[1]["median_s"] / 60 if len(runs) > 1 and runs[1].get("median_s") is not None
                                else None)
    n["mttr.faults"] = count(runs[0].get("faults") if runs else None)
    return n


def load(path: Path = RESULTS) -> dict[str, Any] | None:
    if not path.exists():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("set") != "test":
        raise WriteupError(f"{path} is not a test-set report; the write-up quotes the test set only")
    return report


# --- spans -------------------------------------------------------------------------

def spans(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in SPAN.finditer(text)]


def fill(text: str, nums: dict[str, str]) -> str:
    unknown = sorted({k for k, _ in spans(text) if k not in nums})
    if unknown:
        raise WriteupError(f"unknown number keys: {', '.join(unknown)} (the keys are in eval/harness/writeup.py)")
    return SPAN.sub(lambda m: f"<!-- n:{m.group(1)} -->{nums[m.group(1)]}<!-- /n -->", text)


def check(text: str, nums: dict[str, str]) -> list[str]:
    out = []
    for key, shown in spans(text):
        if key not in nums:
            out.append(f"{key}: no such number")
        elif shown != nums[key]:
            out.append(f"{key}: says {shown!r}, results say {nums[key]!r}")
    return out


def update(paths: tuple[Path, ...] | list[Path], nums: dict[str, str]) -> list[Path]:
    changed = []
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        new = fill(text, nums)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path)
    return changed


# --- the CV entry ------------------------------------------------------------------

CV = """\
IncidentLens: AIOps platform for Kubernetes (personal project, 2026 to 2027)
Terraform · AWS EKS · ArgoCD · Argo Rollouts · OpenTelemetry · Prometheus · Grafana · Chaos Mesh · Python · LLM agents

- Built a GitOps platform on Kubernetes from Terraform modules (AWS EKS and OCI), with signed images, Kyverno
  policy and canary releases that roll back automatically on SLO regression.
- Defined SLOs with multiwindow burn-rate alerts. Cut median time to detect from {detect.B0.mttd} (static
  thresholds) to {detect.B2.mttd} on {faults} held-out chaos incidents across {fault_types} fault types.
- Built an LLM triage agent with read-only tools for metrics, logs, traces and deploy history. It named the
  correct root cause in {triage.A1.top1} of incidents vs {triage.B2.top1} for a rule-based baseline, with
  {triage.A1.unsafe} unsafe actions, at {triage.A1.cost} per incident.
- Changed median time to recover by {mttr.change} with approval-gated remediation (live test, {mttr.faults}
  incidents). Published runbooks, blameless postmortems and the full evaluation data.
"""


def cv(nums: dict[str, str]) -> str:
    text = CV
    for key in sorted(nums, key=len, reverse=True):
        text = text.replace("{" + key + "}", nums[key])
    left = re.findall(r"\{[A-Za-z0-9_.]+\}", text)
    if left:
        raise WriteupError(f"CV template names unknown keys: {', '.join(left)}")
    return text

"""Gate G3: the test set is opened once, with a system nobody changes afterwards.

`harness final` writes eval/dataset/<run>.g3.json before it triages the first
test incident. The lock holds hashes of everything that decides a verdict or
a score:

  code      the detectors, the correlator, the triage agent and its config,
            the runbooks the agent reads, and the harness's scorers
  prompts   the system prompt of every variant, and the model it calls
  dataset   the G2 lock, and the alerts and incidents the detectors produced
            on the recording
  plan      variants and seeds

A stopped final run resumes only while every hash still matches: a prompt
changed after the test set was opened makes a new evaluation, not a resumed
one, and the lock says which file moved. When every run is done, the lock
gets `completed_at` and the hashes of the score files. Committing it is gate
G3. `harness results` refuses runs whose prompt is not the one in the lock.

The lock holds no labels and no scores, only hashes, like the G2 lock. It
cannot stop someone from deleting it and starting again; Git history and the
prompt hash inside every run in eval/runs/ make that visible.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_runner.catalog import REPO_ROOT

from .dataset import FreezeError

GATE_FORMAT = 1
# What decides a verdict or a score. Reporting (results.py, figures.py, stats.py)
# is left out on purpose: a chart can be fixed after G3, a number cannot.
FROZEN = (
    "aiops/detector/*.py", "aiops/detector/config.yaml",
    "aiops/correlator/*.py", "aiops/correlator/graph.yaml",
    "aiops/triage/*.py", "aiops/triage/config.yaml",
    "docs/runbooks/*.md",
    "eval/harness/dataset.py", "eval/harness/score.py", "eval/harness/triage.py",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    # Line endings normalised: a Windows checkout and CI hash the same file alike.
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def code_hashes(root: Path = REPO_ROOT, patterns: tuple[str, ...] = FROZEN) -> dict[str, str]:
    out = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            out[path.relative_to(root).as_posix()] = file_sha256(path)
    return out


def prompt_hashes(variants: list[str], cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    from triage.prompt import prompt_sha256, system_prompt

    out = {}
    for v in variants:
        if v == "B2":
            out[v] = {"model": "rule", "system_sha256": prompt_sha256("rule_rca")}
        else:
            spec = cfg["variants"][v]
            out[v] = {"model": spec["model"], "system_sha256": prompt_sha256(system_prompt(runbooks=spec["runbooks"])),
                      "traces": spec["traces"], "runbooks": spec["runbooks"]}
    return out


def state(*, dataset_lock: Path, recording: Path, variants: list[str], seeds: list[int], cfg: dict[str, Any],
          root: Path = REPO_ROOT) -> dict[str, Any]:
    detected = {}
    for name in ("alerts.jsonl", "incidents-B2.jsonl"):
        path = Path(recording) / name
        if not path.exists():
            raise FreezeError(f"{path} is missing: run `make detect RECORDING=... MODEL=...` first")
        detected[name] = file_sha256(path)
    return {
        "dataset_lock_sha256": file_sha256(dataset_lock),
        "detected": detected,
        "variants": list(variants),
        "seeds": list(seeds),
        "prompts": prompt_hashes(list(variants), cfg),
        "code": code_hashes(root),
    }


def differences(locked: dict[str, Any], current: dict[str, Any]) -> list[str]:
    out = []
    for key in ("dataset_lock_sha256", "variants", "seeds"):
        if locked.get(key) != current.get(key):
            out.append(f"{key}: {locked.get(key)} -> {current.get(key)}")
    for group in ("detected", "prompts", "code"):
        a, b = locked.get(group, {}), current.get(group, {})
        for name in sorted(set(a) | set(b)):
            if a.get(name) != b.get(name):
                what = "added" if name not in a else "removed" if name not in b else "changed"
                out.append(f"{group} {name}: {what}")
    return out


def open_gate(path: Path, run: str, current: dict[str, Any]) -> dict[str, Any]:
    """Write the lock, or check that the system still matches the one that opened the test set."""
    path = Path(path)
    if path.exists():
        locked = json.loads(path.read_text(encoding="utf-8"))
        changed = differences(locked, current)
        if changed:
            raise FreezeError(
                f"the test set was opened on {locked['opened_at']} with a different system ({path}):\n  "
                + "\n  ".join(changed)
                + "\nResults are frozen at gate G3. Put the files back to resume, or report what was measured.")
        return locked
    lock = {"format": GATE_FORMAT, "run": run, "opened_at": now_iso(), "completed_at": None, **current}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, indent=1) + "\n", encoding="utf-8")
    return lock


def close_gate(path: Path, scores: list[Path], spent_usd: float) -> dict[str, Any]:
    lock = json.loads(Path(path).read_text(encoding="utf-8"))
    lock["completed_at"] = now_iso()
    lock["scores_sha256"] = {Path(p).name: file_sha256(p) for p in scores}
    lock["spent_usd"] = round(spent_usd, 4)
    Path(path).write_text(json.dumps(lock, indent=1) + "\n", encoding="utf-8")
    return lock


def check_runs(lock: dict[str, Any], runs_dir: Path) -> list[str]:
    """Runs on disk whose prompt or model is not the one the lock froze."""
    problems = []
    for variant, frozen in lock["prompts"].items():
        for path in sorted((Path(runs_dir) / variant).rglob("*.json")):
            run = json.loads(path.read_text(encoding="utf-8"))
            if run.get("system_sha256") != frozen["system_sha256"]:
                problems.append(f"{path.relative_to(runs_dir).as_posix()}: prompt is not the frozen one")
            elif run.get("model") != frozen["model"]:
                problems.append(f"{path.relative_to(runs_dir).as_posix()}: model {run.get('model')}, "
                                f"frozen {frozen['model']}")
    return problems


def check_frozen(lock: dict[str, Any], cfg: dict[str, Any], root: Path = REPO_ROOT) -> list[str]:
    """What changed in the repository since a G3 lock: code and prompts only, so
    CI can run it without the data. Empty means the evaluated system is intact."""
    current = {"code": code_hashes(root), "prompts": prompt_hashes(lock["variants"], cfg)}
    return differences({"code": lock["code"], "prompts": lock["prompts"]}, current)

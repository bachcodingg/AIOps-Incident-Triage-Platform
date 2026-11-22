"""Week 10: the evaluation runs on the locked test set (gate G3), done once.

  1. The dataset must still match its G2 lock (labels, recording, split, model).
  2. The G3 lock is written, or checked when resuming (gate3.py): from here on
     no prompt, model, detector or scorer changes.
  3. B0, B1 and B2 detection is scored on the test set.
  4. Every variant triages every test incident with every seed, under one
     spending cap for the whole evaluation. Runs go to eval/runs/<run>/test/,
     scores next to the sealed labels, as on the tuning set.
  5. The G3 lock gets `completed_at`. Commit it with the runs: that is gate G3.

The order of the variants puts the free and the main ones first: B2 (rules),
A1 (the paid model), A4 (local), then the paid ablations A2 and A3. If the cap
stops the run, what is done is what the plan cannot cut (scope cuts, plan
section 9: "keep A1 against B2").
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from correlator.graph import ServiceGraph
from detector.alerts import read_alerts
from detector.series import load
from recorder.store import Recording
from triage.run import load_config

from . import gate3
from .dataset import FreezeError, load_split, verify_lock
from .score import score
from .triage import Budget, run_set, summary

FINAL_VARIANTS = ("B2", "A1", "A4", "A2", "A3")
FINAL_SEEDS = (0, 1, 2)
DEFAULT_BUDGET_USD = 10.0  # the plan's low-cost path: EUR 5 to 10 for the final runs


def run_final(*, run: str, labels: Path, recording: Path, dataset_lock: Path, g3_lock: Path, runs_dir: Path,
              seals: Path, split: Path | None = None, model: Path | None = None,
              variants: tuple[str, ...] = FINAL_VARIANTS, seeds: tuple[int, ...] = FINAL_SEEDS,
              budget_usd: float = DEFAULT_BUDGET_USD, log: Callable[[str], None] = print,
              cfg: dict[str, Any] | None = None, **triage_kwargs: Any) -> dict[str, Any]:
    labels_dir = Path(labels).parent
    split = Path(split) if split else labels_dir / "split.json"
    problems = verify_lock(dataset_lock, labels, recording, split, model, seals)
    if problems:
        raise FreezeError("the dataset no longer matches its G2 lock:\n  " + "\n  ".join(problems))
    cfg = cfg or load_config()
    rec = Recording(Path(recording))
    current = gate3.state(dataset_lock=dataset_lock, recording=rec.path, variants=list(variants),
                          seeds=list(seeds), cfg=cfg)
    lock = gate3.open_gate(g3_lock, run, current)
    log(f"gate G3: test set opened {lock['opened_at']} ({g3_lock})")
    stale = gate3.check_runs(lock, runs_dir)
    if stale:
        raise FreezeError("runs on disk were made with another prompt or model:\n  " + "\n  ".join(stale[:10]))

    items = load_split(split)["test"]
    graph = ServiceGraph.load()
    frame, _ = load(rec.chunks())
    graph.add_observed(frame)
    detection = score(read_alerts(rec.path / "alerts.jsonl"), items, graph)
    det_path = labels_dir / "detect-test.json"
    det_path.write_text(json.dumps(detection, indent=1) + "\n", encoding="utf-8")
    log("detection: " + ", ".join(f"{v} recall {d['recall']:.0%}" for v, d in detection.items()
                                  if d["recall"] is not None))

    incidents = [json.loads(line) for line in (rec.path / "incidents-B2.jsonl").read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    budget = Budget(budget_usd)
    written = [det_path]
    out: dict[str, Any] = {"detection": detection, "triage": {}}
    for variant in variants:
        log(f"{variant}: {sum(i['kind'] == 'fault' for i in items)} faults x {len(seeds)} seeds "
            f"(spent so far ${budget.spent:.2f} of ${budget.cap:.2f})")
        rows = run_set(rec, items, incidents, variant, list(seeds), runs_dir, log=log, budget=budget, **triage_kwargs)
        path = labels_dir / f"triage-test-{variant}.json"
        out["triage"][variant] = summary(rows)
        path.write_text(json.dumps({"rows": rows, "summary": out["triage"][variant]}, indent=1, default=str) + "\n",
                        encoding="utf-8")
        written.append(path)
    gate3.close_gate(g3_lock, written, budget.spent)
    out["spent_usd"] = budget.spent
    return out

"""Gate G2: freeze the dataset.

Inputs: the sealed labels of a dataset run (outside the repository), the public
seal committed when the run ended (eval/seals/), and the telemetry recording.

  1. The labels must match the seal: not one line changed since the run.
  2. Unusable items are set aside, with the reason counted: aborted or failed
     injections, and incidents whose telemetry has a gap.
  3. The rest is split, stratified by fault: 2 incidents of every fault type
     for tuning, the others (3 per fault, 42 for a full run) for the test set.
     Fault-free windows and benign releases are split 40/60. The random seed is
     the labels' head hash, which nobody could choose: it did not exist until
     the last label was written.
  4. The split, with its labels, is written next to the labels (sealed). Only
     hashes go into Git (eval/dataset/<run>.lock.json): the test set's, the
     tuning set's, the recording's, and the model's. Anyone with the data can
     check them with `harness verify`; nobody can quietly swap an incident.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_runner.labels import check_sealed_location, read, verify
from chaos_runner.schedule import BENIGN, FAULT, FAULT_FREE
from detector.alerts import parse_time
from recorder.store import Recording

LOCK_FORMAT = 1
TUNING_PER_FAULT = 2
TUNING_SHARE_OTHER = 0.4
# Telemetry an incident needs around it: detection looks back over windows
# of up to an hour (the MAD history), the agent at the minutes before and after.
CONTEXT_BEFORE_S = 60 * 60
CONTEXT_AFTER_S = 15 * 60


class FreezeError(ValueError):
    pass


def canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def set_hash(records: list[dict[str, Any]]) -> str:
    lines = "\n".join(canonical(r) for r in sorted(records, key=lambda r: r["seq"]))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def usable(records: list[dict[str, Any]], recording: Recording) -> tuple[list[dict[str, Any]], Counter[str], list[dict]]:
    keep, reasons, excluded = [], Counter(), []
    for r in records:
        if r.get("kind") not in (FAULT, FAULT_FREE, BENIGN):
            continue
        reason = None
        if r.get("aborted"):
            reason = "aborted"
        elif r.get("error"):
            reason = "injection failed"
        elif not recording.covered(parse_time(r["start"]) - CONTEXT_BEFORE_S, parse_time(r["end"]) + CONTEXT_AFTER_S):
            reason = "telemetry gap"
        if reason:
            reasons[reason] += 1
            excluded.append({"seq": r["seq"], "kind": r["kind"], "reason": reason})
        else:
            keep.append(r)
    return keep, reasons, excluded


def split(items: list[dict[str, Any]], seed: int, tuning_per_fault: int = TUNING_PER_FAULT) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    tuning, test = [], []
    by_fault: dict[str, list[dict]] = {}
    for r in items:
        if r["kind"] == FAULT:
            by_fault.setdefault(r["fault"], []).append(r)
    for fault in sorted(by_fault):
        runs = sorted(by_fault[fault], key=lambda r: r["seq"])
        rng.shuffle(runs)
        k = min(tuning_per_fault, len(runs) - 1)  # every fault keeps at least one test incident
        tuning += runs[:k]
        test += runs[k:]
    for kind in (FAULT_FREE, BENIGN):
        runs = sorted((r for r in items if r["kind"] == kind), key=lambda r: r["seq"])
        rng.shuffle(runs)
        k = round(len(runs) * TUNING_SHARE_OTHER)
        tuning += runs[:k]
        test += runs[k:]
    return {"tuning": sorted(tuning, key=lambda r: r["seq"]), "test": sorted(test, key=lambda r: r["seq"])}


def _counts(records: list[dict[str, Any]]) -> dict[str, int]:
    c = Counter(r["kind"] for r in records)
    return {"incidents": c[FAULT], "fault_free_windows": c[FAULT_FREE], "benign_releases": c[BENIGN]}


def build(labels_path: Path, seal_path: Path, recording_dir: Path, model_path: Path | None = None,
          tuning_per_fault: int = TUNING_PER_FAULT) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (public lock, sealed split). Pure: writes nothing."""
    seal = json.loads(Path(seal_path).read_text(encoding="utf-8"))
    count, head = verify(Path(labels_path), seal["head_sha256"])
    records = read(Path(labels_path))
    run = next((r for r in records if r.get("kind") == "run-start"), None)
    if run is None or run.get("run") != seal["run"]:
        raise FreezeError(f"{labels_path} is not run {seal['run']}")
    recording = Recording(Path(recording_dir))
    items, reasons, excluded = usable(records, recording)
    parts = split(items, int(head[:16], 16), tuning_per_fault)
    by_fault: dict[str, dict[str, int]] = {}
    for name, part in parts.items():
        for r in part:
            if r["kind"] == FAULT:
                by_fault.setdefault(r["fault"], {"tuning": 0, "test": 0})[name] += 1
    span = recording.span()
    lock = {
        "format": LOCK_FORMAT,
        "run": seal["run"],
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "labels": {"records": count, "head_sha256": head, "seal": Path(seal_path).name},
        "recording": {"name": recording.path.name, "chunks": len(recording.chunk_paths()),
                      "from": span and datetime.fromtimestamp(span[0], timezone.utc).isoformat().replace("+00:00", "Z"),
                      "to": span and datetime.fromtimestamp(span[1], timezone.utc).isoformat().replace("+00:00", "Z"),
                      "queries_sha256": recording.meta["queries_sha256"], "sha256": recording.sha256()},
        "split": {"seed": "first 16 hex digits of the labels head", "tuning_per_fault": tuning_per_fault,
                  "tuning": _counts(parts["tuning"]), "test": _counts(parts["test"]), "by_fault": by_fault},
        "excluded": dict(reasons),
        "tuning_set_sha256": set_hash(parts["tuning"]),
        "test_set_sha256": set_hash(parts["test"]),
        "model_sha256": hashlib.sha256(Path(model_path).read_bytes()).hexdigest() if model_path else None,
    }
    sealed = {"run": seal["run"], "labels_head_sha256": head, "tuning": parts["tuning"], "test": parts["test"],
              "excluded": excluded}
    return lock, sealed


def freeze(labels_path: Path, seal_path: Path, recording_dir: Path, lock_path: Path,
           split_path: Path | None = None, model_path: Path | None = None,
           tuning_per_fault: int = TUNING_PER_FAULT) -> dict[str, Any]:
    if Path(lock_path).exists():
        raise FreezeError(f"{lock_path} exists: a frozen dataset is not frozen again. Record a new run instead.")
    split_path = Path(split_path) if split_path else Path(labels_path).parent / "split.json"
    check_sealed_location(split_path.parent)
    lock, sealed = build(labels_path, seal_path, recording_dir, model_path, tuning_per_fault)
    split_path.parent.mkdir(parents=True, exist_ok=True)
    split_path.write_text(json.dumps(sealed, indent=1) + "\n", encoding="utf-8")
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    Path(lock_path).write_text(json.dumps(lock, indent=1) + "\n", encoding="utf-8")
    return lock


def verify_lock(lock_path: Path, labels_path: Path, recording_dir: Path, split_path: Path | None = None,
                model_path: Path | None = None, seals_dir: Path | None = None) -> list[str]:
    """Everything that no longer matches the lock; empty when the dataset is intact."""
    lock = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    problems = []
    try:
        verify(Path(labels_path), lock["labels"]["head_sha256"])
    except ValueError as exc:
        return [f"labels: {exc}"]
    recording = Recording(Path(recording_dir))
    if recording.sha256() != lock["recording"]["sha256"]:
        problems.append("recording: its chunks changed since the freeze")
    split_path = Path(split_path) if split_path else Path(labels_path).parent / "split.json"
    sealed = json.loads(split_path.read_text(encoding="utf-8"))
    for part in ("tuning", "test"):
        if set_hash(sealed[part]) != lock[f"{part}_set_sha256"]:
            problems.append(f"{part} set: does not match its hash")
    if model_path and lock.get("model_sha256") != hashlib.sha256(Path(model_path).read_bytes()).hexdigest():
        problems.append("model: not the one frozen with the dataset")
    if problems:
        return problems
    # The split must also be the one the labels and the seed produce, not a hand-picked one.
    seals_dir = Path(seals_dir) if seals_dir else Path(lock_path).resolve().parent.parent / "seals"
    _, rebuilt = build(labels_path, seals_dir / lock["labels"]["seal"], recording_dir, None,
                       lock["split"]["tuning_per_fault"])
    if set_hash(rebuilt["test"]) != lock["test_set_sha256"]:
        problems.append("test set: not the split the labels' head hash produces")
    return problems


def load_split(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

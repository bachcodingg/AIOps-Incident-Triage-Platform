"""Sealed ground truth.

Every label is one JSON line in <labels dir>/<run>/labels.jsonl. Each line
carries the SHA-256 of the line before it, so the last line's hash (the head)
pins the whole file: publish the head when a run ends (it goes into Git with the
test-set hash at gate G2) and nobody, including the person who wrote the agent,
can change a label afterwards without the head changing.

Sealed means three things here:
  - the labels live outside the repository, which the AIOps layer, its prompts
    and its tools read; LabelWriter refuses a directory inside it
  - in the cluster, chaos objects live in the `chaos` namespace, which the
    AIOps service account will have no access to (week 6)
  - the published head makes later edits detectable
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .catalog import REPO_ROOT

GENESIS = "0" * 64
DEFAULT_LABELS_DIR = Path.home() / ".incidentlens" / "labels"


class SealError(ValueError):
    pass


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def check_sealed_location(path: Path, repo_root: Path = REPO_ROOT) -> None:
    resolved, root = Path(path).resolve(), repo_root.resolve()
    if resolved == root or root in resolved.parents:
        raise SealError(
            f"{path} is inside the repository ({root}). Labels must live where the AIOps "
            f"layer cannot read them; the default is {DEFAULT_LABELS_DIR}."
        )


class LabelWriter:
    def __init__(self, run_dir: Path, repo_root: Path = REPO_ROOT):
        check_sealed_location(run_dir, repo_root)
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.run_dir, 0o700)
        except OSError:  # Windows: permissions come from the user profile instead
            pass
        self.path = self.run_dir / "labels.jsonl"
        self.count, self.head = verify(self.path) if self.path.exists() else (0, GENESIS)

    def write(self, record: dict[str, Any]) -> str:
        line = _canonical({**record, "seq": self.count + 1, "prev": self.head})
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.count += 1
        self.head = _line_hash(line)
        return self.head


def verify(path: Path, expected_head: str | None = None) -> tuple[int, str]:
    """Check the chain from the first line to the last. Returns (records, head)."""
    head, count = GENESIS, 0
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SealError(f"line {n} is not JSON: {exc}") from None
        if record.get("prev") != head:
            raise SealError(f"line {n} does not follow line {n - 1}: the file was changed")
        if record.get("seq") != count + 1:
            raise SealError(f"line {n} has seq {record.get('seq')}, expected {count + 1}")
        if _canonical(record) != line:
            raise SealError(f"line {n} is not in canonical form: the file was changed")
        head, count = _line_hash(line), count + 1
    if expected_head is not None and head != expected_head:
        raise SealError(f"head is {head}, expected {expected_head}")
    return count, head


def read(path: Path) -> list[dict[str, Any]]:
    verify(path)
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]

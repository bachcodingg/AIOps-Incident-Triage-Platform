"""harness: freeze the dataset (gate G2) and score the detectors.

  harness freeze --run RUN --recording REC [--model NAME]   split, hash, write eval/dataset/RUN.lock.json
  harness verify --run RUN --recording REC                  does the data still match the lock?
  harness score --run RUN --recording REC                   B0/B1/B2 detection on the tuning set

--run finds the sealed labels (~/.incidentlens/labels/RUN), the public seal
(eval/seals/) and the lock; each can also be given as a path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chaos_runner.catalog import REPO_ROOT
from chaos_runner.labels import DEFAULT_LABELS_DIR, SealError
from correlator.graph import ServiceGraph
from detector.alerts import read_alerts
from detector.cli import resolve_model
from detector.series import load
from recorder.store import Recording, resolve

from .dataset import FreezeError, freeze, load_split, verify_lock
from .score import markdown, score

SEALS = REPO_ROOT / "eval" / "seals"
LOCKS = REPO_ROOT / "eval" / "dataset"


def _labels(args: argparse.Namespace) -> Path:
    if args.labels:
        return Path(args.labels)
    if not args.run:
        raise FreezeError("give --run or --labels")
    return DEFAULT_LABELS_DIR / args.run / "labels.jsonl"


def _seal(args: argparse.Namespace) -> Path:
    if args.seal:
        return Path(args.seal)
    for path in sorted(SEALS.glob("*.json")):
        if json.loads(path.read_text(encoding="utf-8")).get("run") == args.run:
            return path
    raise FreezeError(f"no seal for {args.run} in {SEALS}; `make chaos-run` writes it when the run ends")


def cmd_freeze(args: argparse.Namespace) -> int:
    labels = _labels(args)
    out = Path(args.out) if args.out else LOCKS / f"{args.run or labels.parent.name}.lock.json"
    lock = freeze(labels, _seal(args), resolve(args.recording), out, Path(args.split) if args.split else None,
                  resolve_model(args.model) if args.model else None, args.tuning_per_fault)
    s = lock["split"]
    print(f"tuning: {s['tuning']}\ntest:   {s['test']}")
    if lock["excluded"]:
        print(f"excluded: {lock['excluded']}")
    if s["test"]["incidents"] < 40:
        print(f"warning: {s['test']['incidents']} test incidents; the plan asks for 40 or more", file=sys.stderr)
    print(f"test set sha256 {lock['test_set_sha256']}\nlock written to {out}: commit it (gate G2)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    labels = _labels(args)
    lock = Path(args.lock) if args.lock else LOCKS / f"{args.run or labels.parent.name}.lock.json"
    problems = verify_lock(lock, labels, resolve(args.recording), Path(args.split) if args.split else None,
                           resolve_model(args.model) if args.model else None, SEALS)
    for p in problems:
        print(f"FAIL {p}", file=sys.stderr)
    if not problems:
        print("ok: labels, recording, tuning and test sets match the lock")
    return 1 if problems else 0


def cmd_score(args: argparse.Namespace) -> int:
    if args.set == "test" and not args.final:
        print("error: the test set is scored once, in week 10, with --final (gate G3). Tune on --set tuning.",
              file=sys.stderr)
        return 2
    split = Path(args.split) if args.split else _labels(args).parent / "split.json"
    items = load_split(split)[args.set]
    graph = ServiceGraph.load()
    rec_dir = resolve(args.recording) if args.recording else None
    if rec_dir:
        frame, _ = load(Recording(rec_dir).chunks())
        graph.add_observed(frame)
    if not args.alerts and not rec_dir:
        raise FreezeError("give --alerts, or --recording to use its alerts.jsonl")
    alerts = read_alerts(Path(args.alerts) if args.alerts else rec_dir / "alerts.jsonl")
    results = score(alerts, items, graph)
    text = markdown(results, args.set)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(s: argparse.ArgumentParser) -> None:
        s.add_argument("--run", help="dataset run id, e.g. run-20261120T080000Z")
        s.add_argument("--labels", help="the run's sealed labels.jsonl (default: from --run)")
        s.add_argument("--split", help="the sealed split.json (default: next to the labels)")

    s = sub.add_parser("freeze", help="gate G2: split the dataset and write its lock")
    common(s)
    s.add_argument("--seal", help="the run's public seal (default: found in eval/seals by --run)")
    s.add_argument("--recording", required=True, help="recording name or directory")
    s.add_argument("--model", help="the trained Isolation Forest, to pin it too")
    s.add_argument("--tuning-per-fault", type=int, default=2)
    s.add_argument("--out", help="default: eval/dataset/<run>.lock.json")
    s.set_defaults(func=cmd_freeze)

    s = sub.add_parser("verify", help="check labels, recording and split against a lock")
    common(s)
    s.add_argument("--lock", help="default: eval/dataset/<run>.lock.json")
    s.add_argument("--recording", required=True)
    s.add_argument("--model")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("score", help="detection metrics per variant")
    common(s)
    s.add_argument("--recording", help="adds Tempo's service graph, and holds alerts.jsonl")
    s.add_argument("--alerts", help="alerts.jsonl from `detector run` (default: the recording's)")
    s.add_argument("--set", choices=["tuning", "test"], default="tuning")
    s.add_argument("--final", action="store_true", help="week 10 only: allow --set test")
    s.add_argument("--out", help="also write the markdown here")
    s.add_argument("--json", help="also write the numbers here")
    s.set_defaults(func=cmd_score)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (FreezeError, SealError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

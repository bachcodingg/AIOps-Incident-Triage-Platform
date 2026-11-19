"""harness: freeze the dataset (gate G2) and score the detectors.

  harness freeze --run RUN --recording REC [--model NAME]   split, hash, write eval/dataset/RUN.lock.json
  harness verify --run RUN --recording REC                  does the data still match the lock?
  harness score --run RUN --recording REC                   B0/B1/B2 detection on the tuning set
  harness triage --run RUN --recording REC --variant A1     triage and score RCA on the tuning set (week 8)
  harness mttr --run RUN --recording REC [--baseline-run R0 --baseline-recording REC0]   time to recover (week 9)

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
from .mttr import markdown as mttr_markdown, recover_times, summary as mttr_summary
from .triage import markdown as triage_markdown, run_set, summary

SEALS = REPO_ROOT / "eval" / "seals"
LOCKS = REPO_ROOT / "eval" / "dataset"
RUNS = REPO_ROOT / "eval" / "runs"


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


def cmd_triage(args: argparse.Namespace) -> int:
    if args.set == "test" and not args.final:
        print("error: the test set is triaged once, in week 10, with --final (gate G3). Tune on --set tuning.",
              file=sys.stderr)
        return 2
    labels = _labels(args)
    split = Path(args.split) if args.split else labels.parent / "split.json"
    items = load_split(split)[args.set]
    rec = Recording(resolve(args.recording))
    inc_path = Path(args.incidents) if args.incidents else rec.path / "incidents-B2.jsonl"
    incidents = [json.loads(line) for line in inc_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    run_id = args.run or labels.parent.name
    runs_dir = Path(args.runs) if args.runs else RUNS / run_id / args.set
    results = {}
    for variant in args.variant:
        print(f"{variant}: {sum(i['kind'] == 'fault' for i in items)} faults x {len(args.seeds)} seed(s)")
        rows = run_set(rec, items, incidents, variant, args.seeds, runs_dir, limit=args.limit,
                       **({"model": args.model} if args.model else {}))
        results[variant] = summary(rows)
        # Rows pair runs with labels: they stay with the sealed labels, out of Git.
        out = labels.parent / f"triage-{args.set}-{variant}.json"
        out.write_text(json.dumps({"rows": rows, "summary": results[variant]}, indent=1, default=str) + "\n",
                       encoding="utf-8")
    text = triage_markdown(results, args.set)
    print(text)
    print(f"runs in {runs_dir}; scores next to the labels in {labels.parent}")
    return 0


def _incidents(recording: str) -> list[dict]:
    path = Recording(resolve(recording)).path / "incidents-B2.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cmd_mttr(args: argparse.Namespace) -> int:
    runs = {f"with the loop ({args.run})": (args.run, args.recording)}
    if args.baseline_run:
        runs[f"without ({args.baseline_run})"] = (args.baseline_run, args.baseline_recording)
    results = {}
    for name, (run, rec) in runs.items():
        rows = recover_times(DEFAULT_LABELS_DIR / run / "labels.jsonl", _incidents(rec))
        for r in rows:
            t = "missed" if r["recover_s"] is None else f"{r['recover_s'] / 60:.1f} min"
            print(f"  {name}: {r['fault']} on {r['target']}: {t}")
        results[name] = mttr_summary(rows)
    print(mttr_markdown(results))
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

    s = sub.add_parser("triage", help="triage the incidents of a set with one or more variants, and score them")
    common(s)
    s.add_argument("--recording", required=True)
    s.add_argument("--variant", nargs="+", default=["B2"], help="B2 (rule, free), A1-A4 (model; A1-A3 cost money)")
    s.add_argument("--seeds", type=int, nargs="+", default=[0])
    s.add_argument("--model", help="override the variants' model, e.g. qwen3:8b for free tuning runs")
    s.add_argument("--incidents", help="default: incidents-B2.jsonl in the recording (`make detect`)")
    s.add_argument("--limit", type=int, help="only the first N faults: a cheap smoke run")
    s.add_argument("--runs", help=f"where the runs go (default: {RUNS}/<run>/<set>)")
    s.add_argument("--set", choices=["tuning", "test"], default="tuning")
    s.add_argument("--final", action="store_true", help="week 10 only: allow --set test")
    s.set_defaults(func=cmd_triage)

    s = sub.add_parser("mttr", help="time to recover per fault of a live-test run, against a run without the loop")
    s.add_argument("--run", required=True, help="the chaos run made with the loop running")
    s.add_argument("--recording", required=True, help="its recording, after `make detect`")
    s.add_argument("--baseline-run", help="the same faults without the loop")
    s.add_argument("--baseline-recording")
    s.set_defaults(func=cmd_mttr)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (FreezeError, SealError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

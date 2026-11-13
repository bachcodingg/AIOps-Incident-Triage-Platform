"""detector: turn a recording into alerts from all three detectors.

  detector train --recording BASELINE [--out NAME]     fit the Isolation Forests on fault-free traffic
  detector run --recording RUN [--model NAME]          alerts from all three detectors -> RUN/alerts.jsonl

Recordings and models are found by name in ~/.incidentlens/{recordings,models}.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from recorder.store import Recording, resolve

from .alerts import Alert, iso, write_jsonl
from .anomaly import absent_alerts, iforest_alerts, load_model, mad_alerts, save_model, train_iforest
from .series import load, parse_iso

CONFIG = Path(__file__).with_name("config.yaml")
DEFAULT_MODELS = Path.home() / ".incidentlens" / "models"


def resolve_model(name_or_path: str) -> Path:
    """A model file, or the name of one in ~/.incidentlens/models."""
    path = Path(name_or_path)
    return path if path.suffix == ".pkl" or path.exists() else DEFAULT_MODELS / f"{name_or_path}.pkl"


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def detect(chunks: Iterable[dict[str, Any]], config: dict[str, Any], models: dict | None = None) -> list[Alert]:
    """Static and burn-rate alerts as recorded, plus the anomaly detector's."""
    frame, alerts = load(chunks)
    alerts = alerts + mad_alerts(frame, config["mad"]) + absent_alerts(frame, config["absent"])
    if models:
        alerts += iforest_alerts(frame, models, config["iforest"])
    return sorted(alerts, key=lambda a: (a.start, a.detector, a.name, a.component))


def _window(args: argparse.Namespace) -> tuple[float | None, float | None]:
    return (parse_iso(args.start) if args.start else None, parse_iso(args.end) if args.end else None)


def cmd_train(args: argparse.Namespace) -> int:
    rec = Recording(resolve(args.recording))
    config = load_config(Path(args.config))
    frame, _ = load(rec.chunks(*_window(args)))
    hours = len(frame) * frame.step / 3600
    if hours < args.min_hours:
        print(f"error: {hours:.1f} h of traffic; the plan trains on 72 h, and at least {args.min_hours} h "
              f"(--min-hours) are needed to see a whole day", file=sys.stderr)
        return 2
    models = train_iforest(frame, config["iforest"], seed=args.seed)
    meta = {"recording": rec.path.name, "recording_sha256": rec.sha256(), "from": iso(frame.start),
            "to": iso(frame.end), "hours": round(hours, 2), "seed": args.seed,
            "config_sha256": hashlib.sha256(Path(args.config).read_bytes()).hexdigest()}
    out = resolve_model(args.out or rec.path.name)
    save_model(out, models, meta)
    for component, m in models.items():
        print(f"  {component:<11} {m.trained_points} points, features {', '.join(m.features)}, threshold {m.threshold:.3f}")
    print(f"trained on {hours:.1f} h; model written to {out}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    rec = Recording(resolve(args.recording))
    models = None
    if args.model:
        models, meta = load_model(resolve_model(args.model))
        print(f"model trained on {meta['recording']} ({meta['hours']} h)")
    alerts = detect(rec.chunks(*_window(args)), load_config(Path(args.config)), models)
    out = Path(args.out) if args.out else rec.path / "alerts.jsonl"
    write_jsonl(out, alerts)
    counts = Counter((a.detector, a.name) for a in alerts)
    for (detector, name), n in sorted(counts.items()):
        print(f"  {detector:<10} {name:<28} {n}")
    print(f"{len(alerts)} alerts written to {out}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="detector", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(CONFIG))
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("train", help="fit the Isolation Forests on a fault-free recording")
    s.add_argument("--recording", required=True)
    s.add_argument("--out", help=f"model file or name (default: {DEFAULT_MODELS}/<recording>.pkl)")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--min-hours", type=float, default=24)
    s.add_argument("--from", dest="start")
    s.add_argument("--to", dest="end")
    s.set_defaults(func=cmd_train)

    s = sub.add_parser("run", help="alerts from every detector for a recording")
    s.add_argument("--recording", required=True)
    s.add_argument("--model", help="Isolation Forest model (file or name); without it only MAD and absent run")
    s.add_argument("--out", help="default: alerts.jsonl in the recording")
    s.add_argument("--from", dest="start")
    s.add_argument("--to", dest="end")
    s.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

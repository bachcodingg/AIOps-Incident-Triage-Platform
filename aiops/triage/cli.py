"""triage: run the triage agent or the rule baseline on a recorded incident.

  triage run --recording REC --incident inc-0007 [--variant A1] [--seed 0]   replay one incident
  triage prompt [--variant A1]                                               the system prompt and tools, free
  triage schema                                                              the verdict's JSON schema

Incidents come from `make detect` (`correlator --variant B2`)
(incidents-B2.jsonl in the recording: A1 detects as B2 does). Claude needs ANTHROPIC_API_KEY or an
`ant auth login` profile; A4 needs a local Ollama. Every run costs money except
B2, `--model qwen3:8b` and `prompt`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from recorder.record import parse_iso
from recorder.store import Recording, resolve

from .agent import dump
from .prompt import system_prompt
from .run import VARIANTS, load_config, triage_incident
from .tools import toolbox
from .verdict import schema


def _incident(rec: Recording, args: argparse.Namespace) -> dict:
    path = Path(args.incidents) if args.incidents else rec.path / "incidents-B2.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and (inc := json.loads(line))["id"] == args.incident:
            return inc
    raise ValueError(f"no incident {args.incident} in {path}")


def cmd_run(args: argparse.Namespace) -> int:
    rec = Recording(resolve(args.recording))
    incident = _incident(rec, args)
    run = triage_incident(rec, incident, args.variant, seed=args.seed,
                          at=parse_iso(args.at) if args.at else None, model=args.model)
    out = Path(args.out) if args.out else rec.path / "triage" / f"{incident['id']}-{args.variant}-s{args.seed}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run, indent=1, default=str) + "\n" if isinstance(run, dict) else dump(run), encoding="utf-8")
    v = run["verdict"]
    for n, h in enumerate(v["hypotheses"], 1):
        print(f"  {n}. {h['component']} / {h['category']} ({h['confidence']:.2f}): {h['reason']}")
    print(f"action: {v['action']} {v.get('action_target') or ''}".rstrip())
    print(f"{'valid' if run['valid'] else 'FALLBACK: ' + v.get('fallback', '')}; {len(run['tool_calls'])} tool calls, "
          f"${run['cost_usd']:.4f}, {run['seconds']:.1f}s; run written to {out}")
    return 0 if run["valid"] else 1


def cmd_prompt(args: argparse.Namespace) -> int:
    v = load_config()["variants"][args.variant]
    print(system_prompt(runbooks=v["runbooks"]))
    for t in toolbox(traces=v["traces"], runbooks=v["runbooks"]):
        print(f"\n### tool {t.name}\n{t.description}\n{json.dumps(t.input_schema())}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="triage", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("run", help="triage one recorded incident")
    s.add_argument("--recording", required=True)
    s.add_argument("--incident", required=True, help="incident id, e.g. inc-0007")
    s.add_argument("--incidents", help="incidents file (default: the recording's incidents-B2.jsonl)")
    s.add_argument("--variant", choices=VARIANTS, default="A1")
    s.add_argument("--model", help="override the variant's model (one from config.yaml)")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--at", help="triage time, ISO (default: incident opened + triage_delay_seconds)")
    s.add_argument("--out", help="run record (default: <recording>/triage/<incident>-<variant>-s<seed>.json)")
    s.set_defaults(func=cmd_run)
    s = sub.add_parser("prompt", help="print the system prompt and tool definitions of a variant")
    s.add_argument("--variant", choices=VARIANTS[1:], default="A1")
    s.set_defaults(func=cmd_prompt)
    s = sub.add_parser("schema", help="print the verdict JSON schema")
    s.set_defaults(func=lambda a: print(json.dumps(schema(), indent=1)) or 0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

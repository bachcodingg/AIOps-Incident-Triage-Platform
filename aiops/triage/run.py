"""Triage one recorded incident with one variant: the entry point the CLI and
the evaluation harness share.

The caller hands over the incident as the correlator wrote it (its alerts)
and nothing else. Labels never come through here.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from correlator.graph import ServiceGraph
from detector.alerts import ANOMALY, Alert, read_alerts
from recorder.store import Recording

from .agent import Agent, Limits, Price
from .llm import LLM, ClaudeLLM, OllamaLLM
from .prompt import brief, prompt_sha256, system_prompt
from .replay import View, visible_alert
from .rule_rca import rule_rca
from .tools import toolbox

CONFIG = Path(__file__).with_name("config.yaml")
VARIANTS = ("B2", "A1", "A2", "A3", "A4")


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def make_llm(model: str, cfg: dict[str, Any], seed: int = 0) -> LLM:
    spec = cfg["models"].get(model)
    if spec is None:
        raise ValueError(f"model {model!r} is not in {CONFIG.name}; known: {', '.join(cfg['models'])}")
    if spec["provider"] == "anthropic":
        return ClaudeLLM(model, effort=spec.get("effort", "medium"))
    if spec["provider"] == "ollama":
        return OllamaLLM(model, host=spec.get("host", "http://localhost:11434"), seed=seed)
    raise ValueError(f"unknown provider {spec['provider']!r}")


def make_agent(variant: str, cfg: dict[str, Any], *, seed: int = 0, llm: LLM | None = None,
               model: str | None = None) -> Agent:
    v = cfg["variants"][variant]
    model = model or v["model"]
    tools = toolbox(traces=v["traces"], runbooks=v["runbooks"])
    prices = {m: Price(**s["price"]) for m, s in cfg["models"].items() if s.get("price")}
    return Agent(llm or make_llm(model, cfg, seed), tools, system_prompt(runbooks=v["runbooks"]),
                 variant=variant, seed=seed, limits=Limits(**cfg["limits"]),
                 price=prices.get(model, Price()), prices=prices)


def incident_alerts(incident: dict[str, Any]) -> list[Alert]:
    return [Alert.from_dict(a) for a in incident["alerts"]]


def anomaly_alerts(recording: Recording) -> list[Alert]:
    """The anomaly detector's alerts (`detector run`); the static and burn-rate
    ones come from the recording itself."""
    path = recording.path / "alerts.jsonl"
    return [a for a in read_alerts(path) if a.detector == ANOMALY] if path.exists() else []


def triage_incident(recording: Recording, incident: dict[str, Any], variant: str, *, seed: int = 0,
                    at: float | None = None, cfg: dict[str, Any] | None = None, llm: LLM | None = None,
                    model: str | None = None, anomalies: list[Alert] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    alerts = incident_alerts(incident)
    opened = min(a.start for a in alerts)
    now = at if at is not None else opened + cfg["triage_delay_seconds"]
    view = View.at(recording, now, anomalies if anomalies is not None else anomaly_alerts(recording),
                   graph=ServiceGraph.load())
    known = [v for a in alerts if (v := visible_alert(a, now))]
    text = brief(incident["id"], known, now)
    if variant == "B2":
        started = time.monotonic()
        verdict = rule_rca(view, known)
        return {"format": 1, "incident": incident["id"], "variant": "B2", "model": "rule", "seed": seed,
                "triage_at": now, "brief": text, "verdict": verdict.model_dump(), "valid": True, "attempts": 1,
                "calls": [], "tool_calls": [], "usage": {}, "cost_usd": 0.0,
                "seconds": round(time.monotonic() - started, 3), "system_sha256": prompt_sha256("rule_rca")}
    agent = make_agent(variant, cfg, seed=seed, llm=llm, model=model)
    run = agent.triage(view, incident["id"], text)
    return run.to_dict()

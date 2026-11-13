"""The fault catalog (chaos/catalog.yaml): loading, validation and templating."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

import yaml

CHAOS_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = CHAOS_DIR.parent
DEFAULT_CATALOG = CHAOS_DIR / "catalog.yaml"
EXPERIMENTS_DIR = CHAOS_DIR / "experiments"
RUNBOOKS_DIR = REPO_ROOT / "docs" / "runbooks"

INJECTORS = {"config-release", "image-release", "chaos-mesh", "load", "sql-fill", "node-stop"}
RELEASE_INJECTORS = {"config-release", "image-release"}
ACTIONS = {"rollback", "restart", "scale_out", "none_escalate", "none_verify"}
CATEGORIES = {"bad-release", "resource", "load", "network", "dns", "dependency", "infrastructure"}
AFTER_HOOKS = {"reindex"}
TEMPLATED_INJECTORS = {"chaos-mesh", "load"}  # these apply chaos/experiments/<file>
# Variables only the runner knows at injection time; validation uses these stand-ins.
RUNTIME_VARS = {"load": {"pods": "1", "rate_per_minute": "60"}}

_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class CatalogError(ValueError):
    pass


def parse_duration(text: str | int) -> int:
    """'1h', '10m', '90s' or '1m30s' to seconds."""
    if isinstance(text, int):
        return text
    m = _DURATION.match(str(text).strip())
    if not m or not any(m.groups()):
        raise CatalogError(f"not a duration: {text!r} (use e.g. 10m, 90s, 1m30s)")
    h, mi, s = (int(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


def format_duration(seconds: int) -> str:
    return f"{int(seconds)}s"


def render(text: str, variables: dict[str, Any]) -> str:
    """Strict ${name} substitution: a missing variable is an error, not a blank."""
    try:
        return Template(text).substitute({k: str(v) for k, v in variables.items()})
    except KeyError as exc:
        raise CatalogError(f"template needs ${{{exc.args[0]}}}, which is not set") from None


@dataclass(frozen=True)
class Component:
    name: str
    namespace: str
    labels: dict[str, str]
    kind: str | None = None  # "rollout" for the four services ArgoCD releases

    def selector(self) -> str:
        return ",".join(f"{k}={v}" for k, v in self.labels.items())


@dataclass(frozen=True)
class Target:
    name: str
    env: dict[str, str] = field(default_factory=dict)
    intensities: tuple[str, ...] = ()
    vars: dict[str, str] = field(default_factory=dict)

    @property
    def is_link(self) -> bool:
        return "->" in self.name

    @property
    def ends(self) -> tuple[str, str]:
        source, destination = (part.strip() for part in self.name.split("->", 1))
        return source, destination


@dataclass(frozen=True)
class Fault:
    id: str
    name: str
    injector: str
    duration: tuple[int, int]
    targets: dict[str, Target]
    root_cause: dict[str, str]
    correct_action: str
    symptom: str
    dashboard: dict[str, Any]
    runbook: str
    intensities: tuple[str, ...] = ()
    experiment: str | None = None
    after: tuple[str, ...] = ()

    def intensities_for(self, target: str) -> tuple[str | None, ...]:
        return self.targets[target].intensities or self.intensities or (None,)

    def experiment_text(self) -> str:
        assert self.experiment, f"{self.id} has no experiment"
        return (EXPERIMENTS_DIR / self.experiment).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Catalog:
    version: int
    schedule: dict[str, Any]
    components: dict[str, Component]
    faults: dict[str, Fault]
    path: Path = DEFAULT_CATALOG

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CATALOG) -> Catalog:
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        components = {
            name: Component(name, c["namespace"], dict(c["labels"]), c.get("kind"))
            for name, c in raw["components"].items()
        }
        faults = {}
        for f in raw["faults"]:
            fault = _parse_fault(f)
            if fault.id in faults:
                raise CatalogError(f"duplicate fault id {fault.id}")
            faults[fault.id] = fault
        catalog = cls(raw["version"], raw.get("schedule", {}), components, faults, path)
        catalog.validate()
        return catalog

    def fault(self, fault_id: str) -> Fault:
        try:
            return self.faults[fault_id.upper()]
        except KeyError:
            raise CatalogError(f"no fault {fault_id}; known: {', '.join(self.faults)}") from None

    def rollouts(self) -> list[str]:
        return [name for name, c in self.components.items() if c.kind == "rollout"]

    def variables(self, fault: Fault, target: str, intensity: str | None, duration_s: int) -> dict[str, str]:
        """Everything a template may use for one injection."""
        t = fault.targets[target]
        v: dict[str, str] = {
            "fault": fault.id,
            "target": target,
            "intensity": "" if intensity is None else str(intensity),
            "duration": format_duration(duration_s),
        }
        ends = {"target": target}
        if t.is_link:
            v["source"], v["destination"] = t.ends
            ends = {"source": v["source"], "destination": v["destination"]}
        for role, name in ends.items():
            if name in self.components:
                c = self.components[name]
                v[f"{role}_namespace"] = c.namespace
                v[f"{role}_labels"] = json.dumps(c.labels)  # a YAML flow mapping
        v.update(t.vars)
        return v

    def validate(self) -> None:
        for f in self.faults.values():
            where = f"fault {f.id}"
            if f.injector not in INJECTORS:
                raise CatalogError(f"{where}: unknown injector {f.injector!r}")
            if f.correct_action not in ACTIONS:
                raise CatalogError(f"{where}: unknown action {f.correct_action!r}")
            if f.root_cause.get("category") not in CATEGORIES:
                raise CatalogError(f"{where}: unknown root-cause category {f.root_cause.get('category')!r}")
            if not f.targets:
                raise CatalogError(f"{where}: no targets")
            if not 0 < f.duration[0] <= f.duration[1]:
                raise CatalogError(f"{where}: bad duration range {f.duration}")
            if bad := set(f.after) - AFTER_HOOKS:
                raise CatalogError(f"{where}: unknown after-hooks {sorted(bad)}")
            if f.injector in TEMPLATED_INJECTORS and not (f.experiment and (EXPERIMENTS_DIR / f.experiment).is_file()):
                raise CatalogError(f"{where}: experiment file {f.experiment!r} not found in {EXPERIMENTS_DIR}")
            for name, t in f.targets.items():
                ends = t.ends if t.is_link else (name,)
                if f.injector in RELEASE_INJECTORS:
                    if self.components.get(name, Component(name, "", {})).kind != "rollout":
                        raise CatalogError(f"{where}: {name} is not a released service")
                    if not t.env:
                        raise CatalogError(f"{where}: release target {name} sets no env")
                if f.injector == "chaos-mesh":
                    for end in ends:
                        if end not in self.components:
                            raise CatalogError(f"{where}: unknown component {end!r}")
                # Every template must render for every target and intensity.
                for intensity in f.intensities_for(name):
                    v = self.variables(f, name, intensity, f.duration[0])
                    v.update(name="check", **RUNTIME_VARS.get(f.injector, {}))
                    render(f.symptom, v)
                    render(json.dumps(f.root_cause), v)
                    render(json.dumps(t.env), v)
                    render(json.dumps(f.dashboard), v)
                    if f.experiment:
                        render(f.experiment_text(), v)


def _parse_fault(f: dict[str, Any]) -> Fault:
    try:
        lo, hi = (parse_duration(d) for d in f["duration"])
        targets = {
            name: Target(
                name,
                env={k: str(v) for k, v in (t or {}).get("env", {}).items()},
                intensities=tuple(str(i) for i in (t or {}).get("intensities", ())),
                vars={k: str(v) for k, v in (t or {}).get("vars", {}).items()},
            )
            for name, t in f["targets"].items()
        }
        return Fault(
            id=f["id"],
            name=f["name"],
            injector=f["injector"],
            duration=(lo, hi),
            targets=targets,
            root_cause=dict(f["root_cause"]),
            correct_action=f["correct_action"],
            symptom=f["symptom"].strip(),
            dashboard=dict(f["dashboard"]),
            runbook=f["runbook"],
            intensities=tuple(str(i) for i in f.get("intensities", ())),
            experiment=f.get("experiment"),
            after=tuple(f.get("after", ())),
        )
    except KeyError as exc:
        raise CatalogError(f"fault {f.get('id', '?')}: missing {exc.args[0]!r}") from None

"""Runs injections and writes what really happened as labels.

One item at a time: wait for a healthy system, inject, hold for the duration,
stop, wait for recovery, write the label, wait out the quiet gap. Timestamps in
the labels are the real ones, not the plan's.
"""

from __future__ import annotations

import hashlib
import signal
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .catalog import Catalog, Fault, render
from .cluster import Cluster, ClusterError
from .injectors import ImageRelease, Injection, injector_for, new_tag
from .labels import LabelWriter
from .schedule import BENIGN, FAULT, FAULT_FREE, Item


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Interrupted(Exception):
    pass


def _raise_interrupted(*_: Any) -> None:
    raise Interrupted()


class Runner:
    def __init__(
        self,
        cluster: Cluster,
        catalog: Catalog,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
        poll_s: float = 15,
    ):
        self.c = cluster
        self.catalog = catalog
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.poll_s = poll_s
        self.recovery_timeout_s = int(catalog.schedule.get("recovery_timeout_minutes", 15)) * 60
        self.current: dict[str, Any] | None = None  # the label being built, written even if interrupted

    # --- health -----------------------------------------------------------------
    def health_problems(self) -> list[str]:
        problems = []
        try:
            if unready := self.c.unready_pods():
                problems.append(f"pods not ready: {', '.join(unready)}")
            if not self.c.search_works():
                problems.append("search through the gateway returns no results")
        except ClusterError as exc:
            problems.append(str(exc))
        return problems

    def wait_until(self, check: Callable[[], bool], timeout_s: float) -> datetime | None:
        deadline = self.clock() + timedelta(seconds=timeout_s)
        while True:
            try:
                if check():
                    return self.clock()
            except ClusterError as exc:
                self.log(f"  (check failed, retrying: {exc})")
            if self.clock() >= deadline:
                return None
            self.sleep(self.poll_s)

    def symptom(self, fault: Fault, variables: dict[str, str]) -> str:
        return render(fault.symptom, variables)

    # --- one injection ------------------------------------------------------------
    def inject(
        self,
        item: Item,
        *,
        check_symptom: bool = False,
        symptom_timeout_s: int | None = None,
        stop_after_symptom_s: int | None = None,
        on_symptom: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Inject one fault and return its label. With check_symptom (the dry
        run) the fault's symptom must be absent before and appear during it;
        stop_after_symptom_s then ends it that long after the symptom showed."""
        assert item.kind == FAULT and item.fault and item.target
        fault = self.catalog.fault(item.fault)
        name = f"{item.id}-{fault.id.lower()}"
        variables = self.catalog.variables(fault, item.target, item.intensity, item.duration_s)
        variables["name"] = name
        env = {k: render(v, variables) for k, v in fault.targets[item.target].env.items()}
        inj = Injection(name, item.target, item.intensity, item.duration_s, variables, fault, env)
        query = self.symptom(fault, variables)
        root_cause = {k: render(v, variables) for k, v in fault.root_cause.items()}

        label: dict[str, Any] = {
            "kind": FAULT,
            "item": item.id,
            "fault": fault.id,
            "fault_name": fault.name,
            "injector": fault.injector,
            "target": item.target,
            "intensity": item.intensity,
            "planned_duration_s": item.duration_s,
            "root_cause": root_cause,
            "correct_action": fault.correct_action,
            "symptom_query": query,
        }
        self.current = label
        problems = self.health_problems()
        label["baseline_healthy"] = not problems
        if problems:
            self.log(f"  baseline not healthy: {'; '.join(problems)}")
        if check_symptom:
            label["symptom_before"] = self.c.prom_true(query)

        injector = injector_for(fault.injector, self.c)
        self.log(f"{name}: {fault.id} {fault.name} on {item.target}"
                 f"{f' at {item.intensity}' if item.intensity else ''} for {item.duration_s}s")
        start = self.clock()
        label["start"] = iso(start)
        details: dict[str, Any] = {}
        try:
            details = injector.start(inj)
            label["details"] = details
            end_at = start + timedelta(seconds=item.duration_s)
            if check_symptom:
                timeout = min(symptom_timeout_s or item.duration_s, item.duration_s)
                seen = self.wait_until(lambda: self.c.prom_true(query), timeout)
                label["symptom_seen"] = iso(seen)
                if seen is not None:
                    self.log(f"  symptom after {(seen - start).total_seconds():.0f}s")
                    if on_symptom:
                        on_symptom(label)
                    if stop_after_symptom_s is not None:
                        end_at = min(end_at, seen + timedelta(seconds=stop_after_symptom_s))
                else:
                    self.log("  symptom did not show up")
            while (left := (end_at - self.clock()).total_seconds()) > 0:
                self.sleep(min(left, 30))
        except (Interrupted, KeyboardInterrupt):
            label["aborted"] = True
            raise
        except ClusterError as exc:
            label["error"] = str(exc)
            self.log(f"  injection failed: {exc}")
        finally:
            try:
                injector.stop(inj, details)
            finally:
                label["end"] = iso(self.clock())
                self.log(f"  stopped at {label['end']}")
        for hook in fault.after:
            self._after(hook)
        recovered = self.wait_until(lambda: not self.health_problems() and not self.c.prom_true(query),
                                    self.recovery_timeout_s)
        label["recovered"] = iso(recovered)
        self.log(f"  recovered at {label['recovered']}" if recovered else "  did not recover in time")
        return label

    def _after(self, hook: str) -> None:
        if hook == "reindex":
            # Redis came back empty; a restarted indexer does a full reindex.
            self.c.restart_rollout("indexer")

    # --- other items ------------------------------------------------------------
    def quiet(self, item: Item) -> dict[str, Any]:
        start = self.clock()
        self.sleep(item.duration_s)
        return {"kind": FAULT_FREE, "item": item.id, "start": iso(start), "end": iso(self.clock()),
                "healthy_at_end": not self.health_problems()}

    def benign_release(self, item: Item) -> dict[str, Any]:
        assert item.target
        release = ImageRelease(self.c)
        inj = Injection(f"{item.id}-release", item.target, None, item.duration_s, {}, None, {"BUILD_ID": new_tag()})
        start = self.clock()
        details: dict[str, Any] = {}
        try:
            details = release.start(inj)
            self.sleep(item.duration_s)
        finally:
            release.stop(inj, details)
        return {"kind": BENIGN, "item": item.id, "target": item.target, "start": iso(start),
                "end": iso(self.clock()), "details": details}

    # --- a whole run ------------------------------------------------------------
    def run(self, items: list[Item], writer: LabelWriter, run_id: str, seed: int) -> str:
        previous = signal.signal(signal.SIGTERM, _raise_interrupted)
        try:
            writer.write({
                "kind": "run-start", "run": run_id, "seed": seed, "at": iso(self.clock()),
                "catalog_sha256": hashlib.sha256(self.catalog.path.read_bytes()).hexdigest(),
                "plan": [i.to_dict() for i in items],
            })
            for n, item in enumerate(items, start=1):
                self.log(f"[{n}/{len(items)}] {item.kind} {item.id}")
                self.current = None
                if item.kind == FAULT:
                    healthy = self.wait_until(lambda: not self.health_problems(), self.recovery_timeout_s)
                    if healthy is None:
                        self.log("  starting although the system is not healthy")
                    label = self.inject(item)
                elif item.kind == BENIGN:
                    label = self.benign_release(item)
                else:
                    label = self.quiet(item)
                writer.write(label)
                self.current = None
                self.sleep(item.gap_s)
            writer.write({"kind": "run-end", "run": run_id, "at": iso(self.clock())})
        except (Interrupted, KeyboardInterrupt):
            if self.current is not None:
                writer.write({**self.current, "aborted": True})
            writer.write({"kind": "run-end", "run": run_id, "at": iso(self.clock()), "interrupted": True})
            raise
        finally:
            signal.signal(signal.SIGTERM, previous)
        return writer.head

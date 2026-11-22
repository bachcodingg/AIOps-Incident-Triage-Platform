"""Week 10: the final run on the test set (gate G3) and `make results`, on
synthetic data with no model."""

import json
import xml.etree.ElementTree as ET

import pytest

from correlator.correlate import correlate, select
from correlator.graph import ServiceGraph
from detector.alerts import write_jsonl
from detector.cli import detect, load_config
from harness import gate3, stats
from harness.cli import main
from harness.dataset import FreezeError, freeze
from harness.figures import write_all
from harness.final import run_final
from harness.results import build, markdown, update_readme
from harness.triage import Budget, BudgetExceeded, run_set
from recorder.store import Recording
from test_harness import make_run
from test_triage_eval import LABEL, recording as triage_recording
from triage.llm import ScriptedLLM


# --- statistics ----------------------------------------------------------------

def test_mcnemar_exact():
    # 8 incidents only A gets right, 1 only B: p = 2 * (C(9,0) + C(9,1)) / 2^9
    a = [True] * 8 + [False] + [True] * 20 + [False] * 5
    b = [False] * 8 + [True] + [True] * 20 + [False] * 5
    m = stats.mcnemar(a, b)
    assert (m.a_only, m.b_only, m.both, m.neither) == (8, 1, 20, 5)
    assert m.p_value == pytest.approx(20 / 512)
    assert stats.mcnemar([True, False], [True, False]).p_value == 1.0


def test_bootstrap_is_over_incidents_and_reproducible():
    # 40 incidents, 3 seeds each; every seed agrees, so 3 runs are worth one incident
    num, den = [3] * 28 + [0] * 12, [3] * 40
    e = stats.ratio(num, den)
    assert e.value == pytest.approx(0.7) and e.n == 40
    assert 0.5 < e.low < 0.7 < e.high < 0.9
    assert stats.ratio(num, den) == e                      # fixed seed: same numbers twice
    assert stats.ratio([0, 0], [0, 0]).value is None
    d = stats.paired_difference(num, den, [3] * 40, den)
    assert d.value == pytest.approx(-0.3) and d.high < 0
    med = stats.statistic([[1.0], [2.0], [3.0], []])
    assert med.value == 2.0 and med.n == 3
    assert stats.majority([True, True, False]) and not stats.majority([True, False])


# --- the final run ---------------------------------------------------------------

def frozen_dataset(tmp_path):
    labels, seal, recording = make_run(tmp_path)
    rec = Recording(recording)
    alerts = detect(rec.chunks(), load_config())
    write_jsonl(rec.path / "alerts.jsonl", alerts)
    incidents = correlate(select(alerts, ("burn-rate", "anomaly")), ServiceGraph.load())
    (rec.path / "incidents-B2.jsonl").write_text("".join(json.dumps(i.to_dict()) + "\n" for i in incidents))
    lock = seal.parent.parent / "dataset" / "run-x.lock.json"
    freeze(labels, seal, recording, lock)
    return labels, seal, recording, lock


def test_final_opens_the_test_set_once_and_refuses_a_changed_system(tmp_path):
    labels, seal, recording, lock = frozen_dataset(tmp_path)
    g3 = lock.parent / "run-x.g3.json"
    kw = dict(run="run-x", labels=labels, recording=recording, dataset_lock=lock, g3_lock=g3,
              runs_dir=tmp_path / "runs", seals=seal.parent, variants=("B2",), seeds=(0, 1), log=lambda s: None)
    out = run_final(**kw)
    assert set(out["triage"]) == {"B2"} and out["spent_usd"] == 0
    locked = json.loads(g3.read_text())
    assert locked["completed_at"] and set(locked["scores_sha256"]) == {"detect-test.json", "triage-test-B2.json"}
    assert "aiops/triage/config.yaml" in locked["code"] and "docs/runbooks/F01-crash-loop.md" in locked["code"]
    public = g3.read_text()
    assert "catalog" not in public and "bad-release" not in public  # hashes, no labels
    assert (labels.parent / "detect-test.json").exists() and (labels.parent / "triage-test-B2.json").exists()

    run_final(**kw)  # resuming with the same system is fine

    locked["code"]["aiops/triage/prompt.py"] = "0" * 64   # as if the prompt had been edited since
    g3.write_text(json.dumps(locked))
    with pytest.raises(FreezeError, match="code aiops/triage/prompt.py: changed"):
        run_final(**kw)
    with pytest.raises(FreezeError, match="seeds"):
        run_final(**{**kw, "seeds": (0,)})


def test_final_refuses_a_dataset_that_no_longer_matches_g2(tmp_path):
    labels, seal, recording, lock = frozen_dataset(tmp_path)
    next(iter(sorted((recording / "chunks").glob("*.gz")))).write_bytes(b"x")
    with pytest.raises(FreezeError, match="G2 lock"):
        run_final(run="run-x", labels=labels, recording=recording, dataset_lock=lock,
                  g3_lock=lock.parent / "run-x.g3.json", runs_dir=tmp_path / "runs", seals=seal.parent,
                  variants=("B2",), seeds=(0,), log=lambda s: None)


def test_check_runs_flags_a_run_made_with_another_prompt(tmp_path):
    lock = {"prompts": {"A1": {"model": "claude-opus-5-5", "system_sha256": "a" * 64}}}
    d = tmp_path / "A1" / "s0"
    d.mkdir(parents=True)
    (d / "inc-1.json").write_text(json.dumps({"system_sha256": "a" * 64, "model": "claude-opus-5-5"}))
    assert gate3.check_runs(lock, tmp_path) == []
    (d / "inc-2.json").write_text(json.dumps({"system_sha256": "b" * 64, "model": "claude-opus-5-5"}))
    (d / "inc-3.json").write_text(json.dumps({"system_sha256": "a" * 64, "model": "qwen3:8b"}))
    problems = gate3.check_runs(lock, tmp_path)
    assert len(problems) == 2 and "not the frozen one" in problems[0] and "qwen3:8b" in problems[1]


def test_the_budget_stops_paid_runs_before_they_start(tmp_path):
    rec, incidents = triage_recording(tmp_path)
    with pytest.raises(BudgetExceeded, match="cap"):
        run_set(rec, [LABEL], incidents, "A1", [0], tmp_path / "runs", log=lambda s: None,
                budget=Budget(0.0), llm=ScriptedLLM([]))
    assert not list((tmp_path / "runs").rglob("*.json"))
    # B2 costs nothing and is never stopped
    rows = run_set(rec, [LABEL], incidents, "B2", [0], tmp_path / "runs", log=lambda s: None, budget=Budget(0.0))
    assert rows[0]["detected"] and rows[0]["system_sha256"]


# --- results ---------------------------------------------------------------------

FAULTS = {"F02": ("catalog", "bad-release", "rollback"), "F07": ("catalog->postgres", "network", "none_escalate"),
          "F10": ("redis", "dependency", "restart")}


def synthetic_scores(labels_dir):
    """A test set of 3 faults x 4 incidents, scored as `harness final` would leave it."""
    items, seq = [], 0
    for fault, (comp, cat, action) in FAULTS.items():
        for _ in range(4):
            seq += 1
            items.append({"kind": "fault", "seq": seq, "fault": fault, "start": "x", "end": "y",
                          "root_cause": {"component": comp, "category": cat}, "correct_action": action})
    labels_dir.mkdir(parents=True)
    (labels_dir / "split.json").write_text(json.dumps({"tuning": [], "test": items}))

    def det(recall_every):
        rows = [{"seq": i["seq"], "fault": i["fault"], "detected": i["seq"] % recall_every != 0,
                 "ttd_s": 90.0 + i["seq"]} for i in items]
        for r in rows:
            if not r["detected"]:
                r["ttd_s"] = None
        return {"detectors": ["x"], "incidents": len(rows), "recall": None, "mttd_s": None,
                "false_alarms_per_day": 0.5, "benign_release_alarms_per_day": 0.0, "alerts_per_incident": 4.0,
                "incidents_per_incident": 1.5, "noise_reduction": 0.625, "per_fault": {}, "rows": rows,
                "quiet_hours": {"fault-free": 20.0, "benign-release": 1.0}}
    (labels_dir / "detect-test.json").write_text(json.dumps({"B0": det(2), "B1": det(3), "B2": det(12)}))

    def triage(variant, right):
        rows = []
        for i in items:
            for seed in (0, 1, 2):
                detected = i["seq"] != 12
                ok = detected and right(i, seed)
                comp, cat, action = FAULTS[i["fault"]]
                top = {"component": comp, "category": cat} if ok else {"component": "gateway", "category": "load"}
                rows.append({"seq": i["seq"], "fault": i["fault"], "seed": seed, "incident": f"inc-{i['seq']}",
                             "detected": detected, **({} if not detected else {
                                 "valid": True, "top1": ok, "top3": ok or seed == 0, "action": ok,
                                 "unsafe": (not ok) and action == "none_escalate", "cost_usd": 0.03 if variant != "B2" else 0.0,
                                 "seconds": 20.0 + seed, "verdict_action": action if ok else "scale_out",
                                 "verdict_top": top})})
        (labels_dir / f"triage-test-{variant}.json").write_text(json.dumps({"rows": rows, "summary": {}}))
    triage("B2", lambda i, s: i["fault"] == "F02")
    triage("A1", lambda i, s: i["fault"] != "F07" or s == 0)
    triage("A2", lambda i, s: i["fault"] == "F10" or i["seq"] % 2 == 0)
    return items


def test_results_report_targets_tests_and_confusion(tmp_path):
    labels_dir = tmp_path / "labels" / "run-x"
    synthetic_scores(labels_dir)
    lock = {"opened_at": "2027-01-11T08:00:00Z", "completed_at": "2027-01-11T20:00:00Z", "spent_usd": 4.2,
            "dataset_lock_sha256": "d" * 64, "seeds": [0, 1, 2], "variants": ["B2", "A1", "A2"],
            "prompts": {"B2": {"model": "rule", "system_sha256": "b" * 64},
                        "A1": {"model": "claude-opus-5-5", "system_sha256": "a" * 64, "traces": True, "runbooks": True}}}
    report = build(labels_dir, "test", run="run-x", lock=lock)

    a1, b2 = report["triage"]["A1"], report["triage"]["B2"]
    assert a1["top1"]["value"] == pytest.approx(25 / 33)     # 33 triaged runs; F07 right on seed 0 only
    assert b2["top1"]["value"] == pytest.approx(12 / 33)
    assert a1["top1_end_to_end"]["value"] == pytest.approx(25 / 36)
    assert a1["top1"]["low"] < a1["top1"]["value"] < a1["top1"]["high"]
    assert a1["cost_mean_usd"]["value"] == pytest.approx(0.03) and a1["paid_runs"] == 33
    # McNemar on majority votes: A1 right on the F10 incidents that B2 misses
    m = report["mcnemar"]["top1"]
    assert (m["a_only"], m["b_only"]) == (3, 0) and m["p_value"] == pytest.approx(0.25)
    assert set(report["mcnemar"]["per_seed_top1"]) == {"0", "1", "2"}
    assert report["ablations"]["A2 without traces"]["top1_end_to_end"]["value"] < 0
    # confusion by fault type: F07 goes to "load" on two seeds out of three
    c = report["confusion"]["A1"]
    assert c["expected"]["F07"] == "network" and c["counts"]["F07"] == {"network": 4, "load": 8}
    assert c["counts"]["F10"].get("missed") == 3
    assert any(w.startswith("F07") for w in report["wrong"])
    by_metric = {t["metric"]: t for t in report["targets"]}
    assert by_metric["RCA top-1"]["status"].startswith("met")
    assert by_metric["Unsafe actions"]["status"] == "missed"     # escalate-labelled F07 got scale_out
    assert by_metric["Cost per incident"]["status"] == "met"
    assert by_metric["Detection recall (10 min)"]["status"].startswith("met")

    text = markdown(report)
    for heading in ("## Against the targets", "## Detection", "## Triage", "## A1 against B2", "## Ablations",
                    "## By fault type", "## What did not work", "## What was frozen"):
        assert heading in text
    assert "| RCA top-1 | ≥ 70% |" in text and "claude-opus-5-5" in text

    files = write_all(tmp_path / "figures", report)
    assert {f.name for f in files} == {"detection-recall.svg", "detection-mttd.svg", "triage.svg", "ablations.svg",
                                       "confusion-A1.svg", "confusion-B2.svg"}
    for f in files:
        ET.fromstring(f.read_text(encoding="utf-8"))  # well-formed SVG

    readme = tmp_path / "README.md"
    readme.write_text("# x\n\n## Results\n\n<!-- results:start -->\nNot measured yet.\n<!-- results:end -->\n\nmore\n")
    assert update_readme(readme, report)
    new = readme.read_text()
    assert "| A1 burn-rate + anomaly + LLM agent | 92% |" in new and "Not measured yet" not in new and new.endswith("more\n")


def test_results_cli_refuses_an_unfinished_or_missing_final(tmp_path, capsys):
    labels_dir = tmp_path / "labels" / "run-x"
    synthetic_scores(labels_dir)
    args = ["results", "--labels", str(labels_dir / "labels.jsonl"), "--run", "run-x",
            "--g3-lock", str(tmp_path / "g3.json"), "--runs", str(tmp_path / "runs"), "--out", str(tmp_path / "out" / "results.md")]
    assert main(args) == 2 and "has not been run" in capsys.readouterr().err
    (tmp_path / "g3.json").write_text(json.dumps({"completed_at": None, "prompts": {}}))
    assert main(args) == 2 and "not completed" in capsys.readouterr().err
    assert main(args + ["--partial"]) == 0
    assert (tmp_path / "out" / "results.md").exists() and (tmp_path / "out" / "results.json").exists()
    assert (tmp_path / "out" / "figures" / "triage.svg").exists()
    # the tuning set is reported next to the labels, never into eval/
    assert main(["results", "--labels", str(labels_dir / "labels.jsonl"), "--set", "tuning"]) == 2
    assert "no scores for the tuning set" in capsys.readouterr().err


def test_gate_check_catches_a_change_after_g3(tmp_path, capsys):
    from triage.run import load_config

    cfg = load_config()
    lock = {"opened_at": "2027-01-11T08:00:00Z", "variants": ["B2", "A1"],
            "code": gate3.code_hashes(), "prompts": gate3.prompt_hashes(["B2", "A1"], cfg)}
    assert gate3.check_frozen(lock, cfg) == []
    (tmp_path / "run-x.g3.json").write_text(json.dumps(lock))
    assert main(["gate-check", "--locks", str(tmp_path)]) == 0
    lock["code"]["aiops/triage/config.yaml"] = "0" * 64
    lock["prompts"]["A1"]["system_sha256"] = "0" * 64
    (tmp_path / "run-x.g3.json").write_text(json.dumps(lock))
    assert main(["gate-check", "--locks", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "code aiops/triage/config.yaml: changed" in err and "prompts A1: changed" in err
    assert main(["gate-check", "--locks", str(tmp_path / "none")]) == 0

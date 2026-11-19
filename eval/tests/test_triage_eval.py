import json

from correlator.correlate import correlate, select
from correlator.graph import ServiceGraph
from detector.alerts import iso, write_jsonl
from detector.cli import detect, load_config
from harness.cli import main
from harness.triage import judge, match, run_set, summary, markdown
from recorder.store import Recording
from tests.synth import T0, make_chunks, write_recording
from triage.llm import ScriptedLLM

FAULT = T0 + 2 * 3600
LABEL = {"kind": "fault", "seq": 3, "fault": "F02", "start": iso(FAULT), "end": iso(FAULT + 900),
         "root_cause": {"component": "catalog", "category": "bad-release"}, "correct_action": "rollback"}
MISSED = {**LABEL, "seq": 4, "fault": "F09", "start": iso(T0 + 30 * 60), "end": iso(T0 + 40 * 60),
          "root_cause": {"component": "cluster", "category": "dns"}, "correct_action": "none_escalate"}


def recording(tmp_path):
    def modify(metric, comp, t, v):
        if metric == "errors" and FAULT <= t < FAULT + 900 and comp in ("catalog", "gateway"):
            return 0.4 if comp == "catalog" else 1.2
        return v
    chunks = make_chunks(2.5, modify=modify)
    for c in chunks:  # the catalog release that starts the fault
        c["deploys"] = {"history": [{"app": "catalog", "deployed_at": iso(FAULT - 60), "id": 3, "revision": "r",
                                     "parameters": {"image.tag": "abc1234"}}] if c["start"] <= iso(FAULT - 60) < c["end"] else [],
                        "applications": [], "rollouts": [], "observed_at": None}
    rec = Recording(write_recording(tmp_path / "rec", chunks))
    alerts = detect(rec.chunks(), load_config())
    write_jsonl(rec.path / "alerts.jsonl", alerts)
    incidents = [i.to_dict() for i in correlate(select(alerts, ("burn-rate", "anomaly")), ServiceGraph.load())]
    return rec, incidents


def verdict(component, category, action, target=None):
    return {"hypotheses": [{"component": component, "category": category}], "action": action, "action_target": target}


def test_judge():
    assert judge(verdict("catalog", "bad-release", "rollback", "catalog"), LABEL) == \
        {"top1": True, "top3": True, "action": True, "unsafe": False}
    link = {**LABEL, "root_cause": {"component": "catalog->postgres", "category": "network"}, "correct_action": "none_escalate"}
    assert judge(verdict("postgres->catalog", "network", "none_escalate"), link)["top1"]
    assert judge(verdict("catalog", "network", "restart", "catalog"), link)["unsafe"]       # a write where none was right
    f12 = {**LABEL, "root_cause": {"component": "postgres", "category": "bad-release"}}
    assert not judge(verdict("postgres", "bad-release", "rollback", "catalog"), f12)["unsafe"]
    assert judge(verdict("gateway", "bad-release", "rollback", "gateway"), LABEL)["unsafe"]  # wrong service
    assert not judge({"hypotheses": [], "action": "none_escalate"}, LABEL)["top3"]           # a fallback


def test_run_set_triages_matched_incidents_and_stores_runs_without_labels(tmp_path):
    rec, incidents = recording(tmp_path)
    pairs = match([LABEL, MISSED], incidents)
    assert pairs[0][1] is not None and pairs[1][1] is None
    runs = tmp_path / "runs"
    rows = run_set(rec, [LABEL, MISSED], incidents, "B2", [0], runs, log=lambda s: None)
    s = summary(rows)
    assert s["triaged"] == 1 and s["faults"] == 2 and s["top1"] == 1.0 and s["top1_end_to_end"] == 0.5
    assert "| B2 | 1/2 | 100% |" in markdown({"B2": s}, "tuning")
    stored = list(runs.rglob("*.json"))
    assert len(stored) == 1
    text = stored[0].read_text(encoding="utf-8")
    assert "F02" not in text and "correct_action" not in text  # nothing from the label

    good = {"hypotheses": [{"component": "catalog", "category": "bad-release", "confidence": 0.9, "reason": "r"}],
            "evidence": [{"tool": "list_alerts", "query": "{}", "finding": "catalog errors"}],
            "action": "rollback", "action_target": "catalog", "confidence": 0.9, "summary": "s"}
    llm = ScriptedLLM([[("list_alerts", {})], json.dumps(good)])
    rows = run_set(rec, [LABEL], incidents, "A1", [0], runs, log=lambda s: None, llm=llm)
    assert rows[0]["top1"] and rows[0]["valid"]
    again = run_set(rec, [LABEL], incidents, "A1", [0], runs, log=lambda s: None, llm=ScriptedLLM([]))
    assert again[0]["top1"]  # resumed from the stored run, no new model call


def test_triage_refuses_the_test_set_without_final(capsys):
    assert main(["triage", "--run", "x", "--recording", "r", "--set", "test"]) == 2
    assert "week 10" in capsys.readouterr().err


def test_time_to_recover_per_fault(tmp_path):
    from chaos_runner.labels import LabelWriter
    from harness.mttr import markdown as mttr_markdown, recover_times, summary as mttr_summary

    w = LabelWriter(tmp_path / "labels" / "run-live")
    w.write({"kind": "run-start", "run": "run-live"})
    w.write({**{k: v for k, v in LABEL.items() if k != "seq"}, "target": "catalog"})
    w.write({**{k: v for k, v in MISSED.items() if k != "seq"}, "target": "coredns"})
    w.write({**{k: v for k, v in LABEL.items() if k != "seq"}, "aborted": True})
    incidents = [{"id": "inc-1", "opened": iso(FAULT + 120), "closed": iso(FAULT + 120 + 480)}]
    rows = recover_times(w.path, incidents)
    assert [(r["fault"], r["recover_s"]) for r in rows] == [("F02", 600), ("F09", None)]
    s = mttr_summary(rows)
    assert s["recovered"] == 1 and s["median_s"] == 600
    text = mttr_markdown({"with": s, "without": {**s, "median_s": 1200}})
    assert "-50%" in text

"""Week 11: the write-up's numbers come from results.json, and postmortem drafts
come from a live test's store and sealed labels."""

import json

import pytest

from chaos_runner.catalog import REPO_ROOT
from chaos_runner.labels import LabelWriter
from detector.alerts import iso
from harness import postmortem as pm
from harness import writeup
from harness.cli import main
from harness.results import build, update_readme
from remediator.store import Store
from test_results import synthetic_scores

T0 = 1_800_000_000.0


def report(tmp_path):
    labels_dir = tmp_path / "labels" / "run-x"
    synthetic_scores(labels_dir)
    lock = {"opened_at": "x", "completed_at": "y", "spent_usd": 4.2, "dataset_lock_sha256": "d" * 64,
            "seeds": [0, 1, 2], "variants": ["B2", "A1", "A2"], "prompts": {}}
    return build(labels_dir, "test", run="run-x", lock=lock)


# --- numbers ---------------------------------------------------------------------

def test_without_results_every_number_is_missing():
    nums = writeup.numbers(None)
    assert nums and set(nums.values()) == {writeup.MISSING}
    assert set(nums) == set(writeup.numbers({"set": "test"}))  # same keys either way


def test_numbers_fill_spans_and_check_catches_a_stale_one(tmp_path):
    nums = writeup.numbers(report(tmp_path))
    assert nums["triage.A1.top1"] == "76%"          # 25 of 33, as in test_results
    assert nums["triage.B2.cost"] == "$0" and nums["triage.A1.cost"] == "$0.030"
    assert nums["detect.B2.mttd"] == "1.6 min" and nums["seeds"] == "3" and nums["spent"] == "$4.20"
    assert nums["mcnemar.p"] == "0.250" and nums["mcnemar.a1_only"] == "3"
    assert nums["ablation.A2.top1"].startswith("−") and nums["ablation.A2.top1"].endswith(" pp")
    assert nums["faults"] == "12" and nums["fault_types"] == "3"

    text = "A1 <!-- n:triage.A1.top1 -->–<!-- /n --> vs B2 <!-- n:triage.B2.top1 -->–<!-- /n -->."
    assert len(writeup.check(text, nums)) == 2
    filled = writeup.fill(text, nums)
    assert filled == "A1 <!-- n:triage.A1.top1 -->76%<!-- /n --> vs B2 <!-- n:triage.B2.top1 -->36%<!-- /n -->."
    assert writeup.check(filled, nums) == []
    stale = filled.replace("76%", "81%")
    assert writeup.check(stale, nums) == ["triage.A1.top1: says '81%', results say '76%'"]
    with pytest.raises(writeup.WriteupError, match="triage.A9.top1"):
        writeup.fill("<!-- n:triage.A9.top1 -->x<!-- /n -->", nums)


def test_cv_entry_has_no_placeholders_left(tmp_path):
    text = writeup.cv(writeup.numbers(report(tmp_path)))
    assert "{" not in text and "76% of incidents vs 36%" in text
    assert "{" not in writeup.cv(writeup.numbers(None))


def test_the_committed_write_up_matches_the_committed_results():
    """What CI runs: every span in README.md and docs/blog.md says what eval/results.json says."""
    nums = writeup.numbers(writeup.load())
    found = 0
    for path in writeup.DOCS:
        text = path.read_text(encoding="utf-8")
        found += len(writeup.spans(text))
        assert writeup.check(text, nums) == [], path
    assert found >= 10


def test_writeup_check_cli(tmp_path, capsys):
    doc = tmp_path / "doc.md"
    doc.write_text("<!-- n:triage.A1.top1 -->81%<!-- /n -->", encoding="utf-8")
    missing = tmp_path / "none.json"
    assert main(["writeup", "--check", "--results", str(missing), str(doc)]) == 1
    assert "results say '–'" in capsys.readouterr().err
    assert main(["writeup", "--results", str(missing), str(doc)]) == 0
    assert main(["writeup", "--check", "--results", str(missing), str(doc)]) == 0
    tuning = tmp_path / "tuning.json"
    tuning.write_text(json.dumps({"set": "tuning"}))
    assert main(["writeup", "--check", "--results", str(tuning), str(doc)]) == 2  # never quote the tuning set


def test_readme_what_did_not_work_block(tmp_path):
    r = report(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("<!-- results:start -->\nold\n<!-- results:end -->\n\n## What did not work\n\n"
                      "<!-- wrong:start -->\nnot measured\n<!-- wrong:end -->\n", encoding="utf-8")
    assert update_readme(readme, r)
    text = readme.read_text(encoding="utf-8")
    assert "not measured" not in text and "- F07 (labelled network)" in text


# --- postmortems -------------------------------------------------------------------

def live_test(tmp_path):
    """A live test of three faults: one fixed with an approved rollback, one
    misdiagnosed and escalated, one nobody noticed."""
    writer = LabelWriter(tmp_path / "labels" / "live-x")
    writer.write({"kind": "run-start", "run": "live-x", "seed": 1})
    faults = [("F02", "catalog", "0.10", {"component": "catalog", "category": "bad-release"}, "rollback",
               {"release": "image", "tag": "3f9a1c2", "image": "x"}),
              ("F07", "search-api->redis", "200", {"component": "search-api->redis", "category": "network"},
               "none_escalate", {}),
              ("F12", "catalog", None, {"component": "postgres", "category": "bad-release"}, "rollback", {})]
    for n, (fid, target, intensity, rc, action, details) in enumerate(faults):
        s = T0 + n * 3600
        writer.write({"kind": "fault", "fault": fid, "fault_name": fid + " name", "target": target,
                      "intensity": intensity, "root_cause": rc, "correct_action": action, "start": iso(s),
                      "end": iso(s + 600), "recovered": iso(s + 700), "details": details})
    writer.write({"kind": "run-end", "run": "live-x"})

    store = Store(tmp_path / "state.db")
    alerts = [{"detector": "burn-rate", "name": "ProductsAvailabilityBudgetBurn", "component": "gateway",
               "start": iso(T0 + 90), "labels": {"sloth_slo": "products-availability"}},
              {"detector": "anomaly", "name": "AnomalyMAD", "component": "catalog", "start": iso(T0 + 60), "labels": {}}]
    store.upsert_incident({"id": "inc-1", "opened": T0 + 95, "components": ["catalog", "gateway"], "alerts": alerts}, T0)
    store.audit("loop", "incident-opened", "inc-1", {"components": ["catalog", "gateway"]}, at=T0 + 95)
    store.set_incident("inc-1", verdict={"hypotheses": [{"component": "catalog", "category": "bad-release",
                                                         "confidence": 0.8, "reason": "5xx after release 7"}],
                                         "evidence": [{"tool": "deploy_history", "query": "{}", "finding": "release 7"}],
                                         "action": "rollback", "action_target": "catalog", "summary": "Roll back."},
                       closed=T0 + 400, status="resolved")
    store.audit("agent", "triaged", "inc-1", {"variant": "A1", "root_cause": "catalog / bad-release",
                                             "action": "rollback", "target": "catalog"}, at=T0 + 150)
    pid = store.add_proposal("inc-1", "rollback", "catalog", "approval", "one approval", "executed",
                             {"commands": ["kubectl -n argocd patch applications.argoproj.io catalog ..."]})
    store.set_proposal(pid, decided_by="Jane Doe", decided_at=T0 + 200)
    store.audit("policy", "proposed", "inc-1", {"proposal": pid, "mode": "approval"}, at=T0 + 151)
    store.audit("Jane Doe", "approved", "inc-1", {"proposal": pid, "action": "rollback", "target": "catalog"}, at=T0 + 200)
    store.audit("remediator", "executed", "inc-1", {"proposal": pid, "action": "rollback"}, at=T0 + 205)
    store.audit("loop", "resolved", "inc-1", {}, at=T0 + 400)

    t1 = T0 + 3600
    store.upsert_incident({"id": "inc-2", "opened": t1 + 420, "components": ["search-api"], "alerts": [
        {"detector": "anomaly", "name": "AnomalyMAD", "component": "search-api", "start": iso(t1 + 400), "labels": {}}]}, t1)
    store.set_incident("inc-2", verdict={"hypotheses": [{"component": "gateway", "category": "load",
                                                         "confidence": 0.5, "reason": "more traffic"}],
                                         "action": "scale_out", "action_target": "gateway"},
                       closed=t1 + 1500, status="resolved")
    store.audit("loop", "incident-opened", "inc-2", {"components": ["search-api"]}, at=t1 + 420)
    store.audit("policy", "escalated", "inc-2", {"reason": "low confidence"}, at=t1 + 480)
    # a false alarm between faults
    store.upsert_incident({"id": "inc-3", "opened": T0 + 2400, "components": ["indexer"], "alerts": []}, T0 + 2400)
    return writer.path, store


def test_cases_match_incidents_to_faults_and_say_what_is_interesting(tmp_path):
    labels, store = live_test(tmp_path)
    cases = pm.cases(labels, store)
    by_fault = {(c["label"] or {}).get("fault"): c for c in cases}
    assert [i["id"] for i in by_fault["F02"]["incidents"]] == ["inc-1"]
    assert by_fault["F02"]["judged"]["top1"] and pm.recover_s(by_fault["F02"]) == 400
    assert [i["id"] for i in by_fault[None]["incidents"]] == ["inc-3"]
    why = {k: pm.reasons(c, cases) for k, c in by_fault.items()}
    assert why["F02"] == []
    assert "wrong root cause: gateway / load" in why["F07"] and "escalated to a human" in why["F07"]
    assert any(w.startswith("slow detection") for w in why["F07"])
    assert why["F12"] == ["missed: no incident opened"] and why[None] == ["false alarm: no fault explains it"]
    table = pm.listing(cases)
    assert "| 2 | F02 | catalog | inc-1 | right | right | 6.7 min | – |" in table


def test_draft_has_the_sre_sections_and_the_facts(tmp_path):
    labels, store = live_test(tmp_path)
    from chaos_runner.catalog import Catalog
    case = pm.pick(pm.cases(labels, store), 2)
    text = pm.draft(case, run="live-x", catalog=Catalog.load(), chain="audit chain intact")
    for heading in ("## Summary", "## Impact", "## Root causes", "## Trigger", "## Resolution", "## Detection",
                    "## Action items", "## Lessons learned", "### Where we got lucky", "## Timeline",
                    "## Supporting information"):
        assert heading in text
    assert "**catalog / bad-release**" in text and "matching the label at rank 1" in text
    assert "image tag `3f9a1c2`" in text and "products-availability" in text
    assert "| +1:00 | anomaly | alert AnomalyMAD on catalog |" in text
    assert "| +2:30 | agent | triage (A1): root cause catalog / bad-release, proposes rollback catalog |" in text
    assert "| the operator | the operator approved proposal 1 |" in text and "Jane Doe" not in text   # blameless by default
    assert "(decided by the operator)" in text
    assert "../runbooks/F02-error-release.md" in text and "/d/shopsearch-red?" in text
    assert "Recovered | 6.7 min after the fault started (its incidents closed)" in text
    assert "Jane Doe" in pm.draft(case, run="live-x", catalog=Catalog.load(), keep_names=True)


def test_postmortem_cli_lists_writes_and_will_not_overwrite(tmp_path, capsys):
    labels, store = live_test(tmp_path)
    base = ["postmortem", "--labels", str(labels), "--db", str(tmp_path / "state.db")]
    assert main(base + ["--list"]) == 0
    assert "missed: no incident opened" in capsys.readouterr().out
    out = tmp_path / "pm.md"
    assert main(base + ["--seq", "2", "--out", str(out)]) == 0
    assert "audit chain intact" in out.read_text(encoding="utf-8")
    assert main(base + ["--seq", "2", "--out", str(out)]) == 2
    assert "--force" in capsys.readouterr().err
    assert main(base + ["--seq", "9", "--out", str(out), "--force"]) == 2
    assert main(["postmortem", "--db", str(tmp_path / "state.db")]) == 2
    assert pm.path_for(pm.pick(pm.cases(labels, store), 3)).name.endswith("-f07-search-api-redis.md")
    assert pm.OUT_DIR == REPO_ROOT / "docs" / "postmortems"

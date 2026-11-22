import json
import time
from types import SimpleNamespace

import pytest

from correlator.correlate import correlate, select
from correlator.graph import ServiceGraph
from detector.alerts import Alert, write_jsonl
from detector.cli import detect, load_config
from recorder.record import iso
from recorder.store import Recording
from triage.agent import Agent, Limits, Price
from triage.llm import ClaudeLLM, LLMError, OllamaLLM, ScriptedLLM, ToolCall, Usage
from triage.prompt import brief, system_prompt
from triage.replay import View, cut
from triage.rule_rca import rule_rca
from triage.run import load_config as triage_config, make_agent, triage_incident
from triage.tools import Tool, QueryMetric, run_tool, toolbox
from triage.verdict import Verdict, fallback, parse

from .synth import CHUNK, STEP, T0, make_chunks, write_recording

FAULT = T0 + 2 * 3600          # faults start two hours into a synthetic recording
GOOD = {
    "hypotheses": [{"component": "catalog", "category": "bad-release", "confidence": 0.8, "reason": "errors after a release"}],
    "evidence": [{"tool": "query_metric", "query": "errors job=shopsearch/catalog", "finding": "0.2 req/s of 5xx from -4m"}],
    "action": "rollback", "action_target": "catalog", "confidence": 0.8, "summary": "The catalog release returns 500s.",
}


def with_metric(modify_metric, component, start, end, value):
    def modify(metric, comp, t, v):
        if comp == component and metric == modify_metric and start <= t < end:
            return value(v) if callable(value) else value
        return v
    return modify


def chain(*mods):
    def modify(metric, comp, t, v):
        for m in mods:
            if v is None:
                return None
            v = m(metric, comp, t, v)
        return v
    return modify


def add_deploy(chunks, app, at, observed_late=True):
    for c in chunks:
        t0 = c["start"]
        c["deploys"] = {"history": [], "applications": [{"app": app, "sync": "Synced", "health": "Healthy"}],
                        "rollouts": [{"name": app, "phase": "Healthy", "current_hash": "b", "stable_hash": "b",
                                      "images": [f"ghcr.io/x/{app}:abc1234"], "replicas": 1, "ready": 1}],
                        "observed_at": iso(_t(c["end"]) + 90)}
        if _t(t0) <= at < _t(c["end"]):
            c["deploys"]["history"].append({"app": app, "deployed_at": iso(at), "id": 7, "revision": "abc",
                                            "parameters": {"image.tag": "abc1234"}})
    return chunks


def _t(s):
    from recorder.record import parse_iso
    return parse_iso(s)


@pytest.fixture
def catalog_fault(tmp_path):
    """Catalog starts answering 20% 5xx after a release at FAULT - 60s; the gateway follows."""
    mod = chain(errors := with_metric("errors", "catalog", FAULT, FAULT + 900, 0.4),
                with_metric("errors", "gateway", FAULT + 30, FAULT + 900, 1.2))
    chunks = add_deploy(make_chunks(2.5, modify=mod, alerts=[
        ({"alertname": "StaticHighErrorRate", "detector": "static", "severity": "baseline", "component": "catalog"},
         FAULT + 120, FAULT + 900)]), "catalog", FAULT - 60)
    rec = write_recording(tmp_path / "rec", chunks)
    return Recording(rec)


def incident_of(alerts):
    return {"id": "inc-0001", "alerts": [a.to_dict() for a in alerts]}


# --- replay -----------------------------------------------------------------------
def test_cut_hides_everything_after_now():
    now = T0 + 100
    chunk = {"start": iso(T0), "end": iso(T0 + CHUNK), "step": STEP,
             "metrics": {"requests": [{"labels": {"job": "j"}, "values": [[T0, 1.0], [T0 + 90, 2.0], [T0 + 150, 99.0]]}]},
             "alerts": [{"labels": {"alertname": "A"}, "values": [[T0 + 105, 1.0]]}],
             "events": [{"time": iso(T0 + 50), "namespace": "shopsearch"}, {"time": iso(T0 + 200), "namespace": "shopsearch"}],
             "deploys": {"history": [{"app": "catalog", "deployed_at": iso(T0 + 250)}], "rollouts": [{"name": "catalog"}],
                         "applications": [], "observed_at": iso(T0 + CHUNK + 90)},
             "logs": {"lines": [{"time": iso(T0 + 10), "line": "ok"}, {"time": iso(T0 + 280), "line": "later"}], "volume": []},
             "traces": {"summaries": [{"trace_id": "a", "start": iso(T0 + 20)}, {"trace_id": "b", "start": iso(T0 + 200)}],
                        "full": [{"trace_id": "a"}, {"trace_id": "b"}]}}
    c = cut(chunk, now)
    assert c["metrics"]["requests"][0]["values"] == [[T0, 1.0], [T0 + 90, 2.0]]
    assert c["alerts"] == []
    assert len(c["events"]) == 1 and c["deploys"]["history"] == []
    assert c["deploys"]["rollouts"] is None  # observed after now: it could show the recovery
    assert [entry["line"] for entry in c["logs"]["lines"]] == ["ok"]
    assert [t["trace_id"] for t in c["traces"]["full"]] == ["a"]
    assert c["end"] == iso(now)
    assert cut(chunk, T0 - 1) is None
    assert chunk["metrics"]["requests"][0]["values"][-1][1] == 99.0  # the recording is not touched


def test_view_shows_no_future_values_and_alerts_still_firing(tmp_path):
    spike = with_metric("latency_p95", "catalog", FAULT + 600, FAULT + 900, 9.0)
    alert = ({"alertname": "StaticHighLatency", "detector": "static", "severity": "baseline", "component": "catalog"},
             FAULT, FAULT + 900)
    rec = Recording(write_recording(tmp_path / "r", make_chunks(2.5, modify=spike, alerts=[alert])))
    view = View.at(rec, FAULT + 300)
    out = run_tool({t.name: t for t in toolbox()}, view, "query_metric",
                   {"metric": "latency_p95", "match": {"job": "shopsearch/catalog"}, "minutes": 10})[0]
    assert "9" not in out.split("|")[1]
    firing = [a for a in view.alerts if a.name == "StaticHighLatency"]
    assert firing and firing[0].end is None  # it resolves later, but not by now


# --- tools ------------------------------------------------------------------------
def test_tools_turn_bad_calls_into_errors_the_model_can_read(catalog_fault):
    view = View.at(catalog_fault, FAULT + 300)
    tools = {t.name: t for t in toolbox()}
    assert run_tool(tools, view, "nope", {})[1]
    out, err, _ = run_tool(tools, view, "query_metric", {"metric": "requests", "minutes": 999})
    assert err and "invalid arguments" in out
    out, err, _ = run_tool(tools, view, "query_metric", {"metric": "up{}"})
    assert err and "unknown metric" in out
    slow = Tool("slow", "", QueryMetric, lambda v, a: time.sleep(1) or "late")
    out, err, _ = run_tool({"slow": slow}, view, "slow", {"metric": "requests"}, timeout_s=0.1)
    assert err and "timed out" in out
    big = Tool("big", "", QueryMetric, lambda v, a: "x" * 10000)
    out, err, _ = run_tool({"big": big}, view, "big", {"metric": "requests"}, max_chars=100)
    assert not err and "cut at 100" in out


def test_tools_read_deploys_and_runbooks(catalog_fault):
    view = View.at(catalog_fault, FAULT + 300)
    tools = {t.name: t for t in toolbox()}
    out = run_tool(tools, view, "deploy_history", {"app": "catalog"})[0]
    assert "1 ArgoCD releases" in out and "image.tag=abc1234" in out
    out, err, _ = run_tool(tools, view, "read_runbook", {"name": "F12-slow-query"})
    assert not err and "## Checks" in out and "![" not in out
    assert run_tool(tools, view, "read_runbook", {"name": "../../chaos/catalog"})[1]


def test_ablations_remove_traces_and_runbooks():
    assert {t.name for t in toolbox()} >= {"get_traces", "read_runbook"}
    no_traces = toolbox(traces=False)
    assert "get_traces" not in {t.name for t in no_traces}
    assert "graph_requests" not in next(t for t in no_traces if t.name == "query_metric").description
    assert "read_runbook" not in {t.name for t in toolbox(runbooks=False)}
    assert "F12-slow-query" in system_prompt() and "F12-slow-query" not in system_prompt(runbooks=False)


def test_the_prompt_is_the_same_for_every_incident():
    # It is cached between incidents, and it must not carry anything about one.
    assert system_prompt() == system_prompt()
    text = brief("inc-0001", [Alert("static", "StaticHighErrorRate", "catalog", FAULT, None, "baseline")], FAULT + 120)
    assert "StaticHighErrorRate" in text and iso(FAULT + 120) in text


# --- verdict ----------------------------------------------------------------------
def test_verdict_parsing_and_validation():
    v, err = parse("Here it is:\n```json\n" + json.dumps(GOOD) + "\n```")
    assert err is None and v.hypotheses[0].component == "catalog"
    link = {**GOOD, "hypotheses": [{**GOOD["hypotheses"][0], "component": "Catalog -> PostgreSQL", "category": "network"}],
            "action": "none_escalate", "action_target": "catalog"}
    v, err = parse(json.dumps(link))
    assert err is None and v.hypotheses[0].component == "catalog->postgres" and v.action_target is None
    for bad in ({**GOOD, "action": "rollback", "action_target": "postgres"},     # no rollout to roll back
                {**GOOD, "action": "delete_namespace"},
                {**GOOD, "hypotheses": []},
                {**GOOD, "hypotheses": [{**GOOD["hypotheses"][0], "component": "kafka"}]},
                {**GOOD, "extra": 1}):
        assert parse(json.dumps(bad))[0] is None
    assert parse("I think it is the catalog")[0] is None
    assert fallback("x")["action"] == "none_escalate"


# --- agent ------------------------------------------------------------------------
def agent_with(script, **kw):
    llm = ScriptedLLM(script, usage=Usage(10_000, 500, 0, 0))
    return Agent(llm, toolbox(), system_prompt(), price=Price(input=4.0, output=20.0), **kw), llm


def test_agent_calls_tools_then_answers(catalog_fault):
    view = View.at(catalog_fault, FAULT + 300)
    agent, llm = agent_with([[("query_metric", {"metric": "errors", "match": {"job": "shopsearch/catalog"}}),
                              ("deploy_history", {})], json.dumps(GOOD)])
    run = agent.triage(view, "inc-0001", "brief")
    assert run.valid and run.verdict["action"] == "rollback" and run.attempts == 1
    assert [c["name"] for c in run.tool_calls] == ["query_metric", "deploy_history"]
    assert run.cost_usd == pytest.approx(2 * (10_000 * 4 + 500 * 20) / 1e6)
    assert llm.calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    d = run.to_dict()
    assert d["system"] and d["brief"] == "brief" and json.dumps(d)


def test_agent_retries_once_then_falls_back(catalog_fault):
    view = View.at(catalog_fault, FAULT + 300)
    run = agent_with(["not json", json.dumps(GOOD)])[0].triage(view, "i", "b")
    assert run.valid and run.attempts == 2
    agent, llm = agent_with(["not json", '{"action": "rollback"}'])
    run = agent.triage(view, "i", "b")
    assert not run.valid and run.verdict["action"] == "none_escalate" and "invalid answer twice" in run.verdict["fallback"]
    assert llm.calls[1]["allow_tools"] is False and "not a valid verdict" in llm.calls[1]["messages"][-1]["content"]


def test_agent_limits_switch_the_tools_off(catalog_fault):
    view = View.at(catalog_fault, FAULT + 300)
    call = [("list_alerts", {})]
    agent, llm = agent_with([call, call, call, json.dumps(GOOD)], limits=Limits(max_tool_calls=2))
    run = agent.triage(view, "i", "b")
    assert run.stopped_by == "2 tool calls" and len(run.tool_calls) == 2
    # The third turn's calls were not run: tools were off, so it counts as a bad answer.
    assert run.valid and run.attempts == 2
    agent, _ = agent_with([call] * 10, limits=Limits(max_cost_usd=0.05))
    run = agent.triage(view, "i", "b")
    assert run.stopped_by == "$0.05" and not run.valid


def test_agent_falls_back_on_refusal_and_on_provider_errors(catalog_fault):
    view = View.at(catalog_fault, FAULT + 300)
    llm = ScriptedLLM(["sorry"], stop_reason="refusal")
    run = Agent(llm, toolbox(), "s").triage(view, "i", "b")
    assert not run.valid and run.verdict["fallback"].startswith("refused")

    class Broken(ScriptedLLM):
        def reply(self, *a, **k):
            raise LLMError("503")
    run = Agent(Broken([]), toolbox(), "s").triage(view, "i", "b")
    assert not run.valid and "503" in run.verdict["fallback"]


# --- providers ----------------------------------------------------------------------
class Block(dict):
    def to_dict(self):
        return dict(self)


def test_claude_request_and_response_shape():
    sent = {}

    def create(**kw):
        sent.update(kw)
        return SimpleNamespace(
            content=[Block(type="thinking", thinking="", signature="sig"),
                     Block(type="tool_use", id="tu1", name="list_alerts", input={"minutes": 30})],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=3000,
                                  cache_creation_input_tokens=0),
            stop_reason="tool_use", model="claude-opus-5-5", id="msg_1", stop_details=None)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    llm = ClaudeLLM(client=client)
    r = llm.reply("system", [llm.user("brief")], toolbox(), allow_tools=False)
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert sent["cache_control"] == {"type": "ephemeral"} and sent["tool_choice"] == {"type": "none"}
    assert sent["output_config"] == {"effort": "medium"} and "temperature" not in sent
    assert r.tool_calls[0].name == "list_alerts" and r.usage.cache_read_tokens == 3000
    assert r.message["content"][0]["signature"] == "sig"  # thinking goes back unchanged
    results = llm.tool_results([(r.tool_calls[0], "out", False)])
    assert len(results) == 1 and results[0]["content"][0]["tool_use_id"] == "tu1"


def test_ollama_request_and_response_shape():
    sent = {}

    def post(url, body, timeout):
        sent.update(body, url=url)
        return {"model": "qwen3:8b", "message": {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "list_alerts", "arguments": {"minutes": 30}}}]},
                "prompt_eval_count": 900, "eval_count": 40, "done_reason": "stop"}

    llm = OllamaLLM(post=post, seed=3)
    r = llm.reply("system", [llm.user("brief")], toolbox(), allow_tools=True)
    assert sent["url"].endswith("/api/chat") and sent["options"]["seed"] == 3
    assert sent["messages"][0] == {"role": "system", "content": "system"} and sent["tools"]
    assert r.stop_reason == "tool_use" and r.tool_calls[0].input == {"minutes": 30} and r.usage.input_tokens == 900


# --- rule baseline -------------------------------------------------------------------
def test_rule_baseline_blames_the_released_service(catalog_fault):
    view = View.at(catalog_fault, FAULT + 300)
    alert = Alert("static", "StaticHighErrorRate", "catalog", FAULT + 120, None, "baseline")
    v = rule_rca(view, [alert])
    assert (v.hypotheses[0].component, v.hypotheses[0].category, v.action, v.action_target) == \
        ("catalog", "bad-release", "rollback", "catalog")


def test_rule_baseline_redis_down_and_a_slow_link(tmp_path):
    down = chain(with_metric("ready", "redis", FAULT, FAULT + 600, 0.0),
                 with_metric("errors", "search-api", FAULT + 30, FAULT + 600, 3.0))
    rec = Recording(write_recording(tmp_path / "a", make_chunks(2.5, modify=down)))
    v = rule_rca(View.at(rec, FAULT + 240), [Alert("anomaly", "AnomalyMAD", "search-api", FAULT + 60, None)])
    assert (v.hypotheses[0].component, v.hypotheses[0].category, v.action) == ("redis", "dependency", "restart")

    slow = with_metric("latency_p95", "search-api", FAULT, FAULT + 600, 0.4)
    rec = Recording(write_recording(tmp_path / "b", make_chunks(2.5, modify=slow)))
    v = rule_rca(View.at(rec, FAULT + 240), [Alert("anomaly", "AnomalyMAD", "search-api", FAULT + 60, None)])
    assert (v.hypotheses[0].component, v.hypotheses[0].category, v.action) == ("search-api->redis", "network", "none_escalate")


# --- end to end ----------------------------------------------------------------------
def test_triage_a_correlated_incident_end_to_end(catalog_fault):
    alerts = detect(catalog_fault.chunks(), load_config())
    write_jsonl(catalog_fault.path / "alerts.jsonl", alerts)
    incidents = correlate(select(alerts, ("static", "burn-rate", "anomaly")), ServiceGraph.load())
    inc = next(i for i in incidents if i.opened >= FAULT).to_dict()
    b2 = triage_incident(catalog_fault, inc, "B2")
    assert b2["valid"] and b2["verdict"]["hypotheses"][0]["component"] == "catalog"
    llm = ScriptedLLM([[("list_alerts", {"minutes": 30})], json.dumps(GOOD)])
    a1 = triage_incident(catalog_fault, inc, "A1", llm=llm)
    assert a1["valid"] and a1["triage_at"] == pytest.approx(min(a.start for a in incidents[0].alerts if a.start >= FAULT) + 120, abs=300)
    assert "Incident inc-" in llm.calls[0]["messages"][0]["content"]


def test_config_variants_build():
    cfg = triage_config()
    for variant in ("A1", "A2", "A3", "A4"):
        agent = make_agent(variant, cfg, llm=ScriptedLLM([]))
        assert agent.limits.max_cost_usd == cfg["limits"]["max_cost_usd"]
    assert make_agent("A1", cfg, llm=ScriptedLLM([])).price.output == 20.0

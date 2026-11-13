from correlator.cli import main
from correlator.correlate import VARIANTS, correlate, select
from correlator.graph import ServiceGraph
from detector.alerts import Alert, write_jsonl
from detector.series import load

from .synth import T0, make_chunks, write_recording

GRAPH = ServiceGraph.load()


def alert(component, start, end=None, detector="static", name=None):
    return Alert(detector, name or f"Alert-{component}", component, T0 + start, None if end is None else T0 + end)


def test_a_database_fault_and_everything_it_causes_is_one_incident():
    # PostgreSQL first, then the gateway (not its neighbour), then the catalog between them.
    alerts = [alert("postgres", 0, 600), alert("gateway", 60, 600), alert("catalog", 90, 600)]
    incidents = correlate(alerts, GRAPH)
    assert len(incidents) == 1
    assert incidents[0].components == ["catalog", "gateway", "postgres"]
    assert incidents[0].opened == T0 and incidents[0].closed == T0 + 600


def test_unrelated_components_at_the_same_time_stay_apart():
    incidents = correlate([alert("redis", 0, 300), alert("postgres", 30, 300)], GRAPH)
    assert [i.components for i in incidents] == [["redis"], ["postgres"]]


def test_infrastructure_relates_to_everything():
    incidents = correlate([alert("redis", 0, 300), alert("postgres", 30, 300), alert("node", 60, 300)], GRAPH)
    assert len(incidents) == 1


def test_alerts_more_than_five_minutes_apart_are_separate_incidents():
    incidents = correlate([alert("catalog", 0, 60), alert("catalog", 400, 500)], GRAPH)
    assert len(incidents) == 2
    assert [i.id for i in incidents] == ["inc-0001", "inc-0002"]


def test_a_still_firing_alert_keeps_its_incident_open_for_late_alerts():
    incidents = correlate([alert("catalog", 0, 1200), alert("gateway", 700, 1200)], GRAPH)
    assert len(incidents) == 1


def test_but_a_burn_rate_ticket_firing_for_hours_does_not_swallow_the_next_incident():
    ticket = alert("gateway", 0, None, detector="burn-rate")
    later = alert("catalog", 7200, 7500)
    assert len(correlate([ticket, later], GRAPH)) == 2


def test_tempo_adds_edges_the_static_graph_lacks():
    graph = ServiceGraph.load()
    graph.edges.discard(frozenset(("search-api", "redis")))
    frame, _ = load(make_chunks(0.25))
    new = graph.add_observed(frame)
    assert frozenset(("search-api", "redis")) in new
    # catalog -> postgresql is Tempo's name for the database; it maps onto the existing edge.
    assert frozenset(("catalog", "postgres")) in graph.observed and frozenset(("catalog", "postgres")) not in new


def test_variants_pick_their_detectors():
    alerts = [alert("catalog", 0, detector="static"), alert("catalog", 0, detector="burn-rate"),
              alert("catalog", 0, detector="anomaly")]
    assert [a.detector for a in select(alerts, VARIANTS["B0"])] == ["static"]
    assert [a.detector for a in select(alerts, VARIANTS["B2"])] == ["burn-rate", "anomaly"]


def test_cli(tmp_path, capsys):
    rec = write_recording(tmp_path / "rec", make_chunks(0.25))
    write_jsonl(tmp_path / "alerts.jsonl", [alert("postgres", 0, 300, "anomaly"), alert("catalog", 30, 300, "burn-rate"),
                                            alert("gateway", 40, 300, "burn-rate")])
    out = tmp_path / "incidents.jsonl"
    assert main(["--alerts", str(tmp_path / "alerts.jsonl"), "--recording", str(rec), "--variant", "B2",
                 "--out", str(out)]) == 0
    assert "3 alerts -> 1 incidents (67% fewer)" in capsys.readouterr().out
    assert len(out.read_text().splitlines()) == 1

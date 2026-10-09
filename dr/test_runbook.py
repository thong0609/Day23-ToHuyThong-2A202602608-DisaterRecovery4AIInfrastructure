"""Regression checks for orchestration and operator control; no live services."""
import json

from dr import runbook


def test_runbook_calls_failover_once_and_records_seven_steps(monkeypatch, tmp_path):
    monkeypatch.setattr(runbook, "LOG", tmp_path / "runbook.jsonl")
    monkeypatch.setattr(runbook.time, "sleep", lambda _: None)
    monkeypatch.setattr(runbook, "probe", lambda *args: (False, "not ready"))
    calls = []
    requests = []

    def failover(*args, **kwargs):
        calls.append((args, kwargs))
        return dict(ok=True, target="b", state=dict(region="b", count=200,
                                                    weights=True, ts=123.0))

    def get(url, **kwargs):
        requests.append(url)
        return runbook.httpx.Response(200, json={"region": "b"})

    monkeypatch.setattr(runbook.fo, "failover", failover)
    monkeypatch.setattr(runbook.httpx, "get", get)
    result = runbook.run("a", "b", "fs", True)
    assert result["ok"]
    assert len(calls) == 1
    assert sum(url.endswith("/v1/infer") for url in requests) == 10
    events = [json.loads(line) for line in runbook.LOG.read_text().splitlines()]
    assert [e["step"] for e in events] == list(range(1, 8))
    assert events[3]["state"]["ts"] == 123.0
    assert events[3]["ts"] > 123.0


def test_confirmation_defaults_to_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert not runbook.confirm(False, "Cut over?")
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert runbook.confirm(False, "Cut over?")


def test_healthy_primary_aborts_before_failover(monkeypatch, tmp_path):
    monkeypatch.setattr(runbook, "LOG", tmp_path / "runbook.jsonl")
    monkeypatch.setattr(runbook.time, "sleep", lambda _: None)
    monkeypatch.setattr(runbook, "probe", lambda *args: (True, "ready"))
    monkeypatch.setattr(runbook.httpx, "get", lambda *args, **kwargs:
                        runbook.httpx.Response(200))
    def forbidden(*args, **kwargs):
        raise AssertionError("Healthy primary must not trigger failover")
    monkeypatch.setattr(runbook.fo, "failover", forbidden)
    assert not runbook.run("a", "b", "fs", True)["ok"]

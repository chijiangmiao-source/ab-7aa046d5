"""API/HTTP smoke + freeze/conflict semantics tests."""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as appmod  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr(appmod, "DB_PATH", tmp.name)
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        yield c
    os.unlink(tmp.name)


TWO_HOP = {
    "auditId": "FC-API-001",
    "tasks": [
        {"id": "T1", "priority": 1},
        {"id": "T2", "priority": 5},
        {"id": "T3", "priority": 10},
    ],
    "locks": [{"id": "L1"}, {"id": "L2"}],
    "events": [
        {"type": "acquire", "task": "T3", "lock": "L1"},
        {"type": "acquire", "task": "T2", "lock": "L2"},
        {"type": "acquire", "task": "T3", "lock": "L2"},
        {"type": "acquire", "task": "T1", "lock": "L1"},
    ],
}


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "healthy"


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"lock" in r.data.lower() or "锁".encode() in r.data


def test_submit_freeze_reread(client):
    r = client.post("/api/audits", json=TWO_HOP)
    assert r.status_code == 200
    v = r.get_json()
    assert v["status"] == "ok"
    assert v["frozen"] is False
    assert v["steps"][-1]["effectivePriorities"]["T2"] == 1
    assert v["steps"][-1]["effectivePriorities"]["T3"] == 1

    # identical retransmission -> replayed and consistent
    r2 = client.post("/api/audits", json=TWO_HOP)
    assert r2.status_code == 200
    v2 = r2.get_json()
    assert v2["frozen"] is True
    assert v2["replayed"] is True

    # re-read frozen verdict by audit id
    r3 = client.get("/api/audits/FC-API-001")
    assert r3.status_code == 200
    assert r3.get_json()["status"] == "ok"


def test_conflict_on_different_content_same_id(client):
    client.post("/api/audits", json=TWO_HOP)
    changed = json.loads(json.dumps(TWO_HOP))
    changed["tasks"][0]["priority"] = 3  # same auditId, different input
    r = client.post("/api/audits", json=changed)
    assert r.status_code == 409
    assert r.get_json()["status"] == "conflict"
    # original frozen verdict untouched
    got = client.get("/api/audits/FC-API-001").get_json()
    assert got["steps"][-1]["effectivePriorities"]["T2"] == 1


def test_invalid_event_is_rejected_and_located(client):
    bad = json.loads(json.dumps(TWO_HOP))
    bad["auditId"] = "FC-API-BAD"
    bad["events"].append({"type": "release", "task": "T1", "lock": "L1"})
    r = client.post("/api/audits", json=bad)
    assert r.status_code == 422
    v = r.get_json()
    assert v["status"] == "rejected"
    assert v["failedEvent"] == 5
    assert v["steps"] == []
    # the rejected audit is still frozen under its own id
    got = client.get("/api/audits/FC-API-BAD").get_json()
    assert got["status"] == "rejected"


def test_missing_audit_id_400(client):
    r = client.post("/api/audits", json={"tasks": [], "locks": []})
    assert r.status_code == 400


def test_reread_unknown_404(client):
    r = client.get("/api/audits/NOPE")
    assert r.status_code == 404

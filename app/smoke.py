"""Live HTTP smoke test used by the verify container.

Exercises the real network path: health, page, submit (two-hop inheritance),
identical replay, frozen re-read, conflict, and rejection location.
"""

import json
import sys

import requests

base = sys.argv[1] if len(sys.argv) > 1 else "http://app:8080"
failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)


try:
    r = requests.get(base + "/api/health", timeout=5)
    check("health 200", r.status_code == 200 and r.json()["status"] == "healthy")

    r = requests.get(base + "/", timeout=5)
    check("index page", r.status_code == 200 and "飞控" in r.text)

    payload = {
        "auditId": "SMOKE-TWOHOP",
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
            {"type": "release", "task": "T2", "lock": "L2"},
        ],
    }
    r = requests.post(base + "/api/audits", json=payload, timeout=5)
    v = r.json()
    # after event 4 both intermediate owners are boosted through two hops
    blocked_eff = v["steps"][4]["effectivePriorities"]
    # after the release T3 acquires L2 but T2 leaves the graph -> T2 falls back
    final_eff = v["steps"][-1]["effectivePriorities"]
    check(
        "two-hop inheritance visible over HTTP",
        r.status_code == 200
        and blocked_eff["T2"] == 1
        and blocked_eff["T3"] == 1
        and final_eff["T3"] == 1
        and final_eff["T2"] == 5,
        json.dumps({"blocked": blocked_eff, "final": final_eff}),
    )

    # priority rollback trajectory
    rb = {
        "auditId": "SMOKE-ROLLBACK",
        "tasks": [{"id": "A", "priority": 8}, {"id": "U", "priority": 2}],
        "locks": [{"id": "M"}],
        "events": [
            {"type": "acquire", "task": "A", "lock": "M"},
            {"type": "acquire", "task": "U", "lock": "M"},
            {"type": "release", "task": "A", "lock": "M"},
        ],
    }
    v = requests.post(base + "/api/audits", json=rb, timeout=5).json()
    check(
        "priority falls back after release+handoff",
        v["steps"][-1]["effectivePriorities"]["A"] == 8
        and next(lk for lk in v["steps"][-1]["locks"] if lk["lock"] == "M")["owner"] == "U",
    )

    # tied handoff
    tie = {
        "auditId": "SMOKE-TIE",
        "tasks": [
            {"id": "O", "priority": 9},
            {"id": "W1", "priority": 4},
            {"id": "W2", "priority": 4},
        ],
        "locks": [{"id": "K"}],
        "events": [
            {"type": "acquire", "task": "O", "lock": "K"},
            {"type": "acquire", "task": "W1", "lock": "K"},
            {"type": "acquire", "task": "W2", "lock": "K"},
            {"type": "release", "task": "O", "lock": "K"},
        ],
    }
    v = requests.post(base + "/api/audits", json=tie, timeout=5).json()
    owner = next(lk for lk in v["steps"][-1]["locks"] if lk["lock"] == "K")["owner"]
    check("tied handoff picks smallest task id", owner == "W1", owner)

    # identical replay
    r = requests.post(base + "/api/audits", json=payload, timeout=5)
    v = r.json()
    check("identical retransmission replays identically",
          r.status_code == 200 and v.get("replayed") is True and v.get("frozen") is True)

    # frozen re-read
    v = requests.get(base + "/api/audits/SMOKE-TWOHOP", timeout=5).json()
    check("frozen verdict re-readable", v["status"] == "ok" and v.get("frozen") is True)

    # different content, same audit id -> conflict
    changed = json.loads(json.dumps(payload))
    changed["tasks"][0]["priority"] = 9
    r = requests.post(base + "/api/audits", json=changed, timeout=5)
    check("conflict on different content", r.status_code == 409
          and r.json()["status"] == "conflict")

    # invalid event located
    bad = json.loads(json.dumps(payload))
    bad["auditId"] = "SMOKE-BAD"
    bad["events"].append({"type": "release", "task": "T1", "lock": "L1"})
    r = requests.post(base + "/api/audits", json=bad, timeout=5)
    v = r.json()
    check("invalid event located and rejected",
          r.status_code == 422 and v["status"] == "rejected"
          and v["failedEvent"] == 6 and v["steps"] == [])

except requests.RequestException as exc:
    check("HTTP connectivity", False, repr(exc))

if failures:
    print("SMOKE FAILURES:", failures)
    sys.exit(1)
print("SMOKE: ALL HTTP CHECKS PASSED")

#!/usr/bin/env python3
"""Verification entrypoint for the ``verify`` compose service.

Runs, in order:

1. Build check  - all Python sources compile.
2. Rule tests   - the full unittest suite for the arbitration engine/store.
3. API/HTTP smoke against the live ``web`` service, exercising:
   - health endpoint and static page
   - two-hop transitive inheritance
   - priority fallback after release
   - tie handover by smallest task id
   - invalid-event localization + old-success clearing
   - same-input replay and different-input conflict (409)
   - frozen verdict re-read

Exits 0 only when every phase passes; non-zero otherwise. Compose reports the
status code via the container's exit status.
"""

from __future__ import annotations

import json
import os
import py_compile
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_URL = os.environ.get("WEB_BASE_URL", "http://web:8080").rstrip("/")

failures: list[str] = []


def report(phase: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {phase}" + (f" - {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(phase)


def check(label: bool, phase: str, detail: str) -> bool:
    report(phase, bool(label), detail)
    return bool(label)


# --------------------------------------------------------------------------- #
# 1. Build check
# --------------------------------------------------------------------------- #
def build_check() -> None:
    source_dirs = [os.path.join(ROOT, d) for d in ("app", "tests", "verify")]
    bad: list[str] = []
    count = 0
    for directory in source_dirs:
        for dirpath, _dirs, files in os.walk(directory):
            for name in files:
                if name.endswith(".py"):
                    count += 1
                    path = os.path.join(dirpath, name)
                    try:
                        py_compile.compile(path, doraise=True)
                    except py_compile.PyCompileError as exc:
                        bad.append(f"{path}: {exc}")
    check(not bad, "build: python compile check",
          f"{count} files compiled" if not bad else "; ".join(bad))


# --------------------------------------------------------------------------- #
# 2. Rule tests
# --------------------------------------------------------------------------- #
def rule_tests() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_rules", "-v"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-1] if proc.stderr else ""
    check(proc.returncode == 0, "rules: unittest suite", tail)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)


# --------------------------------------------------------------------------- #
# 3. HTTP / API smoke
# --------------------------------------------------------------------------- #
def http(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def eff_map(snapshot: dict) -> dict:
    return {t["id"]: t["effectivePriority"] for t in snapshot["tasks"]}


def smoke() -> None:
    status, body = http("GET", "/health")
    check(status == 200 and body.get("status") == "ok", "http: GET /health", str(body))

    # / serves HTML; read raw.
    try:
        with urllib.request.urlopen(BASE_URL + "/", timeout=5) as resp:
            html = resp.read().decode()
        check(resp.status == 200 and "优先级继承裁决台" in html,
              "http: GET / serves console", f"{len(html)} bytes")
    except (urllib.error.URLError, OSError) as exc:
        report("http: GET / serves console", False, str(exc))

    # --- two-hop inheritance ------------------------------------------------
    twohop = {
        "auditId": "VERIFY-TWOHOP",
        "tasks": [{"id": 1, "priority": 8}, {"id": 2, "priority": 5}, {"id": 3, "priority": 1}],
        "locks": [{"id": 1, "priority": 9}, {"id": 2, "priority": 9}],
        "events": [
            {"type": "acquire", "taskId": 2, "lockId": 2},
            {"type": "acquire", "taskId": 1, "lockId": 1},
            {"type": "acquire", "taskId": 2, "lockId": 1},
            {"type": "acquire", "taskId": 3, "lockId": 2},
            {"type": "release", "taskId": 1, "lockId": 1},
        ],
    }
    status, body = http("POST", "/api/verdicts", twohop)
    v = body.get("verdict", {})
    ok = (
        status == 200 and not body.get("replayed") and v.get("accepted")
        and eff_map(v["steps"][4]["snapshot"]) == {1: 1, 2: 1, 3: 1}
        and [3, 2, 1] in v["steps"][4]["snapshot"]["chains"]
        # step 5: A releases L1 to B; A falls back to base 8, B still boosted by C
        and eff_map(v["steps"][5]["snapshot"]) == {1: 8, 2: 1, 3: 1}
        and v["steps"][5]["snapshot"]["locks"][0]["holder"] == 2
    )
    check(ok, "api: two-hop inheritance + release recompute",
          f"step4/5 eff={eff_map(v['steps'][4]['snapshot']) if v.get('steps') else 'n/a'}")

    # --- replay: same payload ----------------------------------------------
    status, body = http("POST", "/api/verdicts", twohop)
    check(status == 200 and body.get("replayed") is True,
          "api: identical resubmission replays verdict", f"status={status}")

    # --- conflict: same audit id, different content -------------------------
    status, body = http("POST", "/api/verdicts", {**twohop, "events": []})
    check(status == 409 and body.get("error") == "conflict",
          "api: different content under same audit id -> 409", f"status={status}")

    # --- re-read frozen verdict ---------------------------------------------
    status, body = http("GET", "/api/verdicts/VERIFY-TWOHOP")
    check(status == 200 and body["verdict"]["accepted"],
          "api: re-read frozen verdict by audit id", f"status={status}")

    # --- fallback after release ---------------------------------------------
    fallback = {
        "auditId": "VERIFY-FALLBACK",
        "tasks": [{"id": 1, "priority": 8}, {"id": 2, "priority": 1}],
        "locks": [{"id": 1, "priority": 9}],
        "events": [
            {"type": "acquire", "taskId": 1, "lockId": 1},
            {"type": "acquire", "taskId": 2, "lockId": 1},
            {"type": "release", "taskId": 1, "lockId": 1},
        ],
    }
    status, body = http("POST", "/api/verdicts", fallback)
    v = body.get("verdict", {})
    ok = (
        v.get("accepted")
        and eff_map(v["steps"][2]["snapshot"]) == {1: 1, 2: 1}
        and eff_map(v["steps"][3]["snapshot"]) == {1: 8, 2: 1}
        and v["steps"][3]["snapshot"]["locks"][0]["holder"] == 2
    )
    check(ok, "api: priority falls back after release handover",
          f"after release eff={eff_map(v['steps'][3]['snapshot']) if v.get('steps') else 'n/a'}")

    # --- tie handover --------------------------------------------------------
    tie = {
        "auditId": "VERIFY-TIE",
        "tasks": [{"id": 1, "priority": 4}, {"id": 2, "priority": 2}, {"id": 3, "priority": 2}],
        "locks": [{"id": 1, "priority": 9}],
        "events": [
            {"type": "acquire", "taskId": 1, "lockId": 1},
            {"type": "acquire", "taskId": 3, "lockId": 1},
            {"type": "acquire", "taskId": 2, "lockId": 1},
            {"type": "release", "taskId": 1, "lockId": 1},
        ],
    }
    status, body = http("POST", "/api/verdicts", tie)
    v = body.get("verdict", {})
    final_locks = {l["id"]: l for l in v["finalSnapshot"]["locks"]} if v.get("finalSnapshot") else {}
    check(v.get("accepted") and final_locks.get(1, {}).get("holder") == 2
          and final_locks[1]["waiters"] == [3],
          "api: tie handover goes to smallest task id (2)",
          f"holder={final_locks.get(1, {}).get('holder')}")

    # --- invalid event localization + clearing old successes ----------------
    bad = {
        "auditId": "VERIFY-INVALID",
        "tasks": [{"id": 1, "priority": 8}, {"id": 2, "priority": 5}],
        "locks": [{"id": 1, "priority": 9}],
        "events": [
            {"type": "acquire", "taskId": 1, "lockId": 1},
            {"type": "release", "taskId": 2, "lockId": 1},
            {"type": "set-priority", "taskId": 1, "priority": 1},
        ],
    }
    status, body = http("POST", "/api/verdicts", bad)
    v = body.get("verdict", {})
    ok = (
        v.get("accepted") is False
        and v.get("errorIndex") == 2
        and "非拥有者" in v.get("error", "")
        and v.get("finalSnapshot") is None
        and v["steps"][2]["snapshot"] is None
        and v["steps"][1]["snapshot"] is not None  # prior success recorded pre-freeze
    )
    check(ok, "api: invalid event located at index 2 and verdict frozen",
          f"errorIndex={v.get('errorIndex')}")

    # frozen bad verdict replays identically
    status, body = http("POST", "/api/verdicts", bad)
    check(status == 200 and body["replayed"] and body["verdict"]["errorIndex"] == 2,
          "api: invalid verdict is frozen and replays identically", "")

    # --- bad request shapes / unknown references ----------------------------
    status, body = http("POST", "/api/verdicts", {"auditId": "", "tasks": [], "locks": [], "events": []})
    check(status == 400, "http: empty auditId rejected with 400", f"status={status}")
    status, body = http("POST", "/api/verdicts",
                        {"auditId": "VERIFY-UNKNOWN-REF", "tasks": [], "locks": [],
                         "events": [{"type": "acquire", "taskId": 1, "lockId": 1}]})
    v = body.get("verdict", {})
    check(status == 200 and v.get("accepted") is False and v.get("errorIndex") == 1
          and "不存在" in v.get("error", ""),
          "api: unknown object reference located and frozen at index 1",
          f"{v.get('error')}")


def main() -> int:
    print(f"== verification against {BASE_URL} ==", flush=True)
    build_check()
    rule_tests()
    smoke()
    print("-" * 60, flush=True)
    if failures:
        print(f"VERIFY RESULT: FAIL ({len(failures)} phase(s)): {', '.join(failures)}", flush=True)
        return 1
    print("VERIFY RESULT: PASS (all rule, build and API/HTTP checks passed)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

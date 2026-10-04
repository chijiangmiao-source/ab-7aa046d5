"""HTTP API + frozen-verdict store for the flight-control lock audit."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from engine import (
    MAX_EVENTS,
    MAX_LOCKS,
    MAX_TASKS,
    AuditError,
    build_error,
    replay,
)

DB_PATH = os.environ.get("AUDIT_DB", "/tmp/audit/verdicts.db")

app = Flask(__name__, static_folder="static", static_url_path="")
_db_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS verdicts(
               audit_id     TEXT PRIMARY KEY,
               input_hash   TEXT NOT NULL,
               payload      TEXT NOT NULL,
               created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    return conn


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _validate_envelope(data: Any) -> tuple[str, list, list, list]:
    if not isinstance(data, dict):
        raise AuditError("请求体必须是 JSON 对象")
    audit_id = str(data.get("auditId", "")).strip()
    if not audit_id:
        raise AuditError("缺少稳定审计标识 auditId")
    if len(audit_id) > 128:
        raise AuditError("auditId 长度不得超过 128")
    tasks = data.get("tasks")
    locks = data.get("locks")
    events = data.get("events", [])
    if not isinstance(tasks, list) or not isinstance(locks, list):
        raise AuditError("tasks 与 locks 必须是数组")
    if not isinstance(events, list):
        raise AuditError("events 必须是数组")
    if len(tasks) > MAX_TASKS:
        raise AuditError(f"任务至多 {MAX_TASKS} 个")
    if len(locks) > MAX_LOCKS:
        raise AuditError(f"锁至多 {MAX_LOCKS} 把")
    if len(events) > MAX_EVENTS:
        raise AuditError(f"事件至多 {MAX_EVENTS} 项")
    return audit_id, tasks, locks, events


@app.post("/api/audits")
def submit_audit():
    data = request.get_json(silent=True)
    try:
        audit_id, tasks, locks, events = _validate_envelope(data)
    except AuditError as err:
        return jsonify(build_error(err)), 400

    input_hash = hashlib.sha256(_canonical(data).encode()).hexdigest()

    with _db_lock:
        conn = _db()
        row = conn.execute(
            "SELECT input_hash, payload FROM verdicts WHERE audit_id=?",
            (audit_id,),
        ).fetchone()
        if row is not None:
            conn.close()
            if row["input_hash"] != input_hash:
                return (
                    jsonify(
                        {
                            "status": "conflict",
                            "error": (
                                f"审计标识 {audit_id} 已被不同输入占用，"
                                "内容不一致，拒绝覆盖"
                            ),
                            "frozen": True,
                        }
                    ),
                    409,
                )
            # Identical retransmission: replay on the spot and confirm the
            # replayed result matches the frozen verdict byte-for-byte.
            try:
                verdict = replay(tasks, locks, events)
            except AuditError as err:
                verdict = build_error(err)
            frozen = json.loads(row["payload"])
            verdict["replayed"] = _canonical(
                {k: x for k, x in verdict.items() if k not in ("frozen", "replayed")}
            ) == _canonical(
                {k: x for k, x in frozen.items() if k not in ("frozen", "replayed")}
            )
            verdict["frozen"] = True
            # a replayed rejection reports the same verdict status code
            return jsonify(verdict), (422 if verdict["status"] == "rejected" else 200)

        # First sighting: evaluate, then freeze.
        try:
            verdict = replay(tasks, locks, events)
            status_code = 200
        except AuditError as err:
            verdict = build_error(err)
            status_code = 422

        verdict["frozen"] = False
        conn.execute(
            "INSERT INTO verdicts(audit_id, input_hash, payload) VALUES(?,?,?)",
            (audit_id, input_hash, json.dumps(verdict, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()

    return jsonify(verdict), status_code


@app.get("/api/audits/<audit_id>")
def get_audit(audit_id: str):
    with _db_lock:
        conn = _db()
        row = conn.execute(
            "SELECT payload FROM verdicts WHERE audit_id=?", (audit_id,)
        ).fetchone()
        conn.close()
    if row is None:
        return jsonify({"status": "missing", "error": "未找到该审计标识的冻结裁决"}), 404
    verdict = json.loads(row["payload"])
    verdict["frozen"] = True
    return jsonify(verdict), 200


@app.get("/api/health")
def health():
    ok = True
    try:
        with _db_lock:
            conn = _db()
            conn.execute("SELECT 1")
            conn.close()
    except Exception:  # pragma: no cover - surfaced as unhealthy
        ok = False
    return jsonify({"status": "healthy" if ok else "unhealthy", "service": "lock-audit"}), (
        200 if ok else 503
    )


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

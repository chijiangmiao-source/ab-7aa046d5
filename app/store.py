"""Frozen-verdict store: idempotent replay and content-conflict detection.

A submission keyed by a stable audit id is frozen on first sight.  Re-sending
the *same* content under the same audit id replays the stored verdict; sending
*different* content is a conflict and is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone

from engine import Engine, MAX_EVENTS, MAX_LOCKS, MAX_TASKS, VALID_EVENT_TYPES


class ValidationError(ValueError):
    """Structural request error -> HTTP 400 (no verdict is produced)."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _specs(items: object, kind: str, limit: int) -> list[dict]:
    if not isinstance(items, list):
        raise ValidationError(f"{kind} 必须是数组")
    if len(items) > limit:
        raise ValidationError(f"{kind} 数量上限为 {limit}，收到 {len(items)}")
    normalized: list[dict] = []
    seen: set[int] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError(f"{kind}[{i}] 必须是对象")
        tid = item.get("id")
        if not _is_int(tid):
            raise ValidationError(f"{kind}[{i}] 的 id 必须是整数")
        if tid in seen:
            raise ValidationError(f"{kind} id {tid} 重复")
        seen.add(tid)
        prio = item.get("priority")
        if not _is_int(prio):
            raise ValidationError(f"{kind}[{i}] 的 priority 必须是整数")
        normalized.append({"id": tid, "priority": prio})
    return normalized


def normalize(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    audit_id = payload.get("auditId")
    if not isinstance(audit_id, str) or not audit_id.strip():
        raise ValidationError("auditId 必须是非空字符串")
    audit_id = audit_id.strip()

    tasks = _specs(payload.get("tasks", []), "tasks", MAX_TASKS)
    locks = _specs(payload.get("locks", []), "locks", MAX_LOCKS)

    events = payload.get("events", [])
    if not isinstance(events, list):
        raise ValidationError("events 必须是数组")
    if len(events) > MAX_EVENTS:
        raise ValidationError(f"events 数量上限为 {MAX_EVENTS}，收到 {len(events)}")
    norm_events: list[dict] = []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise ValidationError(f"events[{i}] 必须是对象")
        clean: dict = {"type": ev.get("type")}
        if "taskId" in ev:
            clean["taskId"] = ev["taskId"]
        if "lockId" in ev:
            clean["lockId"] = ev["lockId"]
        if "priority" in ev:
            clean["priority"] = ev["priority"]
        norm_events.append(clean)

    return {"auditId": audit_id, "tasks": tasks, "locks": locks, "events": norm_events}


def canonical_hash(sub: dict) -> str:
    raw = json.dumps(
        {"tasks": sub["tasks"], "locks": sub["locks"], "events": sub["events"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def replay(sub: dict) -> dict:
    """Re-run the deterministic state machine from scratch."""
    engine = Engine(sub["tasks"], sub["locks"])
    return engine.apply(sub["events"])


class VerdictStore:
    def __init__(self, path: str | None = None):
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}
        if path is None:
            path = os.environ.get("DATA_FILE", "data/verdicts.json")
        self.path = path
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._records = data
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError):
            # Corrupt storage must not take the service down; start cold.
            self._records = {}

    def _persist_locked(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._records, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def submit(self, sub: dict) -> tuple[dict, int]:
        """Return (envelope, http_status). 200 new, 200 replayed, 409 conflict."""
        incoming_hash = canonical_hash(sub)
        with self._lock:
            existing = self._records.get(sub["auditId"])
            if existing is not None:
                if existing["inputHash"] != incoming_hash:
                    return (
                        {
                            "error": "conflict",
                            "message": (
                                f"审计标识 {sub['auditId']} 已有不同内容的冻结裁决，"
                                "内容不同即冲突"
                            ),
                            "auditId": sub["auditId"],
                            "storedHash": existing["inputHash"],
                            "incomingHash": incoming_hash,
                            "storedAt": existing["createdAt"],
                        },
                        409,
                    )
                return self._public(existing, replayed=True), 200

            verdict = replay(sub)
            record = {
                "auditId": sub["auditId"],
                "inputHash": incoming_hash,
                "input": sub,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "verdict": verdict,
            }
            self._records[sub["auditId"]] = record
            try:
                self._persist_locked()
            except OSError:
                # Memory still holds the verdict even if disk persistence fails.
                pass
            return self._public(record, replayed=False), 200

    @staticmethod
    def _public(record: dict, replayed: bool) -> dict:
        return {
            "auditId": record["auditId"],
            "inputHash": record["inputHash"],
            "createdAt": record["createdAt"],
            "replayed": replayed,
            "conflict": False,
            "verdict": record["verdict"],
        }

    def get(self, audit_id: str) -> dict | None:
        with self._lock:
            record = self._records.get(audit_id)
            return self._public(record, replayed=True) if record else None

    def list(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "auditId": r["auditId"],
                    "inputHash": r["inputHash"],
                    "createdAt": r["createdAt"],
                    "accepted": r["verdict"]["accepted"],
                    "errorIndex": r["verdict"]["errorIndex"],
                }
                for r in sorted(self._records.values(), key=lambda r: r["createdAt"])
            ]

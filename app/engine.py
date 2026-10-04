"""Priority inheritance protocol engine for the flight-control lock audit.

Rules (smaller numeric priority == more urgent):

* Non-reentrant mutex locks. A task already holding a lock cannot acquire it
  again, and a lock is held by at most one task.
* When tasks block, an effective priority propagates along *any* nesting depth
  of wait edges to the blocking owner:
      waiter --(waits for lock held by owner)--> owner
  The owner's effective priority is the minimum (most urgent) of its own base
  priority and the effective priorities of every task that can reach it
  through the wait graph.
* On release or cancel, effective priorities are recomputed from the current
  wait graph, and the released lock is handed to the waiter with the highest
  effective priority, ties broken by the smallest task id.
* Invalid events (release by a non-owner, cancel of a running task, duplicate
  wait on the same lock, waiting cycles, references to unknown tasks/locks)
  reject the whole audit: the offending event is located and every earlier
  successful effect is rolled back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MAX_TASKS = 16
MAX_LOCKS = 32
MAX_EVENTS = 128

VALID_EVENTS = {"acquire", "release", "set-priority", "cancel"}


class AuditError(ValueError):
    """An input/validation or protocol error; carries event location info."""

    def __init__(self, message: str, index: Optional[int] = None):
        super().__init__(message)
        self.index = index  # 0-based event index, None for input-level errors


@dataclass
class Task:
    tid: str
    base_priority: int
    cancelled: bool = False
    # locks currently owned, in acquisition order
    held: list[str] = field(default_factory=list)
    # lock the task is currently blocked on (at most one per task)
    waiting_for: Optional[str] = None

    def clone(self) -> "Task":
        t = Task(self.tid, self.base_priority, self.cancelled)
        t.held = list(self.held)
        t.waiting_for = self.waiting_for
        return t


@dataclass
class Lock:
    lid: str
    owner: Optional[str] = None
    # ordered list of waiting task ids
    waiters: list[str] = field(default_factory=list)

    def clone(self) -> "Lock":
        l = Lock(self.lid, self.owner)
        l.waiters = list(self.waiters)
        return l


class AuditEngine:
    """Pure state machine: replay events, recording a snapshot per step."""

    def __init__(self, tasks: list[dict], locks: list[dict]):
        if not (1 <= len(tasks) <= MAX_TASKS):
            raise AuditError(f"任务数量必须在 1..{MAX_TASKS} 之间")
        if not (1 <= len(locks) <= MAX_LOCKS):
            raise AuditError(f"锁数量必须在 1..{MAX_LOCKS} 之间")

        self.tasks: dict[str, Task] = {}
        self.locks: dict[str, Lock] = {}

        seen_t: set[str] = set()
        for raw in tasks:
            tid = str(raw.get("id", "")).strip()
            if not tid:
                raise AuditError("存在缺少 id 的任务")
            if tid in seen_t:
                raise AuditError(f"任务 id 重复: {tid}")
            seen_t.add(tid)
            pri = _as_int(raw.get("priority"), f"任务 {tid} 的优先级")
            self.tasks[tid] = Task(tid, pri)

        seen_l: set[str] = set()
        for raw in locks:
            lid = str(raw.get("id", "")).strip()
            if not lid:
                raise AuditError("存在缺少 id 的锁")
            if lid in seen_l:
                raise AuditError(f"锁 id 重复: {lid}")
            seen_l.add(lid)
            self.locks[lid] = Lock(lid)

        # snapshots: one entry *before* any event plus one after every event
        self.steps: list[dict] = []
        self.steps.append(self._snapshot(None, None, "initial"))

    # ------------------------------------------------------------------ run
    def run(self, events: list[dict]) -> list[dict]:
        if len(events) > MAX_EVENTS:
            raise AuditError(f"事件数量不得超过 {MAX_EVENTS}")
        for i, ev in enumerate(events):
            kind = ev.get("type")
            if kind not in VALID_EVENTS:
                raise AuditError(
                    f"不支持的事件类型: {kind!r}（允许 {sorted(VALID_EVENTS)}）", i
                )
            tid = str(ev.get("task", "")).strip()
            if tid not in self.tasks:
                raise AuditError(f"事件引用了不存在的任务: {tid or '(空)'}", i)
            if kind in ("acquire", "release"):
                lid = str(ev.get("lock", "")).strip()
                if lid not in self.locks:
                    raise AuditError(f"事件引用了不存在的锁: {lid or '(空)'}", i)
                if kind == "acquire":
                    self._acquire(i, tid, lid)
                else:
                    self._release(i, tid, lid)
            elif kind == "set-priority":
                try:
                    pri = _as_int(ev.get("priority"), "set-priority 的新优先级")
                except AuditError as err:
                    raise AuditError(str(err), i) from None
                self._set_priority(i, tid, pri)
            else:  # cancel
                self._cancel(i, tid)
        return self.steps

    # ------------------------------------------------------------- operations
    def _acquire(self, idx: int, tid: str, lid: str) -> None:
        task = self.tasks[tid]
        lock = self.locks[lid]
        if task.cancelled:
            raise AuditError(f"任务 {tid} 已取消，不能再请求锁 {lid}", idx)
        if lid in task.held:
            raise AuditError(
                f"不可重入: 任务 {tid} 已持有锁 {lid}，不能重复 acquire", idx
            )
        if task.waiting_for is not None:
            raise AuditError(
                f"任务 {tid} 正在等待锁 {task.waiting_for}，不能再等待锁 {lid}"
                "（重复/嵌套等待）",
                idx,
            )

        if lock.owner is None:
            lock.owner = tid
            task.held.append(lid)
            self._commit(idx, "acquire", f"任务 {tid} 获得空闲锁 {lid}")
            return

        # block: become a waiter, then check whether that closes a cycle
        if tid in lock.waiters:
            raise AuditError(f"任务 {tid} 已在锁 {lid} 的等待队列中", idx)
        lock.waiters.append(tid)
        task.waiting_for = lid
        if self._wait_cycle():
            raise AuditError(
                f"等待环: 任务 {tid} 等待锁 {lid} 后形成循环等待", idx
            )
        self._commit(
            idx,
            "acquire",
            f"任务 {tid} 阻塞，等待锁 {lid}（持有者 {lock.owner}）",
        )

    def _release(self, idx: int, tid: str, lid: str) -> None:
        task = self.tasks[tid]
        lock = self.locks[lid]
        if lock.owner != tid:
            owner = lock.owner or "(无)"
            raise AuditError(
                f"非拥有者释放: 锁 {lid} 的持有者是 {owner}，而非 {tid}", idx
            )

        task.held.remove(lid)
        lock.owner = None
        handed: Optional[str] = None

        # recompute effective priorities over the wait graph *without* this
        # lock, then pick the most urgent waiter (smallest id as tie-break).
        eff = self._effective_priorities()
        if lock.waiters:
            chosen = min(lock.waiters, key=lambda w: (eff[w], w))
            handed = chosen
            waiter = self.tasks[chosen]
            lock.waiters.remove(chosen)
            waiter.waiting_for = None
            lock.owner = chosen
            waiter.held.append(lid)

        # priorities re-settle after the graph/ownership change
        self._commit(
            idx,
            "release",
            f"任务 {tid} 释放锁 {lid}"
            + (f"，锁移交给 {handed}" if handed else "，无等待者"),
        )

    def _set_priority(self, idx: int, tid: str, pri: int) -> None:
        task = self.tasks[tid]
        if task.cancelled:
            raise AuditError(f"任务 {tid} 已取消，不能再设置优先级", idx)
        old = task.base_priority
        task.base_priority = pri
        self._commit(idx, "set-priority", f"任务 {tid} 的基准优先级 {old} -> {pri}")

    def _cancel(self, idx: int, tid: str) -> None:
        task = self.tasks[tid]
        # Only a blocked task may be cancelled: cancelling a running task
        # (one that is not waiting) is an invalid event.
        if task.waiting_for is None:
            if task.cancelled:
                raise AuditError(f"任务 {tid} 已取消，不能重复 cancel", idx)
            raise AuditError(
                f"任务 {tid} 当前未被阻塞，取消运行中的任务属于非法事件", idx
            )

        lid = task.waiting_for
        lock = self.locks[lid]
        lock.waiters.remove(tid)
        task.waiting_for = None
        task.cancelled = True  # cancelled task leaves the system entirely

        # It may still nest other locks acquired before it blocked; release
        # each of them, handing to the currently most-urgent waiter.
        handed: list[str] = []
        for held in list(task.held):
            hlock = self.locks[held]
            hlock.owner = None
            if hlock.waiters:
                eff = self._effective_priorities()
                chosen = min(hlock.waiters, key=lambda w: (eff[w], w))
                waiter = self.tasks[chosen]
                hlock.waiters.remove(chosen)
                waiter.waiting_for = None
                hlock.owner = chosen
                waiter.held.append(held)
                handed.append(f"{held}->{chosen}")
        task.held = []

        note = f"等待任务 {tid} 被取消，退出对锁 {lid} 的等待"
        if handed:
            note += "；其持有锁移交: " + ", ".join(handed)
        self._commit(idx, "cancel", note)

    # ------------------------------------------------------------ graph math
    def _wait_cycle(self) -> bool:
        """True iff following task -> waited-lock -> owner reaches itself."""
        color: dict[str, int] = {tid: 0 for tid in self.tasks}  # 0/1/2

        def visit(tid: str) -> bool:
            color[tid] = 1
            t = self.tasks[tid]
            if t.waiting_for is not None:
                owner = self.locks[t.waiting_for].owner
                if owner is not None:
                    if color[owner] == 1:
                        return True
                    if color[owner] == 0 and visit(owner):
                        return True
            color[tid] = 2
            return False

        return any(color[t] == 0 and visit(t) for t in self.tasks)

    def _effective_priorities(self) -> dict[str, int]:
        """Most urgent priority reachable to each task via wait edges.

        edge: waiter w --(waits on lock owned by o)--> o, meaning urgency of
        w (including everything inherited by w) propagates to o.
        """
        eff = {tid: t.base_priority for tid, t in self.tasks.items()}

        # Iterate to fixed point; with <=16 tasks this trivially converges.
        changed = True
        while changed:
            changed = False
            for tid, task in self.tasks.items():
                if task.waiting_for is None:
                    continue
                owner = self.locks[task.waiting_for].owner
                if owner is None:
                    continue
                cand = min(eff[tid], task.base_priority)
                if cand < eff[owner]:
                    eff[owner] = cand
                    changed = True
        return eff

    # -------------------------------------------------------------- snapshot
    def _commit(self, idx: int, etype: str, note: str) -> None:
        self.steps.append(self._snapshot(idx, etype, note))

    def _snapshot(
        self, idx: Optional[int], etype: Optional[str], note: str
    ) -> dict:
        eff = self._effective_priorities()
        running, waiting, holding = [], [], []
        for tid, t in sorted(self.tasks.items()):
            if t.cancelled:
                continue
            if t.waiting_for is not None:
                waiting.append(
                    {
                        "task": tid,
                        "lock": t.waiting_for,
                        "owner": self.locks[t.waiting_for].owner,
                        "basePriority": t.base_priority,
                        "effectivePriority": eff[tid],
                    }
                )
            else:
                running.append(
                    {
                        "task": tid,
                        "basePriority": t.base_priority,
                        "effectivePriority": eff[tid],
                    }
                )
            if t.held:
                holding.append(
                    {
                        "task": tid,
                        "locks": list(t.held),
                        "basePriority": t.base_priority,
                        "effectivePriority": eff[tid],
                    }
                )
        locks = []
        for lid, lk in sorted(self.locks.items()):
            locks.append(
                {
                    "lock": lid,
                    "owner": lk.owner,
                    "waiters": [
                        {
                            "task": w,
                            "basePriority": self.tasks[w].base_priority,
                            "effectivePriority": eff[w],
                        }
                        for w in lk.waiters
                    ],
                }
            )
        return {
            "index": idx,
            "type": etype,
            "note": note,
            "running": running,
            "waiting": waiting,
            "holding": holding,
            "locks": locks,
            "effectivePriorities": {
                tid: eff[tid] for tid in sorted(eff) if not self.tasks[tid].cancelled
            },
        }


def _as_int(value, what: str) -> int:
    if isinstance(value, bool):
        raise AuditError(f"{what} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AuditError(f"{what} 必须是整数，收到 {value!r}")


def replay(tasks: list[dict], locks: list[dict], events: list[dict]) -> dict:
    """Replay an audit on a fresh engine.

    On any error the partially-mutated engine is discarded, so the caller
    always observes an all-or-nothing verdict ("清除旧成功").
    """
    engine = AuditEngine(tasks, locks)
    steps = engine.run(events)
    return {
        "status": "ok",
        "error": None,
        "failedEvent": None,
        "steps": steps,
    }


def build_error(err: AuditError) -> dict:
    return {
        "status": "rejected",
        "error": str(err),
        "failedEvent": (err.index + 1) if err.index is not None else None,
        "steps": [],
    }

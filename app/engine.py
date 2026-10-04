"""Priority-inheritance lock arbitration engine.

Rules (smaller number = more urgent):

* Locks are non-reentrant mutual-exclusion locks.
* A task waiting on a lock held by another task propagates its *effective*
  urgency to the holder.  Propagation is transitive: it follows any nested
  wait chain (``A waits on lock held by B, B waits on lock held by C``).
* Effective priority is recomputed from the current wait graph after every
  successful ``release`` and every ``cancel``.
* On release a lock is handed to the waiter with the highest effective
  priority (smallest value); ties are broken by the smallest task id.
* Invalid events (release by a non-owner, cancel of a running task, a task
  waiting twice on the same lock, creating a wait cycle, references to
  unknown objects) pinpoint the offending event, discard every prior
  successful effect of that submission ("clear old successes") and freeze
  the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MAX_TASKS = 16
MAX_LOCKS = 32
MAX_EVENTS = 128

VALID_EVENT_TYPES = frozenset({"acquire", "release", "set-priority", "cancel"})


class EngineError(Exception):
    """Marker base class; the message is operator-facing Chinese text."""


@dataclass
class Task:
    tid: int
    base: int
    waiting_for: Optional[int] = None  # lock id the task is blocked on

    def snapshot(self) -> dict:
        return {
            "id": self.tid,
            "basePriority": self.base,
            "waitingFor": self.waiting_for,
        }


@dataclass
class Lock:
    lid: int
    base: int
    holder: Optional[int] = None
    waiters: list[int] = field(default_factory=list)  # arrival order

    def snapshot(self) -> dict:
        return {
            "id": self.lid,
            "basePriority": self.base,
            "holder": self.holder,
            "waiters": list(self.waiters),
        }


class Engine:
    """Pure in-memory state machine; one instance per submission replay."""

    def __init__(self, task_specs: list[dict], lock_specs: list[dict]):
        self.tasks: dict[int, Task] = {}
        self.locks: dict[int, Lock] = {}
        for spec in task_specs:
            tid = spec["id"]
            self.tasks[tid] = Task(tid=tid, base=int(spec["priority"]))
        for spec in lock_specs:
            lid = spec["id"]
            self.locks[lid] = Lock(lid=lid, base=int(spec["priority"]))

    # ------------------------------------------------------------------ #
    # Effective priority / inheritance graph
    # ------------------------------------------------------------------ #
    def effective_priority(self, tid: int, stack: Optional[list[int]] = None) -> int:
        """Effective urgency of a task.

        A task inherits the highest urgency (minimum numeric value) found
        anywhere in its nested waiter subtree: every task directly waiting on
        a lock it holds, their waiters, and so on.
        """

        if stack is None:
            stack = []
        # A wait cycle is rejected when it is created, so reaching a task
        # already on the stack here would be an internal inconsistency.
        if tid in stack:
            raise EngineError(f"等待图中存在涉及任务 {tid} 的环")
        task = self.tasks[tid]
        prio = task.base
        stack = stack + [tid]
        for lock in self.locks.values():
            if lock.holder == tid:
                for wid in lock.waiters:
                    w_prio = self.effective_priority(wid, stack)
                    if w_prio < prio:
                        prio = w_prio
        return prio

    def _holder_chain(self, waiter: int) -> list[int]:
        """Tasks reachable by following holder links from a waiter."""
        chain: list[int] = []
        cur = waiter
        seen = set()
        while cur is not None:
            lid = self.tasks[cur].waiting_for
            if lid is None:
                break
            holder = self.locks[lid].holder
            if holder is None or holder in seen:
                break
            seen.add(holder)
            chain.append(holder)
            cur = holder
        return chain

    def inherited_from(self, tid: int) -> list[int]:
        """Waiters (transitively, de-duplicated, arrival order) that drive tid."""
        result: list[int] = []
        seen = {tid}

        def walk(owner: int) -> None:
            for lock in self.locks.values():
                if lock.holder != owner:
                    continue
                for wid in lock.waiters:
                    if wid not in seen:
                        seen.add(wid)
                        result.append(wid)
                        walk(wid)

        walk(tid)
        return result

    def inheritance_chains(self) -> list[list[int]]:
        """Every distinct nested wait chain ``[waiter, holder, ...]``."""
        chains: list[list[int]] = []
        for task in self.tasks.values():
            if task.waiting_for is not None:
                chains.append([task.tid] + self._holder_chain(task.tid))
        return chains

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #
    def snapshot(self, note: str = "") -> dict:
        eff = {tid: self.effective_priority(tid) for tid in sorted(self.tasks)}
        return {
            "tasks": [
                {
                    **self.tasks[tid].snapshot(),
                    "effectivePriority": eff[tid],
                    "inheritedFrom": self.inherited_from(tid),
                    "holdingLocks": sorted(
                        lid for lid, lk in self.locks.items() if lk.holder == tid
                    ),
                    "state": "waiting" if self.tasks[tid].waiting_for is not None else "running",
                }
                for tid in sorted(self.tasks)
            ],
            "locks": [self.locks[lid].snapshot() for lid in sorted(self.locks)],
            "chains": self.inheritance_chains(),
            "note": note,
        }

    # ------------------------------------------------------------------ #
    # Event processing
    # ------------------------------------------------------------------ #
    def apply(self, events: list[dict]) -> dict:
        steps: list[dict] = []
        # Step 0: the frozen baseline before any event runs.
        steps.append(
            {
                "index": 0,
                "raw": None,
                "type": "init",
                "ok": True,
                "detail": "初始基准状态",
                "snapshot": self.snapshot(),
            }
        )

        for idx, ev in enumerate(events, start=1):
            etype = ev.get("type")
            tid = ev.get("taskId")
            lid = ev.get("lockId")
            try:
                if etype not in VALID_EVENT_TYPES:
                    raise EngineError(
                        f"事件类型必须是 acquire/release/set-priority/cancel 之一，收到 {etype!r}"
                    )
                if not isinstance(tid, int) or isinstance(tid, bool) or tid not in self.tasks:
                    raise EngineError(f"事件引用了不存在的任务 {tid!r}")
                task = self.tasks[tid]

                if etype == "acquire":
                    blocked = self._acquire(task, lid)
                    if blocked:
                        detail = (
                            f"任务 {tid} 请求锁 {lid} 被持有，进入等待队列；"
                            f"紧急度沿嵌套等待链传递"
                        )
                    else:
                        detail = f"任务 {tid} 获取锁 {lid}，成为持有者"
                elif etype == "release":
                    handed = self._release(task, lid)
                    if handed is not None:
                        detail = (
                            f"任务 {tid} 释放锁 {lid}，按有效优先级（并列取最小任务 id）"
                            f"移交给等待者任务 {handed}"
                        )
                    else:
                        detail = f"任务 {tid} 释放锁 {lid}，无等待者，锁空闲"
                elif etype == "set-priority":
                    self._set_priority(task, ev.get("priority"))
                    detail = f"任务 {tid} 基准优先级调整为 {task.base}"
                else:  # cancel
                    self._cancel(task)
                    detail = f"任务 {tid} 已取消，移出所有等待队列"

                steps.append(
                    {
                        "index": idx,
                        "raw": ev,
                        "type": etype,
                        "ok": True,
                        "detail": detail,
                        "snapshot": self.snapshot(),
                    }
                )
            except EngineError as exc:
                # Freeze immediately: the offending event is located, and the
                # whole submission is void — callers discard prior successes.
                steps.append(
                    {
                        "index": idx,
                        "raw": ev,
                        "type": etype if etype in VALID_EVENT_TYPES else "invalid",
                        "ok": False,
                        "detail": str(exc),
                        "snapshot": None,
                    }
                )
                return {
                    "accepted": False,
                    "errorIndex": idx,
                    "errorEvent": ev,
                    "error": str(exc),
                    "steps": steps,
                    "finalSnapshot": None,
                }

        return {
            "accepted": True,
            "errorIndex": None,
            "errorEvent": None,
            "error": None,
            "steps": steps,
            "finalSnapshot": self.snapshot("终态"),
        }

    def _acquire(self, task: Task, lid: object) -> bool:
        """Return True when the task joined the wait queue, False if it got the lock."""
        if not isinstance(lid, int) or isinstance(lid, bool) or lid not in self.locks:
            raise EngineError(f"acquire 引用了不存在的锁 {lid!r}")
        lock = self.locks[lid]

        if lock.holder == task.tid:
            raise EngineError(f"任务 {task.tid} 已持有锁 {lid}，该锁不可重入")
        if task.waiting_for is not None:
            if task.waiting_for == lid and task.tid in lock.waiters:
                raise EngineError(f"任务 {task.tid} 已在锁 {lid} 等待队列中，不得重复等待同一锁")
            raise EngineError(
                f"任务 {task.tid} 已阻塞在锁 {task.waiting_for} 上，不能再等待锁 {lid}"
            )

        if lock.holder is None:
            lock.holder = task.tid
            return False

        # Join the FIFO wait queue, then reject the event if it would close a
        # wait cycle (e.g. the holder chain ultimately depends on this task).
        lock.waiters.append(task.tid)
        task.waiting_for = lid
        chain = self._holder_chain(task.tid)
        if task.tid in chain:
            lock.waiters.pop()
            task.waiting_for = None
            raise EngineError(
                f"任务 {task.tid} 等待锁 {lid} 将形成等待环（沿嵌套等待链回到自身）"
            )
        return True

    def _release(self, task: Task, lid: object) -> Optional[int]:
        """Release; return the waiter the lock was handed to, or None."""
        if not isinstance(lid, int) or isinstance(lid, bool) or lid not in self.locks:
            raise EngineError(f"release 引用了不存在的锁 {lid!r}")
        lock = self.locks[lid]
        if lock.holder != task.tid:
            owner = "无人持有" if lock.holder is None else f"任务 {lock.holder}"
            raise EngineError(
                f"非拥有者释放：锁 {lid} 当前由{owner}，任务 {task.tid} 无权释放"
            )

        lock.holder = None
        chosen: Optional[int] = None
        if lock.waiters:
            # Effective priorities are read off the graph as it stands before
            # the handover (nobody holds this lock yet, so the result does not
            # depend on the new holder).
            ranked = sorted(
                lock.waiters,
                key=lambda w: (self.effective_priority(w), w),
            )
            chosen = ranked[0]
            lock.waiters.remove(chosen)
            self.tasks[chosen].waiting_for = None
            lock.holder = chosen
        return chosen

    def _set_priority(self, task: Task, value: object) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise EngineError(f"set-priority 的优先级必须是整数，收到 {value!r}")
        task.base = value

    def _cancel(self, task: Task) -> None:
        if task.waiting_for is None:
            holding = [lid for lid, lk in self.locks.items() if lk.holder == task.tid]
            if holding:
                raise EngineError(
                    f"任务 {task.tid} 仍在运行且持有锁 {holding}，不能取消（须先释放）"
                )
            raise EngineError(f"任务 {task.tid} 处于运行态，取消运行任务非法")
        lid = task.waiting_for
        lock = self.locks[lid]
        if task.tid in lock.waiters:
            lock.waiters.remove(task.tid)
        task.waiting_for = None

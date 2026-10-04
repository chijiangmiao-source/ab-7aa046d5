"""Rule tests run by the verify container (pytest)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import AuditError, build_error, replay  # noqa: E402

T = lambda tid, p: {"id": tid, "priority": p}
L = lambda lid: {"id": lid}
AC = lambda t, l: {"type": "acquire", "task": t, "lock": l}
RL = lambda t, l: {"type": "release", "task": t, "lock": l}
SP = lambda t, p: {"type": "set-priority", "task": t, "priority": p}
CN = lambda t: {"type": "cancel", "task": t}


def eff_at(verdict, step=-1):
    return verdict["steps"][step]["effectivePriorities"]


def lock_of(verdict, lid, step=-1):
    return next(
        lk for lk in verdict["steps"][step]["locks"] if lk["lock"] == lid
    )


# ----------------------------------------------------------- 1. two-hop chain
def test_two_hop_inheritance():
    """U(1) -> L1 held by M(10); M -> L2 held by L(5): both inherit 1."""
    v = replay(
        [T("U", 1), T("M", 10), T("L", 5)],
        [L("L1"), L("L2")],
        [
            AC("M", "L1"),
            AC("L", "L2"),
            AC("M", "L2"),  # M blocks behind L
            AC("U", "L1"),  # U blocks behind M
        ],
    )
    e = eff_at(v)
    assert e == {"L": 1, "M": 1, "U": 1}
    # waiting structure captured in snapshots
    waits = {(w["task"], w["lock"]) for w in v["steps"][-1]["waiting"]}
    assert ("U", "L1") in waits and ("M", "L2") in waits


def test_release_hands_off_and_priority_falls_back():
    v = replay(
        [T("A", 8), T("U", 2)],
        [L("M")],
        [AC("A", "M"), AC("U", "M"), RL("A", "M"), RL("U", "M")],
    )
    # while U waits, A is boosted to U's urgency
    assert eff_at(v, step=2)["A"] == 2
    # after handoff the lock is U's and A falls back to its base 8
    s3 = v["steps"][3]
    assert lock_of(v, "M", step=3)["owner"] == "U"
    assert s3["effectivePriorities"]["A"] == 8
    assert s3["effectivePriorities"]["U"] == 2
    # final release frees the lock
    assert lock_of(v, "M")["owner"] is None


def test_tie_handoff_smallest_task_id():
    v = replay(
        [T("O", 9), T("W1", 4), T("W2", 4)],
        [L("K")],
        [AC("O", "K"), AC("W1", "K"), AC("W2", "K"), RL("O", "K")],
    )
    lk = lock_of(v, "K")
    assert lk["owner"] == "W1"
    assert [w["task"] for w in lk["waiters"]] == ["W2"]
    # O no longer connected to the wait graph -> back to base priority
    assert eff_at(v)["O"] == 9


def test_three_hop_chain_propagates_to_root_owner():
    """U(1)->L1<-M2(10,holds L1,waits L2)<-M3(20,holds L2,waits L3)<-R(30).
    Urgency 1 must reach every owner along the arbitrary nesting chain."""
    v = replay(
        [T("U", 1), T("M2", 10), T("M3", 20), T("R", 30)],
        [L("L1"), L("L2"), L("L3")],
        [
            AC("M2", "L1"),
            AC("M3", "L2"),
            AC("R", "L3"),
            AC("M3", "L3"),  # M3 -> R
            AC("M2", "L2"),  # M2 -> M3
            AC("U", "L1"),   # U -> M2
        ],
    )
    assert eff_at(v) == {"M2": 1, "M3": 1, "R": 1, "U": 1}


def test_task_cannot_wait_on_two_locks_at_once():
    with pytest.raises(AuditError) as exc:
        replay(
            [T("O", 9), T("W1", 7), T("W2", 7), T("W3", 1)],
            [L("Ka"), L("Kb")],
            [
                AC("O", "Ka"),
                AC("W1", "Ka"),  # W1 blocks on Ka
                AC("W2", "Kb"),
                AC("W1", "Kb"),  # illegal: W1 already waiting
            ],
        )
    assert exc.value.index == 3


def test_set_priority_propagates_through_existing_graph():
    v = replay(
        [T("A", 8), T("U", 5)],
        [L("M")],
        [AC("A", "M"), AC("U", "M"), SP("U", 1)],
    )
    assert eff_at(v)["A"] == 1
    # releasing hands to U and A falls back
    v2 = replay(
        [T("A", 8), T("U", 5)],
        [L("M")],
        [AC("A", "M"), AC("U", "M"), SP("U", 1), RL("A", "M")],
    )
    assert eff_at(v2)["A"] == 8


def test_cancel_waiter_removes_edge_and_recomputes():
    v = replay(
        [T("A", 8), T("U", 2)],
        [L("M")],
        [AC("A", "M"), AC("U", "M"), CN("U")],
    )
    assert eff_at(v)["A"] == 8
    assert lock_of(v, "M")["owner"] == "A"
    assert lock_of(v, "M")["waiters"] == []


def test_cancel_owner_in_chain_hands_over_held_locks():
    # O holds L1 and waits on L2 (held by B); urgent U waits on L1.
    # Cancelling O must give L1 to U and detach the chain.
    v = replay(
        [T("O", 9), T("B", 6), T("U", 1)],
        [L("L1"), L("L2")],
        [
            AC("O", "L1"),
            AC("B", "L2"),
            AC("O", "L2"),
            AC("U", "L1"),
            CN("O"),
        ],
    )
    assert lock_of(v, "L1")["owner"] == "U"
    l2 = lock_of(v, "L2")
    assert l2["owner"] == "B"
    assert all(w["task"] != "O" for w in l2["waiters"])
    # B no longer inherits O's (inherited) urgency
    assert eff_at(v)["B"] == 6


def test_most_urgent_waiter_wins_regardless_of_queue_order():
    v = replay(
        [T("O", 9), T("lo", 3), T("hi", 1)],
        [L("K")],
        [AC("O", "K"), AC("lo", "K"), AC("hi", "K"), RL("O", "K")],
    )
    assert lock_of(v, "K")["owner"] == "hi"


# ----------------------------------------------------------------- rejections
INVALID_CASES = [
    ("non-owner release", [T("A", 1), T("B", 2)], [L("M")],
     [AC("A", "M"), RL("B", "M")], 2),
    ("release free lock", [T("A", 1)], [L("M")], [RL("A", "M")], 1),
    ("cancel running task", [T("A", 1)], [L("M")],
     [AC("A", "M"), CN("A")], 2),
    ("duplicate wait same lock", [T("A", 1), T("B", 2)], [L("M")],
     [AC("A", "M"), AC("B", "M"), AC("B", "M")], 3),
    ("reentrant acquire", [T("A", 1)], [L("M")],
     [AC("A", "M"), AC("A", "M")], 2),
    ("unknown task", [T("A", 1)], [L("M")], [AC("X", "M")], 1),
    ("unknown lock", [T("A", 1)], [L("M")], [AC("A", "X")], 1),
    ("waiting cycle", [T("A", 5), T("B", 6)], [L("L1"), L("L2")],
     [AC("A", "L1"), AC("B", "L2"), AC("A", "L2"), AC("B", "L1")], 4),
]


@pytest.mark.parametrize("name,tasks,locks,events,failed", INVALID_CASES)
def test_invalid_events_rejected_with_location(name, tasks, locks, events, failed):
    with pytest.raises(AuditError) as exc:
        replay(tasks, locks, events)
    assert exc.value.index == failed - 1
    verdict = build_error(exc.value)
    assert verdict["status"] == "rejected"
    assert verdict["failedEvent"] == failed
    assert verdict["steps"] == []  # earlier successes cleared


def test_cycle_detected_through_indirection():
    # A holds L1 waits L2; B holds L2 waits L3; C holds L3 waits L1 -> cycle
    events = [
        AC("A", "L1"), AC("B", "L2"), AC("C", "L3"),
        AC("A", "L2"), AC("B", "L3"), AC("C", "L1"),
    ]
    with pytest.raises(AuditError) as exc:
        replay([T("A", 1), T("B", 2), T("C", 3)],
               [L("L1"), L("L2"), L("L3")], events)
    assert exc.value.index == 5


def test_input_limits():
    with pytest.raises(AuditError):
        replay([T(f"T{i}", i) for i in range(17)], [L("M")], [])
    with pytest.raises(AuditError):
        replay([T("A", 1)], [L(f"L{i}") for i in range(33)], [])
    with pytest.raises(AuditError):
        replay([T("A", 1)], [L("M")],
               [{"type": "noop", "task": "A"}])

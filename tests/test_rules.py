"""Rule tests for the priority-inheritance arbitration engine and store."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from engine import Engine  # noqa: E402
from store import ValidationError, VerdictStore, normalize  # noqa: E402


def run(tasks, locks, events):
    return Engine(tasks, locks).apply(events)


def eff(result, step_index):
    """Effective-priority map {taskId: prio} at the given step index."""
    snap = result["steps"][step_index]["snapshot"]
    return {t["id"]: t["effectivePriority"] for t in snap["tasks"]}


def final_locks(result):
    return {l["id"]: l for l in result["finalSnapshot"]["locks"]}


class TwoHopInheritanceTests(unittest.TestCase):
    # B(5) holds L2; A(8) holds L1; B waits L1; C(1) waits L2 => C -> B -> A.
    PRESET = dict(
        tasks=[{"id": 1, "priority": 8}, {"id": 2, "priority": 5}, {"id": 3, "priority": 1}],
        locks=[{"id": 1, "priority": 9}, {"id": 2, "priority": 9}],
        events=[
            {"type": "acquire", "taskId": 2, "lockId": 2},
            {"type": "acquire", "taskId": 1, "lockId": 1},
            {"type": "acquire", "taskId": 2, "lockId": 1},
            {"type": "acquire", "taskId": 3, "lockId": 2},
            {"type": "release", "taskId": 1, "lockId": 1},
            {"type": "release", "taskId": 2, "lockId": 1},
            {"type": "release", "taskId": 2, "lockId": 2},
        ],
    )

    def test_chain_propagates_urgency_two_hops(self):
        r = run(**self.PRESET)
        self.assertTrue(r["accepted"])
        # Step 4: C -> B -> A, both A and B inherit urgency 1.
        self.assertEqual(eff(r, 4), {1: 1, 2: 1, 3: 1})
        snap4 = r["steps"][4]["snapshot"]
        self.assertIn([3, 2, 1], snap4["chains"])
        self.assertIn([2, 1], snap4["chains"])
        by_id = {t["id"]: t for t in snap4["tasks"]}
        self.assertEqual(by_id[1]["inheritedFrom"], [2, 3])
        self.assertEqual(by_id[2]["inheritedFrom"], [3])

    def test_release_recomputes_and_chain_breaks(self):
        r = run(**self.PRESET)
        # Step 5: A hands L1 to B. B still holds L2 with C queued, so B keeps
        # inheriting urgency 1 but A falls back to its own base 8.
        self.assertEqual(eff(r, 5), {1: 8, 2: 1, 3: 1})
        locks5 = {l["id"]: l for l in r["steps"][5]["snapshot"]["locks"]}
        self.assertEqual(locks5[1]["holder"], 2)
        self.assertEqual(locks5[2]["holder"], 2)
        # Step 6: B releases L1 (no waiters); inheritance from C persists via L2.
        self.assertEqual(eff(r, 6), {1: 8, 2: 1, 3: 1})
        # Step 7: B releases L2 -> handed to C; graph empty, everyone at base.
        self.assertEqual(eff(r, 7), {1: 8, 2: 5, 3: 1})
        locks = {l["id"]: l for l in r["finalSnapshot"]["locks"]}
        self.assertEqual(locks[2]["holder"], 3)
        self.assertIsNone(locks[1]["holder"])


class FallbackTests(unittest.TestCase):
    def test_priority_falls_back_after_release(self):
        r = run(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 1}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "release", "taskId": 1, "lockId": 1},
                {"type": "release", "taskId": 2, "lockId": 1},
            ],
        )
        self.assertTrue(r["accepted"])
        self.assertEqual(eff(r, 2), {1: 1, 2: 1})       # boosted while holding
        self.assertEqual(eff(r, 3), {1: 8, 2: 1})       # handover: T1 back to base
        locks = {l["id"]: l for l in r["steps"][3]["snapshot"]["locks"]}
        self.assertEqual(locks[1]["holder"], 2)
        self.assertEqual(eff(r, 4), {1: 8, 2: 1})       # lock free, still base

    def test_set_priority_recomputes_inheritance(self):
        r = run(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 7}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "set-priority", "taskId": 2, "priority": 2},
            ],
        )
        self.assertEqual(eff(r, 2), {1: 7, 2: 7})
        self.assertEqual(eff(r, 3), {1: 2, 2: 2})


class TieHandoverTests(unittest.TestCase):
    def test_tie_broken_by_smallest_task_id(self):
        # Waiters arrive 3 then 2, both effective priority 2: id 2 wins.
        r = run(
            [{"id": 1, "priority": 4}, {"id": 2, "priority": 2}, {"id": 3, "priority": 2}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 3, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "release", "taskId": 1, "lockId": 1},
            ],
        )
        self.assertTrue(r["accepted"])
        locks = final_locks(r)
        self.assertEqual(locks[1]["holder"], 2)
        self.assertEqual(locks[1]["waiters"], [3])
        # Chosen waiter becomes running holder, not waiting.
        tasks = {t["id"]: t for t in r["finalSnapshot"]["tasks"]}
        self.assertIsNone(tasks[2]["waitingFor"])
        self.assertEqual(tasks[2]["holdingLocks"], [1])

    def test_highest_effective_not_just_base_wins(self):
        # T4 base 6 but inherits 1 from a nested urgent task must outrank
        # waiter T3 whose base is 3.
        # L2: holder 4, waiter 5(prio1); L1: holder 1, waiters 4(eff1), 3(eff3).
        r = run(
            [
                {"id": 1, "priority": 9},
                {"id": 3, "priority": 3}, {"id": 4, "priority": 6},
                {"id": 5, "priority": 1},
            ],
            [{"id": 1, "priority": 9}, {"id": 2, "priority": 9}],
            [
                {"type": "acquire", "taskId": 4, "lockId": 2},  # 4 holds L2
                {"type": "acquire", "taskId": 1, "lockId": 1},  # 1 holds L1
                {"type": "acquire", "taskId": 5, "lockId": 2},  # urgent 5 waits L2
                {"type": "acquire", "taskId": 4, "lockId": 1},  # 4(->eff1) waits L1
                {"type": "acquire", "taskId": 3, "lockId": 1},  # 3 (eff3) waits L1
                {"type": "release", "taskId": 1, "lockId": 1},  # 4 must win over 3
            ],
        )
        self.assertTrue(r["accepted"], r["error"])
        self.assertEqual(final_locks(r)[1]["holder"], 4)


class CancelTests(unittest.TestCase):
    def test_cancel_waiter_recomputes_inheritance(self):
        r = run(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 1}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "cancel", "taskId": 2},
            ],
        )
        self.assertTrue(r["accepted"])
        self.assertEqual(eff(r, 3), {1: 8, 2: 1})
        self.assertEqual(final_locks(r)[1]["waiters"], [])

    def test_cancel_running_task_is_invalid(self):
        r = run([{"id": 1, "priority": 8}], [], [{"type": "cancel", "taskId": 1}])
        self.assertFalse(r["accepted"])
        self.assertEqual(r["errorIndex"], 1)
        self.assertIsNone(r["finalSnapshot"])

    def test_cancel_running_holder_is_invalid(self):
        r = run(
            [{"id": 1, "priority": 8}], [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "cancel", "taskId": 1},
            ],
        )
        self.assertFalse(r["accepted"])
        self.assertEqual(r["errorIndex"], 2)


class InvalidEventTests(unittest.TestCase):
    def assert_rejected_at(self, result, index):
        self.assertFalse(result["accepted"])
        self.assertEqual(result["errorIndex"], index)
        self.assertIsNone(result["finalSnapshot"])
        self.assertFalse(result["steps"][index]["ok"])
        self.assertIsNone(result["steps"][index]["snapshot"])

    def test_release_by_non_owner(self):
        r = run(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 5}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "release", "taskId": 2, "lockId": 1},
                {"type": "set-priority", "taskId": 1, "priority": 1},
            ],
        )
        self.assert_rejected_at(r, 2)
        self.assertIn("非拥有者", r["error"])

    def test_release_free_lock(self):
        r = run([{"id": 1, "priority": 8}], [{"id": 1, "priority": 9}],
                [{"type": "release", "taskId": 1, "lockId": 1}])
        self.assert_rejected_at(r, 1)

    def test_duplicate_wait_same_lock(self):
        r = run(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 5}],
            [{"id": 1, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
            ],
        )
        self.assert_rejected_at(r, 3)
        self.assertIn("重复等待", r["error"])

    def test_non_reentrant_acquire(self):
        r = run([{"id": 1, "priority": 8}], [{"id": 1, "priority": 9}],
                [{"type": "acquire", "taskId": 1, "lockId": 1},
                 {"type": "acquire", "taskId": 1, "lockId": 1}])
        self.assert_rejected_at(r, 2)
        self.assertIn("不可重入", r["error"])

    def test_wait_cycle_is_rejected(self):
        # 1 holds L1, 2 holds L2 and waits L1; 1 waiting L2 would close a ring.
        r = run(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 5}],
            [{"id": 1, "priority": 9}, {"id": 2, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 2},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "acquire", "taskId": 1, "lockId": 2},
            ],
        )
        self.assert_rejected_at(r, 4)
        self.assertIn("环", r["error"])
        # Rolled back tentative queue insertion: L2 has no waiters.
        snap3 = r["steps"][3]["snapshot"]
        self.assertEqual({l["id"]: l for l in snap3["locks"]}[2]["waiters"], [])

    def test_unknown_task_and_lock(self):
        r = run([{"id": 1, "priority": 8}], [{"id": 1, "priority": 9}],
                [{"type": "acquire", "taskId": 9, "lockId": 1}])
        self.assert_rejected_at(r, 1)
        r = run([{"id": 1, "priority": 8}], [{"id": 1, "priority": 9}],
                [{"type": "acquire", "taskId": 1, "lockId": 9}])
        self.assert_rejected_at(r, 1)
        self.assertIn("不存在", r["error"])

    def test_unknown_event_type(self):
        r = run([{"id": 1, "priority": 8}], [], [{"type": "boost", "taskId": 1}])
        self.assert_rejected_at(r, 1)

    def test_blocked_task_cannot_acquire_another_lock(self):
        r = run(
            [{"id": 1, "priority": 8}, {"id": 2, "priority": 5}],
            [{"id": 1, "priority": 9}, {"id": 2, "priority": 9}],
            [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 2},
            ],
        )
        self.assert_rejected_at(r, 3)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "verdicts.json")
        self.store = VerdictStore(self.path)
        self.sub = normalize({
            "auditId": "A-1",
            "tasks": [{"id": 1, "priority": 8}, {"id": 2, "priority": 1}],
            "locks": [{"id": 1, "priority": 9}],
            "events": [
                {"type": "acquire", "taskId": 1, "lockId": 1},
                {"type": "acquire", "taskId": 2, "lockId": 1},
            ],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_freeze_replay_conflict(self):
        rec1, s1 = self.store.submit(self.sub)
        self.assertEqual(s1, 200)
        self.assertFalse(rec1["replayed"])
        accepted1 = rec1["verdict"]["accepted"]

        rec2, s2 = self.store.submit(normalize({**self.sub}))
        self.assertEqual(s2, 200)
        self.assertTrue(rec2["replayed"])
        self.assertEqual(rec2["inputHash"], rec1["inputHash"])
        self.assertEqual(rec2["verdict"]["accepted"], accepted1)

        different = normalize({**self.sub, "events": self.sub["events"] + [
            {"type": "release", "taskId": 1, "lockId": 1}]})
        rec3, s3 = self.store.submit(different)
        self.assertEqual(s3, 409)
        self.assertEqual(rec3["error"], "conflict")

        fetched = self.store.get("A-1")
        self.assertEqual(fetched["inputHash"], rec1["inputHash"])

    def test_persistence_across_instances(self):
        self.store.submit(self.sub)
        reopened = VerdictStore(self.path)
        rec, status = reopened.submit(normalize({**self.sub}))
        self.assertEqual(status, 200)
        self.assertTrue(rec["replayed"])

    def test_invalid_submission_is_still_frozen(self):
        bad = normalize({
            "auditId": "BAD", "tasks": [{"id": 1, "priority": 8}],
            "locks": [{"id": 1, "priority": 9}],
            "events": [{"type": "release", "taskId": 1, "lockId": 1}],
        })
        rec, status = self.store.submit(bad)
        self.assertEqual(status, 200)
        self.assertFalse(rec["verdict"]["accepted"])
        self.assertEqual(rec["verdict"]["errorIndex"], 1)

    def test_limits_and_shape(self):
        with self.assertRaises(ValidationError):
            normalize({"auditId": "x", "tasks": [{"id": i, "priority": 1} for i in range(17)]})
        with self.assertRaises(ValidationError):
            normalize({"auditId": "x",
                       "events": [{"type": "cancel", "taskId": 1}] * 129})
        with self.assertRaises(ValidationError):
            normalize({"auditId": "", "tasks": []})
        with self.assertRaises(ValidationError):
            normalize({"auditId": "x", "tasks": [{"id": 1, "priority": "no"}]})
        with self.assertRaises(ValidationError):
            normalize({"auditId": "x", "locks": [{"id": 1, "priority": 1},
                                                {"id": 1, "priority": 2}]})


if __name__ == "__main__":
    unittest.main()

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/absorption_guard.py"
spec = importlib.util.spec_from_file_location("guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def snapshot(parent="started", child="backlog", edges=True, day=1):
    return {"complete": True, "observed_at": "2026-09-%02dT00:00:00Z" % day, "issues": [
        {"id": "P-1", "title": "設計", "url": "https://example.com/P-1",
         "status_type": parent, "blocked_by": [], "labels": []},
        {"id": "C-1", "title": "実装", "url": "https://example.com/C-1",
         "status_type": child, "blocked_by": ["P-1"] if edges else [], "labels": []},
    ]}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state.json"
        self.sent = []

    def run_snapshot(self, value):
        return guard.run(value, self.state, self.sent.append)

    def test_initial_completed_issues_only_establish_baseline(self):
        self.assertEqual(self.run_snapshot(snapshot("completed")), [])
        self.assertEqual(self.run_snapshot(snapshot("completed", day=2)), [])

    def test_transition_notifies_once_even_after_queue_item_is_removed(self):
        self.run_snapshot(snapshot())
        self.assertEqual(len(self.run_snapshot(snapshot("completed", day=2))), 1)
        self.sent.clear()  # User consumed the Board notification.
        self.assertEqual(self.run_snapshot(snapshot("completed", day=3)), [])
        self.run_snapshot(snapshot(day=4))  # Reopen/recomplete does not nag.
        self.assertEqual(self.run_snapshot(snapshot("completed", day=5)), [])

    def test_edge_removed_on_completion_is_detected(self):
        self.run_snapshot(snapshot())
        self.run_snapshot(snapshot("completed", edges=False, day=2))
        self.assertEqual(self.sent[0]["pairs"], [("P-1", "C-1")])

    def test_edge_removed_before_completion_does_not_notify(self):
        self.run_snapshot(snapshot())
        self.run_snapshot(snapshot(edges=False, day=2))
        self.assertEqual(self.run_snapshot(snapshot("completed", edges=False, day=3)), [])

    def test_canceled_prerequisite_and_terminal_children_are_excluded(self):
        baseline = guard.plan(snapshot())
        for value in [snapshot("canceled"), snapshot("completed", "completed"),
                      snapshot("completed", "canceled")]:
            self.assertEqual(guard.notices(guard.plan(value, baseline)), [])

    def test_ng_on_either_side_is_silent(self):
        baseline = guard.plan(snapshot())
        for index in (0, 1):
            value = snapshot("completed")
            value["issues"][index]["labels"] = ["kobito:ng"]
            self.assertEqual(guard.notices(guard.plan(value, baseline)), [])

    def test_groups_children_in_one_notice(self):
        value = snapshot()
        extra = copy.deepcopy(value["issues"][1])
        extra.update(id="C-2", url="https://example.com/C-2")
        value["issues"].append(extra)
        self.run_snapshot(value)
        value["issues"][0]["status_type"] = "completed"
        value["observed_at"] = "2026-09-02T00:00:00Z"
        self.run_snapshot(value)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.sent[0]["pairs"]), 2)

    def test_failed_delivery_retries_with_same_dedupe_key(self):
        self.run_snapshot(snapshot())
        attempted = []

        def fail(notice):
            attempted.append(notice["key"])
            raise RuntimeError("board unavailable")

        with self.assertRaises(RuntimeError):
            guard.run(snapshot("completed", day=2), self.state, fail)
        self.run_snapshot(snapshot("completed", day=3))
        self.assertEqual(attempted, [self.sent[0]["key"]])

    def test_pending_notice_is_dropped_if_child_has_since_completed(self):
        self.run_snapshot(snapshot())
        def fail(notice):
            raise RuntimeError("board unavailable")
        with self.assertRaises(RuntimeError):
            guard.run(snapshot("completed", day=2), self.state, fail)
        self.assertEqual(self.run_snapshot(snapshot("completed", "completed", day=3)), [])

    def test_invalid_or_old_snapshot_does_not_advance_state(self):
        self.run_snapshot(snapshot(day=2))
        before = self.state.read_bytes()
        partial = snapshot("completed", day=3)
        partial["complete"] = False
        missing = snapshot("completed", day=3)
        missing["issues"] = missing["issues"][1:]
        for value in [partial, missing, snapshot(day=1)]:
            with self.assertRaises(ValueError):
                self.run_snapshot(value)
            self.assertEqual(self.state.read_bytes(), before)

    def test_dry_run_has_no_files_or_notification_side_effects(self):
        result = subprocess.run([
            sys.executable, str(SCRIPT), "--state", str(self.state), "--dry-run",
        ], input=json.dumps(snapshot()), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [])
        self.assertEqual(list(self.state.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

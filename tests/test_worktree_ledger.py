import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/worktree_ledger.py"
spec = importlib.util.spec_from_file_location("ledger", SCRIPT)
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def commit(cwd, name):
    (Path(cwd) / name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", name)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root = base / "repos"
        self.root.mkdir()
        remote = base / "remote.git"
        git(base, "init", "-q", "--bare", str(remote))
        self.repo = self.root / "app"
        git(base, "init", "-q", "-b", "main", str(self.repo))
        commit(self.repo, "init")
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-q", "origin", "main")

    def worktree(self, name, branch):
        path = self.root / name
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
        return path

    def ledger(self, issues=None, prs=None):
        entries = ledger.collect(self.root, {"FEZ"})
        by_branch = {e["branch"]: e for e in entries}
        return {b: ledger.classify(e, issues or {}, prs or {})[0] for b, e in by_branch.items()}, by_branch

    def test_merged_clean_worktree_of_done_issue_can_be_deleted(self):
        path = self.worktree("app-fez1", "agent/fez-1-done")
        commit(path, "work")
        git(path, "push", "-q", "origin", "agent/fez-1-done")
        result, _ = self.ledger({"FEZ-1": "completed"}, {"app:agent/fez-1-done": "merged"})
        self.assertEqual(result["agent/fez-1-done"], ledger.DELETE)

    def test_uncommitted_change_needs_review_even_when_issue_is_done(self):
        path = self.worktree("app-fez245", "codex/fez-245-tile")
        (path / "scratch.txt").write_text("unsaved")
        result, entries = self.ledger({"FEZ-245": "completed"}, {"app:codex/fez-245-tile": "merged"})
        self.assertEqual(result["codex/fez-245-tile"], ledger.REVIEW)
        self.assertEqual(entries["codex/fez-245-tile"]["uncommitted"], 1)

    def test_unpushed_commit_needs_review(self):
        path = self.worktree("app-fez2", "agent/fez-2-local")
        commit(path, "local-only")
        result, entries = self.ledger({"FEZ-2": "completed"}, {"app:agent/fez-2-local": "merged"})
        self.assertEqual(result["agent/fez-2-local"], ledger.REVIEW)
        self.assertEqual(entries["agent/fez-2-local"]["unpushed"], 1)

    def test_open_issue_is_kept(self):
        self.worktree("app-fez3", "agent/fez-3-open")
        result, _ = self.ledger({"FEZ-3": "started"})
        self.assertEqual(result["agent/fez-3-open"], ledger.KEEP)

    def test_done_issue_without_merged_pr_needs_review(self):
        self.worktree("app-fez4", "agent/fez-4-closed")
        self.worktree("app-fez5", "agent/fez-5-nopr")
        result, _ = self.ledger({"FEZ-4": "completed", "FEZ-5": "canceled"},
                                {"app:agent/fez-4-closed": "closed"})
        self.assertEqual(result["agent/fez-4-closed"], ledger.REVIEW)
        self.assertEqual(result["agent/fez-5-nopr"], ledger.REVIEW)

    def test_name_without_issue_is_unknown_and_unknown_state_is_not_done(self):
        self.worktree("app-misc", "agent/slack-notes")
        self.worktree("app-fez6", "agent/fez-6-x")
        result, _ = self.ledger({})
        self.assertEqual(result["agent/slack-notes"], ledger.UNKNOWN)
        self.assertEqual(result["agent/fez-6-x"], ledger.UNKNOWN)

    def test_loose_issue_branches_are_listed_but_other_branches_are_not(self):
        git(self.repo, "branch", "fez-7-loose")
        git(self.repo, "branch", "experiment")
        _, entries = self.ledger()
        self.assertEqual(entries["fez-7-loose"]["kind"], "branch")
        self.assertNotIn("experiment", entries)
        self.assertNotIn("main", entries)

    def test_issue_id_ignores_other_prefixes_and_normalizes(self):
        self.assertEqual(ledger.issue_id("codex/fez-245-tile", {"FEZ"}), "FEZ-245")
        self.assertEqual(ledger.issue_id("TsumoAI-fez0217", {"FEZ"}), "FEZ-217")
        self.assertIsNone(ledger.issue_id("node-24-upgrade", {"FEZ"}))

    def test_collection_does_not_modify_repositories(self):
        path = self.worktree("app-fez8", "agent/fez-8")
        (path / "scratch.txt").write_text("unsaved")
        before = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        ledger.collect(self.root, {"FEZ"})
        after = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                               capture_output=True, text=True).stdout
        self.assertEqual(before, after)
        self.assertTrue((path / "scratch.txt").exists())

    def test_render_counts_every_category(self):
        self.worktree("app-fez9", "agent/fez-9")
        text, counts = ledger.render(ledger.collect(self.root, {"FEZ"}), {"FEZ-9": "started"}, {})
        self.assertEqual(counts[ledger.KEEP], 1)
        self.assertIn("| そのまま |", text)


if __name__ == "__main__":
    unittest.main()

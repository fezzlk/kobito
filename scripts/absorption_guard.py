#!/usr/bin/env python3
"""Notify about unfinished dependents; never infer whether code fixes an issue."""

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


TERMINAL = {"completed", "canceled"}
STATES = TERMINAL | {"triage", "backlog", "unstarted", "started"}


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observed_at must have a timezone")
    return parsed


def validate(snapshot):
    if snapshot.get("complete") is not True:
        raise ValueError("partial snapshots must not advance the baseline")
    timestamp(snapshot["observed_at"])
    issues = {}
    for issue in snapshot["issues"]:
        key = issue["id"]
        if not isinstance(key, str) or not key or key in issues:
            raise ValueError("issue IDs must be unique nonempty strings")
        if issue["status_type"] not in STATES:
            raise ValueError("unknown status_type")
        if not isinstance(issue["title"], str) or not issue["url"].startswith("https://"):
            raise ValueError("title and HTTPS issue URL are required")
        for field in ("blocked_by", "labels"):
            if not isinstance(issue[field], list) or not all(
                isinstance(value, str) for value in issue[field]
            ):
                raise ValueError(field + " must be an array of strings")
        issues[key] = issue
    for issue in issues.values():
        if any(key not in issues for key in issue["blocked_by"]):
            raise ValueError("every referenced prerequisite must be fetched")
    return issues


def plan(snapshot, previous=None):
    """Return next durable state and pending pairs, without side effects."""
    issues = validate(snapshot)
    previous = previous or {}
    if previous and timestamp(snapshot["observed_at"]) < timestamp(previous["observed_at"]):
        raise ValueError("snapshot is older than the stored baseline")
    old = previous.get("issues", {})
    sent = {tuple(pair) for pair in previous.get("sent", [])}
    pending = {tuple(pair) for pair in previous.get("pending", [])}
    completed = {
        key for key, issue in issues.items()
        if key in old and old[key]["status_type"] not in TERMINAL
        and issue["status_type"] == "completed"
    }
    for key, issue in issues.items():
        # A tracker may remove an edge when the prerequisite is completed.
        prerequisites = set(issue["blocked_by"]) | set(old.get(key, {}).get("blocked_by", []))
        pending.update((parent, key) for parent in prerequisites & completed)
    pending = {
        (parent, child) for parent, child in pending - sent
        if parent in issues and child in issues
        and issues[parent]["status_type"] == "completed"
        and issues[child]["status_type"] not in TERMINAL
        and "kobito:ng" not in issues[parent]["labels"]
        and "kobito:ng" not in issues[child]["labels"]
    }
    return {
        "version": 1, "observed_at": snapshot["observed_at"],
        "issues": issues, "sent": sorted(sent), "pending": sorted(pending),
    }


def notices(state):
    grouped = {}
    for parent, child in state["pending"]:
        grouped.setdefault(parent, []).append(child)
    result = []
    for parent, children in sorted(grouped.items()):
        pairs = [(parent, child) for child in sorted(children)]
        digest = hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:24]
        prerequisite = state["issues"][parent]
        body = ["前提Issueが完了しました。次の関連Issueは未完了です。",
                "", parent + ": " + prerequisite["title"], prerequisite["url"], ""]
        for child in sorted(children):
            issue = state["issues"][child]
            body.append("- " + child + ": " + issue["title"] + " " + issue["url"])
        body.extend(["", "取りこぼし・未実装との自動判定ではありません。",
                     "個別実装、対応済み、保留のどれかを各Issueで確認してください。",
                     "この通知は着手承認を求めるものではなく、Issueを自動更新しません。"])
        result.append({"key": "absorption-guard-" + digest, "pairs": pairs,
                       "title": "前提完了後の未完了Issue: " + parent,
                       "body": "\n".join(body)})
    return result


def load_state(path):
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value.get("version") != 1:
        raise ValueError("unsupported state version; do not discard notification history")
    return value


def save_state(path, state):
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def deliver(board_cli, notice):
    # Links stay in the body: older Board versions render approval buttons
    # for any related-link, even for informational notifications.
    subprocess.run([
        sys.executable, str(board_cli), "add", "--direction", "agent-to-user",
        "--from", "kobito", "--type", "fyi", "--dedupe-key", notice["key"],
        "--title", notice["title"], "--body", notice["body"],
    ], check=True, timeout=30, stdout=subprocess.PIPE, text=True)


def run(snapshot, state_path, sender):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # All workers on this host share this lock and ledger, including worktrees.
    with state_path.with_suffix(state_path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = plan(snapshot, load_state(state_path))
        save_state(state_path, state)  # Persist the outbox before delivery.
        delivered = []
        for notice in notices(state):
            sender(notice)  # Failure leaves pending pairs for the next pass.
            pairs = set(notice["pairs"])
            state["sent"] = sorted({tuple(pair) for pair in state["sent"]} | pairs)
            state["pending"] = sorted({tuple(pair) for pair in state["pending"]} - pairs)
            save_state(state_path, state)
            delivered.append(notice["key"])
        return delivered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default="-", help="complete JSON snapshot, or stdin")
    parser.add_argument("--state", required=True, type=Path, help="shared durable ledger path")
    parser.add_argument("--board-cli", type=Path,
                        default=Path.home() / "repos/human-agent-board/board.py")
    parser.add_argument("--dry-run", action="store_true", help="no writes or notifications")
    args = parser.parse_args()
    snapshot = json.load(sys.stdin) if args.snapshot == "-" else json.loads(Path(args.snapshot).read_text())
    if args.dry_run:
        print(json.dumps(notices(plan(snapshot, load_state(args.state))), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"delivered": run(snapshot, args.state, lambda notice: deliver(args.board_cli, notice))}))


if __name__ == "__main__":
    main()

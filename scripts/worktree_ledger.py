#!/usr/bin/env python3
"""Read-only ledger of leftover git worktrees and agent branches.

Collects facts with read-only git commands, joins them with issue and PR states
supplied as JSON (this script never calls Linear or GitHub), and prints a
Markdown table that sorts each item into one of:

- 消してよい: issue done, PR merged, nothing uncommitted or unpushed
- 見るべき:   uncommitted or unpushed work, or the facts do not line up
- そのまま:   issue not finished yet
- 判定不可:   no issue identifier in the branch or directory name

It never deletes, checks out, stashes, fetches, or otherwise writes to a repo.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

DELETE, REVIEW, KEEP, UNKNOWN = "消してよい", "見るべき", "そのまま", "判定不可"
ORDER = [REVIEW, DELETE, KEEP, UNKNOWN]
DONE = {"completed", "canceled"}
ISSUE_RE = re.compile(r"(?<![a-z])([a-z]{2,10})[-_]?(\d{1,5})(?!\d)", re.IGNORECASE)


def git(repo, *args):
    """Run a read-only git command; return stdout or None on failure."""
    result = subprocess.run(
        ["git", "-C", str(repo), "--no-optional-locks", *args],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else None


def issue_id(text, prefixes):
    for match in ISSUE_RE.finditer(text or ""):
        prefix = match.group(1).upper()
        if prefix in prefixes:
            return "%s-%d" % (prefix, int(match.group(2)))
    return None


def parse_worktrees(porcelain):
    items, current = [], {}
    for line in porcelain.splitlines():
        if not line:
            if current:
                items.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value or True
    if current:
        items.append(current)
    return items


def facts_for(repo, ref):
    """Uncommitted and unpushed counts plus last commit date for a ref."""
    unpushed = git(repo, "rev-list", "--count", ref, "--not", "--remotes")
    last = git(repo, "log", "-1", "--format=%cs", ref)
    return {
        "unpushed": int(unpushed.strip()) if unpushed else None,
        "last_commit": last.strip() if last else None,
    }


def collect(root, prefixes):
    """Gather linked worktrees and loose agent branches of every repo under root."""
    entries = []
    for repo in sorted(p for p in Path(root).iterdir() if (p / ".git").is_dir()):
        porcelain = git(repo, "worktree", "list", "--porcelain")
        if porcelain is None:
            continue
        worktrees = parse_worktrees(porcelain)
        checked_out = set()
        for wt in worktrees[1:]:  # the first entry is the main checkout
            path = Path(wt["worktree"])
            branch = wt.get("branch", "").replace("refs/heads/", "") or None
            if branch:
                checked_out.add(branch)
            entry = {"repo": repo.name, "kind": "worktree", "path": str(path), "branch": branch,
                     "issue": issue_id(branch, prefixes) or issue_id(path.name, prefixes)}
            if wt.get("prunable") or not path.exists():
                entry.update(missing=True, uncommitted=None, unpushed=None, last_commit=None)
            else:
                status = git(path, "status", "--porcelain")
                entry["uncommitted"] = len(status.splitlines()) if status is not None else None
                entry.update(facts_for(path, "HEAD"))
            entries.append(entry)
        main_branch = worktrees[0].get("branch", "").replace("refs/heads/", "") if worktrees else ""
        branches = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads") or ""
        for branch in branches.split():
            if branch in checked_out or branch == main_branch:
                continue
            issue = issue_id(branch, prefixes)
            if not issue:
                continue  # only agent-style branches that name an issue
            entry = {"repo": repo.name, "kind": "branch", "path": None, "branch": branch,
                     "issue": issue, "uncommitted": 0}
            entry.update(facts_for(repo, "refs/heads/" + branch))
            entries.append(entry)
    return entries


def classify(entry, issues, prs):
    """Return (category, reason). Unsaved work always wins over everything else."""
    if entry.get("missing"):
        return REVIEW, "worktreeのディレクトリが無い(git worktree pruneの候補)"
    if entry["uncommitted"] is None or entry["unpushed"] is None:
        return REVIEW, "gitの状態を読めなかった"
    if entry["uncommitted"]:
        return REVIEW, "未コミットの変更が%d件" % entry["uncommitted"]
    if entry["unpushed"]:
        return REVIEW, "どのリモートにも無いコミットが%d件" % entry["unpushed"]
    issue = entry["issue"]
    if not issue:
        return UNKNOWN, "名前からissueを特定できない"
    state = issues.get(issue)
    if state is None:
        return UNKNOWN, "%sの状態が入力に無い" % issue
    if state not in DONE:
        return KEEP, "%sは未完了(%s)" % (issue, state)
    pr = prs.get("%s:%s" % (entry["repo"], entry["branch"])) if entry["branch"] else None
    if pr == "merged":
        return DELETE, "%sは%s、PRはマージ済み" % (issue, state)
    if pr is None:
        return REVIEW, "%sは%sだがPRが無い(調査用か、別ブランチでマージ)" % (issue, state)
    return REVIEW, "%sは%sだがPRは%s" % (issue, state, pr)


def render(entries, issues, prs):
    rows = []
    for entry in entries:
        category, reason = classify(entry, issues, prs)
        rows.append((ORDER.index(category), category, reason, entry))
    rows.sort(key=lambda r: (r[0], r[3]["repo"], r[3]["branch"] or "", r[3]["path"] or ""))
    counts = {c: sum(1 for r in rows if r[1] == c) for c in ORDER}
    lines = ["# Worktree残骸台帳", "",
             "読み取り専用の集計です。削除・checkout・stashは行っていません。", "",
             " / ".join("%s %d件" % (c, counts[c]) for c in ORDER), "",
             "| 区分 | 理由 | リポジトリ | 種類 | ブランチ | 場所 | 未コミット | 未push | 最終コミット |",
             "| -- | -- | -- | -- | -- | -- | --: | --: | -- |"]
    for _, category, reason, e in rows:
        cells = [category, reason, e["repo"], e["kind"], e["branch"] or "(detached)",
                 Path(e["path"]).name if e["path"] else "-",
                 "-" if e["uncommitted"] is None else str(e["uncommitted"]),
                 "-" if e["unpushed"] is None else str(e["unpushed"]),
                 e["last_commit"] or "-"]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines) + "\n", counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path.home() / "repos"))
    parser.add_argument("--prefix", action="append", default=None,
                        help="issue key prefix, e.g. FEZ (repeatable; default FEZ)")
    parser.add_argument("--states", help='JSON: {"issues": {"FEZ-1": "completed"}, '
                        '"prs": {"repo:branch": "merged|open|closed"}}')
    parser.add_argument("--collect-only", action="store_true",
                        help="print collected facts as JSON (to look up issue and PR states)")
    args = parser.parse_args(argv)
    prefixes = {p.upper() for p in (args.prefix or ["FEZ"])}
    entries = collect(args.root, prefixes)
    if args.collect_only:
        json.dump(entries, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return 0
    states = json.loads(Path(args.states).read_text()) if args.states else {}
    text, _ = render(entries, states.get("issues", {}), states.get("prs", {}))
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

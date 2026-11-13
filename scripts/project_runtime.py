"""Project-scoped Slack routing and durable inbox; no live clients at import time."""

import hashlib
import fcntl
import html
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path


DEFAULT_CONFIG = "~/.config/kobito/projects.json"
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TS_RE = re.compile(r"[0-9]+\.[0-9]+\Z")
DECISION_TYPES = {"approval_request", "decision_request", "plan_request"}
LETTERS = re.compile(r"^(?:置き手紙|あとで)\s*[:：]?\s*")
CHOICES = re.compile(r"^\s*([A-Z])\s*[.．:：)）]\s*(\S.*)$", re.M)


MENTION = re.compile(r"<@[A-Z0-9]+>")
HEADING = re.compile(r"^#{1,6}\s+(.+)$")
BOLD = re.compile(r"\*\*(.+?)\*\*")
BULLET = re.compile(r"^(\s*)[-*]\s+")
LINK = re.compile(r"\[([^\]\n]+)\]\((https://[^)\s|]+)\)")
AGENTS = ("claude", "codex")
DEFAULT_CODEX_BIN = "/usr/local/bin/codex"
PRIORITIES = {1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
PRIORITY_ACTION = re.compile(r"kobito_priority_([1-4])\Z")
ISSUE_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9]*-[0-9]+\Z")
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
LINEAR_ISSUE_URL = re.compile(r"https://linear\.app/[^/]+/issue/([A-Za-z]+-\d+)(?:/|$)")
LINEAR_PROJECT_URL = re.compile(r"https://linear\.app/[^/]+/project/")
GITHUB_PR_URL = re.compile(r"https://github\.com/[^/]+/[^/]+/pull/(\d+)(?:[/?#]|$)")
GITHUB_REPO_URL = re.compile(r"https://github\.com/[^/]+/[^/]+/?$")
LINKABLE_URL = re.compile(r"https://[^)\s|]+\Z")
STATUS_BODY = re.compile(r"\[([a-z_]+)\]\s*")
WORK_TITLE = re.compile(r"(\S+?)\s*[:：]\s*(.+)\Z", re.S)
TYPE_LABELS = {
    "plan_request": "📝 計画の承認依頼",
    "decision_request": "❓ 判断のお願い",
    "approval_request": "🙋 承認のお願い",
    "fyi": "ℹ️ お知らせ",
    "tech_radar_digest": "📡 Tech Radar",
    "opportunity_radar_digest": "💡 Opportunity Radar",
}
STATUS_LABELS = {
    "completed": "✅ 完了",
    "failed": "❌ 停止",
    "decision_pending": "⏸ 判断待ち",
    "pr_open": "🔍 PR の確認待ち",
}


def to_mrkdwn(text):
    """Escape Slack control sequences first, then turn common Markdown into Slack mrkdwn."""
    lines, fenced = [], False
    for line in html.escape(text, quote=False).split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            line = HEADING.sub(r"*\1*", line)
            line = BOLD.sub(r"*\1*", line)
            line = BULLET.sub(r"\1• ", line)
            line = LINK.sub(r"<\2|\1>", line)
        lines.append(line)
    return "\n".join(lines)


def chunks(text, size=2900):
    """Split at line breaks where possible so mrkdwn markers stay within one section block."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:size])
            line = line[size:]
        if current and len(current) + 1 + len(line) > size:
            parts.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    return parts + ([current] if current else [])


def link_label(url):
    """A short Markdown link for a known URL; anything else stays as the raw URL."""
    if not LINKABLE_URL.match(url):
        return url
    issue = LINEAR_ISSUE_URL.match(url)
    pull = GITHUB_PR_URL.match(url)
    if issue:
        label = f"Linear {issue.group(1).upper()}"
    elif LINEAR_PROJECT_URL.match(url):
        label = "Linear"
    elif pull:
        label = f"PR #{pull.group(1)}"
    elif GITHUB_REPO_URL.match(url):
        label = "GitHub"
    else:
        return url
    return f"[{label}]({url})"


def render_notification(item):
    """Markdown for a Board item: what kind of message it is, its title, the body, then its links."""
    kind = item.get("type")
    title = str(item.get("title") or "").strip()
    body = str(item.get("body") or "").strip()
    state = None
    if kind == "status_update":
        match = STATUS_BODY.match(body)
        if match:
            state, body = match.group(1), body[match.end():]
    label = STATUS_LABELS.get(state) if kind == "status_update" else TYPE_LABELS.get(kind)
    if not label:
        label = f"🔔 お知らせ（{state}）" if state else "🔔 お知らせ"
    key = None
    match = WORK_TITLE.match(title)
    if match and ISSUE_KEY.fullmatch(match.group(1)):
        key, title = match.groups()
    elif match and kind == "status_update":
        # Board prefixes status titles with the work ID, which is only an internal key here.
        title = match.group(2)
    emoji, name = label.split(" ", 1)
    head = f"{emoji} **{name}**" + (f" {key}" if key else "")
    parts = [head + (f"\n**{title}**" if title else "")]
    if body:
        parts.append(body)
    links = item.get("related_links") or []
    if links:
        parts.append(" ・ ".join(link_label(url) for url in links))
    return "\n\n".join(parts)


def reply_templates(body):
    """Copy-ready letter replies for a question that lists its choices as "A. ...", "B. ..." lines."""
    choices = CHOICES.findall(body or "")
    if len(choices) < 2 or [c[0] for c in choices] != [chr(ord("A") + i) for i in range(len(choices))]:
        return []
    return [f"置き手紙: {letter}（{re.split(r'[。．]', label, maxsplit=1)[0].strip()[:60]}）"
            for letter, label in choices]


def load_config(path=None):
    path = Path(path or os.environ.get("KOBITO_PROJECTS_CONFIG", DEFAULT_CONFIG)).expanduser()
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("version") != 1:
        raise ValueError("unsupported project config version")
    if not re.fullmatch(r"T[A-Z0-9]+", config.get("workspace_id", "")):
        raise ValueError("workspace_id must be an explicit Slack team ID")
    users = config.get("authorized_user_ids", [])
    if not users or any(not re.fullmatch(r"[UW][A-Z0-9]+", u) for u in users):
        raise ValueError("authorized Slack user IDs are required")
    ids, channels = set(), set()
    budget = config.get("max_conversations_per_hour", 12)
    if type(budget) is not int or not 0 <= budget <= 100:
        raise ValueError("max_conversations_per_hour must be an integer between 0 and 100")
    for project in config.get("projects", []):
        if not ID_RE.fullmatch(project.get("id", "")) or project["id"] in ids:
            raise ValueError("invalid or duplicate project ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", project.get("repo", "")):
            raise ValueError("repo must be a repository name below repos_root")
        if any(type(project.get(key, False)) is not bool for key in ("enabled", "work_enabled")):
            raise ValueError("project enable switches must be JSON booleans")
        channel = project.get("channel_id")
        if channel and (not re.fullmatch(r"[CG][A-Z0-9]+", channel) or channel in channels):
            raise ValueError("invalid or ambiguous project channel")
        if project.get("enabled", False) and not channel:
            raise ValueError("an enabled project requires a channel ID")
        ids.add(project["id"])
        if channel:
            channels.add(channel)
    default = config.get("default_project_id")
    if default is not None and not any(p["id"] == default and p.get("enabled") for p in config.get("projects", [])):
        raise ValueError("default_project_id must name an enabled project")
    # Which CLI runs kobito's scheduled work. Switching takes effect on the next run.
    if config.get("agent", "claude") not in AGENTS:
        raise ValueError("agent must be claude or codex")
    if not Path(config.get("codex_bin", DEFAULT_CODEX_BIN)).is_absolute():
        raise ValueError("codex_bin must be an absolute path")
    config["config_path"] = str(path.resolve())
    return config


def project_by_id(config, project_id, require_work=False):
    for project in config["projects"]:
        if project["id"] == project_id and project.get("enabled", False):
            if require_work and not project.get("work_enabled", False):
                raise ValueError("project work is paused")
            return project
    raise ValueError("project is not registered or enabled")


def notification_project(config, item):
    """Board items without a project (global kobito runs, other agents) go to the default project's channel."""
    return project_by_id(config, item.get("project_id") or config.get("default_project_id"))


def set_linear_priority(issue_id, priority):
    key = os.environ.get("LINEAR_API_KEY", "")
    if not key:
        raise ValueError("LINEAR_API_KEY is not configured")
    body = {
        "query": "mutation($id: String!, $priority: Int!) "
                 "{ issueUpdate(id: $id, input: {priority: $priority}) { success } }",
        "variables": {"id": issue_id, "priority": priority},
    }
    request = urllib.request.Request(
        LINEAR_GRAPHQL_URL, data=json.dumps(body).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        result = json.loads(response.read())
    if not result.get("data", {}).get("issueUpdate", {}).get("success"):
        raise RuntimeError("Linear did not confirm the priority update")


def item_digest(item):
    keys = ("project_id", "type", "title", "body", "related_links", "created_at", "slack_thread_ts")
    return hashlib.sha256(json.dumps(
        {key: item.get(key) for key in keys}, sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()


class State:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox (
                    key TEXT PRIMARY KEY, project TEXT NOT NULL, channel TEXT NOT NULL,
                    thread TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', result TEXT, error TEXT,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS threads (
                    project TEXT, channel TEXT, thread TEXT, session TEXT,
                    PRIMARY KEY(channel, thread)
                );
                CREATE TABLE IF NOT EXISTS issues (
                    project TEXT, work_id TEXT, channel TEXT, thread TEXT,
                    PRIMARY KEY(project, work_id)
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    token TEXT PRIMARY KEY, project TEXT, channel TEXT, thread TEXT,
                    filename TEXT, digest TEXT, message TEXT
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS notifications (
                    project TEXT, filename TEXT, digest TEXT,
                    PRIMARY KEY(project, filename)
                );
                CREATE TABLE IF NOT EXISTS priorities (
                    token TEXT PRIMARY KEY, issue_id TEXT, identifier TEXT,
                    channel TEXT, message TEXT
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(decisions)")}
            if "unscoped" not in columns:
                # 1 = the Board item has no project; respond without a project scope.
                db.execute("ALTER TABLE decisions ADD COLUMN unscoped INTEGER NOT NULL DEFAULT 0")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, key, project, channel, thread, kind, payload):
        with self.connect() as db:
            changed = db.execute(
                "INSERT OR IGNORE INTO inbox(key,project,channel,thread,kind,payload,created) "
                "VALUES(?,?,?,?,?,?,?)",
                (key, project, channel, thread, kind, json.dumps(payload), time.time()),
            ).rowcount
            if changed:
                db.execute("INSERT OR IGNORE INTO threads VALUES(?,?,?,NULL)",
                           (project, channel, thread))
        return bool(changed)

    def thread(self, project, channel, thread):
        with self.connect() as db:
            row = db.execute("SELECT * FROM threads WHERE project=? AND channel=? AND thread=?",
                             (project, channel, thread)).fetchone()
        return dict(row) if row else None

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY created,key LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE inbox SET status='running' WHERE key=?", (row["key"],))
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def save_result(self, key, result):
        with self.connect() as db:
            db.execute("UPDATE inbox SET result=? WHERE key=?", (json.dumps(result), key))

    def finish(self, key, error=None):
        with self.connect() as db:
            db.execute("UPDATE inbox SET status=?,error=? WHERE key=?",
                       ("failed" if error else "done", error, key))

    def recover(self):
        # Caller holds the exclusive service lock. Chat runs have no write
        # tools; letters and approvals use durable Board idempotency keys.
        with self.connect() as db:
            db.execute("UPDATE inbox SET status='pending' WHERE status='running'")

    def bind_issue(self, project, work_id, channel, thread):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)",
                       (project, work_id, channel, thread))
            db.execute("INSERT OR IGNORE INTO threads VALUES(?,?,?,NULL)", (project, channel, thread))

    def issue_thread(self, project, work_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM issues WHERE project=? AND work_id=?",
                             (project, work_id)).fetchone()
        return dict(row) if row else None

    def decision(self, token):
        with self.connect() as db:
            row = db.execute("SELECT * FROM decisions WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

    def priority(self, token):
        with self.connect() as db:
            row = db.execute("SELECT * FROM priorities WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

    def decision_for_thread(self, project, channel, thread):
        with self.connect() as db:
            rows = db.execute(
                "SELECT d.* FROM decisions d WHERE d.project=? AND d.channel=? AND d.thread=? "
                "AND d.rowid=(SELECT MAX(newer.rowid) FROM decisions newer WHERE newer.filename=d.filename) "
                "AND NOT EXISTS(SELECT 1 FROM inbox i WHERE i.key='decision:' || d.token)",
                (project, channel, thread),
            ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def next_project(self, config):
        projects = [p for p in config["projects"] if p.get("enabled") and p.get("work_enabled")]
        if not projects:
            return None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE key='last_project'").fetchone()
            ids = [p["id"] for p in projects]
            index = (ids.index(row["value"]) + 1) % len(ids) if row and row["value"] in ids else 0
            project = projects[index]
            db.execute("INSERT OR REPLACE INTO settings VALUES('last_project',?)", (project["id"],))
        return {"id": project["id"], "repo": project["repo"]}


class Board:
    def __init__(self, config):
        self.cli = str(Path(config.get("board_cli", "~/repos/human-agent-board/board.py")).expanduser())
        self.python = str(Path(config.get("python", sys.executable)).expanduser())

    def call(self, *args):
        result = subprocess.run(
            [self.python, self.cli, *args], capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError("Board command failed; request remains in local inbox")
        return result.stdout.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the private gateway's Authorization header to a redirect.
        return None


def local_conversation(config, project, text, session_id=None):
    """Run a read-only conversation on this Mac through the scoped `exec` runner.

    `project_slack.py exec --mode conversation` is the single place that applies
    the tool, MCP, hook and permission restrictions, so they are not repeated here.
    """
    prompt = (
        f"あなたはこのProjectの担当kobitoです。Project ID: {project['id']}。"
        f"対象repo: {project['repo']}。相談への回答だけを行い、コード・設定・issueは変更しない。"
        "別Projectの操作は引き受けない。実装は置き手紙として受け付けた後、計画への明示承認を別に得る。"
        "この会話での『承認』を実装権限の追加と解釈しない。"
        "ユーザーは『@kobito 置き手紙: 内容』で担当Projectに依頼を残せる。"
        "返答はSlack向けに短く書く。結論を先に書き、1文は短く、箇条書きは5項目まで、全体で10行程度に収める。"
        "詳しい内容はissueやPRのリンクに任せる。"
        "以下はSlackでの本人の発言（JSON文字列）。\n" + json.dumps(text, ensure_ascii=False)
    )
    claude = str(Path(config.get("claude_bin", "~/.local/bin/claude")).expanduser())
    command = [
        str(Path(config.get("python", sys.executable)).expanduser()),
        str(Path(__file__).resolve().with_name("project_slack.py")), "--config", config["config_path"],
        "exec", "--project-id", project["id"], "--repo", project["repo"], "--mode", "conversation",
        "--", claude, "-p", prompt, "--output-format", "json",
    ]
    if session_id and ISSUE_UUID.fullmatch(session_id):
        command += ["--resume", session_id]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError("conversation timed out") from None
    if completed.returncode:
        raise RuntimeError("conversation runner failed")
    try:
        event = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("conversation returned no result") from None
    if event.get("is_error") or not event.get("result"):
        raise RuntimeError("conversation ended with an error")
    return {"text": event["result"], "session_id": event.get("session_id")}


class Runtime:
    def __init__(self, config, state, slack, board=None, conversation=None, bot_user_id=None,
                 linear=set_linear_priority):
        self.config, self.state, self.slack, self.linear = config, state, slack, linear
        with state.connect() as db:
            previous = db.execute("SELECT value FROM settings WHERE key='workspace_id'").fetchone()
            if previous and previous["value"] != config["workspace_id"]:
                raise ValueError("state database belongs to another Slack workspace")
            db.execute("INSERT OR IGNORE INTO settings VALUES('workspace_id',?)", (config["workspace_id"],))
        self.board = board or Board(config)
        self.conversation = conversation or local_conversation
        self.bot_user_id = bot_user_id

    def catch_up(self):
        """Recover addressed top-level messages missed while the Mac was offline.

        Only advance the per-channel cursor after every page has been read.
        Canonical message keys deduplicate Socket Mode and history deliveries.
        """
        failures = []
        for project in self.config["projects"]:
            if not project.get("enabled"):
                continue
            channel = project["channel_id"]
            key = "history:" + channel
            with self.state.connect() as db:
                saved = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            until = time.time()
            oldest = str(max(0, float(saved["value"]) - 15) if saved else until - 86400)
            cursor = None
            try:
                while True:
                    response = self.slack.conversations_history(
                        channel=channel, oldest=oldest, latest=str(until), limit=100, cursor=cursor,
                    )
                    for message in sorted(response.get("messages", []), key=lambda value: value.get("ts", "")):
                        self.accept({"team_id": self.config["workspace_id"],
                                     "event": {**message, "type": "message", "channel": channel}})
                    cursor = response.get("response_metadata", {}).get("next_cursor")
                    if not cursor:
                        break
            except Exception:
                failures.append(channel)
                continue
            with self.state.connect() as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(until)))
        return failures

    def accept(self, payload):
        if payload.get("team_id") != self.config["workspace_id"]:
            return False
        event = payload.get("event", {})
        user, channel, ts = event.get("user"), event.get("channel"), event.get("ts", "")
        if user not in self.config["authorized_user_ids"] or event.get("bot_id") or event.get("subtype"):
            return False
        if event.get("type") not in ("app_mention", "message") or not TS_RE.fullmatch(ts):
            return False
        project = next((p for p in self.config["projects"]
                        if p.get("enabled") and p.get("channel_id") == channel), None)
        if not project:
            return False
        thread = event.get("thread_ts") or ts
        if not TS_RE.fullmatch(thread):
            return False
        text = event.get("text", "")
        mention = "<@" + (self.bot_user_id or "") + ">"
        mentioned = bool(self.bot_user_id) and mention in text
        known_thread = self.state.thread(project["id"], channel, thread)
        if not mentioned and not known_thread:
            return False
        if not mentioned and MENTION.search(text):
            # Replies addressed to someone else (e.g. @Linear) in a kobito thread are not for kobito.
            return False
        text = text.replace(mention, "").strip() if mentioned else text.strip()
        decision_text = text.rstrip("。.!！ ").lower()
        decision = {"承認": "approval", "承認します": "approval", "approve": "approval",
                    "却下": "rejection", "却下します": "rejection", "reject": "rejection"}.get(decision_text)
        if decision and known_thread:
            saved = self.state.decision_for_thread(project["id"], channel, thread)
            if saved:
                return self.state.enqueue(
                    "decision:" + saved["token"], project["id"], channel, thread, "decision",
                    {"token": saved["token"], "decision": decision, "user": user},
                )
            return self.state.enqueue(
                f"message:{channel}:{ts}", project["id"], channel, thread, "notice",
                {"text": "承認対象を一つに特定できません。該当する計画メッセージの承認・却下ボタンを使ってください。", "user": user},
            )
        letter = LETTERS.match(text)
        if letter:
            text = text[letter.end():].strip()
        if not text:
            return False
        kind = "letter" if letter else "conversation"
        if kind == "conversation":
            with self.state.connect() as db:
                count = db.execute("SELECT COUNT(*) FROM inbox WHERE kind='conversation' AND created>?",
                                   (time.time() - 3600,)).fetchone()[0]
            if count >= int(self.config.get("max_conversations_per_hour", 12)):
                kind = "notice"
                text = "この時間の即時会話枠に達しました。時間を置くか、『@kobito 置き手紙: 内容』で依頼を残してください。"
        return self.state.enqueue(
            f"message:{channel}:{ts}", project["id"], channel, thread,
            kind, {"text": text, "user": user},
        )

    def accept_action(self, payload):
        team = payload.get("team", {}).get("id")
        user = payload.get("user", {}).get("id")
        channel = payload.get("channel", {}).get("id")
        message = payload.get("container", {}).get("message_ts")
        actions = payload.get("actions", [])
        if team != self.config["workspace_id"] or user not in self.config["authorized_user_ids"] or len(actions) != 1:
            return False
        action = actions[0]
        priority = PRIORITY_ACTION.match(action.get("action_id") or "")
        if priority:
            return self.accept_priority(action, channel, message, user, int(priority.group(1)))
        decision = {"kobito_approve": "approval", "kobito_reject": "rejection"}.get(action.get("action_id"))
        saved = self.state.decision(action.get("value")) if decision else None
        if not saved or saved["channel"] != channel or saved["message"] != message:
            return False
        try:
            project = project_by_id(self.config, saved["project"])
        except ValueError:
            return False
        if project["channel_id"] != channel:
            return False
        return self.state.enqueue(
            "decision:" + saved["token"], saved["project"], channel, saved["thread"], "decision",
            {"token": saved["token"], "decision": decision, "user": user},
        )

    def accept_priority(self, action, channel, message, user, value):
        saved = self.state.priority(action.get("value"))
        if not saved or saved["channel"] != channel or saved["message"] != message:
            return False
        try:
            project = project_by_id(self.config, self.config.get("default_project_id"))
        except ValueError:
            return False
        if project["channel_id"] != channel:
            return False
        # The action timestamp makes Socket Mode retries idempotent while allowing a later correction.
        stamp = action.get("action_ts") or ""
        if not TS_RE.fullmatch(stamp):
            return False
        return self.state.enqueue(
            f"priority:{saved['token']}:{stamp}", project["id"], channel, message, "priority",
            {"token": saved["token"], "priority": value, "user": user},
        )

    def post_priority_digest(self, issues):
        """Post one message per issue with Urgent/High/Medium/Low buttons to the default project's channel."""
        project = project_by_id(self.config, self.config.get("default_project_id"))
        posted = 0
        for issue in issues[:12]:
            if not ISSUE_UUID.fullmatch(str(issue.get("id", ""))) or not ISSUE_KEY.fullmatch(str(issue.get("identifier", ""))):
                raise ValueError("each issue needs a Linear UUID and identifier")
            text = f"**{issue['identifier']}** {str(issue.get('title', ''))[:200]}"
            if issue.get("project"):
                text += f"\n{str(issue['project'])[:100]}"
            if str(issue.get("url", "")).startswith("https://linear.app/"):
                text += "\n" + issue["url"]
            token = secrets.token_hex(16)
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": to_mrkdwn(text)}}, {
                "type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": label},
                     "action_id": f"kobito_priority_{value}", "value": token}
                    for value, label in PRIORITIES.items()
                ],
            }]
            response = self.post(project["channel_id"], "優先度未設定: " + text, blocks=blocks)
            with self.state.connect() as db:
                db.execute("INSERT INTO priorities VALUES(?,?,?,?,?)",
                           (token, issue["id"], issue["identifier"], project["channel_id"], response["ts"]))
            posted += 1
        return posted

    def post(self, channel, text, thread=None, blocks=None):
        args = {
            "channel": channel, "text": to_mrkdwn(text[:12000]),
            "mrkdwn": True, "parse": "none", "link_names": False,
            "unfurl_links": False, "unfurl_media": False,
        }
        if thread:
            args["thread_ts"] = thread
        if blocks:
            args["blocks"] = blocks
        result = self.slack.chat_postMessage(**args)
        if not result.get("ok", True) or not result.get("ts"):
            raise RuntimeError("Slack did not confirm delivery")
        return result

    def process_one(self):
        item = self.state.claim()
        if not item:
            return False
        try:
            project = project_by_id(self.config, item["project"])
            if project["channel_id"] != item["channel"]:
                raise ValueError("project channel changed; pending message requires review")
            payload = item["payload"]
            if payload.get("user") not in self.config["authorized_user_ids"]:
                raise ValueError("requester is no longer authorized")
            result = json.loads(item["result"]) if item.get("result") else None
            if result is None:
                key = hashlib.sha256(item["key"].encode()).hexdigest()
                if item["kind"] == "notice":
                    result = {"text": payload["text"]}
                elif item["kind"] == "letter":
                    self.board.call(
                        "add", "--direction", "user-to-agent", "--from", "user", "--type", "task",
                        "--title", "Slackの置き手紙", "--body", payload["text"],
                        "--project-id", project["id"], "--external-id", "slack-" + key,
                        "--thread-ts", item["thread"],
                    )
                    result = {"text": "置き手紙を受け取りました。このProjectの定期実行で確認します。実装前に必要な計画・承認をここで確認します。"}
                elif item["kind"] == "decision":
                    saved = self.state.decision(payload["token"])
                    scope = [] if saved["unscoped"] else ["--project-id", project["id"]]
                    self.board.call(
                        "respond", saved["filename"], "--decision", payload["decision"], "--via", "Slack",
                        *scope, "--expected-digest", saved["digest"],
                        "--response-id", "slack-" + saved["token"],
                    )
                    label = "承認" if payload["decision"] == "approval" else "却下"
                    result = {"text": label + "を記録しました。担当kobitoが次の定期実行で確認します。"}
                elif item["kind"] == "priority":
                    saved = self.state.priority(payload["token"])
                    self.linear(saved["issue_id"], payload["priority"])
                    result = {"text": f"{saved['identifier']} の優先度を {PRIORITIES[payload['priority']]} にしました。"}
                else:
                    thread = self.state.thread(project["id"], item["channel"], item["thread"])
                    result = self.conversation(self.config, project, payload["text"], thread["session"])
                self.state.save_result(item["key"], result)
            self.post(item["channel"], result["text"], item["thread"])
            if result.get("session_id"):
                with self.state.connect() as db:
                    db.execute("UPDATE threads SET session=? WHERE project=? AND channel=? AND thread=?",
                               (result["session_id"], project["id"], item["channel"], item["thread"]))
            self.state.finish(item["key"])
        except Exception as error:
            # Store only the error class: SDK/HTTP exception text can contain
            # credentials or message bodies. Failed requests are never discarded.
            self.state.finish(item["key"], type(error).__name__)
            try:
                self.post(item["channel"], "処理を完了できませんでした。依頼は保存されています。設定・接続を確認後に再処理できます。", item["thread"])
            except Exception:
                pass
        return True

    def notify(self, item, filename):
        # A CLI notifier and the listener's retry can run at the same time.
        # Keep this lock separate from SQLite so incoming events can still ack.
        with self.state.path.with_suffix(".notification.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._notify(item, filename)

    def sync_notifications(self):
        failures = 0
        for pending in json.loads(self.board.call("notifications")):
            item = pending["item"]
            try:
                notification_project(self.config, item)
            except ValueError:
                continue
            try:
                if item.get("type") in DECISION_TYPES:
                    # Do not re-send an obsolete approval card from the outbox.
                    try:
                        current = json.loads(self.board.call("get", pending["filename"], "--json"))
                    except RuntimeError:
                        continue
                    if item_digest(current) != item_digest(item):
                        self.board.call("notification-complete", pending["id"])
                        continue
                self.notify(item, pending["filename"])
                self.board.call("notification-complete", pending["id"])
            except Exception:
                failures += 1
        return failures

    def _notify(self, item, filename):
        project = notification_project(self.config, item)
        digest = item_digest(item)
        with self.state.connect() as db:
            previous = db.execute("SELECT digest FROM notifications WHERE project=? AND filename=?",
                                  (project["id"], filename)).fetchone()
        if previous and previous["digest"] == digest:
            return
        channel = project["channel_id"]
        work_id = item.get("work_id")
        for url in item.get("related_links") or []:
            match = LINEAR_ISSUE_URL.search(url)
            if match:
                work_id = match.group(1).upper()
                break
        saved_thread = self.state.issue_thread(project["id"], work_id) if work_id else None
        thread = item.get("slack_thread_ts") or (saved_thread["thread"] if saved_thread else None)
        if thread and not self.state.thread(project["id"], channel, thread):
            raise ValueError("notification thread is not registered to this project")
        text = render_notification(item)
        templates = reply_templates(item.get("body"))
        if templates:
            text += "\n\n返信は、下の返信用メッセージから一つをコピーして、このスレッドに貼ってください。"
        token, blocks = None, None
        # A choice question is answered with a reply template; approve/reject would not say which choice.
        if item.get("type") in DECISION_TYPES and not templates:
            if len(text) > 12000:
                raise ValueError("decision is too long; provide a reviewable plan link")
            token = secrets.token_hex(16)
            # Escaped before formatting, so message content still cannot inject mentions.
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": part}}
                      for part in chunks(to_mrkdwn(text))] + [{
                "type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": label},
                     "action_id": action, "value": token}
                    for label, action in (("承認", "kobito_approve"), ("却下", "kobito_reject"))
                ],
            }]
        response = self.post(channel, text, thread, blocks)
        thread = thread or response["ts"]
        with self.state.connect() as db:
            db.execute("INSERT OR IGNORE INTO threads VALUES(?,?,?,NULL)", (project["id"], channel, thread))
            if token:
                db.execute("INSERT INTO decisions(token,project,channel,thread,filename,digest,message,unscoped) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (token, project["id"], channel, thread, filename, item_digest(item), response["ts"],
                            0 if item.get("project_id") else 1))
            db.execute("INSERT OR REPLACE INTO notifications VALUES(?,?,?)",
                       (project["id"], filename, digest))
            if work_id:
                db.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)",
                           (project["id"], work_id, channel, thread))
        # One message per choice so a long-press copy on mobile yields exactly one reply.
        for template in templates:
            self.post(channel, template, thread)

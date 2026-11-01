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
    config["config_path"] = str(path.resolve())
    return config


def project_by_id(config, project_id, require_work=False):
    for project in config["projects"]:
        if project["id"] == project_id and project.get("enabled", False):
            if require_work and not project.get("work_enabled", False):
                raise ValueError("project work is paused")
            return project
    raise ValueError("project is not registered or enabled")


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
            """)
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


def gateway_conversation(config, project, text, session_id=None):
    url = os.environ.get("AI_GATEWAY_URL", "").rstrip("/")
    token = os.environ.get("AI_GATEWAY_SHARED_TOKEN", "")
    if not url.startswith("https://") or not token:
        raise ValueError("HTTPS gateway and shared token must be configured")
    prompt = (
        f"あなたはこのProjectの担当kobitoです。Project ID: {project['id']}。"
        f"対象repo: {project['repo']}。相談への回答だけを行い、コード・設定・issueは変更しない。"
        "別Projectの操作は引き受けない。実装は置き手紙として受け付けた後、計画への明示承認を別に得る。"
        "この会話での『承認』を実装権限の追加と解釈しない。"
        "ユーザーは『@kobito 置き手紙: 内容』で担当Projectに依頼を残せる。"
        "以下はSlackでの本人の発言（JSON文字列）。\n" + json.dumps(text, ensure_ascii=False)
    )
    payload = {
        "source": "kobito-slack", "project_id": project["id"], "repo": project["repo"],
        "execution_mode": "conversation", "prompt": prompt,
    }
    if session_id:
        payload["resume_session_id"] = session_id
    request = urllib.request.Request(
        url + "/api/run", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    result = None
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=180) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event.get("type") == "gateway_error":
                raise RuntimeError("gateway could not complete the conversation")
            if event.get("type") == "result":
                if event.get("is_error"):
                    raise RuntimeError("conversation ended with an error")
                result = {"text": event.get("result", ""), "session_id": event.get("session_id")}
    if not result or not result["text"]:
        raise RuntimeError("gateway returned no completed response")
    return result


class Runtime:
    def __init__(self, config, state, slack, board=None, conversation=None, bot_user_id=None):
        self.config, self.state, self.slack = config, state, slack
        with state.connect() as db:
            previous = db.execute("SELECT value FROM settings WHERE key='workspace_id'").fetchone()
            if previous and previous["value"] != config["workspace_id"]:
                raise ValueError("state database belongs to another Slack workspace")
            db.execute("INSERT OR IGNORE INTO settings VALUES('workspace_id',?)", (config["workspace_id"],))
        self.board = board or Board(config)
        self.conversation = conversation or gateway_conversation
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

    def post(self, channel, text, thread=None, blocks=None):
        args = {
            "channel": channel, "text": html.escape(text[:12000], quote=False),
            "mrkdwn": False, "parse": "none", "link_names": False,
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
                    self.board.call(
                        "respond", saved["filename"], "--decision", payload["decision"], "--via", "Slack",
                        "--project-id", project["id"], "--expected-digest", saved["digest"],
                        "--response-id", "slack-" + saved["token"],
                    )
                    label = "承認" if payload["decision"] == "approval" else "却下"
                    result = {"text": label + "を記録しました。担当kobitoが次の定期実行で確認します。"}
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
                project_by_id(self.config, item.get("project_id"))
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
        project = project_by_id(self.config, item.get("project_id"))
        digest = item_digest(item)
        with self.state.connect() as db:
            previous = db.execute("SELECT digest FROM notifications WHERE project=? AND filename=?",
                                  (project["id"], filename)).fetchone()
        if previous and previous["digest"] == digest:
            return
        channel = project["channel_id"]
        work_id = item.get("work_id")
        for url in item.get("related_links") or []:
            match = re.search(r"https://linear\.app/[^/]+/issue/([A-Za-z]+-\d+)(?:/|$)", url)
            if match:
                work_id = match.group(1).upper()
                break
        saved_thread = self.state.issue_thread(project["id"], work_id) if work_id else None
        thread = item.get("slack_thread_ts") or (saved_thread["thread"] if saved_thread else None)
        if thread and not self.state.thread(project["id"], channel, thread):
            raise ValueError("notification thread is not registered to this project")
        text = f"[{project.get('name', project['repo'])}担当kobito] {item.get('title', '')}\n{item.get('body', '')}"
        text += "\n" + "\n".join(item.get("related_links") or [])
        templates = reply_templates(item.get("body"))
        if templates:
            text += "\n\n返信は、下の返信用メッセージから一つをコピーして、このスレッドに貼ってください。"
        token, blocks = None, None
        # A choice question is answered with a reply template; approve/reject would not say which choice.
        if item.get("type") in DECISION_TYPES and not templates:
            if len(text) > 12000:
                raise ValueError("decision is too long; provide a reviewable plan link")
            token = secrets.token_hex(16)
            # Plain text prevents message content from injecting mentions.
            blocks = [{"type": "section", "text": {"type": "plain_text", "text": text[i:i + 2900]}}
                      for i in range(0, len(text), 2900)] + [{
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
                db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                           (token, project["id"], channel, thread, filename, item_digest(item), response["ts"]))
            db.execute("INSERT OR REPLACE INTO notifications VALUES(?,?,?)",
                       (project["id"], filename, digest))
            if work_id:
                db.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)",
                           (project["id"], work_id, channel, thread))
        # One message per choice so a long-press copy on mobile yields exactly one reply.
        for template in templates:
            self.post(channel, template, thread)

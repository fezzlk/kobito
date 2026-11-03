import copy
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from project_runtime import Runtime, State, load_config
from project_slack import discover, execute, apply_discovery


class Slack:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"100.{len(self.posts):06d}"}


class Board:
    def __init__(self):
        self.calls = []

    def call(self, *args):
        self.calls.append(args)
        return "request.yaml"


@pytest.fixture
def setup(tmp_path):
    config = {
        "version": 1, "workspace_id": "TOWNER", "authorized_user_ids": ["UOWNER"],
        "config_path": str(tmp_path / "config.json"), "repos_root": str(tmp_path / "repos"),
        "projects": [
            {"id": "project-a", "name": "A", "repo": "a", "channel_id": "CONE", "enabled": True, "work_enabled": True,
             "linear_url": "https://linear.app/test/project/a-123"},
            {"id": "project-b", "name": "B", "repo": "b", "channel_id": "CTWO", "enabled": True, "work_enabled": True,
             "linear_url": "https://linear.app/test/project/b-456"},
        ],
    }
    for project in config["projects"]:
        (tmp_path / "repos" / project["repo"]).mkdir(parents=True)
    state, slack, board, calls = State(tmp_path / "state.sqlite3"), Slack(), Board(), []

    def conversation(cfg, project, text, session):
        calls.append((project["id"], text, session))
        return {"text": "回答 " + project["name"], "session_id": "session-" + project["id"]}

    runtime = Runtime(config, state, slack, board, conversation, bot_user_id="UBOT")
    return runtime, calls, tmp_path


def event(text="<@UBOT> 状況を教えて", channel="CONE", ts="1.000001", **values):
    message = {"type": "app_mention", "user": "UOWNER", "channel": channel, "ts": ts, "text": text}
    message.update(values)
    return {"team_id": "TOWNER", "event": message}


def test_immediate_conversation_stays_in_assigned_channel_and_thread(setup):
    runtime, calls, _ = setup
    assert runtime.accept(event())
    assert runtime.process_one()
    assert calls == [("project-a", "状況を教えて", None)]
    assert runtime.slack.posts[0]["channel"] == "CONE"
    assert runtime.slack.posts[0]["thread_ts"] == "1.000001"
    assert runtime.board.calls == []
    assert runtime.accept(event("続きを教えて", ts="2.000001", type="message", thread_ts="1.000001"))
    runtime.process_one()
    assert calls[-1] == ("project-a", "続きを教えて", "session-project-a")


def test_thread_memory_is_not_reused_by_another_project(setup):
    runtime, calls, _ = setup
    runtime.accept(event())
    runtime.process_one()
    assert not runtime.accept(event("続けて", channel="CTWO", ts="2.000001", type="message", thread_ts="1.000001"))
    runtime.accept(event(channel="CTWO", ts="3.000001"))
    runtime.process_one()
    assert calls[-1][0] == "project-b"
    assert calls[-1][2] is None


@pytest.mark.parametrize("change", [
    {"user": "UOTHER"}, {"bot_id": "B123"}, {"subtype": "message_changed"},
    {"channel": "COTHER"}, {"ts": "not-a-ts"}, {"thread_ts": "bad"},
])
def test_untrusted_or_unbound_events_are_ignored(setup, change):
    runtime, _, _ = setup
    assert not runtime.accept(event(**change))
    assert not runtime.process_one()


def test_workspace_and_unaddressed_chatter_are_ignored(setup):
    runtime, _, _ = setup
    incoming = event()
    incoming["team_id"] = "TOTHER"
    assert not runtime.accept(incoming)
    assert not runtime.accept(event("雑談", type="message"))


@pytest.mark.parametrize("prefix", ["置き手紙:", "置き手紙：", "あとで "])
def test_letter_is_durable_and_does_not_start_an_ai_run(setup, prefix):
    runtime, calls, tmp_path = setup
    assert runtime.accept(event("<@UBOT> " + prefix + "テストを改善して"))
    # Reopening the database models a listener restart after acknowledging Slack.
    runtime.state = State(tmp_path / "state.sqlite3")
    runtime.process_one()
    assert calls == []
    args = runtime.board.calls[0]
    assert args[args.index("--project-id") + 1] == "project-a"
    assert args[args.index("--body") + 1] == "テストを改善して"
    assert args[args.index("--thread-ts") + 1] == "1.000001"


def test_duplicate_events_including_app_mention_and_message_coalesce(setup):
    runtime, calls, _ = setup
    assert runtime.accept(event())
    assert not runtime.accept(event(type="message"))
    assert not runtime.accept(event())
    runtime.process_one()
    assert not runtime.process_one()
    assert len(calls) == 1


def decision_item(project="project-a"):
    return {"project_id": project, "type": "plan_request", "title": "変更計画", "body": "変更内容",
            "related_links": ["https://linear.app/test/issue/TEST-1"]}


def action(runtime, **changes):
    with runtime.state.connect() as db:
        saved = dict(db.execute("SELECT * FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone())
    payload = {"team": {"id": "TOWNER"}, "user": {"id": "UOWNER"},
               "channel": {"id": saved["channel"]}, "container": {"message_ts": saved["message"]},
               "actions": [{"action_id": "kobito_approve", "value": saved["token"]}]}
    payload.update(changes)
    return payload


def test_decision_buttons_require_owner_project_and_original_message(setup):
    runtime, _, _ = setup
    runtime.notify(decision_item(), "plan.yaml")
    assert not runtime.accept_action(action(runtime, user={"id": "UOTHER"}))
    assert not runtime.accept_action(action(runtime, channel={"id": "CTWO"}))
    assert not runtime.accept_action(action(runtime, container={"message_ts": "999.1"}))
    assert runtime.accept_action(action(runtime))
    assert not runtime.accept_action(action(runtime))
    runtime.process_one()
    args = runtime.board.calls[0]
    assert args[:2] == ("respond", "plan.yaml")
    assert "--expected-digest" in args and "--response-id" in args
    assert args[args.index("--project-id") + 1] == "project-a"


def test_unambiguous_thread_approval_uses_the_same_idempotent_action(setup):
    runtime, calls, _ = setup
    runtime.notify(decision_item(), "plan.yaml")
    root = runtime.slack.posts[0]
    assert runtime.accept(event("承認します", ts="2.000001", type="message", thread_ts="100.000001"))
    runtime.process_one()
    assert calls == []
    assert runtime.board.calls[0][:2] == ("respond", "plan.yaml")
    assert not runtime.accept_action(action(runtime))


def test_ambiguous_approval_is_not_forwarded_as_work(setup):
    runtime, calls, _ = setup
    runtime.accept(event())
    runtime.process_one()
    runtime.accept(event("承認", ts="2.000001", type="message", thread_ts="1.000001"))
    runtime.process_one()
    assert len(calls) == 1
    assert not runtime.board.calls
    assert "承認対象" in runtime.slack.posts[-1]["text"]


def test_notification_reuses_issue_thread_and_preserves_full_plan(setup):
    runtime, _, _ = setup
    first = decision_item()
    first["body"] = "詳細" * 2000
    runtime.notify(first, "plan.yaml")
    sections = [b["text"]["text"] for b in runtime.slack.posts[0]["blocks"] if b["type"] == "section"]
    assert first["body"] in "".join(sections)
    runtime.notify({**first, "type": "status_update", "body": "完了"}, "status.yaml")
    assert runtime.slack.posts[-1]["thread_ts"] == "100.000001"


def test_misrouted_notification_and_state_reuse_fail_closed(setup):
    runtime, _, _ = setup
    with pytest.raises(ValueError):
        runtime.notify(decision_item("unknown"), "plan.yaml")
    altered = copy.deepcopy(runtime.config)
    altered["workspace_id"] = "TOTHER"
    with pytest.raises(ValueError):
        Runtime(altered, runtime.state, runtime.slack)
    assert runtime.slack.posts == []


def test_failed_chat_is_retained_without_leaking_exception_text(setup):
    runtime, _, _ = setup
    def fail(*args):
        raise RuntimeError("secret-token-do-not-log")
    runtime.conversation = fail
    runtime.accept(event())
    runtime.process_one()
    with runtime.state.connect() as db:
        row = db.execute("SELECT status,error FROM inbox").fetchone()
    assert tuple(row) == ("failed", "RuntimeError")
    assert "secret-token" not in runtime.slack.posts[0]["text"]


def test_revoked_user_and_paused_project_do_not_start_work(setup):
    runtime, calls, _ = setup
    runtime.accept(event())
    runtime.config["authorized_user_ids"] = ["UOTHER"]
    runtime.process_one()
    assert not calls
    runtime.config["authorized_user_ids"] = ["UOWNER"]
    runtime.config["projects"][0]["enabled"] = False
    assert not runtime.accept(event(ts="3.000001"))


def test_hourly_budget_only_limits_immediate_ai_calls_not_letters(setup):
    runtime, calls, _ = setup
    runtime.config["max_conversations_per_hour"] = 1
    runtime.accept(event())
    runtime.process_one()
    runtime.accept(event(ts="2.000001"))
    runtime.process_one()
    runtime.accept(event("<@UBOT> 置き手紙: 調べて", ts="3.000001"))
    runtime.process_one()
    assert len(calls) == 1 and len(runtime.board.calls) == 1


def test_round_robin_respects_work_pause_without_using_linear_backlog_status(setup):
    runtime, _, _ = setup
    assert runtime.state.next_project(runtime.config)["id"] == "project-a"
    assert runtime.state.next_project(runtime.config)["id"] == "project-b"
    runtime.config["projects"][0]["work_enabled"] = False
    assert runtime.state.next_project(runtime.config)["id"] == "project-b"


def test_discovery_matches_bookmarks_not_channel_names_and_reports_ambiguity(setup):
    runtime, _, _ = setup
    class Discovery:
        def auth_test(self): return {"team_id": "TOWNER", "user_id": "UBOT"}
        def conversations_list(self, **kwargs):
            return {"channels": [{"id": "CX", "name": "wrong-name"}, {"id": "CY", "name": "a"}]}
        def bookmarks_list(self, **kwargs):
            return {"bookmarks": [{"link": "https://linear.app/test/project/a-123/overview"}]}
    result = discover(runtime.config, Discovery())
    assert result["projects"][0]["status"] == "ambiguous"
    assert result["projects"][1]["status"] == "not_found"


def test_config_rejects_channel_aliases_and_path_traversal(setup):
    runtime, _, tmp_path = setup
    config = copy.deepcopy(runtime.config)
    config["projects"][1]["channel_id"] = "CONE"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError): load_config(path)
    config["projects"][1]["channel_id"] = "CTWO"
    config["projects"][0]["repo"] = "../other"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError): load_config(path)


def test_scoped_executor_refuses_wrong_repo_and_enforces_read_only_chat(setup, monkeypatch):
    runtime, _, _ = setup
    args = Namespace(project_id="project-a", repo="b", mode="conversation", argv=["claude", "-p", "hello"])
    with pytest.raises(ValueError): execute(runtime.config, args)
    args.repo = "a"
    captured = {}
    monkeypatch.setattr("project_slack.os.chdir", lambda path: captured.update(cwd=path))
    monkeypatch.setattr("project_slack.os.execvpe", lambda cmd, argv, env: captured.update(argv=argv, env=env))
    execute(runtime.config, args)
    assert "--restricted" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--tools") + 1] == "Read,Glob,Grep"
    assert "--strict-mcp-config" in captured["argv"]
    assert captured["env"]["KOBITO_PROJECT_ID"] == "project-a"
    assert captured["cwd"].endswith("/repos/a")


def test_startup_history_recovers_letters_and_deduplicates_socket_deliveries(setup):
    runtime, calls, _ = setup
    class History(Slack):
        def conversations_history(self, **kwargs):
            if kwargs["channel"] == "CTWO":
                raise RuntimeError("private channel unavailable")
            return {"messages": [{"ts": "1.000001", "text": "<@UBOT> 置き手紙: 調査して", "user": "UOWNER"}]}
    runtime.slack = History()
    assert runtime.catch_up() == ["CTWO"]
    assert not runtime.accept(event("<@UBOT> 置き手紙: 調査して"))
    runtime.catch_up()
    runtime.process_one()
    assert not runtime.process_one()
    assert calls == [] and len(runtime.board.calls) == 1
    with runtime.state.connect() as db:
        assert db.execute("SELECT value FROM settings WHERE key='history:CONE'").fetchone()
        assert not db.execute("SELECT value FROM settings WHERE key='history:CTWO'").fetchone()


def test_applying_discovered_bindings_does_not_enable_background_work(setup):
    runtime, _, tmp_path = setup
    project = runtime.config["projects"][0]
    project.update(channel_id=None, enabled=False, work_enabled=False)
    apply_discovery(runtime.config, {"projects": [{"project_id": "project-a", "status": "matched",
                      "matched_channel_ids": ["CNEW"]}]}, enable=True)
    saved = load_config(tmp_path / "config.json")
    assert saved["projects"][0]["channel_id"] == "CNEW"
    assert saved["projects"][0]["enabled"] is True
    assert saved["projects"][0]["work_enabled"] is False


def test_changed_plan_cannot_be_approved_with_an_old_notification(setup):
    runtime, _, _ = setup
    runtime.notify(decision_item(), "plan.yaml")
    old = action(runtime)
    runtime.notify({**decision_item(), "body": "改訂後の計画"}, "plan.yaml")
    # The Board digest guard remains authoritative. The old button can only
    # produce an action for its old digest, never silently approve the revision.
    assert runtime.accept_action(old)
    old_digest = runtime.state.decision(old["actions"][0]["value"])["digest"]
    new_digest = runtime.state.decision(action(runtime)["actions"][0]["value"])["digest"]
    assert old_digest != new_digest


def test_background_work_lock_is_not_stolen_by_another_process(setup, monkeypatch):
    import fcntl
    import os
    runtime, _, tmp_path = setup
    runtime.config["state_path"] = str(tmp_path / "state.sqlite3")
    from project_slack import WorkSlotBusy
    with (tmp_path / "state.work.lock").open("a+") as existing:
        fcntl.flock(existing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        args = Namespace(project_id="project-a", repo="a", mode="work", argv=["claude", "-p", "work"])
        with pytest.raises(WorkSlotBusy): execute(runtime.config, args)


def test_gateway_does_not_follow_redirects_with_its_bearer_token(monkeypatch):
    import io
    from project_runtime import NoRedirect, gateway_conversation
    monkeypatch.setenv("AI_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("AI_GATEWAY_SHARED_TOKEN", "private-test-token")
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://gateway.example/api/run"
            assert json.loads(request.data)["execution_mode"] == "conversation"
            return io.BytesIO(b'data: {"type":"result","result":"answer"}\n\n')
    def build_opener(handler):
        assert isinstance(handler, NoRedirect)
        assert handler.redirect_request(None, None, 302, "redirect", {}, "https://other.example") is None
        return Opener()
    monkeypatch.setattr("project_runtime.urllib.request.build_opener", build_opener)
    assert gateway_conversation({}, {"id": "project-a", "repo": "a"}, "hello")["text"] == "answer"


def test_pending_notifications_retry_without_duplicate_plan_cards(setup):
    runtime, _, _ = setup
    item = decision_item()
    class PendingBoard:
        completed = []
        def call(self, *args):
            if args[0] == "notifications":
                return json.dumps([{"id": "one", "item": item, "filename": "plan.yaml"}])
            if args[0] == "get":
                return json.dumps(item)
            self.completed.append(args)
            return ""
    runtime.board = PendingBoard()
    assert runtime.sync_notifications() == 0
    assert runtime.sync_notifications() == 0
    assert len(runtime.slack.posts) == 1
    assert len(runtime.board.completed) == 2
    item["body"] = "改訂した計画"
    assert runtime.sync_notifications() == 0
    assert len(runtime.slack.posts) == 2


def test_launch_agent_is_only_rendered_and_contains_no_secrets(setup, monkeypatch):
    import plistlib
    from project_slack import main
    runtime, _, tmp_path = setup
    config_path = tmp_path / "config.json"
    runtime.config["state_path"] = str(tmp_path / "state.sqlite3")
    config_path.write_text(json.dumps(runtime.config))
    output = tmp_path / "service.plist"
    monkeypatch.setenv("SLACK_BOT_TOKEN", "private-test-token")
    assert main(["--config", str(config_path), "launch-agent", "--output", str(output)]) == 0
    manifest = plistlib.loads(output.read_bytes())
    assert manifest["ProgramArguments"][-1] == "serve"
    assert "private-test-token" not in output.read_text()
    assert "EnvironmentVariables" not in manifest
    assert main(["--config", str(config_path), "launch-agent", "--output", str(output)]) == 1


def test_choice_question_posts_one_copyable_letter_reply_per_choice(setup):
    runtime, _, _ = setup
    item = {**decision_item(), "type": "decision_request",
            "body": "どれか一つで返信してください:\nA. README.md を新規作成する。計画を出す\nB. 上の改善点リストだけで十分\nC. 別の文書を指していた"}
    runtime.notify(item, "question.yaml")
    question, *templates = runtime.slack.posts
    assert "返信用メッセージ" in question["text"]
    assert "blocks" not in question
    assert [t["text"] for t in templates] == [
        "置き手紙: A（README.md を新規作成する）", "置き手紙: B（上の改善点リストだけで十分）",
        "置き手紙: C（別の文書を指していた）"]
    assert all(t["thread_ts"] == "100.000001" for t in templates)
    reply = event(templates[0]["text"], ts="100.000009", thread_ts="100.000001")
    reply["event"]["type"] = "message"
    assert runtime.accept(reply)
    runtime.process_one()
    assert runtime.board.calls[-1][runtime.board.calls[-1].index("--body") + 1] == "A（README.md を新規作成する）"


def test_plan_without_choices_gets_no_reply_templates(setup):
    runtime, _, _ = setup
    runtime.notify({**decision_item(), "body": "A案で進めます\n変更内容"}, "plan.yaml")
    assert len(runtime.slack.posts) == 1


def test_markdown_is_rendered_as_slack_mrkdwn_without_allowing_mentions(setup):
    runtime, _, _ = setup
    runtime.post("CONE", "## 見出し\n**太字**と`code`\n- 項目 <@UOTHER> <!here>\n"
                         "[PR](https://github.com/x/y/pull/1)\n```\n**そのまま**\n```")
    post = runtime.slack.posts[-1]
    assert post["mrkdwn"] is True and post["link_names"] is False
    assert post["text"].split("\n") == [
        "*見出し*", "*太字*と`code`", "• 項目 &lt;@UOTHER&gt; &lt;!here&gt;",
        "<https://github.com/x/y/pull/1|PR>", "```", "**そのまま**", "```"]


def test_decision_card_sections_use_mrkdwn(setup):
    runtime, _, _ = setup
    runtime.notify({**decision_item(), "body": "**計画**\n- 手順"}, "plan.yaml")
    sections = [b["text"] for b in runtime.slack.posts[0]["blocks"] if b["type"] == "section"]
    assert all(s["type"] == "mrkdwn" for s in sections)
    assert "*計画*\n• 手順" in sections[0]["text"]


def test_thread_reply_addressed_to_another_app_is_ignored(setup):
    runtime, calls, _ = setup
    runtime.accept(event())
    runtime.process_one()
    reply = event("<@ULINEAR> RJHプロジェクトに起票: README", ts="1.000002", thread_ts="1.000001")
    reply["event"]["type"] = "message"
    assert not runtime.accept(reply)
    assert len(calls) == 1


def with_default(runtime):
    runtime.config["default_project_id"] = "project-a"
    return runtime


def test_projectless_notification_goes_to_default_project_channel(setup):
    runtime, _, _ = setup
    item = {"type": "action_required", "title": "kobitoの稼働確認", "body": "対処"}
    with pytest.raises(ValueError):
        runtime.notify(item, "health.yaml")
    with_default(runtime).notify(item, "health.yaml")
    assert runtime.slack.posts[0]["channel"] == "CONE"


def test_projectless_decision_is_answered_without_project_scope(setup):
    runtime, _, _ = setup
    with_default(runtime).notify({**decision_item(), "project_id": None}, "plan.yaml")
    assert runtime.accept_action(action(runtime))
    runtime.process_one()
    args = runtime.board.calls[0]
    assert args[:2] == ("respond", "plan.yaml")
    assert "--project-id" not in args


def test_projectless_items_are_retried_from_the_outbox(setup):
    runtime, _, _ = setup
    item = {"type": "fyi", "title": "週次", "body": "Radar"}
    class PendingBoard:
        def call(self, *args):
            if args[0] == "notifications":
                return json.dumps([{"id": "one", "item": item, "filename": "fyi.yaml"}])
            return ""
    runtime.board = PendingBoard()
    assert runtime.sync_notifications() == 0
    assert runtime.slack.posts == []
    with_default(runtime).sync_notifications()
    assert runtime.slack.posts[0]["channel"] == "CONE"


def test_default_project_must_be_enabled(setup):
    runtime, _, tmp_path = setup
    config = copy.deepcopy(runtime.config)
    config.pop("config_path")
    config["default_project_id"] = "missing"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="default_project_id"):
        load_config(path)


ISSUE = {"id": "0f0e0d0c-0b0a-4908-8706-050403020100", "identifier": "FEZ-273",
         "title": "LINEの退役", "project": "kobito", "url": "https://linear.app/test/issue/FEZ-273/x"}


def priority_action(runtime, value=2, **changes):
    with runtime.state.connect() as db:
        saved = dict(db.execute("SELECT * FROM priorities ORDER BY rowid DESC LIMIT 1").fetchone())
    payload = {"team": {"id": "TOWNER"}, "user": {"id": "UOWNER"},
               "channel": {"id": saved["channel"]}, "container": {"message_ts": saved["message"]},
               "actions": [{"action_id": f"kobito_priority_{value}", "value": saved["token"],
                            "action_ts": "200.000001"}]}
    payload.update(changes)
    return payload


def test_priority_digest_posts_buttons_and_updates_linear_on_click(setup):
    runtime, _, _ = setup
    updates = []
    runtime.linear = lambda issue_id, priority: updates.append((issue_id, priority))
    assert with_default(runtime).post_priority_digest([ISSUE]) == 1
    post = runtime.slack.posts[0]
    assert post["channel"] == "CONE"
    buttons = post["blocks"][-1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Urgent", "High", "Medium", "Low"]
    assert ISSUE["id"] not in json.dumps(post)
    assert not runtime.accept_action(priority_action(runtime, user={"id": "UOTHER"}))
    assert not runtime.accept_action(priority_action(runtime, channel={"id": "CTWO"}))
    assert not runtime.accept_action(priority_action(runtime, container={"message_ts": "999.1"}))
    forged = priority_action(runtime)
    forged["actions"][0]["value"] = "0" * 32
    assert not runtime.accept_action(forged)
    assert runtime.accept_action(priority_action(runtime))
    assert not runtime.accept_action(priority_action(runtime))
    runtime.process_one()
    assert updates == [(ISSUE["id"], 2)]
    assert runtime.slack.posts[-1]["thread_ts"] == "100.000001"
    assert "FEZ-273 の優先度を High" in runtime.slack.posts[-1]["text"]


def test_priority_digest_rejects_malformed_issues_and_reports_linear_failure(setup):
    runtime, _, _ = setup
    with pytest.raises(ValueError):
        with_default(runtime).post_priority_digest([{**ISSUE, "id": "../x"}])
    def fail(issue_id, priority):
        raise RuntimeError("secret detail")
    runtime.linear = fail
    runtime.post_priority_digest([ISSUE])
    runtime.accept_action(priority_action(runtime))
    runtime.process_one()
    assert "処理を完了できませんでした" in runtime.slack.posts[-1]["text"]
    assert "secret" not in json.dumps(runtime.slack.posts)


def test_existing_decision_table_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE decisions (token TEXT PRIMARY KEY, project TEXT, channel TEXT, thread TEXT, "
               "filename TEXT, digest TEXT, message TEXT)")
    db.execute("INSERT INTO decisions VALUES('t','project-a','CONE','1.1','plan.yaml','d','1.2')")
    db.commit()
    db.close()
    assert State(path).decision("t")["unscoped"] == 0

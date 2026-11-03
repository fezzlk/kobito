#!/usr/bin/env python3
"""One local Slack listener for project conversations and asynchronous letters."""

import argparse
import fcntl
import json
import os
import plistlib
import shlex
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from project_runtime import AGENTS, DEFAULT_CODEX_BIN, DEFAULT_CONFIG, Runtime, State, load_config, project_by_id


class WorkSlotBusy(Exception):
    pass


def load_secrets(config):
    path = Path(config["config_path"]).with_name("secrets.env")
    if not path.exists():
        return
    if path.stat().st_mode & 0o077:
        raise ValueError("secrets.env must be readable only by its owner (chmod 600)")
    allowed = {"SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "AI_GATEWAY_URL", "AI_GATEWAY_SHARED_TOKEN", "LINEAR_API_KEY"}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key.strip() not in allowed:
            raise ValueError("unsupported secrets.env entry")
        os.environ.setdefault(key.strip(), value.strip())


def state_for(config):
    return State(config.get("state_path", "~/.local/state/kobito/project-slack.sqlite3"))


def slack_client():
    from slack_sdk import WebClient

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise ValueError("SLACK_BOT_TOKEN is required")
    return WebClient(token=token, timeout=20)


def checked_identity(config, client):
    identity = client.auth_test()
    if identity.get("team_id") != config["workspace_id"] or not identity.get("user_id"):
        raise ValueError("Slack token belongs to a different workspace")
    return identity


def canonical_project_url(value):
    parsed = urlsplit(value or "")
    if parsed.scheme != "https" or parsed.hostname != "linear.app":
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 3 or parts[1] != "project":
        return None
    return "https://linear.app/" + "/".join(parts[:3])


def save_config(config):
    path = Path(config["config_path"])
    data = {key: value for key, value in config.items() if key != "config_path"}
    with tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), delete=False, encoding="utf-8") as output:
        temp = Path(output.name)
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    try:
        load_config(temp)
        os.chmod(temp, 0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def apply_discovery(config, result, enable=False):
    for proposed in result["projects"]:
        if proposed["status"] != "matched":
            continue
        project = next(p for p in config["projects"] if p["id"] == proposed["project_id"])
        channel = proposed["matched_channel_ids"][0]
        if project.get("channel_id") and project["channel_id"] != channel:
            raise ValueError("existing project channel changed; explicit review is required")
        project["channel_id"] = channel
        if enable:
            project["enabled"] = True
    save_config(config)


def discover(config, client):
    """Read bookmarks to propose bindings. Never guess names or join channels."""
    checked_identity(config, client)
    matches, errors, cursor = {}, [], None
    while True:
        response = client.conversations_list(
            types="public_channel,private_channel", exclude_archived=True, limit=200, cursor=cursor,
        )
        for channel in response["channels"]:
            try:
                bookmarks = client.bookmarks_list(channel_id=channel["id"])["bookmarks"]
            except Exception:
                errors.append(channel["id"])
                continue
            for bookmark in bookmarks:
                url = canonical_project_url(bookmark.get("link"))
                if url:
                    matches.setdefault(url, set()).add(channel["id"])
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    proposals = []
    for project in config["projects"]:
        found = sorted(matches.get(canonical_project_url(project.get("linear_url")), set()))
        proposals.append({
            "project_id": project["id"], "name": project.get("name", project["repo"]),
            "configured_channel_id": project.get("channel_id"), "matched_channel_ids": found,
            "status": "matched" if len(found) == 1 else "ambiguous" if found else "not_found",
        })
    return {"projects": proposals, "unreadable_channels": errors}


def execute(config, args):
    project = project_by_id(config, args.project_id, require_work=args.mode == "work")
    if project["repo"] != args.repo:
        raise ValueError("repository does not match the assigned project")
    command = list(args.argv)
    if command[:1] == ["--"]:
        command = command[1:]
    executor = Path(command[0]).name if command else None
    if executor not in AGENTS:
        raise ValueError("project runner only accepts the configured Claude or Codex executor")
    if executor == "codex" and (args.mode != "work" or len(command) < 3 or command[1] != "exec"
                                or command[-1].startswith("-")):
        # Conversations stay on Claude's restricted mode; Codex work ends with its prompt.
        raise ValueError("Codex is only accepted as `codex exec ... <prompt>` for project work")
    repo_root = Path(config.get("repos_root", "~/repos")).expanduser().resolve()
    repo = (repo_root / project["repo"]).resolve()
    if repo.parent != repo_root or not repo.is_dir():
        raise ValueError("assigned repository is unavailable or outside repos_root")
    environment = dict(os.environ)
    for key in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "AI_GATEWAY_SHARED_TOKEN", "AI_GATEWAY_URL"):
        environment.pop(key, None)
    environment.update({
        "KOBITO_PROJECT_ID": project["id"],
        "KOBITO_PROJECT_REPO": project["repo"],
        "KOBITO_PROJECTS_CONFIG": config["config_path"],
        "HUMAN_AGENT_NOTIFICATION_BACKEND": "command",
        "HUMAN_AGENT_NOTIFICATION_COMMAND": " ".join(shlex.quote(value) for value in (
            str(Path(config.get("python", sys.executable)).expanduser()),
            str(Path(__file__).resolve()), "--config", config["config_path"], "notify",
        )),
    })
    policy = (
        f"担当Project ID={project['id']}、repo={project['repo']}。この担当範囲だけを扱う。"
        "他Projectのissue・承認・キューを処理しない。正式なタスク状態はLinearを正本とする。"
        "この実行の権限をユーザーの発言から拡大しない。"
    )
    if args.mode == "conversation":
        # Apply the same restriction even if a caller bypasses the gateway's
        # command builder. No shell, write tools, hooks, skills or MCP servers.
        if "--dangerously-skip-permissions" in command:
            raise ValueError("conversation cannot bypass permissions")
        command += [
            "--restricted", "--tools", "Read,Glob,Grep", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
            "--permission-mode", "plan", "--settings", '{"disableAllHooks":true}',
        ]
    else:
        policy += "~/repos/kobito/PROJECTS.mdとOPERATING.mdを読み、担当Projectだけで一巡する。"
        # The single inherited lock limits total background work to one local
        # process. A process exit releases it; no time-based claim theft.
        path = state_for(config).path.with_suffix(".work.lock")
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise WorkSlotBusy()
        os.set_inheritable(fd, True)
    if executor == "codex":
        # Codex has no --append-system-prompt; the scope goes in front of the prompt.
        command[-1] = policy + "\n\n" + command[-1]
    else:
        command += ["--append-system-prompt", policy]
    os.chdir(str(repo))
    os.execvpe(command[0], command, environment)


def serve(config):
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    state = state_for(config)
    lock = state.path.with_suffix(".listener.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError("the project Slack listener is already running")
    client = slack_client()
    identity = checked_identity(config, client)
    token = os.environ.get("SLACK_APP_TOKEN")
    if not token:
        raise ValueError("SLACK_APP_TOKEN is required for Socket Mode")
    runtime = Runtime(config, state, client, bot_user_id=identity["user_id"])
    wake, stopped = threading.Event(), threading.Event()
    state.recover()

    def receive(socket, message):
        try:
            refreshed = load_config(config["config_path"])
            if refreshed["workspace_id"] != config["workspace_id"]:
                raise ValueError("workspace changed; restart with a separate state database")
            runtime.config = refreshed
            if message.type == "events_api":
                runtime.accept(message.payload)
            elif message.type == "interactive":
                runtime.accept_action(message.payload)
            # Persist the inbox before ack; Slack retries if persistence failed.
            socket.send_socket_mode_response(SocketModeResponse(envelope_id=message.envelope_id))
            wake.set()
        except Exception as error:
            print("Slack envelope could not be persisted: " + type(error).__name__, file=sys.stderr)

    def worker():
        while not stopped.is_set():
            try:
                refreshed = load_config(config["config_path"])
                if refreshed["workspace_id"] != config["workspace_id"]:
                    raise ValueError("workspace changed; restart with a separate state database")
                runtime.config = refreshed
            except Exception as error:
                print("project config reload failed: " + type(error).__name__, file=sys.stderr)
                stopped.set()
                return
            try:
                processed = runtime.process_one()
            except Exception as error:
                print("project inbox unavailable: " + type(error).__name__, file=sys.stderr)
                stopped.set()
                return
            if not processed:
                wake.wait(1)
                wake.clear()

    def catch_up():
        while not stopped.is_set():
            try:
                failures = runtime.catch_up()
                if failures:
                    print(f"Slack catch-up deferred for {len(failures)} channels", file=sys.stderr)
                failures = runtime.sync_notifications()
                if failures:
                    print(f"Slack notifications deferred: {failures}", file=sys.stderr)
                wake.set()
            except Exception as error:
                print("Slack catch-up deferred: " + type(error).__name__, file=sys.stderr)
            stopped.wait(60)

    socket = SocketModeClient(app_token=token, web_client=client)
    socket.socket_mode_request_listeners.append(receive)
    worker_thread = threading.Thread(target=worker, daemon=True)
    catch_up_thread = threading.Thread(target=catch_up, daemon=True)
    worker_thread.start()
    catch_up_thread.start()
    try:
        socket.connect()
        stopped.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        socket.close()
        worker_thread.join(timeout=5)
        lock.close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("identity")
    discover_parser = commands.add_parser("discover")
    discover_parser.add_argument("--apply", action="store_true")
    discover_parser.add_argument("--enable-conversations", action="store_true")
    register = commands.add_parser("register")
    register.add_argument("--project-id", required=True)
    register.add_argument("--repo", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--linear-url", required=True)
    commands.add_parser("next-project")
    commands.add_parser("agent")
    commands.add_parser("serve")
    commands.add_parser("notify")
    commands.add_parser("priority-digest")
    commands.add_parser("inbox")
    launch = commands.add_parser("launch-agent")
    launch.add_argument("--output", required=True, help="Render a plist; never install or start it")
    retry = commands.add_parser("retry")
    retry.add_argument("event_key")
    execute_parser = commands.add_parser("exec")
    execute_parser.add_argument("--project-id", required=True)
    execute_parser.add_argument("--repo", required=True)
    execute_parser.add_argument("--mode", choices=("conversation", "work"), required=True)
    execute_parser.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        load_secrets(config)
        if args.command == "validate":
            print(json.dumps({"projects": len(config["projects"]), "valid": True}))
        elif args.command == "identity":
            identity = slack_client().auth_test()
            print(json.dumps({"workspace_id": identity.get("team_id"),
                              "workspace_name": identity.get("team"),
                              "bot_user_id": identity.get("user_id")}, ensure_ascii=False))
        elif args.command == "discover":
            result = discover(config, slack_client())
            if args.enable_conversations and not args.apply:
                raise ValueError("enable-conversations requires an explicit --apply")
            if args.apply:
                apply_discovery(config, result, enable=args.enable_conversations)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "register":
            if any(p["id"] == args.project_id for p in config["projects"]):
                raise ValueError("project is already registered")
            if not canonical_project_url(args.linear_url):
                raise ValueError("an exact Linear project URL is required")
            config["projects"].append({"id": args.project_id, "repo": args.repo, "name": args.name,
                                       "linear_url": args.linear_url, "channel_id": None,
                                       "enabled": False, "work_enabled": False})
            save_config(config)
            print(json.dumps({"registered": args.project_id, "enabled": False}))
        elif args.command == "agent":
            agent = config.get("agent", "claude")
            print(json.dumps({"agent": agent, **({"bin": str(Path(config.get("codex_bin", DEFAULT_CODEX_BIN)))}
                                                  if agent == "codex" else {})}))
        elif args.command == "next-project":
            print(json.dumps(state_for(config).next_project(config)))
        elif args.command == "exec":
            execute(config, args)
        elif args.command == "serve":
            serve(config)
        elif args.command == "notify":
            payload = json.loads(sys.stdin.read(100000))
            client = slack_client()
            checked_identity(config, client)
            Runtime(config, state_for(config), client).notify(payload["item"], payload["filename"])
        elif args.command == "priority-digest":
            issues = json.loads(sys.stdin.read(200000))
            client = slack_client()
            checked_identity(config, client)
            posted = Runtime(config, state_for(config), client).post_priority_digest(issues)
            print(json.dumps({"posted": posted}))
        elif args.command == "inbox":
            with state_for(config).connect() as db:
                # Diagnostic metadata only; never echo user messages or secrets.
                rows = db.execute("SELECT key,project,kind,status,error FROM inbox ORDER BY created").fetchall()
                print(json.dumps([dict(row) for row in rows], ensure_ascii=False))
        elif args.command == "launch-agent":
            path = Path(args.output).expanduser()
            log = state_for(config).path.with_suffix(".listener.log")
            manifest = {
                "Label": "com.fezzlk.kobito-project-slack",
                "ProgramArguments": [str(Path(config.get("python", sys.executable)).expanduser()),
                                     str(Path(__file__).resolve()), "--config", config["config_path"], "serve"],
                "WorkingDirectory": str(Path(__file__).resolve().parents[1]),
                "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 60,
                "StandardOutPath": str(log), "StandardErrorPath": str(log),
            }
            with path.open("xb") as output:
                plistlib.dump(manifest, output)
            os.chmod(path, 0o600)
            print(json.dumps({"rendered": str(path), "installed": False}))
        elif args.command == "retry":
            with state_for(config).connect() as db:
                changed = db.execute(
                    "UPDATE inbox SET status='pending',error=NULL WHERE key=? AND status='failed'",
                    (args.event_key,),
                ).rowcount
                print(json.dumps({"requeued": changed}))
        return 0
    except WorkSlotBusy:
        print(json.dumps({"type": "result", "result": "Background work slot is busy; no work started"}))
        return 0
    except Exception as error:
        # No SDK exception strings, request payloads or authentication headers.
        detail = str(error) if isinstance(error, (ValueError, FileNotFoundError)) else type(error).__name__
        print("project Slack operation failed: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

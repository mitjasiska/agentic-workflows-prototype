"""Opt-in Linux/Herdr/Codex acceptance; excluded from offline test discovery.

Run on Herdr's host: python3.12 tests/live_owned_agent_cleanup.py --output /tmp/NEW-DIR
Creates only disposable Git worktrees, a private registry, and new
Herdr workspaces. Linear/config/publication services are synthetic; provider RPC,
process identity, Herdr retirement, journaling and Git deletion are production.
A local unauthenticated Responses fixture completes two real Codex TUI turns and
holds a third turn active through cleanup. No model credentials or /exit needed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
import traceback
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import test_task_start as fixtures
from task_start import cli
from task_start.codex_rpc import CodexRPC
from task_start.config import LocalConfig, Project
from task_start.contexts import ContextRegistry, HerdrContexts
from task_start.integration_process import _process_visibility
from task_start.publication_state import PublicationStore
from task_start.workspace import Herdr


def call(*args):
    result = subprocess.run(["herdr", *args], check=True, capture_output=True, text=True, timeout=45)
    return json.loads(result.stdout)["result"] if result.stdout.strip() else None


def revision():
    names = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z",
                                     "--cached", "--others", "--exclude-standard"])
    files = {os.fsdecode(name): sha256((ROOT / os.fsdecode(name)).read_bytes()).hexdigest()
             for name in names.split(b"\0") if name and (ROOT / os.fsdecode(name)).is_file()}
    return dict(head=subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                             text=True).strip(), files=files)


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        message = dict(id="msg_" + uuid4().hex, type="message", role="assistant", status="completed",
                       content=[dict(type="output_text", text="READY", annotations=[])])
        response = dict(id="resp_" + uuid4().hex, object="response", created_at=int(time.time()),
                        status="completed", model="fixture", output=[message],
                        usage=dict(input_tokens=1, output_tokens=1, total_tokens=2,
                                   input_tokens_details=dict(cached_tokens=0),
                                   output_tokens_details=dict(reasoning_tokens=0)))
        events = [dict(type="response.created", response=dict(response, status="in_progress", output=[])),
                  dict(type="response.output_item.added", output_index=0,
                       item=dict(message, status="in_progress", content=[])),
                  dict(type="response.content_part.added", item_id=message["id"], output_index=0,
                       content_index=0, part=dict(type="output_text", text="", annotations=[])),
                  dict(type="response.output_text.delta", item_id=message["id"], output_index=0,
                       content_index=0, delta="READY"),
                  dict(type="response.output_text.done", item_id=message["id"], output_index=0,
                       content_index=0, text="READY"),
                  dict(type="response.content_part.done", item_id=message["id"], output_index=0,
                       content_index=0, part=message["content"][0]),
                  dict(type="response.output_item.done", output_index=0, item=message),
                  dict(type="response.completed", response=response)]
        chunks = [("event: " + event["type"] + "\ndata: "
                   + json.dumps(dict(event, sequence_number=index)) + "\n\n").encode()
                  for index, event in enumerate(events)]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(sum(map(len, chunks))))
        self.end_headers()
        self.wfile.write(chunks[0])
        self.wfile.flush()
        if self.path.startswith("/active/"):
            self.server.active.set()
            if not self.server.release.wait(180):
                return
        self.wfile.write(b"".join(chunks[1:]))


def wait_for(observe, description, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = observe()
        if result:
            return result
        time.sleep(.2)
    raise AssertionError("Timed out: " + description)


def main(output):
    _process_visibility(Path("/proc"))  # Refuse sandbox/filtered proc before creating resources.
    output.mkdir(parents=True, exist_ok=False)
    before = revision()
    (output / "revision.json").write_text(json.dumps(before, indent=2) + "\n")
    report = (output / "report.txt").open("w", buffering=1)

    def log(value):
        print(value, file=report, flush=True)

    log(datetime.now(timezone.utc).isoformat())
    log(subprocess.check_output(["herdr", "status", "server"], text=True))
    log(subprocess.check_output(["codex", "--version"], text=True).strip())
    case, workspaces = fixtures.LocalGitIntegrationTests(), {}
    server, succeeded = None, False
    try:
        case.setUp()
        registry, identities, herdr = ContextRegistry(), HerdrContexts(), Herdr(case.repo)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server.active, server.release = threading.Event(), threading.Event()
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def workspace(identifier):
            branch = identifier.lower() + "-cleanup-probe"
            path = case.repo.parent / branch
            case.command(case.repo, "worktree", "add", "-b", branch, str(path))
            case.git.save_scope(path, branch, identifier, None)
            (path / "tracked.txt").write_text("uncommitted disposable work\n")
            call("worktree", "open", "--cwd", str(case.repo), "--path", str(path),
                 "--label", identifier, "--no-focus", "--trust-repository")
            entries = herdr.workspaces()
            # Herdr also opens the permanent checkout on first worktree open.
            for created in entries:
                if (created.get("worktree") or {}).get("checkout_path") == str(case.repo):
                    workspaces[created["workspace_id"]] = case.repo
            entry = next(w for w in entries
                         if (w.get("worktree") or {}).get("checkout_path") == str(path))
            wid = entry["workspace_id"]
            workspaces[wid] = path
            pane = next(p for p in identities.snapshot() if p["workspace_id"] == wid)
            return path, wid, pane

        def turns(path, session):
            with CodexRPC(path) as rpc:
                return rpc.request("thread/turns/list", dict(threadId=session, sortDirection="desc",
                                                           limit=1, itemsView="full"))["data"]

        def launch(identifier, path, pane, role, active=False):
            context = registry.allocate(identifier, role, agent="codex", repository=str(case.repo),
                worktree=str(path), endpoint=identities.endpoint(), workspace_id=pane["workspace_id"],
                tab_id=pane["tab_id"], pane_id=pane["pane_id"], terminal_id=pane["terminal_id"])
            mode = "active" if active else "idle"
            provider = ('model_providers.fixture={name="Fixture",base_url="http://127.0.0.1:'
                        + str(server.server_port) + '/' + mode
                        + '/v1",wire_api="responses",requires_openai_auth=false}')
            args = ["codex", "--no-daemon", "--cd", str(path),
                    "-c", 'model_provider="fixture"', "-c", provider, "-c", 'model="fixture"',
                    "-c", 'approval_policy="never"', "--sandbox", "read-only", "--no-alt-screen",
                    "Reply READY only. Do not use tools."]
            call("pane", "run", pane["pane_id"], shlex.join(args))
            trusted = False

            def observe():
                nonlocal trusted
                observed = next(p for p in identities.snapshot() if p["pane_id"] == pane["pane_id"])
                assert observed["terminal_id"] == pane["terminal_id"]
                if not trusted and observed.get("agent") == "codex" and observed.get("agent_status") == "blocked":
                    screen = subprocess.check_output(["herdr", "pane", "read", pane["pane_id"],
                        "--source", "recent-unwrapped", "--lines", "30"], text=True, timeout=10)
                    if "Trust this folder?" in screen and str(case.repo) in screen:
                        call("agent", "send-keys", pane["pane_id"], "enter")
                        trusted = True
                        log("Startup trust confirmed only for disposable " + str(case.repo))
                if observed.get("agent") != "codex":
                    return None
                if active and not server.active.is_set():
                    return None
                if not active and observed.get("agent_status") not in {"idle", "done"}:
                    return None
                known = {c["session_id"] for c in registry.list(identifier)}
                with CodexRPC(path) as rpc:
                    threads = rpc.request("thread/list", dict(cwd=str(path), limit=10,
                                                             modelProviders=[], sourceKinds=["cli"]))["data"]
                for thread in threads:
                    if thread["id"] not in known:
                        latest = turns(path, thread["id"])
                        # A separate app-server may label the TUI's unfinished
                        # on-disk turn "interrupted". Establish activity from the
                        # working TUI + outstanding streamed provider request;
                        # afterward require this same turn to complete.
                        if latest and ((active and observed.get("agent_status") == "working")
                                       or (not active and latest[0]["status"] == "completed")):
                            log("Provider turn observation: " + str((latest[0]["id"], latest[0]["status"])))
                            return thread["id"]
                return None

            session = wait_for(observe, "Codex " + mode + " turn")
            registry.update(context, state="active", session_id=session, session_kind="id", resumability="yes")
            call("pane", "report-agent-session", pane["pane_id"], "--source", "cleanup-fixture",
                 "--agent", "codex", "--agent-session-id", session)
            info = identities.command("pane", "process-info", "--pane", pane["pane_id"])["process_info"]
            log(json.dumps(dict(role=role, mode=mode, session=session, pane=pane["pane_id"], process_info=info)))
            return session, info

        identifier, unrelated = "DEV-900091", "DEV-900092"
        path, wid, first = workspace(identifier)
        second = call("pane", "split", first["pane_id"], "--direction", "down", "--cwd", str(path),
                      "--no-focus")["pane"]
        with PublicationStore(path).locked():
            launch(identifier, path, first, "implementation")
            launch(identifier, path, second, "review")
        log("Selected controller lock released; two real Codex TUIs remain foreground with completed turns")
        other, other_wid, other_pane = workspace(unrelated)
        with PublicationStore(other).locked():
            session, info = launch(unrelated, other, other_pane, "implementation", active=True)
        active_turn = turns(other, session)[0]["id"]
        release, locked = threading.Event(), threading.Event()

        def other_controller():
            with PublicationStore(other).locked():
                locked.set()
                if not release.wait(180):
                    raise AssertionError("Unrelated controller timed out")

        with ThreadPoolExecutor(max_workers=1) as pool:
            controller = pool.submit(other_controller)
            try:
                assert locked.wait(10)
                contexts = registry.list(unrelated)
                other_processes = {p["pid"]: Path("/proc", str(p["pid"]), "stat").read_text().rsplit(")", 1)[1].split()[19]
                                   for p in info["foreground_processes"]}
                issue = replace(fixtures.ISSUE, identifier=identifier, state_name="In Progress", state_type="started")
                with ExitStack() as boundaries:
                    boundaries.enter_context(patch("task_start.cleanup.never_published",
                                                   return_value=dict(remote="origin", repository="example/fixture")))
                    boundaries.enter_context(patch("task_start.cli.load_local",
                                                   return_value=LocalConfig(case.repo.parent, "fixture-placeholder")))
                    boundaries.enter_context(patch("task_start.cli.load_projects",
                                                   return_value=[Project(issue.project, case.repo.name, "main")]))
                    linear = boundaries.enter_context(patch("task_start.cli.Linear")).return_value
                    linear.get_issue.return_value = issue
                    log(cli.cleanup(identifier, force=True))
                    journal = case.git.disposal_files(identifier)[0]
                    raw = journal.read_bytes()
                    (output / "disposal.json").write_bytes(raw)
                    evidence = json.loads(raw)
                    for family in evidence["agent_retirement"]["families"].values():
                        for pid, member in family["members"].items():
                            stat = Path("/proc", pid, "stat")
                            if stat.exists():
                                fields = stat.read_text().rsplit(")", 1)[1].split()
                                assert int(fields[19]) != member["started"] or fields[0] in {"Z", "X", "x"}
                    log("Independent process check: every frozen selected process exited")
                    log(cli.cleanup(identifier, force=True))
                    assert journal.read_bytes() == raw
                    linear.start.assert_not_called()
                assert not path.exists() and not case.git.branches(identifier) and not registry.list(identifier)
                assert not any(w["workspace_id"] == wid for w in herdr.workspaces())
                del workspaces[wid]
                assert other.exists() and case.git.branches(unrelated) and registry.list(unrelated) == contexts
                assert not controller.done() and not server.release.is_set()
                assert any(w["workspace_id"] == other_wid for w in herdr.workspaces())
                assert turns(other, session)[0]["id"] == active_turn
                observed = next(p for p in identities.snapshot() if p["pane_id"] == other_pane["pane_id"])
                assert observed["terminal_id"] == other_pane["terminal_id"] and observed["agent_status"] == "working"
                after = identities.command("pane", "process-info", "--pane", other_pane["pane_id"])["process_info"]
                assert after == info
                for pid, started in other_processes.items():
                    fields = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
                    assert fields[19] == started and fields[0] not in {"Z", "X", "x"}
                log("Unrelated same-repository workspace, controller lock, context, and active Codex process/turn unchanged")
                server.release.set()
                wait_for(lambda: turns(other, session)[0]["status"] == "completed", "unrelated turn completes")
                assert turns(other, session)[0]["id"] == active_turn
                log("Unrelated Codex completed its original turn after selected cleanup and retry")
            finally:
                server.release.set()
                release.set()
            controller.result(timeout=10)
        assert revision() == before, "Checkout changed during acceptance; rerun against the final revision"
        succeeded = True
    except BaseException:
        traceback.print_exc(file=report)
        raise
    finally:
        if server:
            server.release.set()
            server.shutdown()
            server.server_close()
        try:
            # Retire linked workspaces before their permanent-checkout parent;
            # Herdr refuses closing that parent while linked children remain.
            for wid, path in sorted(workspaces.items(), key=lambda item: item[1] == case.repo):
                observed = next(w for w in Herdr(case.repo).workspaces() if w["workspace_id"] == wid)
                assert observed["worktree"]["checkout_path"] == str(path)
                call("workspace", "close", wid)
                assert not any(w["workspace_id"] == wid for w in Herdr(case.repo).workspaces())
                log("Retired disposable fixture workspace " + wid)
        except BaseException:
            case.temp._finalizer.detach()  # Preserve disposable files if retirement is uncertain.
            traceback.print_exc(file=report)
            report.close()
            raise
        case.doCleanups()
        if succeeded:
            log("LIVE_CLEANUP_PASS: two Codex TUIs stopped; selected resources gone; retry idempotent; unrelated active turn preserved; no /exit")
        report.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for dated revision and runtime evidence")
    main(parser.parse_args().output)

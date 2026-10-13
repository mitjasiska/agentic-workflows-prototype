"""Bounded stdio access to Codex's structured local session API.

Each client owns only its helper process, never the Herdr terminal or a daemon.
The client reads history and queues input for the existing terminal-owned session;
it never creates, resumes, or executes a model session itself.
"""

import json
from pathlib import Path
import re
from queue import Empty, Queue
import subprocess
from threading import Thread
import time
from uuid import UUID

from . import TaskError
from .credentials import agent_environment


def validate_readiness_only(proof, checkout):
    """Strict legacy startup evidence, not an integration turn or resume permit."""
    try:
        thread, turn, queue = proof["thread"], proof["turn"], proof["queue"]
        marker = re.fullmatch(r"Handoff readiness ([0-9a-f-]+)\. Do not use tools or modify files\. "
                              r"Reply READY, then wait for the task prompt\.", thread["preview"])
        if (not marker or str(UUID(marker[1])) != marker[1]
                or str(UUID(thread["id"])) != thread["id"]
                or Path(thread["cwd"]) != checkout or not checkout.is_absolute()
                or thread["source"] != "cli" or thread["forkedFromId"] is not None
                or thread["parentThreadId"] is not None
                or thread["status"] not in ({"type": "idle"}, {"type": "notLoaded"})
                or str(UUID(turn["id"])) != turn["id"] or turn["status"] != "completed"
                or turn["error"] is not None or turn["itemsView"] != "full"
                or not isinstance(turn["items"], list) or len(turn["items"]) != 2
                or queue["data"] != [] or queue["nextCursor"] is not None):
            raise ValueError("not a complete, unqueued readiness turn")
        user, reply = turn["items"]
        content = user["content"]
        if (user["type"] != "userMessage" or not isinstance(content, list) or len(content) != 1
                or content[0]["type"] != "text" or content[0]["text"] != thread["preview"]
                or content[0].get("text_elements", []) != []
                or reply["type"] != "agentMessage" or reply["text"] != "READY"
                or reply["phase"] != "final_answer"):
            raise ValueError("input or activity beyond readiness")
    except (KeyError, ValueError, TypeError, AttributeError):
        raise TaskError("Codex history is not provably readiness-only; integration abandonment refused") from None


class CodexRPC:
    def __init__(self, cwd):
        self.cwd = cwd
        self.process = None
        self.messages = Queue()
        self.sequence = 0

    def __enter__(self):
        try:
            self.process = subprocess.Popen(
                ["codex", "app-server", "--listen", "stdio://"], cwd=self.cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1, env=agent_environment())
            self.reader = Thread(target=self.read_messages, daemon=True)
            self.reader.start()
            self.request("initialize", {"clientInfo": {"name": "agentic_workflows", "version": "1"},
                                        "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def read_messages(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (OSError, UnicodeError, ValueError):
            pass
        finally:
            self.messages.put(None)

    def send(self, message):
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise TaskError("Codex session API connection closed") from None

    def request(self, method, params, *, timeout=30):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() >= deadline:
                raise TaskError(f"Codex session API timed out during {method}")
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except Empty:
                raise TaskError(f"Codex session API timed out during {method}") from None
            if not isinstance(message, dict):
                raise TaskError("Codex session API closed or returned malformed JSON")
            if "method" in message:
                if "id" in message:
                    # This client never grants approvals or executes requested actions.
                    raise TaskError("Codex session API unexpectedly requested client action")
                continue
            if message.get("id") != self.sequence:
                raise TaskError("Codex session API response ID mismatch")
            if "error" in message or not isinstance(message.get("result"), dict):
                # Server errors may contain configuration/task text. Do not echo them.
                raise TaskError(f"Codex session API rejected {method}; check installed CLI compatibility")
            return message["result"]

    def __exit__(self, *exc):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.reader.join(timeout=1)

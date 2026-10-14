"""Agent-independent execution contract and the supported launch adapters."""

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Callable, Mapping, Protocol
from uuid import UUID, uuid4

from . import AgentNotReady, HerdrResponseError, TaskError
from .command import command_name
from .codex_rpc import CodexRPC, validate_readiness_only
from .config import AgentConfig
from .credentials import agent_environment, agent_shell_prefix
from .linear import Issue
from .integration_process import stopped_checkout
from .sessions import SessionInvalid, has_immutable_identity, merge_session, same_session, session_identity
from .review_result import unique_object
from .workspace import Workspace, run


@dataclass(frozen=True)
class AgentOptions:
    """Resolved workflow-level choices, before adapter-specific mapping."""

    kind: str
    model: str | None = None
    mode: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", self.kind):
            raise TaskError("agent must be a lowercase name such as codex or pi")
        if (self.model is not None
                and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@*+-]*", self.model)):
            raise TaskError("model must not contain whitespace, control characters, or option syntax")
        if self.mode is not None and not re.fullmatch(r"[a-z][a-z0-9_-]*", self.mode):
            raise TaskError("mode must be a lowercase name without whitespace or control characters")


@dataclass(frozen=True)
class AgentOverrides:
    """Optional per-command choices reusable by future workflow commands."""

    kind: str | None = None
    model: str | None = None
    mode: str | None = None


@dataclass(frozen=True)
class AgentExecution:
    """The small workflow-owned request passed unchanged to an adapter."""

    issue: Issue
    repository: Path
    workspace: Workspace
    options: AgentOptions
    handoff: str
    purpose: str = "implementation"
    policy: Mapping[str, object] = field(default_factory=dict)
    # Optional workflow-owned persistence hook; never includes the semantic handoff.
    runtime_observer: Callable[[dict], None] | None = field(default=None, compare=False, repr=False)
    context_id: str | None = None
    # Persist provider identity and prompt digest BEFORE the one delivery attempt.
    delivery_observer: Callable[[dict, str, str], None] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, str) or not self.handoff.strip():
            raise TaskError("Agent execution handoff must be non-empty text")
        if "\0" in self.handoff:
            raise TaskError("Agent execution handoff contains a NUL character")
        if not isinstance(self.purpose, str) or not self.purpose.strip():
            raise TaskError("Agent execution purpose must be non-empty text")


@dataclass(frozen=True)
class LaunchResult:
    """Adapter result consumed by deterministic workflow output."""

    kind: str
    pane_id: str
    summary: str
    session_id: str | None = None
    turn_id: str | None = None
    session_kind: str | None = None
    resumability: str = "unknown"
    context_id: str | None = None


class AgentAdapter(Protocol):
    kind: str

    def check_available(self) -> None: ...

    def launch(self, execution: AgentExecution) -> LaunchResult: ...

    def verify_review_session(self, workspace: Workspace, reference: dict) -> dict: ...

    def resume_review(self, execution: AgentExecution, reference: dict, *, recreate: bool) -> LaunchResult: ...

    def review_status(self, execution: AgentExecution, terminal_id: str, reference: dict | None) -> str: ...

    def verify_session(self, workspace: Workspace, reference: dict) -> dict: ...

    def verify_stopped_session(self, workspace, reference, receipt=None): ...

    def verify_cleanup_idle(self, workspace, reference, pane): ...

    def resume(self, execution: AgentExecution, reference: dict, *, recreate: bool) -> LaunchResult: ...

    def status(self, execution: AgentExecution, terminal_id: str, reference: dict | None) -> str: ...

    def observe_delivery(self, execution, reference, receipt, completion): ...

    def verify_abandonment(self, path: Path, shell_pid: int | None, *, allow_readiness=False) -> dict: ...


def resolve_agent_options(config: AgentConfig | None, overrides: AgentOverrides, *, section: str = "agent",
                          agent_flag: str = "--agent") -> AgentOptions:
    """Resolve independent CLI-over-config choices without mutating configuration."""
    kind = overrides.kind if overrides.kind is not None else (config.kind if config else None)
    if kind is None:
        raise TaskError(f"Configure [{section}] kind in ~/.agentic-workflows-lite/config.toml or pass {agent_flag}")
    model = overrides.model if overrides.model is not None else (config.model if config else None)
    mode = overrides.mode if overrides.mode is not None else (config.mode if config else None)
    return AgentOptions(kind, model, mode)


def codex_repository_policy(profiles: Mapping[str, str], repository: str) -> dict[str, str]:
    """Translate one explicit local repository override into adapter policy."""
    profile = profiles.get(repository)
    return {"codex_profile": profile} if profile is not None else {}


class HerdrAgentAdapter:
    """Shared Herdr transport; subclasses own executable arguments and prompt injection."""

    kind = ""
    display_name = "Agent"
    SHELL_READY_TIMEOUT = 30

    def __init__(self, options: AgentOptions):
        if options.kind != self.kind:
            raise TaskError(f"{self.display_name} adapter cannot execute agent {options.kind!r}")
        self.options = options
        self.validate_options()

    def validate_options(self) -> None:
        pass

    def observe_delivery(self, execution, reference, receipt, completion):
        raise TaskError(f"{self.display_name} cannot prove this saved pass; preserve its claim for inspection")

    def check_available(self) -> None:
        if shutil.which(self.kind) is None:
            raise TaskError(f"{self.kind} is not installed or not on PATH; install/login to "
                            f"{self.display_name} or use --no-agent")

    def validate_execution(self, execution: AgentExecution) -> None:
        if execution.options != self.options:
            raise TaskError(f"{self.display_name} execution options changed after adapter selection")
        if not execution.repository.is_absolute() or not execution.workspace.path.is_absolute():
            raise TaskError("Agent execution requires an absolute repository and worktree")

    def command(self, group: str, operation: str, *args: str, timeout: float = 120) -> dict:
        output = run(["herdr", group, operation, *args], timeout=timeout)
        expected = {("pane", "list"): "pane_list", ("agent", "start"): "agent_started",
                    ("pane", "read"): "pane_read",
                    ("pane", "process-info"): "pane_process_info",
                    ("agent", "get"): "agent_info", ("agent", "prompt"): "agent_prompted"}
        try:
            payload = json.loads(output)
            result = payload["result"]
            if payload.get("error") or result["type"] != expected[group, operation]:
                raise ValueError("unexpected response")
            return result
        except (ValueError, KeyError, TypeError):
            raise HerdrResponseError(f"Unexpected Herdr {group} {operation} response") from None

    def validate_agent(self, agent: dict, workspace: Workspace, *, review: bool = False,
                       allow_absent: bool = False) -> None:
        if ((agent["agent"] != self.kind and not (allow_absent and agent["agent"] is None))
                or agent["workspace_id"] != workspace.workspace_id
                or (not review and agent["tab_id"] != workspace.tab_id) or agent["pane_id"] != workspace.pane_id
                or not Path(agent["cwd"]).is_absolute() or not Path(agent["foreground_cwd"]).is_absolute()
                or not isinstance(agent["terminal_id"], str) or not agent["terminal_id"]
                or Path(agent["cwd"]).resolve() != workspace.path
                or Path(agent["foreground_cwd"]).resolve() != workspace.path):
            raise ValueError(f"agent target mismatch: agent={agent.get('agent')!r}, "
                             f"pane={agent.get('pane_id')!r}, terminal={agent.get('terminal_id')!r}, "
                             f"cwd={agent.get('cwd')!r}, foreground_cwd={agent.get('foreground_cwd')!r}")

    def check_target(self, workspace: Workspace, *, review: bool = False) -> dict:
        panes = self.command("pane", "list", "--workspace", workspace.workspace_id)["panes"]
        if not isinstance(panes, list):
            raise ValueError("invalid panes")
        for pane in panes:
            if pane["workspace_id"] != workspace.workspace_id:
                raise ValueError("pane workspace mismatch")
            if pane.get("agent") == self.kind and not review:
                raise TaskError(f"{self.display_name} already occupies pane {pane['pane_id']}; "
                                "inspect/continue it or exit it before starting a fresh task session. "
                                "Use --no-agent to focus the workspace without launching a duplicate")
        targets = [pane for pane in panes if pane["pane_id"] == workspace.pane_id]
        if (len(targets) != 1 or (not review and targets[0]["tab_id"] != workspace.tab_id)
                or targets[0].get("agent")):
            raise ValueError("confirmed pane is no longer present")
        return targets[0]

    def agent_name(self, workspace: Workspace) -> str:
        # A bounded unique name, independent of issue title and shell syntax.
        return "task-" + hashlib.sha256(
            f"{workspace.path}:{workspace.pane_id}".encode()).hexdigest()[:20]

    def start_agent(self, workspace: Workspace, args: list[str], *, review: bool = False) -> dict:
        self.filter_shell_credentials(workspace)
        result = self.command("agent", "start", self.agent_name(workspace),
                              "--kind", self.kind, "--pane", workspace.pane_id,
                              "--timeout", "30000", "--", *args)
        self.validate_agent(result["agent"], workspace, review=review)
        if result["argv"] != [self.kind, *args]:
            raise ValueError(f"{self.display_name} launch/readiness not confirmed: "
                             f"status={result['agent']['agent_status']!r}, argv_match={result['argv'] == [self.kind, *args]}")
        if result["agent"]["agent_status"] not in {"idle", "done", "working"}:
            raise AgentNotReady(f"{self.display_name} launch/readiness not confirmed: "
                                f"status={result['agent']['agent_status']!r}", agent=result["agent"])
        return result["agent"]

    def filter_shell_credentials(self, workspace: Workspace) -> None:
        run(["herdr", "pane", "send-text", workspace.pane_id, agent_shell_prefix()],
            env=agent_environment())

    def clear_shell_input(self, workspace: Workspace) -> None:
        # A prior interrupted launch can leave an executable fragment in the
        # reusable shell's input buffer. Cancel it before adding the credential
        # filter and Herdr's agent command (for example, stale `pi` + `codex`).
        # Pane creation/labeling can finish while shell startup children (e.g.
        # lesspipe) still occupy its foreground group. Wait for the same strict
        # shell-only predicate before sending any input. Never retry input/start.
        deadline = time.monotonic() + self.SHELL_READY_TIMEOUT
        established_shell = None
        last = "no completed process observation"

        def timeout_error():
            return TaskError(f"Timed out waiting for an interactive shell prompt before launch; last observed {last}")

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error()
            try:
                process = self.command("pane", "process-info", "--pane", workspace.pane_id,
                                       timeout=remaining)["process_info"]
            except TaskError:
                if time.monotonic() >= deadline:
                    raise timeout_error() from None
                raise
            foreground = process["foreground_processes"]
            shell_pid = process["shell_pid"]
            group = process["foreground_process_group_id"]
            if (process["pane_id"] != workspace.pane_id
                    or any(value is not None and (type(value) is not int or value <= 0)
                           for value in (shell_pid, group))
                    or not isinstance(foreground, list)
                    or any(not isinstance(p, dict) or type(p.get("pid")) is not int or p["pid"] <= 0
                           for p in foreground)):
                raise ValueError("invalid or mismatched shell process observation")
            last = (f"shell_pid={shell_pid}, foreground_group={group}, "
                    f"foreground_pids={[p['pid'] for p in foreground]}")
            # Transport/process scheduling can return even a ready observation
            # after its timeout. Never accept it or send input beyond our bound.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error()
            if established_shell is not None and shell_pid != established_shell:
                raise TaskError(f"Shell identity changed during pane readiness: {last}")
            established_shell = shell_pid
            if shell_pid is not None and group == shell_pid and [p["pid"] for p in foreground] == [shell_pid]:
                break
            time.sleep(min(0.25, remaining))
        run(["herdr", "pane", "send-keys", workspace.pane_id, "ctrl+c"])

    def observe_agent(self, execution: AgentExecution, agent: dict,
                      previous: dict | None = None, *, expected_session=None, allow_absent=False) -> dict:
        """Validate every observation and persist a newly discovered reference immediately."""
        self.validate_agent(agent, execution.workspace, review=execution.purpose in {"review", "integration"}, allow_absent=allow_absent)
        reference = agent.get("agent_session")
        identity = session_identity(reference, self.kind)
        established = None
        if previous is not None:
            if agent["terminal_id"] != previous["terminal_id"]:
                raise TaskError("agent terminal changed during handoff")
            established = previous.get("_session_reference", previous.get("agent_session"))
            if established is not None and not same_session(established, reference, self.kind):
                raise TaskError("agent session changed or is no longer reported during handoff")
        if reference is not None and expected_session is not None:
            if not same_session(expected_session, reference, self.kind):
                raise TaskError("agent session does not match the provider session during handoff")
        # Keep known identity fields even if a later legacy report omits the kind.
        # The raw report (including source) is persisted separately below.
        if identity is not None:
            established = merge_session(established or expected_session, reference, self.kind)
        if execution.runtime_observer:
            values = dict(terminal_id=agent["terminal_id"])
            if identity is not None:
                values.update(herdr_session=json.dumps(reference), session_id=established["value"],
                              session_kind=established["kind"])
            execution.runtime_observer(values)
        return dict(agent, _session_reference=established)

    def confirm_target(self, execution: AgentExecution, previous: dict, *, expected_session=None) -> dict:
        agent = self.command("agent", "get", execution.workspace.pane_id)["agent"]
        return self.observe_agent(execution, agent, previous, expected_session=expected_session)

    def verify_review_session(self, workspace: Workspace, reference: dict) -> dict:
        raise TaskError(f"{self.display_name} cannot safely resume this reviewer session")

    def verify_abandonment(self, path: Path, shell_pid: int | None, *, allow_readiness=False) -> dict:
        raise TaskError(f"Explicit integration abandonment is unsupported for {self.display_name}; inspect manually")

    # Implementation follow-ups use the same exact-session transport as DEV-20.
    # Keep the review entry points compatible with existing callers/adapters.
    def verify_session(self, workspace: Workspace, reference: dict) -> dict:
        return self.verify_review_session(workspace, reference)

    def verify_stopped_session(self, workspace, reference, receipt=None):
        raise TaskError(f"Stopped-session verification is unsupported for {self.display_name}")

    def verify_cleanup_idle(self, workspace, reference, pane):
        """Positive provider inactivity is permission to close, never exit proof."""
        try:
            self.validate_agent(pane, workspace, review=True)
            if (not has_immutable_identity(reference, self.kind)
                    or not same_session(reference, pane.get("agent_session"), self.kind)
                    or pane.get("launch_pending") or pane.get("agent_status") not in {"idle", "done"}):
                raise ValueError("unknown session or active turn")
            self.cleanup_provider_idle(workspace, reference)
        except (KeyError, TypeError, ValueError, TaskError) as error:
            raise TaskError(f"Pane {workspace.pane_id} provider turn is working or unverified; "
                            f"let it finish or explicitly stop it before --force cleanup. {error}") from None

    def cleanup_provider_idle(self, workspace, reference):
        raise TaskError(f"{self.display_name} has no verified idle-provider cleanup capability")

    def resume(self, execution: AgentExecution, reference: dict, *, recreate: bool) -> LaunchResult:
        return self.resume_review(execution, reference, recreate=recreate)

    def status(self, execution: AgentExecution, terminal_id: str, reference: dict | None) -> str:
        return self.review_status(execution, terminal_id, reference)

    def resume_args(self, execution: AgentExecution, reference: dict) -> list[str]:
        raise TaskError(f"{self.display_name} does not support explicit reviewer resume")

    def review_status(self, execution: AgentExecution, terminal_id: str, reference: dict | None) -> str:
        try:
            agent = self.command("agent", "get", execution.workspace.pane_id)["agent"]
        except (TaskError, KeyError, TypeError):
            return "unknown"  # Unavailable observation is not proof of completion.
        if not isinstance(agent, dict):
            return "unknown"
        try:
            if agent["terminal_id"] != terminal_id:
                raise ValueError("terminal changed")
            # A known conflicting reference invalidates identity. Missing metadata
            # during fresh startup is allowed: the pass nonce still binds output.
            if reference and agent.get("agent_session") is not None and not same_session(
                    reference, agent["agent_session"], self.kind):
                raise ValueError("session changed")
            observed_execution = execution
            relocated = agent.get("pane_id") != execution.workspace.pane_id
            if relocated:
                if not isinstance(agent.get("pane_id"), str) or not agent["pane_id"]:
                    return "unknown"
                # A move can race the preceding registry snapshot. Validate all
                # other identity fields now; let the next snapshot prove unique
                # placement and persist it before accepting any result.
                observed_execution = replace(execution, workspace=replace(execution.workspace, pane_id=agent["pane_id"]))
            self.observe_agent(observed_execution, agent, expected_session=reference, allow_absent=True)
            status = agent.get("agent_status")
            if relocated or agent.get("agent") is None or agent.get("launch_pending"):
                return "unknown"
            if status not in {"idle", "done", "working", "blocked"}:
                return "unknown"
            return status
        except (KeyError, TypeError, ValueError):
            raise TaskError("Reviewer runtime identity/status changed or is unknown; inspect its pane") from None

    def resume_review(self, execution: AgentExecution, reference: dict, *, recreate: bool) -> LaunchResult:
        self.validate_execution(execution)
        reference = self.verify_review_session(execution.workspace, reference)
        if execution.runtime_observer:
            execution.runtime_observer(dict(herdr_session=json.dumps(reference)))
        try:
            if recreate:
                self.check_target(execution.workspace, review=True)
                self.clear_shell_input(execution.workspace)
                self.start_agent(execution.workspace, self.resume_args(execution, reference), review=True)
            observed = self.command("agent", "get", execution.workspace.pane_id)["agent"]
            self.validate_agent(observed, execution.workspace, review=True)
            if (not same_session(reference, observed.get("agent_session"), self.kind)
                    or observed["agent_status"] not in {"idle", "done"}):
                raise TaskError("Exact reviewer session is not idle and confirmed; no prompt was submitted")
            observed = self.observe_agent(execution, observed, expected_session=reference)
            # Authenticate the observed provider conversation AFTER startup. A
            # path-based provider may have recreated an empty file during launch.
            self.verify_review_session(execution.workspace, reference)
            if execution.delivery_observer:
                execution.delivery_observer(reference, str(uuid4()), execution.handoff)
            prompted = self.command("agent", "prompt", execution.workspace.pane_id, execution.handoff)["agent"]
            observed = self.observe_agent(execution, prompted, observed, expected_session=reference)
            self.confirm_target(execution, observed, expected_session=reference)
            return LaunchResult(self.kind, execution.workspace.pane_id, "Reviewer follow-up submitted",
                                reference["value"], session_kind=reference["kind"], resumability="yes")
        except (KeyError, TypeError, ValueError):
            raise TaskError("Reviewer resume identity or delivery was not confirmed; inspect its pane") from None


class CodexSessionNotObserved(TaskError):
    """Only a bounded, valid observation with no matching readiness session."""


class CodexAdapter(HerdrAgentAdapter):
    """Codex TUI adapter with durable app-server prompt receipt confirmation."""

    kind = "codex"
    display_name = "Codex"
    MODES = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    POST_TRUST_READY_TIMEOUT = 30

    def cleanup_provider_idle(self, workspace, reference):
        """Read current provider state and queue, including a prompt with no turns."""
        try:
            if reference["kind"] != "id" or str(UUID(reference["value"])) != reference["value"]:
                raise ValueError("invalid Codex session")
            with CodexRPC(workspace.path) as rpc:
                thread = rpc.request("thread/read", dict(threadId=reference["value"]))["thread"]
                if (thread["id"] != reference["value"] or Path(thread["cwd"]).resolve() != workspace.path
                        or thread.get("forkedFromId") is not None or thread.get("parentThreadId") is not None
                        or thread["status"] not in ({"type": "idle"}, {"type": "notLoaded"})):
                    raise ValueError("different or active provider thread")
                page = rpc.request("thread/turns/list", dict(threadId=reference["value"],
                    sortDirection="desc", limit=1, itemsView="full"))
                if (not isinstance(page["data"], list) or len(page["data"]) > 1
                        or not page["data"] and page["nextCursor"] is not None
                        or any(t["status"] not in {"completed", "interrupted", "failed"}
                               or t["itemsView"] != "full" for t in page["data"])):
                    raise ValueError("latest turn is working or uncertain")
                queue = rpc.request("thread/queue/list", dict(threadId=reference["value"], limit=1))
                if queue["data"] != [] or queue["nextCursor"] is not None:
                    raise ValueError("queued input")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TaskError("Codex has an active/unknown turn, changed session, or queued input") from None

    def validate_options(self) -> None:
        mode = "none" if self.options.mode == "off" else self.options.mode
        if mode is not None and mode not in self.MODES:
            raise TaskError("Codex mode must be none/off, minimal, low, medium, high, xhigh, max, or ultra")

    def validate_execution(self, execution: AgentExecution) -> None:
        super().validate_execution(execution)
        profile = execution.policy.get("codex_profile")
        if (profile is not None
                and (not isinstance(profile, str)
                     or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", profile))):
            raise TaskError("Codex profile must be a portable name without path or option syntax")

    def launch_args(self, execution: AgentExecution, bootstrap: str) -> list[str]:
        workspace = execution.workspace
        args = ["--cd", str(workspace.path)]
        profile = execution.policy.get("codex_profile")
        if profile is not None:
            args.extend(["--profile", profile])
        if self.options.model is not None:
            args.extend(["--model", self.options.model])
        if self.options.mode is not None:
            mode = "none" if self.options.mode == "off" else self.options.mode
            args.extend(["--config", "model_reasoning_effort=" + json.dumps(mode)])
        return [*args, "--", bootstrap]

    def verify_abandonment(self, path: Path, shell_pid: int | None, *, allow_readiness=False) -> dict:
        before = stopped_checkout(path, shell_pid)
        proof = dict(provider="codex", checkout=str(path), process=before, history="absent")
        try:
            with CodexRPC(path) as rpc:
                def list_threads():
                    threads = []
                    for archived in (False, True):
                        result = rpc.request("thread/list", dict(cwd=str(path), limit=1, archived=archived,
                            modelProviders=[], sourceKinds=["cli", "vscode", "exec", "appServer", "subAgent",
                                "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]))
                        if (not isinstance(result["data"], list) or result["nextCursor"] is not None
                                or (result["data"] and (archived or not allow_readiness))):
                            raise TaskError("Codex history may be resumable; integration abandonment refused")
                        threads.extend(result["data"])
                    return threads
                threads = list_threads()
                if threads:
                    if len(threads) != 1 or not allow_readiness:
                        raise TaskError("Codex history is ambiguous; integration abandonment refused")
                    candidate = threads[0]
                    if Path(candidate["cwd"]) != path or str(UUID(candidate["id"])) != candidate["id"]:
                        raise ValueError("different readiness checkout/identity")
                    def observe_startup():
                        # Explicitly inspect the complete turn and durable queue:
                        # absent task receipts alone cannot prove no pending delivery.
                        thread = rpc.request("thread/read", {"threadId": candidate["id"]})["thread"]
                        turns = rpc.request("thread/turns/list", dict(threadId=candidate["id"],
                            sortDirection="asc", limit=2, itemsView="full"))
                        queue = rpc.request("thread/queue/list", dict(threadId=candidate["id"], limit=1))
                        if (thread["id"] != candidate["id"] or thread["preview"] != candidate["preview"]
                                or not isinstance(turns["data"], list) or len(turns["data"]) != 1
                                or turns["nextCursor"] is not None):
                            raise ValueError("incomplete or changed readiness history")
                        evidence = dict(thread={k: thread[k] for k in ("id", "cwd", "preview", "source",
                                        "forkedFromId", "parentThreadId", "status")}, turn=turns["data"][0], queue=queue)
                        validate_readiness_only(evidence, path)
                        return evidence
                    startup = observe_startup()
                    if observe_startup() != startup:
                        raise TaskError("Codex readiness history changed during abandonment checks")
                    # A second conversation appearing during these checks is not
                    # covered by the single readiness turn we just verified.
                    final_threads = list_threads()
                    if (len(final_threads) != 1
                            or any(final_threads[0][k] != candidate[k] for k in ("id", "cwd", "preview"))):
                        raise TaskError("Codex history identity changed during abandonment checks")
                    proof.update(history="readiness_only", startup=startup)
            if stopped_checkout(path, shell_pid) != before:
                raise TaskError("Integration process identity changed during abandonment checks")
        except (KeyError, TypeError, ValueError, AttributeError, OSError):
            raise TaskError("Cannot prove absence of Codex history; integration abandonment refused") from None
        return proof

    def verify_review_session(self, workspace: Workspace, reference: dict) -> dict:
        try:
            if reference["kind"] != "id" or str(UUID(reference["value"])) != reference["value"]:
                raise ValueError("not an exact Codex ID")
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SessionInvalid("Codex reviewer history is missing or mismatched; cannot resume") from None
        try:
            with CodexRPC(workspace.path) as rpc:
                thread = rpc.request("thread/read", {"threadId": reference["value"]})["thread"]
            if (not isinstance(thread["id"], str) or not thread["id"]
                    or not isinstance(thread["cwd"], str) or not Path(thread["cwd"]).is_absolute()):
                raise ValueError("unusable provider response")
            cwd = Path(thread["cwd"]).resolve()
        except (KeyError, ValueError, TypeError, OSError):
            # Missing/malformed API responses and transport failures do not prove
            # deletion. They still stop explicit resume before any handoff.
            raise TaskError("Codex reviewer history verification is temporarily unavailable; retry before resuming") from None
        if thread["id"] != reference["value"] or cwd != workspace.path:
            raise SessionInvalid("Codex reviewer history is missing or mismatched; cannot resume")
        return dict(reference)

    def resume_args(self, execution: AgentExecution, reference: dict) -> list[str]:
        return ["resume", *self.launch_args(execution, "")[:-2], "--", reference["value"]]

    def resume_review(self, execution: AgentExecution, reference: dict, *, recreate: bool) -> LaunchResult:
        """Queue to the exact persisted thread, including when Herdr omits its ID.

        The durable input receipt establishes the recipient; no terminal keystroke
        can accidentally deliver this follow-up to a different conversation.
        """
        self.validate_execution(execution)
        self.verify_review_session(execution.workspace, reference)
        thread_id = reference["value"]
        try:
            if recreate:
                self.check_target(execution.workspace, review=True)
                self.clear_shell_input(execution.workspace)
                self.start_agent(execution.workspace, self.resume_args(execution, reference), review=True)
            observed = self.command("agent", "get", execution.workspace.pane_id)["agent"]
            observed = self.observe_agent(execution, observed, expected_session=reference)
            if observed["agent_status"] not in {"idle", "done"}:
                raise TaskError("Reviewer is not idle; no follow-up was queued")
            message_id = str(uuid4())
            inputs = [{"type": "text", "text": execution.handoff, "text_elements": []}]
            if execution.delivery_observer:
                execution.delivery_observer(reference, message_id, execution.handoff)
            with CodexRPC(execution.workspace.path) as rpc:
                queued = rpc.request("thread/queue/add", {"threadId": thread_id,
                    "clientUserMessageId": message_id, "input": inputs})["queuedSubmission"]
                if (queued["clientUserMessageId"] != message_id or queued["input"] != inputs
                        or not isinstance(queued["id"], str) or not queued["id"]):
                    raise ValueError("queue acknowledgement mismatch")
                turn = self.confirm_prompt(rpc, execution.workspace, thread_id, message_id, execution.handoff)
            self.confirm_target(execution, observed, expected_session=reference)
            return LaunchResult(self.kind, execution.workspace.pane_id, "Exact reviewer follow-up confirmed",
                                thread_id, turn, "id", "yes")
        except (KeyError, ValueError, TypeError, OSError):
            raise TaskError("Codex reviewer continuation was not confirmed; inspect the mapped pane/session") from None

    def startup_command(self, group, operation, *args, deadline):
        """Retry malformed observation envelopes only, inside the pre-queue budget.

        Callers must still verify process, terminal, session and provider history.
        Invalid identity fields are not transient and never reach this retry.
        """
        last_error = None
        while True:
            try:
                remaining = self.startup_remaining(deadline)
            except TaskError as expired:
                if last_error is None:
                    raise
                raise TaskError(f"{expired}; last observation: {last_error}") from None
            try:
                result = self.command(group, operation, *args, timeout=remaining)
            except HerdrResponseError as error:
                last_error = error
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                continue
            self.startup_remaining(deadline)
            return result

    def startup_process(self, workspace: Workspace, args: list[str], *, deadline) -> tuple[int, int, int]:
        """Bind a delayed launch to its exact native argv, checkout and OS process.

        This is process evidence only; it never establishes a provider session or
        authorizes a prompt. Platforms without argv/cwd evidence fail closed.
        """
        info = self.startup_command("pane", "process-info", "--pane", workspace.pane_id,
                                    deadline=deadline)["process_info"]
        processes = info["foreground_processes"]
        if (info["pane_id"] != workspace.pane_id or not isinstance(processes, list)
                or any(not isinstance(p, dict) for p in processes)):
            raise ValueError("invalid startup process observation")
        matches = [p for p in processes if isinstance(p.get("argv"), list)
                   and p["argv"] and Path(p["argv"][0]).name == "codex" and p["argv"][1:] == args]
        if len(matches) != 1:
            raise TaskError("Cannot prove the original Codex startup process; inspect the pane")
        process = matches[0]
        identity = (info["shell_pid"], info["foreground_process_group_id"], process["pid"])
        if (any(type(pid) is not int or pid <= 0 for pid in identity)
                or not Path(process["cwd"]).is_absolute()
                or Path(process["cwd"]).resolve() != workspace.path):
            raise ValueError("startup process identity/checkout mismatch")
        return identity

    def startup_blocker(self, workspace: Workspace, *, deadline) -> str | None:
        # Use only the visible viewport, never scrollback or a loose word search.
        # These signatures match Codex's onboarding widgets. Unknown versions,
        # clipped menus and later approval dialogs are deliberately unsupported.
        view = self.startup_command("pane", "read", workspace.pane_id, "--source", "visible", "--format", "text",
                                    deadline=deadline)["read"]
        if (any(view[k] != v for k, v in dict(pane_id=workspace.pane_id,
                workspace_id=workspace.workspace_id, tab_id=workspace.tab_id,
                source="visible", format="text").items())
                or view["truncated"] is not False or not isinstance(view["text"], str)):
            raise ValueError("invalid startup viewport observation")
        lines = [line.strip() for line in view["text"].splitlines()]

        def option(number, text):
            return any(re.fullmatch(r"[›>❯]?\s*" + str(number) + r"\.\s*" + re.escape(text), line)
                       for line in lines)

        if (any(line.startswith("Trust this folder?") for line in lines)
                and "Trust this folder? Codex can read, edit, and run files here" in " ".join(lines)
                and "Folder access" in lines and option(1, "Trust and continue") and option(2, "Quit")):
            return "repository trust"
        if (option(1, "Sign in with ChatGPT") and option(2, "Sign in with Device Code")
                and (option(3, "Use an OpenAI API key") or option(3, "Provide your own API key"))):
            return "authentication setup"
        return None

    def wait_for_startup_action(self, execution: AgentExecution, observed: dict, blocker: str) -> None:
        """Keep the owning operation, handoff and temporary result paths alive.

        Enter is sent to this workflow's stdin, never to the Codex pane. A closed
        or noninteractive caller cannot retain a safe continuation and fails closed.
        """
        reference = observed.get("_session_reference")
        session = (f"provider session observed: {reference['value']}" if reference is not None
                   else "provider session not yet observed")
        message = (f"{execution.context_id or execution.issue.identifier} ({execution.purpose}): "
                   f"Codex is waiting for user {blocker}.\nCheckout: {execution.workspace.path}\n"
                   f"Workspace: {execution.workspace.workspace_id}; pane: {execution.workspace.pane_id}; "
                   f"terminal: {observed['terminal_id']}\n"
                   f"Process verified; {session}; task delivery not attempted.\n"
                   "Handle trust/setup yourself in that Codex pane. Keep this workflow command running; "
                   "return here and press Enter to reconcile the same launch. Do not rerun the task command.\n"
                   "If this command ends, inspect the retained context; automatic recovery is unavailable.")
        print("".join(c if c.isprintable() or c == "\n" else "?" for c in message), file=sys.stderr, flush=True)
        if not sys.stdin.isatty():
            raise TaskError("User action requires the original interactive workflow command; "
                            "stdin is not a terminal. Context retained; inspect it, do not relaunch")
        if sys.stdin.readline() == "":
            raise TaskError("Workflow input closed during user action; context retained for inspection")

    @staticmethod
    def startup_remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TaskError("Timed out reconciling Codex readiness during startup; "
                            "inspect the retained context. No task prompt was sent")
        return remaining

    def reconcile_startup(self, execution, args, process, observed, deadline, *, expected_session=None):
        """Poll only observations of the original runtime within one shared budget."""
        workspace = execution.workspace
        while True:
            if self.startup_process(workspace, args, deadline=deadline) != process:
                raise TaskError("Codex startup process changed during user action; no task prompt was sent")
            observed = self.observe_agent(execution, self.startup_command("agent", "get", workspace.pane_id,
                deadline=deadline)["agent"], observed, expected_session=expected_session)
            blocker = self.startup_blocker(workspace, deadline=deadline)
            remaining = self.startup_remaining(deadline)  # Reject even ready observations returned late.
            if blocker is not None or observed["agent_status"] in {"idle", "done", "working"}:
                return observed, blocker
            if observed["agent_status"] not in {"blocked", "unknown"}:
                raise TaskError("Codex runtime state is unsupported after user action; no task prompt was sent")
            time.sleep(min(0.25, remaining))

    def recover_startup(self, execution: AgentExecution, args: list[str], previous=None, *, allow_ready=False) -> tuple[dict, tuple[int, int, int], float]:
        """Observation and human reconciliation only, reachable strictly pre-queue."""
        workspace = execution.workspace
        deadline = time.monotonic() + self.POST_TRUST_READY_TIMEOUT
        observed = self.observe_agent(execution, self.startup_command("agent", "get", workspace.pane_id,
            deadline=deadline)["agent"], previous)
        process = self.startup_process(workspace, args, deadline=deadline)
        while True:
            blocker = self.startup_blocker(workspace, deadline=deadline)
            if allow_ready and blocker is None and observed["agent_status"] in {"idle", "done", "working"}:
                # Trust may finish while Herdr's response is unavailable. This
                # only advances to provider/history proof, never straight to queue.
                return observed, process, deadline
            if blocker is None or observed["agent_status"] not in {"blocked", "unknown", "idle"}:
                raise TaskError("Codex startup blocker is ambiguous or unsupported; inspect the exact pane/session. "
                                "No task prompt was sent")
            # A session may already be known before setup finishes. Keep it
            # bound across every observation, including the human wait.
            observed = self.observe_agent(execution, self.startup_command("agent", "get", workspace.pane_id,
                deadline=deadline)["agent"], observed)
            if self.startup_process(workspace, args, deadline=deadline) != process:
                raise TaskError("Codex startup identity changed during blocker inspection")
            if execution.runtime_observer:
                execution.runtime_observer(dict(state="awaiting_user"))
            self.wait_for_startup_action(execution, observed, blocker)
            # Acknowledgement is not readiness. Runtime detection can lag behind
            # trust acceptance; preserve identity while polling, never restart.
            deadline = time.monotonic() + self.POST_TRUST_READY_TIMEOUT
            observed, blocker = self.reconcile_startup(execution, args, process, observed, deadline)
            if blocker is None:
                return observed, process, deadline

    def confirm_no_task_input(self, rpc, thread_id, bootstrap, *, deadline=None):
        """After a human boundary, refuse any input beyond our native readiness turn.

        This call path has never attempted queue/add. Combined with exact provider
        history, that permits its first delivery only; missing receipts after a
        queue attempt elsewhere never reach this recovery path.
        """
        cursor, seen, users = None, set(), 0
        deadline = time.monotonic() + 30 if deadline is None else deadline
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaskError("Codex history reconciliation timed out; no task prompt was sent")
            params = {"threadId": thread_id, "sortDirection": "asc", "limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            page = rpc.request("thread/items/list", params, timeout=remaining)
            if not isinstance(page["data"], list):
                raise ValueError("invalid Codex history")
            for entry in page["data"]:
                item = entry["item"]
                if item["type"] == "userMessage":
                    users += 1
                    content = item["content"]
                    if (users != 1 or len(content) != 1 or content[0]["type"] != "text"
                            or content[0]["text"] != bootstrap):
                        raise TaskError("Codex has additional input after user action; task delivery is uncertain. "
                                        "Inspect the session; no task prompt will be replayed")
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen or len(seen) >= 1000:
                raise ValueError("ambiguous Codex history pagination")
            seen.add(cursor)
        if users != 1:
            raise TaskError("Codex readiness history disappeared after user action; no task prompt was sent")
        queue = rpc.request("thread/queue/list", dict(threadId=thread_id, limit=1),
                            timeout=self.startup_remaining(deadline))
        if queue["data"] != [] or queue["nextCursor"] is not None:
            raise TaskError("Codex has pending or uncertain input during startup; inspect the session. "
                            "No task prompt will be replayed")

    def launch(self, execution: AgentExecution) -> LaunchResult:
        self.validate_execution(execution)
        workspace = execution.workspace
        prompt = execution.handoff
        phase = "pane readiness (before launch)"
        thread_id = None
        recovered = False
        startup_identity = None
        startup_deadline = None
        queue_attempted = False

        def recovery_guidance():
            delivery = ("Delivery may have occurred; never resend the task prompt." if queue_attempted else
                        "No task prompt was sent by this launch; READY alone is not delivery evidence.")
            return (f"Workspace {workspace.workspace_id} left intact. {delivery} "
                    f"Inspect {command_name()} contexts {execution.issue.identifier} --all and the exact pane/session. "
                    "For an interrupted loop, quit the agent and its jobs to the original shell, keep the pane open, "
                    f"then run {command_name()} loop {execution.issue.identifier} --abort from outside the checkout.")
        try:
            original_pane = self.check_target(workspace, review=execution.purpose in {"review", "integration"})
            self.clear_shell_input(workspace)
            # A native, single-line readiness turn survives Codex startup dialogs.
            # Its nonce binds the returned Codex thread to this precise launch.
            message_id = str(uuid4())
            bootstrap = (f"Handoff readiness {message_id}. Do not use tools or modify files. "
                         "Reply READY, then wait for the task prompt.")
            args = self.launch_args(execution, bootstrap)
            # Verify the receipt API can initialize before starting a terminal agent.
            phase = "receipt API initialization (before launch)"
            with CodexRPC(workspace.path) as rpc:
                phase = "startup launch/runtime confirmation"
                try:
                    observed = self.observe_agent(execution, self.start_agent(workspace, args, review=execution.purpose in {"review", "integration"}), original_pane)
                except (AgentNotReady, HerdrResponseError) as error:
                    # Persist the validated launch report before any later read
                    # can omit or conflict with its provider/terminal identity.
                    report = error.agent if isinstance(error, AgentNotReady) else None
                    observed = self.observe_agent(execution, report, original_pane) if report is not None else original_pane
                    if observed and observed.get("_session_reference") is not None:
                        thread_id = observed["_session_reference"]["value"]
                    observed, startup_identity, startup_deadline = self.recover_startup(execution, args, observed, allow_ready=True)
                    recovered = True
                phase = "readiness confirmation"
                try:
                    thread_id = self.find_session(rpc, workspace, bootstrap, deadline=startup_deadline)
                except CodexSessionNotObserved as error:
                    if recovered:
                        raise
                    try:
                        observed, startup_identity, startup_deadline = self.recover_startup(execution, args, observed)
                    except (TaskError, KeyError, ValueError, TypeError, OSError) as recovery_error:
                        raise TaskError(f"{error}. Startup inspection: {recovery_error}") from None
                    recovered = True
                    thread_id = self.find_session(rpc, workspace, bootstrap, deadline=startup_deadline)
                provider_session = dict(agent=self.kind, kind="id", value=thread_id)
                if observed.get("agent_session") is not None and not same_session(
                        observed["agent_session"], provider_session, self.kind):
                    raise TaskError("agent session does not match the provider session during handoff")
                if execution.runtime_observer:
                    execution.runtime_observer(dict(session_id=thread_id, session_kind="id"))
                self.confirm_prompt(rpc, workspace, thread_id, None, bootstrap, deadline=startup_deadline)
                # Unlike Herdr metadata, the separate receipt API has now read
                # this exact session's persisted readiness turn.
                if execution.runtime_observer:
                    execution.runtime_observer(dict(resumability="yes"))
                if not recovered:
                    try:
                        observed = self.confirm_target(execution, observed, expected_session=provider_session)
                    except HerdrResponseError:
                        observed, startup_identity, startup_deadline = self.recover_startup(execution, args, observed, allow_ready=True)
                        recovered = True
                if recovered:
                    while True:
                        observed, blocker = self.reconcile_startup(execution, args, startup_identity, observed,
                            startup_deadline, expected_session=provider_session)
                        if blocker is not None:
                            raise TaskError("Codex setup reappeared after provider discovery; inspect the session. "
                                            "No task prompt was sent")
                        self.confirm_no_task_input(rpc, thread_id, bootstrap, deadline=startup_deadline)
                        if self.startup_process(workspace, args, deadline=startup_deadline) != startup_identity:
                            raise TaskError("Codex startup process/readiness changed before delivery; no task prompt was sent")
                        observed = self.observe_agent(execution, self.startup_command("agent", "get", workspace.pane_id,
                            deadline=startup_deadline)["agent"], observed,
                            expected_session=provider_session)
                        remaining = self.startup_remaining(startup_deadline)
                        if observed["agent_status"] in {"idle", "done", "working"}:
                            break
                        if observed["agent_status"] not in {"blocked", "unknown"}:
                            raise TaskError("Codex runtime state is unsupported before delivery; no task prompt was sent")
                        # If readiness regressed during verification, recheck
                        # history too: input may have appeared while waiting.
                        time.sleep(min(0.25, remaining))
                    if execution.runtime_observer:
                        execution.runtime_observer(dict(state="launching"))
                    self.startup_remaining(startup_deadline)
                phase = "prompt queue"
                inputs = [{"type": "text", "text": prompt, "text_elements": []}]
                if execution.delivery_observer:
                    execution.delivery_observer(provider_session, message_id, execution.handoff)
                queue_attempted = True
                queued = rpc.request("thread/queue/add", {"threadId": thread_id,
                                     "clientUserMessageId": message_id, "input": inputs})["queuedSubmission"]
                if (queued["clientUserMessageId"] != message_id or queued["input"] != inputs
                        or not isinstance(queued["id"], str) or not queued["id"]):
                    raise ValueError("Codex queue acknowledgement mismatch")
                phase = "prompt delivery/start"
                turn_id = self.confirm_prompt(rpc, workspace, thread_id, message_id, prompt)
            self.confirm_target(execution, observed, expected_session=provider_session)
            summary = f"Codex turn {turn_id} confirmed in {workspace.pane_id} (session {thread_id})"
            return LaunchResult(self.kind, workspace.pane_id, summary, thread_id, turn_id, "id", "yes")
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise TaskError(f"Codex {phase} was not confirmed in pane {workspace.pane_id}; "
                            f"last check: {error}. Inspect it before retrying. Session: {thread_id or 'not yet observed'}. "
                            f"{recovery_guidance()}") from None
        except TaskError as error:
            # A timeout may follow successful delivery. Never auto-resubmit or
            # kill a possibly working agent, and never create a fallback session.
            raise TaskError(f"Codex {phase} failed in pane {workspace.pane_id}: {error}. "
                            f"Session: {thread_id or 'not yet observed'}. "
                            f"{recovery_guidance()}") from None

    def verify_stopped_session(self, workspace, reference, receipt=None):
        verified = self.verify_session(workspace, reference)
        try:
            with CodexRPC(workspace.path) as rpc:
                thread = rpc.request("thread/read", {"threadId": reference["value"]})["thread"]
                if (thread["id"] != reference["value"] or Path(thread["cwd"]).resolve() != workspace.path
                        or thread.get("forkedFromId") is not None or thread.get("parentThreadId") is not None):
                    raise ValueError("different thread")
                queue = rpc.request("thread/queue/list", dict(threadId=reference["value"], limit=1))
                page = rpc.request("thread/turns/list", dict(threadId=reference["value"],
                    sortDirection="desc", limit=1, itemsView="full"))
                if queue["data"] != [] or queue["nextCursor"] is not None:
                    raise ValueError("pending input")
                if not isinstance(page["data"], list) or len(page["data"]) != 1:
                    raise ValueError("missing latest turn")
                turn = page["data"][0]
                if turn["status"] not in {"completed", "interrupted", "failed"} or turn["itemsView"] != "full":
                    raise ValueError("possibly running or unverifiable turn")
                if receipt:
                    users = [item for item in turn["items"] if item["type"] == "userMessage"]
                    delivered = (len(users) == 1 and users[0]["clientId"] == receipt["message_id"]
                        and len(users[0]["content"]) == 1 and users[0]["content"][0]["type"] == "text"
                        and hashlib.sha256(users[0]["content"][0]["text"].encode()).hexdigest() == receipt["prompt_sha256"])
                    if not delivered:
                        # A receipt is persisted before submission. Absence from
                        # the latest turn is insufficient: inspect every full
                        # turn and the durable queue twice, without sending input.
                        before = self.prove_not_delivered(rpc, reference, receipt, page)
                        if self.prove_not_delivered(rpc, reference, receipt) != before:
                            raise ValueError("history changed during non-delivery proof")
            return verified
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TaskError("Codex stopped-turn evidence is ambiguous, live, or has pending input; abort refused") from None

    def prove_not_delivered(self, rpc, reference, receipt, page=None):
        """Complete, stable provider history is required to release an unsent receipt."""
        turns, ids, cursors = [], set(), set()
        while True:
            if page is None:
                page = rpc.request("thread/turns/list", dict(threadId=reference["value"],
                    sortDirection="desc", limit=100, itemsView="full"))
            if not isinstance(page["data"], list) or not page["data"]:
                raise ValueError("incomplete history")
            for turn in page["data"]:
                if (not isinstance(turn["id"], str) or not turn["id"] or turn["id"] in ids
                        or turn["status"] not in {"completed", "interrupted", "failed"}
                        or turn["itemsView"] != "full" or not isinstance(turn["items"], list)):
                    raise ValueError("ambiguous full history")
                ids.add(turn["id"])
                for item in turn["items"]:
                    if item["type"] == "userMessage":
                        content = item["content"]
                        if (not isinstance(content, list) or len(content) != 1 or content[0]["type"] != "text"
                                or not isinstance(content[0]["text"], str)):
                            raise ValueError("unverifiable input")
                        if (item["clientId"] == receipt["message_id"] or
                                hashlib.sha256(content[0]["text"].encode()).hexdigest() == receipt["prompt_sha256"]):
                            raise ValueError("claimed input exists or conflicts in history")
                    elif item["type"] not in {"agentMessage", "reasoning", "commandExecution", "fileChange",
                                              "mcpToolCall", "webSearch", "imageView", "plan"}:
                        raise ValueError("unsupported or compacted history")
                turns.append(turn)
            cursor = page["nextCursor"]
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in cursors or len(cursors) >= 1000:
                raise ValueError("ambiguous history pagination")
            cursors.add(cursor)
            page = rpc.request("thread/turns/list", dict(threadId=reference["value"],
                sortDirection="desc", limit=100, itemsView="full", cursor=cursor))
        queue = rpc.request("thread/queue/list", dict(threadId=reference["value"], limit=1))
        if queue["data"] != [] or queue["nextCursor"] is not None:
            raise ValueError("pending input")
        return turns

    def observe_delivery(self, execution, reference, receipt, completion):
        """Read the exact latest turn and empty queue; never submit or resume."""
        try:
            with CodexRPC(execution.workspace.path) as rpc:
                thread = rpc.request("thread/read", {"threadId": reference["value"]})["thread"]
                if (thread["id"] != reference["value"] or Path(thread["cwd"]).resolve() != execution.workspace.path
                        or thread.get("forkedFromId") is not None or thread.get("parentThreadId") is not None):
                    raise ValueError("different thread")
                # Only the latest turn can finish this pass. Additional input,
                # forks, pagination ambiguity and queued follow-ups all refuse.
                page = rpc.request("thread/turns/list", dict(threadId=reference["value"],
                    sortDirection="desc", limit=1, itemsView="full"))
                queue = rpc.request("thread/queue/list", dict(threadId=reference["value"], limit=1))
                if queue["data"] != [] or queue["nextCursor"] is not None:
                    raise TaskError("Codex has pending input; saved pass completion is not exclusive")
                if not isinstance(page["data"], list) or len(page["data"]) != 1:
                    raise ValueError("missing turn")
                turn = page["data"][0]
                if turn["itemsView"] != "full" or not isinstance(turn["items"], list):
                    raise ValueError("partial turn")
                users = [item for item in turn["items"] if item["type"] == "userMessage"]
                if len(users) != 1:
                    raise ValueError("ambiguous input")
                user = users[0]
                content = user["content"]
                if (user["clientId"] != receipt["message_id"] or len(content) != 1
                        or content[0]["type"] != "text"
                        or hashlib.sha256(content[0]["text"].encode()).hexdigest() != receipt["prompt_sha256"]):
                    raise ValueError("different pass input")
                if turn["status"] == "inProgress":
                    return False
                if turn["status"] != "completed" or turn["error"] is not None:
                    raise ValueError("incomplete or failed turn")
                finals = [item for item in turn["items"] if item["type"] == "agentMessage"
                          and item.get("phase") == "final_answer"]
                if (len(finals) != 1 or completion is not None and finals[0]["text"] != completion
                        or turn["items"][-1] != finals[0]):
                    raise ValueError("missing structured completion receipt")
                return True
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TaskError("Codex saved turn does not prove exact pass completion; preserve output and inspect the context") from None

    def find_session(self, rpc, workspace: Workspace, bootstrap: str, *, deadline=None) -> str:
        deadline = time.monotonic() + 30 if deadline is None else deadline
        while time.monotonic() < deadline:
            result = rpc.request("thread/list", {"cwd": str(workspace.path), "limit": 100},
                                 timeout=max(0, deadline - time.monotonic()))
            if not isinstance(result["data"], list):
                raise ValueError("invalid Codex thread list")
            matches = [thread for thread in result["data"] if thread["preview"] == bootstrap]
            if len(matches) > 1:
                raise ValueError("ambiguous Codex readiness sessions")
            if matches:
                thread = matches[0]
                if (str(UUID(thread["id"])) != thread["id"]
                        or Path(thread["cwd"]).resolve() != workspace.path):
                    raise ValueError("Codex readiness session mismatch")
                return thread["id"]
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        raise CodexSessionNotObserved("Codex readiness turn not found (last observed: no session matching the readiness marker); "
                        "inspect the pane for trust or startup dialogs")

    def confirm_prompt(self, rpc, workspace: Workspace, thread_id: str,
                       message_id: str | None, prompt: str, *, deadline=None) -> str:
        # Poll persisted receipt, never resubmit. The terminal-owned TUI executes.
        deadline = time.monotonic() + 30 if deadline is None else deadline
        while time.monotonic() < deadline:
            thread = rpc.request("thread/read", {"threadId": thread_id},
                                 timeout=max(0, deadline - time.monotonic()))["thread"]
            if thread["id"] != thread_id or Path(thread["cwd"]).resolve() != workspace.path:
                raise ValueError("Codex receipt workspace/session mismatch")
            cursor, seen = None, set()
            while True:
                params = {"threadId": thread_id, "sortDirection": "asc", "limit": 100}
                if cursor is not None:
                    params["cursor"] = cursor
                page = rpc.request("thread/items/list", params, timeout=max(0, deadline - time.monotonic()))
                if not isinstance(page["data"], list):
                    raise ValueError("invalid Codex history")
                for entry in page["data"]:
                    item = entry["item"]
                    if item["type"] != "userMessage" or (message_id is not None and item["clientId"] != message_id):
                        continue
                    content = item["content"]
                    if (len(content) != 1 or content[0]["type"] != "text"
                            or content[0]["text"] != prompt
                            or not isinstance(entry["turnId"], str) or not entry["turnId"]):
                        raise ValueError("Codex recorded different input")
                    return entry["turnId"]
                cursor = page.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in seen or len(seen) >= 1000:
                    raise ValueError("ambiguous Codex history pagination")
                seen.add(cursor)
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        raise TaskError("Timed out: Codex did not confirm the exact task prompt in a recorded turn; "
                        "check the pane for startup, trust, authentication, or model errors")


class PiAdapter(HerdrAgentAdapter):
    """Pi TUI adapter using Herdr's interactive agent prompt transport."""

    kind = "pi"
    display_name = "Pi"
    MODES = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}

    def cleanup_provider_idle(self, workspace, reference):
        """Require intact current history ending at a terminal provider message."""
        try:
            verified = self.delivery_session(workspace, reference)
            if verified != reference or not reference.get("conversation_id"):
                raise ValueError("missing immutable conversation identity")
            path = Path(reference["value"])
            with path.open("rb") as source:
                raw = source.read(64 * 1024 * 1024 + 1)
            if not raw.endswith(b"\n") or len(raw) > 64 * 1024 * 1024:
                raise ValueError("incomplete history")
            lines = raw.splitlines()
            if any(not line or len(line) > 2 * 1024 * 1024 for line in lines):
                raise ValueError("incomplete history entries")
            entries = [json.loads(line, object_pairs_hook=unique_object) for line in lines]
            header = entries[0]
            if (header["type"] != "session" or header["id"] != reference["conversation_id"]
                    or Path(header["cwd"]).resolve() != workspace.path):
                raise ValueError("session changed")
            parent, ids, latest = None, set(), None
            for entry in entries[1:]:
                if (entry["type"] not in {"message", "model_change", "thinking_level_change"}
                        or not isinstance(entry["id"], str) or not entry["id"] or entry["id"] in ids
                        or entry["parentId"] != parent):
                    raise ValueError("unsupported, compacted or branched history")
                parent = entry["id"]
                ids.add(parent)
                if entry["type"] == "message":
                    latest = entry["message"]
            if latest is not None and (latest["role"] != "assistant"
                    or latest.get("stopReason") not in {"stop", "aborted", "error"}):
                raise ValueError("latest turn is unfinished")
            with path.open("rb") as source:
                if source.read(len(raw) + 1) != raw:
                    raise ValueError("history changed")
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            raise TaskError("Pi current turn is working or its complete session history is unverified") from None

    def delivery_session(self, workspace, reference):
        """The native header exists before Pi's first task message."""
        try:
            path = Path(reference["value"])
            if reference["kind"] != "path" or not path.is_absolute() or path.is_symlink():
                raise ValueError("not a session file")
            with path.open(encoding="utf-8") as source:
                header = json.loads(source.readline(65536))
            if (header["type"] != "session" or not isinstance(header["id"], str) or not header["id"]
                    or Path(header["cwd"]).resolve() != workspace.path
                    or reference.get("conversation_id", header["id"]) != header["id"]):
                raise ValueError("wrong session")
            return dict(reference, conversation_id=header["id"])
        except (OSError, ValueError, KeyError, TypeError):
            raise TaskError("Pi delivery needs its exact persisted session header; no prompt was sent") from None

    def verify_stopped_session(self, workspace, reference, receipt=None):
        if receipt:
            from types import SimpleNamespace
            self.observe_delivery(SimpleNamespace(workspace=workspace), reference, receipt, None, abandoning=True)
            return self.delivery_session(workspace, reference)
        else:
            return self.verify_session(workspace, reference)

    def observe_delivery(self, execution, reference, receipt, completion, *, abandoning=False):
        verified = (self.delivery_session(execution.workspace, reference) if abandoning else
                    self.verify_session(execution.workspace, reference))
        if verified != reference:
            raise TaskError("Pi conversation changed during saved pass")
        try:
            found, parent, final, total = False, None, None, 0
            with Path(reference["value"]).open(encoding="utf-8") as source:
                for line in iter(lambda: source.readline(2 * 1024 * 1024 + 1), ""):
                    total += len(line)
                    if total > 64 * 1024 * 1024 or len(line) > 2 * 1024 * 1024:
                        raise ValueError("history exceeds observation bound")
                    entry = json.loads(line)
                    if entry.get("type") != "message":
                        if found:
                            raise ValueError("history branched or compacted during pass")
                        continue
                    message = entry["message"]
                    content = message.get("content")
                    text = (content if isinstance(content, str) else
                            content[0]["text"] if isinstance(content, list) and len(content) == 1
                            and content[0].get("type") == "text" else None)
                    match = (message["role"] == "user" and text is not None
                             and hashlib.sha256(text.encode()).hexdigest() == receipt["prompt_sha256"])
                    if found:
                        if entry["parentId"] != parent or message["role"] == "user":
                            raise ValueError("different history branch or additional input")
                    elif match:
                        found = True
                    if found:
                        parent = entry["id"]
                        final = (message["role"] == "assistant" and message.get("stopReason") == "stop"
                                 and (completion is None or text == completion))
            if not found:
                if not abandoning:
                    raise ValueError("claimed prompt not found")
                before = self.prove_not_delivered(execution.workspace, reference, receipt)
                if self.prove_not_delivered(execution.workspace, reference, receipt) != before:
                    raise ValueError("history changed during non-delivery proof")
            return abandoning or bool(final)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            raise TaskError("Pi history does not prove the exact saved pass; preserve its claim for inspection") from None

    def prove_not_delivered(self, workspace, reference, receipt):
        """Pi has no durable input queue; stopped-runtime proof is owned by the caller.

        Require the entire native file, with an intact linear entry chain. A
        missing prompt after compaction, branching, truncation or unsupported
        records cannot establish non-delivery.
        """
        self.delivery_session(workspace, reference)
        with Path(reference["value"]).open("rb") as source:
            raw = source.read(64 * 1024 * 1024 + 1)
        if not raw.endswith(b"\n") or len(raw) > 64 * 1024 * 1024:
            raise ValueError("incomplete or oversized history")
        lines = raw.splitlines()
        if any(not line or len(line) > 2 * 1024 * 1024 for line in lines):
            raise ValueError("incomplete history entries")
        entries = [json.loads(line, object_pairs_hook=unique_object) for line in lines]
        header = entries[0]
        if (header["type"] != "session" or header["id"] != reference["conversation_id"]
                or Path(header["cwd"]).resolve() != workspace.path):
            raise ValueError("session changed")
        parent, ids = None, set()
        for entry in entries[1:]:
            if (entry["type"] not in {"message", "model_change", "thinking_level_change"}
                    or not isinstance(entry["id"], str) or not entry["id"] or entry["id"] in ids
                    or entry["parentId"] != parent):
                raise ValueError("unsupported, compacted or branched history")
            parent = entry["id"]
            ids.add(parent)
            if entry["type"] == "message":
                message = entry["message"]
                if message["role"] == "user":
                    content = message["content"]
                    text = (content if isinstance(content, str) else content[0]["text"]
                            if isinstance(content, list) and len(content) == 1 and content[0]["type"] == "text" else None)
                    if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() == receipt["prompt_sha256"]:
                        raise ValueError("unverifiable or previously delivered input")
                elif message["role"] not in {"assistant", "toolResult"}:
                    raise ValueError("unknown history message")
        return hashlib.sha256(raw).hexdigest()

    def validate_options(self) -> None:
        mode = "off" if self.options.mode == "none" else self.options.mode
        if mode is not None and mode not in self.MODES:
            raise TaskError("Pi mode must be off/none, minimal, low, medium, high, xhigh, or max")
        if (self.options.model is not None and ":" in self.options.model
                and self.options.model.rsplit(":", 1)[-1] in self.MODES):
            raise TaskError("Pi model:mode syntax is not accepted by Agentic Workflows Lite; "
                            "remove the thinking suffix from --model and select it with --mode")

    def launch_args(self) -> list[str]:
        args = []
        if self.options.model is not None:
            args.extend(["--model", self.options.model])
        if self.options.mode is not None:
            mode = "off" if self.options.mode == "none" else self.options.mode
            args.extend(["--thinking", mode])
        # Pi has no --cd flag. Herdr starts it in the confirmed pane cwd.
        # Herdr rejects multiline initial-message arguments; submit after startup.
        return args

    def review_args(self) -> list[str]:
        # Herdr process detection alone reports no Pi session identity. Load a
        # session-only reporter for this process; no global integration install.
        extension = Path(__file__).with_name("pi_session.mjs").resolve()
        if not extension.is_file():
            raise TaskError("Pi review session reporter is missing from this workflow installation")
        return [*self.launch_args(), "--extension", str(extension)]

    def verify_review_session(self, workspace: Workspace, reference: dict) -> dict:
        try:
            path = Path(reference["value"])
            if (reference["kind"] != "path" or not path.is_absolute()
                    or not stat.S_ISREG(path.stat().st_mode)):
                raise ValueError("an exact persisted Pi session path is required")
            with path.open(encoding="utf-8") as source:
                header = json.loads(source.readline())
                if (header["type"] != "session" or not isinstance(header["id"], str) or not header["id"].strip()
                        or ("conversation_id" in reference and header["id"] != reference["conversation_id"])
                        or Path(header["cwd"]).resolve() != workspace.path):
                    raise ValueError("session header mismatch")
                has_history = False
                for line in source:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError("invalid session history")
                    if entry.get("type") == "message":
                        if not isinstance(entry.get("message"), dict):
                            raise ValueError("invalid persisted message")
                        has_history = True
                        break
                if not has_history:
                    raise ValueError("no persisted conversation")
            return dict(reference, conversation_id=header["id"])
        except (KeyError, ValueError, TypeError, FileNotFoundError, NotADirectoryError, IsADirectoryError):
            raise SessionInvalid("Pi reviewer needs an exact persisted session with matching conversation identity, cwd/history") from None
        except OSError:
            raise TaskError("Pi reviewer history verification is temporarily unavailable; retry before resuming") from None

    def resume_args(self, execution: AgentExecution, reference: dict) -> list[str]:
        return [*self.review_args(), "--session", reference["value"]]

    def resume_review(self, execution: AgentExecution, reference: dict, *, recreate: bool) -> LaunchResult:
        self.validate_execution(execution)
        self.validate_model_mode(execution.workspace)
        return super().resume_review(execution, reference, recreate=recreate)

    def model_mode_capabilities(self, workspace: Workspace) -> tuple[str, str, tuple[str, ...]]:
        """Ask the installed Pi to resolve the model and report its real mode support."""
        executable = shutil.which(self.kind)
        if executable is None:
            raise TaskError("pi is not installed or not on PATH; install/login to Pi or use --no-agent")
        requests = [
            {"id": "state", "type": "get_state"},
            {"id": "levels", "type": "get_available_thinking_levels"},
        ]
        rpc_input = "".join(json.dumps(request) + "\n" for request in requests).encode()
        args = [executable, *self.launch_args(), "--mode", "rpc", "--no-session", "--no-tools"]
        try:
            result = subprocess.run(args, input=rpc_input, capture_output=True, cwd=workspace.path,
                                    env=agent_environment(),
                                    timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise TaskError("Pi model/mode preflight could not complete; check Pi authentication and configuration") from None
        if result.returncode:
            raise TaskError("Pi model/mode preflight failed; check the requested model, authentication, and configuration")
        try:
            responses = {}
            for line in result.stdout.decode("utf-8").splitlines():
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("invalid response")
                response_id = payload.get("id")
                if response_id in {"state", "levels"}:
                    if response_id in responses:
                        raise ValueError("duplicate response")
                    responses[response_id] = payload
            state_response = responses["state"]
            levels_response = responses["levels"]
            if (state_response["type"] != "response" or state_response["command"] != "get_state"
                    or state_response["success"] is not True
                    or levels_response["type"] != "response"
                    or levels_response["command"] != "get_available_thinking_levels"
                    or levels_response["success"] is not True):
                raise ValueError("unexpected response")
            state = state_response["data"]
            model = state["model"]
            provider = model["provider"]
            model_id = model["id"]
            effective = state["thinkingLevel"]
            levels = levels_response["data"]["levels"]
            if (not isinstance(provider, str) or not provider or not isinstance(model_id, str) or not model_id
                    or effective not in self.MODES or not isinstance(levels, list) or not levels
                    or len(set(levels)) != len(levels)
                    or any(level not in self.MODES for level in levels)):
                raise ValueError("invalid capability data")
            return f"{provider}/{model_id}", effective, tuple(levels)
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise TaskError("Pi model/mode preflight returned an unexpected response") from None

    def validate_model_mode(self, workspace: Workspace) -> None:
        if self.options.mode is None:
            return
        requested = "off" if self.options.mode == "none" else self.options.mode
        model, effective, supported = self.model_mode_capabilities(workspace)
        if requested not in supported or effective != requested:
            available = ", ".join(supported)
            raise TaskError(f"Pi model {model!r} does not support requested mode {requested!r}; "
                            f"supported modes: {available}. Pi would use {effective!r} instead")

    def launch(self, execution: AgentExecution) -> LaunchResult:
        self.validate_execution(execution)
        workspace = execution.workspace
        phase = "model/mode validation"
        try:
            self.validate_model_mode(workspace)
            phase = "startup"
            self.check_target(workspace, review=execution.purpose in {"review", "integration"})
            self.clear_shell_input(workspace)
            args = (self.review_args() if execution.purpose in {"review", "integration"} or execution.policy.get("session_reporting")
                    else self.launch_args())
            observed = self.observe_agent(execution, self.start_agent(workspace, args, review=execution.purpose in {"review", "integration"}))
            observed = self.confirm_target(execution, observed)
            phase = "prompt submission"
            if execution.delivery_observer:
                # Standalone review already supports a Pi path/header appearing
                # only after submission. Record its prompt digest now; the
                # review controller binds the verified conversation at completion.
                # Loop/integration claims still require immutable pre-delivery proof.
                reference = (observed["_session_reference"]
                    if execution.purpose == "review" and execution.policy.get("standalone_review")
                    else self.delivery_session(workspace, observed["_session_reference"]))
                execution.delivery_observer(reference, str(uuid4()), execution.handoff)
            prompted = self.command("agent", "prompt", workspace.pane_id, execution.handoff)["agent"]
            observed = self.observe_agent(execution, prompted, observed)
            if prompted.get("agent_status") not in {"idle", "done", "working"}:
                raise ValueError("Pi prompt submission target changed")
            observed = self.confirm_target(execution, observed)
            identity = session_identity(observed["_session_reference"], self.kind)
            session_id, session_kind = (identity[2], identity[1]) if identity else (None, None)
            summary = f"Pi prompt submitted in {workspace.pane_id}"
            if session_id:
                summary += f" (session {session_id})"
            return LaunchResult(self.kind, workspace.pane_id, summary, session_id,
                                session_kind=session_kind)  # Herdr metadata alone cannot prove resumability.
        except (KeyError, TypeError, ValueError, OSError):
            raise TaskError(f"Pi {phase} was not confirmed in pane {workspace.pane_id}; "
                            "inspect it before retrying. Workspace left intact") from None
        except TaskError as error:
            raise TaskError(f"Pi {phase} failed in pane {workspace.pane_id}: {error}. "
                            "Workspace left intact; inspect the pane before retrying") from None


ADAPTERS: dict[str, type[HerdrAgentAdapter]] = {
    CodexAdapter.kind: CodexAdapter,
    PiAdapter.kind: PiAdapter,
}


def adapter_for(options: AgentOptions) -> AgentAdapter:
    """Select one adapter without falling back to another requested agent."""
    adapter = ADAPTERS.get(options.kind)
    if adapter is None:
        supported = ", ".join(sorted(ADAPTERS))
        raise TaskError(f"Unsupported agent {options.kind!r}; supported agents: {supported}")
    return adapter(options)


def validate_agent_defaults(config: AgentConfig) -> None:
    """Validate a partial layer without selecting a provider or probing a CLI.

    Without a kind, a value must be supported by at least one registered adapter;
    the final merged/CLI selection still validates the actual combination.
    """
    if config.kind is not None:
        adapter_for(AgentOptions(config.kind, config.model, config.mode))
        return
    for kind, adapter in ADAPTERS.items():
        try:
            adapter(AgentOptions(kind, config.model, config.mode))
        except TaskError:
            continue
        return
    raise TaskError("Unsupported project agent/reviewer model or mode")


# Compatibility names for code importing the original concrete launcher.
Codex = CodexAdapter
Pi = PiAdapter

"""One task-private result rendezvous, owned by the existing loop checkpoint.

The provider's structured final receipt authenticates the result and final Git
snapshot. Neither a file nor runtime idleness alone establishes completion.
"""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import time
from uuid import UUID

from . import TaskError
from .command import command_name
from .contexts import context_reference, reconcile
from .review_result import unique_object
from .review_state import snapshot
from .sessions import has_immutable_identity, merge_session
from .workspace import Git


LIMIT = 1024 * 1024
FILES = {"result.json", "completion.json", "complete.py"}


def pass_timing(state):
    """Read-only advisory timing, including unchanged legacy hard deadlines."""
    value = state.get("delivery")
    if not state["active_pass"] or not value or not value["receipt"]:
        return None
    return dict(threshold_seconds=state["timeout"], warning_at=value["deadline"],
                overdue=time.time() >= value["deadline"])


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def object_digest(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=True).encode())


def read_private(path, limit=LIMIT):
    """Bounded no-follow read; partial writes fail parsing, never imply success."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
                raise ValueError("unsafe result")
            raw = source.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("oversized result")
            return raw
    except (OSError, ValueError):
        raise TaskError("Missing, partial, or unsafe retained pass output; preserve the claim and inspect its context") from None


def atomic_private(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def seal(directory, run_id, pass_id, worktree, base, branch):
    """Agent's last tool: durably bind exact result bytes to final Git state."""
    require_current_pass(worktree, run_id, pass_id)
    directory = Path(directory)
    raw = read_private(directory / "result.json")
    value = json.loads(raw, object_pairs_hook=unique_object)
    if value["pass_id"] != pass_id:
        raise TaskError("Cannot seal another pass's result")
    final = snapshot(Path(worktree), base, branch).as_dict()
    # Flush the result too: completion.json is the durable commit marker.
    with (directory / "result.json").open("rb") as source:
        os.fsync(source.fileno())
    proof = json.dumps(dict(run_id=run_id, pass_id=pass_id, result_sha256=digest(raw),
                            snapshot=final, completed_at=time.time()), sort_keys=True).encode()
    atomic_private(directory / "completion.json", proof)
    return "TASK_PASS_COMPLETE " + pass_id + " " + digest(proof)


def require_current_pass(worktree, run_id, pass_id):
    from .loop_state import LoopStore
    store = LoopStore(Path(worktree))
    current, _ = store.read()
    if (current["run_id"] != run_id or current["status"] in {"aborting", "aborted"}
            or (current["active_pass"] or {}).get("pass_id") != pass_id
            or store.abort_journal(run_id) is not None):
        raise TaskError("Pass was abandoned or superseded; late completion cannot be accepted")


def validate_delivery(value, state):
    if value is None:
        return
    fields = {"version", "run_id", "pass_id", "phase", "directory", "device", "inode", "deadline",
              "context", "location", "receipt", "assessment", "review", "invalidated", "slice", "contexts_sha256"}
    if (set(value) != fields or value["version"] != 1 or value["run_id"] != state["run_id"]
            or str(UUID(value["run_id"])) != value["run_id"]
            or str(UUID(value["pass_id"])) != value["pass_id"]
            or type(value["assessment"]) is not bool
            or type(value["invalidated"]) is not bool
            or value["slice"] is not None and (not isinstance(value["slice"], str) or not value["slice"])
            or type(value["deadline"]) not in (int, float) or not 0 < value["deadline"] < float("inf")
            or type(value["device"]) is not int or type(value["inode"]) is not int):
        raise ValueError("invalid retained delivery")
    if (not isinstance(value["contexts_sha256"], str) or len(value["contexts_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["contexts_sha256"])):
        raise ValueError("invalid context set identity")
    path = Path(value["directory"])
    if (not path.is_absolute() or path.name != f"task-loop-{value['run_id']}-{value['pass_id']}"
            or ".." in path.parts or path.is_relative_to(state["binding"]["worktree"])):
        raise ValueError("invalid rendezvous path")
    claim = state["active_pass"] or (state["records"][-1] if state["records"] else {})
    if any(value[k] != claim.get(k) for k in ("pass_id", "phase")):
        raise ValueError("retained delivery is not the current pass")
    context = value["context"]
    if context is not None:
        if (set(context) != {"context_id", "agent", "model", "mode", "session"}
                or context["context_id"] != claim["context_id"]
                or not has_immutable_identity(context["session"], context["agent"])):
            raise ValueError("invalid delivery session")
        saved = state.get("implementation") if value["phase"] not in {"review", "rereview"} else state.get("reviewer")
        if saved is not None and (saved["context_id"] != context["context_id"]
                or len(saved) > 1 and saved != context):
            raise ValueError("delivery context differs from checkpoint")
        options = state.get("reviewer_options" if value["review"] is not None else "implementation_options")
        if options and any(context[k] != options[o] for k, o in
                           (("agent", "kind"), ("model", "model"), ("mode", "mode"))):
            raise ValueError("delivery settings differ from the recorded selection")
    receipt = value["receipt"]
    if receipt is not None:
        if (context is None or set(receipt) != {"message_id", "prompt_sha256"}
                or str(UUID(receipt["message_id"])) != receipt["message_id"]
                or len(receipt["prompt_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in receipt["prompt_sha256"])):
            raise ValueError("invalid delivery receipt")
    if (set(value["location"]) != {"pane_id", "tab_id", "terminal_id"}
            or any(not isinstance(v, str) or not v for v in value["location"].values())):
        raise ValueError("invalid delivery location")
    review = value["review"]
    if (value["phase"] in {"review", "rereview"}) != (review is not None):
        raise ValueError("invalid retained review")
    if review is not None and (set(review) != {"pass_kind", "frozen", "publication"}
            or review["pass_kind"] != ("fresh" if value["phase"] == "review" else "resumed")):
        raise ValueError("invalid retained review metadata")


def check_directory(value):
    path = Path(value["directory"])
    try:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (value["device"], value["inode"])
                or path.resolve() != path or set(p.name for p in path.iterdir()) - FILES):
            raise ValueError("replaced directory")
    except (OSError, ValueError):
        raise TaskError("Retained pass directory is missing, replaced, or unsafe; no handoff can be replayed") from None
    return path


def dispose(value):
    """After durable consumption only. Uncertain passes retain their destination."""
    if not Path(value["directory"]).exists():
        return
    path = check_directory(value)
    for name in FILES:
        item = path / name
        if item.exists() or item.is_symlink():
            read_private(item)
            item.unlink()
    path.rmdir()


class PassDelivery:
    def __init__(self, state, persist, env):
        self.state, self.persist, self.env = state, persist, env
        self.warned = False

    @property
    def evidence(self):
        return self.state["delivery"]

    def create(self, context_id, pass_id, *, review=None):
        self.persist(context_id, pass_id)
        path = Path(tempfile.gettempdir()).resolve() / f"task-loop-{self.state['run_id']}-{pass_id}"
        if path.is_relative_to(self.env.workspace.path):
            raise TaskError("Retained output must be outside the checkout; check TMPDIR")
        # Save the selector before mkdir so a killed controller cannot orphan a
        # delivered destination. No prompt can be sent until identity is saved.
        context = self.env.registry.get(context_id)
        value = dict(version=1, run_id=self.state["run_id"], pass_id=pass_id,
            phase=self.state["next_phase"], directory=str(path), device=0, inode=0,
            deadline=time.time() + self.state["timeout"], context=None, receipt=None,
            location={k: context[k] for k in ("pane_id", "tab_id", "terminal_id")},
            assessment=self.env.local.task_assessment.enabled, review=review, invalidated=False,
            slice=self.env.workspace.slice, contexts_sha256=self.contexts_digest())
        self.state["delivery"] = value
        self.persist(context_id, pass_id)
        path.mkdir(mode=0o700)
        info = path.stat()
        value.update(device=info.st_dev, inode=info.st_ino)
        args = (str(path), value["run_id"], pass_id, str(self.env.workspace.path),
                self.state["snapshot"]["base_commit"], self.env.workspace.branch)
        script = ("import sys\nsys.dont_write_bytecode = True\n"
                  f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
                  "from task_start.pass_delivery import seal\n"
                  f"print(seal(*{args!r}))\n")
        atomic_private(path / "complete.py", script.encode())
        self.persist(context_id, pass_id)
        return path / "result.json"

    def contract(self):
        command = shlex.join([sys.executable, str(Path(self.evidence["directory"]) / "complete.py")])
        return ("\nDURABLE COMPLETION CONTRACT\nThe result destination survives this controller. "
            "After writing the structured result, run the following command as your final tool call. "
            "It flushes your result and seals the final Git state; it does not edit the checkout. "
            "The read-only review exception permits this completion write too.\n" + command + "\n"
            "Then reply with exactly the TASK_PASS_COMPLETE receipt printed by that command, "
            "without any other text or tools. This replaces the earlier instruction to end immediately "
            "after writing JSON. If sealing fails, report the failure; never invent a receipt.\n")

    def started(self):
        # Only the delivering owner calls this after bounded startup/receipt.
        # Restart observations never move the original warning timestamp.
        self.evidence["deadline"] = time.time() + self.state["timeout"]
        self.persist(self.state["active_pass"]["context_id"], self.evidence["pass_id"])

    def warn_if_overdue(self):
        if self.warned:
            return
        timing = pass_timing(self.state)
        if timing is None or not timing["overdue"]:
            return
        self.warned = True  # One notice per pass in this controller invocation.
        value = self.evidence
        elapsed = time.time() - (value["deadline"] - self.state["timeout"])
        issue = self.state["binding"]["issue"]
        print(f"Info: {issue} {value['phase']} pass {value['pass_id']} "
              f"({self.state['active_pass']['context_id']}): elapsed {elapsed:.0f}s; "
              f"warning threshold {self.state['timeout']:g}s exceeded. "
              "Still observing the original agent turn; verified completion remains pending. "
              f"Leave it working, inspect {command_name()} loop {issue} --status [--json], or request "
              f"{command_name()} loop {issue} --pause-after-current in another terminal. "
              "Ctrl+C stops this waiting controller, not necessarily the agent turn. "
              "To stop/abandon work, explicitly quit task agents, then use "
              f"{command_name()} loop {issue} --abort or {command_name()} cleanup {issue} --force from outside the checkout "
              "(cleanup discards work).", file=sys.stderr, flush=True)

    def contexts_digest(self):
        return object_digest(sorted(c["context_id"] for c in
                                    self.env.registry.list(self.env.issue.identifier, include_retired=True)))

    def check_contexts(self):
        if self.evidence["contexts_sha256"] == self.contexts_digest():
            return
        # Only an independently retained, exact standalone R1 can account for
        # additions while collecting the original initial/completion I1. This
        # never changes the saved digest, claim, seal or provider receipt.
        from .publication_state import PublicationStore
        from .review_handoff_state import eligible_review
        if (self.evidence["phase"] not in {"initial_implementation", "implementation"} or
                eligible_review(self.env, PublicationStore(self.env.workspace.path), self.state,
                                intervening=True) is None):
            raise TaskError("Task context set changed since delivery; an intervening execution cannot be adopted")

    def invalidate_review(self):
        if self.evidence["review"] is not None and not self.evidence["invalidated"]:
            self.evidence["invalidated"] = True
            self.persist(self.state["active_pass"]["context_id"], self.evidence["pass_id"])

    def snapshot(self):
        """Record review drift immediately, before a later observation can erase it."""
        if self.evidence["invalidated"]:
            raise TaskError("Saved review observed Git drift and remains invalidated; inspect before reconciliation")
        binding, before = self.state["binding"], self.state["snapshot"]
        try:
            current = snapshot(Path(binding["worktree"]), before["base_commit"], binding["branch"]).as_dict()
            if self.evidence["review"] is not None:
                base = Git(Path(binding["repository"])).command(
                    "rev-parse", "--verify", f"refs/heads/{binding['base_branch']}^{{commit}}").strip()
                if current != before or base != before["base_commit"]:
                    raise TaskError("Checkout/base changed during review; retained pass is invalidated")
        except TaskError:
            self.invalidate_review()
            raise
        return current

    def checkpoint(self):
        # Like normal review polling, record in finally without swallowing an
        # interruption or treating restoration as permission to accept the pass.
        if self.evidence["review"] is not None:
            try:
                self.snapshot()
            except TaskError:
                pass

    def attach(self, execution, *, add_contract=True):
        def claim(reference, message_id, prompt):
            context = self.env.registry.get(self.state["active_pass"]["context_id"])
            reference = merge_session(context_reference(context), reference, execution.options.kind)
            if not has_immutable_identity(reference, execution.options.kind):
                raise TaskError("Delivery lacks immutable provider identity; no prompt was sent")
            if self.evidence["receipt"] is not None:
                raise TaskError("A delivery was already claimed; no prompt may be replayed")
            self.evidence.update(context=dict(context_id=context["context_id"], agent=execution.options.kind,
                model=execution.options.model, mode=execution.options.mode, session=reference),
                receipt=dict(message_id=message_id, prompt_sha256=digest(prompt.encode())),
                deadline=time.time() + self.state["timeout"])
            self.persist(context["context_id"], self.evidence["pass_id"])
        handoff = execution.handoff + (self.contract() if add_contract else "")
        return replace(execution, handoff=handoff, delivery_observer=claim)

    def verify(self, adapter, execution):
        value = self.evidence
        require_current_pass(self.state["binding"]["worktree"], value["run_id"], value["pass_id"])
        self.check_contexts()
        if value["invalidated"]:
            raise TaskError("Saved review observed Git drift and remains invalidated; inspect before reconciliation")
        path = check_directory(value)
        if value["context"] is None or value["receipt"] is None:
            raise TaskError("Handoff has no durable delivery identity; inspect the retained claim; never replay it")
        # Herdr can report idle during agent activity. A partially written file
        # is pending while the exact provider turn is demonstrably still working.
        if not adapter.observe_delivery(execution, value["context"]["session"], value["receipt"], None):
            return None
        raw = read_private(path / "result.json")
        proof_raw = read_private(path / "completion.json", 16384)
        try:
            # The saved deadline is advisory, including for legacy seals. Keep
            # the actual completion timestamp and every identity/integrity check.
            proof = json.loads(proof_raw, object_pairs_hook=unique_object)
            if (set(proof) != {"run_id", "pass_id", "result_sha256", "snapshot", "completed_at"}
                    or proof["run_id"] != value["run_id"] or proof["pass_id"] != value["pass_id"]
                    or proof["result_sha256"] != digest(raw)
                    or type(proof["completed_at"]) not in (int, float)
                    or not 0 < proof["completed_at"] < float("inf")):
                raise ValueError("different completion")
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise TaskError("Retained completion seal is invalid or conflicts with result bytes") from None
        token = "TASK_PASS_COMPLETE " + value["pass_id"] + " " + digest(proof_raw)
        if not adapter.observe_delivery(execution, value["context"]["session"], value["receipt"], token):
            return None
        after = self.snapshot()
        before = self.state["snapshot"]
        try:
            scope = Git(self.env.repo).resolve_scope(self.env.workspace.path, self.env.workspace.branch,
                                                    self.env.issue.identifier, None)
            base = Git(self.env.repo).command("rev-parse", "--verify",
                f"refs/heads/{self.state['binding']['base_branch']}^{{commit}}").strip()
        except TaskError:
            self.invalidate_review()
            raise
        if (proof["snapshot"] != after or after["head"] != before["head"]
                or scope != value["slice"]
                or base != before["base_commit"]
                or after["base_commit"] != before["base_commit"]
                or value["review"] is not None and before != after):
            self.invalidate_review()
            raise TaskError("Git state drifted from the sealed pass or pinned review/history; completion refused")
        self.completed_snapshot = after
        return raw.decode("utf-8")

    def runtime(self):
        """Strict observation of the recorded runtime, never relocation/recreation."""
        from .agent import AgentExecution, AgentOptions, adapter_for
        value, env = self.evidence, self.env
        require_current_pass(self.state["binding"]["worktree"], value["run_id"], value["pass_id"])
        expected = value["context"]
        self.check_contexts()
        if expected is None or value["receipt"] is None:
            raise TaskError("Claim predates proven delivery; inspect contexts and retained output; never replay this pass")
        context = env.registry.get(expected["context_id"])
        current_reference = context_reference(context)
        try:
            # A Pi startup observation may retain only the locator when its
            # controller dies in prompt submission. The pre-delivery receipt
            # already pinned the header UUID; verify that exact UUID below.
            same_identity = (current_reference is not None and
                             merge_session(current_reference, expected["session"], context["agent"]) == expected["session"])
        except ValueError:
            same_identity = False
        if (context["retired_at"] or context["state"] not in {"active", "reviewing", "uncertain", "launching"}
                or context["role"] != ("review" if value["review"] is not None else "implementation")
                or any(context[k] != expected[k] for k in ("agent", "model", "mode"))
                or not same_identity
                or any(context[k] != v for k, v in value["location"].items())
                or any(context[k] != self.state["binding"][k] for k in
                       ("issue", "repository", "worktree", "workspace_id", "endpoint"))):
            raise TaskError("Saved pass context/session/settings changed; no replacement or replay is allowed")
        pane, notes = reconcile(context, env.identities.snapshot())
        if (pane is None or pane.get("agent") != expected["agent"]
                or any(pane.get(k) != v for k, v in value["location"].items())
                or Path(pane.get("cwd", "")).resolve() != env.workspace.path
                or any(not n.startswith("renamed/unlabeled:") for n in notes)):
            raise TaskError("Saved pass runtime is absent, moved, or uncertain; restore its exact session/location before recovery")
        options = AgentOptions(expected["agent"], expected["model"], expected["mode"])
        adapter = adapter_for(options)
        adapter.check_available()
        verified = adapter.verify_session(env.workspace, expected["session"])
        if verified != expected["session"]:
            raise TaskError("Provider conversation changed since pass delivery")
        workspace = replace(env.workspace, pane_id=pane["pane_id"], tab_id=pane["tab_id"])
        execution = AgentExecution(env.issue, env.repo, workspace, options, "Observe saved pass only",
            purpose="review" if value["review"] is not None else "implementation")
        return adapter, execution, pane, expected["session"]

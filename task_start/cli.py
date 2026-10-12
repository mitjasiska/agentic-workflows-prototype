import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import re
import signal
import sys

from . import TaskError
from .ownership import ownership_gate
from .publication_state import PublicationStore
from .metrics import observe_run
from .agent import (AgentExecution, AgentOverrides, adapter_for,
                    codex_repository_policy, resolve_agent_options)
from .config import (check_project_settings_unchanged, load_local, load_projects, project_settings,
                     repository_path, resolve_project)
from .contexts import ContextRegistry, HerdrContexts, inspect_contexts, launch_registered
from .handoff import implementation_handoff
from .linear import Linear
from .loop import loop
from .review import review
from .publish import publish
from .integrate import abandon_integration, integrate
from .preparation import check_issue_structure, prepare_task
from .workspace import Git, Herdr, slice_slug
from .status import task_status


def issue_identifier(value: str) -> str:
    value = value.upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("expected an issue identifier such as DEV-7")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="task", description="Manage Linear task workspaces in Herdr")
    commands = result.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Create or reopen a task workspace")
    start.add_argument("issue", type=issue_identifier)
    start.add_argument("--slice", type=parse_slice, help="Select an explicit implementation slice (branch suffix)")
    add_agent_options(start, include_no_agent=True)
    cleanup_command = commands.add_parser("cleanup",
        help="Remove a completed task, or force local cleanup of an unpublished or proven merged execution",
        description="Normally requires a completed issue, proven merge and stopped selected agents. "
                    "--force instead authorizes destructive "
                    "disposal of one never-published or proven merged local execution, including dirty task contents, "
                    "without changing its Linear status. Merged recovery can use exact Git/Herdr identity when "
                    "legacy scope metadata is absent. Use --branch to select one proven merged execution "
                    "when multiple default/sliced candidates exist. "
                    "Force may stop exact owned idle agent sessions; active turns must finish or be stopped first. "
                    "Run outside its checkouts; exact identity and stopped-process "
                    "proof for registered execution are required. Other executions retain their resource claims; "
                    "unmanaged host references are outside the V1 guarantee. Independent workspaces may keep running. "
                    "A same-workspace controller must finish or stop; short registry contention can be retried. "
                    "Rerun --force to finish a partial disposal.")
    cleanup_command.add_argument("issue", type=issue_identifier)
    cleanup_command.add_argument("--force", action="store_true",
        help="Destroy uncommitted files and retained artifacts and stop owned idle agent sessions of one unpublished or proven merged execution; preserve Linear status")
    cleanup_command.add_argument("--branch", help="Select an exact local issue branch for proven merged cleanup (requires --force)")
    contexts = commands.add_parser("contexts", help="Inspect machine-local workflow contexts (read-only)")
    contexts.add_argument("issue", nargs="?", type=issue_identifier)
    contexts.add_argument("--all", action="store_true", help="Include retired contexts")
    status = commands.add_parser("status", help="Inspect task state and bounded saved history (read-only)")
    status.add_argument("issue", nargs="?", type=issue_identifier)
    status.add_argument("--all", action="store_true", help="List exactly registered AWL task workspaces by configured Linear project")
    status.add_argument("--json", action="store_true", help="Print the versioned canonical status projection")
    review_command = commands.add_parser("review", help="Run a fresh independent task review or an explicit re-review")
    review_command.add_argument("issue", type=issue_identifier)
    review_command.add_argument("--resume", metavar="REVIEW_CONTEXT_ID")
    add_agent_options(review_command)
    review_command.add_argument("--json", action="store_true", help="Print the structured review result")
    review_command.add_argument("--timeout", type=int, default=1800, metavar="SECONDS")
    loop_command = commands.add_parser("loop", help="Prepare a task and run bounded implementation/review passes",
        description="Prepare an unsliced workspace and launch initial implementation when no context exists, "
                    "then run independent review and fixes. Continue a verified implementation completion or "
                    "adopt an exactly proven completed standalone review; otherwise collect structured completion "
                    "in the original implementation conversation. Existing contexts retain their settings. "
                    "Configured use needs only task loop ISSUE: [agent] selects initial implementation and "
                    "[reviewer] selects review. Optional --i-* and --r-* flags override those roles. "
                    "Both selections require explicit resolved model and mode. Repeat task loop ISSUE after "
                    "controller interruption to observe its original pass without sending another prompt. "
                    "If an agent turn was interrupted, stop the waiting controller with Ctrl+C first. "
                    "For deliberate abandonment, quit task agents to their original shells, keep panes open, "
                    "stop child/background jobs, then run --abort outside the task checkout on Herdr's Linux host. "
                    "Abort verifies the registered execution, not machine-wide checkout non-use. "
                    "Independent workspaces can abort or clean up while this loop runs. "
                    "Stop manually detached or external helpers before reuse.")
    loop_command.add_argument("issue", type=issue_identifier)
    controls = loop_command.add_mutually_exclusive_group()
    controls.add_argument("--pause-after-current", dest="action", action="store_const", const="pause",
                          help="Request a sticky pause at the next handoff boundary")
    controls.add_argument("--continue", dest="action", action="store_const", const="continue",
                          help="Explicitly continue the exact paused boundary")
    controls.add_argument("--status", dest="action", action="store_const", const="status",
                          help="Inspect checkpoint without resuming or contacting Linear")
    controls.add_argument("--new", dest="action", action="store_const", const="new",
                          help="Explicitly replace a stopped loop after inspection, with a fresh reviewer")
    controls.add_argument("--abort", dest="action", action="store_const", const="abort",
                          help="Abandon an unfinished pass after Linux registered-execution checks, preserving checkout and session history; Ctrl+C alone does not abort")
    loop_command.set_defaults(action="run")
    loop_command.add_argument("--from-review", action="store_true",
                              help="Explicitly enter review of completed implementation (adopt exact standalone evidence when present)")
    add_agent_options(loop_command, prefix="i-", role="initial implementation")
    add_agent_options(loop_command, prefix="r-", role="reviewer")
    loop_command.add_argument("--max-reviews", type=int, help="Review limit (default 3; maximum 20)")
    loop_command.add_argument("--max-passes", type=int, help="Total pass limit (default 6; maximum 40)")
    loop_command.add_argument("--timeout", type=int, metavar="SECONDS", help="Advisory warning threshold per pass; keeps observing (configured [loop] timeout or 7200; maximum 86400)")
    loop_command.add_argument("--json", action="store_true", help="Print the combined lifecycle result")
    pr_command = commands.add_parser("pr", help="Commit and publish the exact clean-reviewed task as a GitHub PR")
    pr_command.add_argument("issue", type=issue_identifier)
    integration = commands.add_parser("integrate", help="Resolve unpublished integration conflicts in an isolated agent checkout",
        description="Recover confirmed advanced-base conflicts for clean-reviewed uncommitted tasks. "
                    "Uses [agent]; optional --agent/--model/--mode override it. Explicit resolved model and mode are required. "
                    "Installs only a proven result, then requires fresh independent review. Never publishes.")
    integration.add_argument("issue", type=issue_identifier)
    add_agent_options(integration)
    integration.add_argument("--timeout", type=int, metavar="SECONDS", help="Agent completion timeout (default 1800)")
    integration.add_argument("--abandon", metavar="CONTEXT", help="Explicitly abandon a stopped uncertain sessionless Codex G context; retain all evidence, launch nothing")
    for command in (start, review_command, loop_command, integration):
        command.epilog = ("If Codex pauses for recognized trust/setup, handle it in the indicated pane, "
                          "then press Enter in this original command to reconcile the same launch. "
                          "Keep this command running; do not rerun it to bypass trust. "
                          "See docs/lifecycle.md for interruption and uncertain-delivery limits.")
    return result


def add_agent_options(command: argparse.ArgumentParser, *, include_no_agent: bool = False,
                      prefix: str = "", role: str = "execution") -> None:
    """Add reusable execution selection flags to a workflow subcommand."""
    dest = prefix.replace("-", "_")
    command.add_argument(f"--{prefix}agent", dest=f"{dest}agent_kind", metavar="KIND",
                         help=f"Override the configured {role} agent (codex or pi)")
    command.add_argument(f"--{prefix}model", dest=f"{dest}model",
                         help=f"Override the configured {role} model")
    command.add_argument(f"--{prefix}mode", dest=f"{dest}mode",
                         help=f"Override the configured {role} reasoning/thinking mode")
    if include_no_agent:
        command.add_argument("--no-agent", action="store_true",
                             help="Prepare/focus the workspace without starting an execution agent")


def parse_slice(value: str) -> str:
    try:
        return slice_slug(value)
    except TaskError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def start(identifier: str, *, no_agent: bool = False, slice: str | None = None,
          agent_kind: str | None = None, model: str | None = None,
          mode: str | None = None) -> str:
    if slice is not None:
        slice = slice_slug(slice)
    overrides = AgentOverrides(agent_kind, model, mode)
    if no_agent and any(value is not None for value in (agent_kind, model, mode)):
        raise TaskError("--no-agent conflicts with --agent, --model, and --mode")
    local = load_local(no_agent=no_agent)
    agent = None
    options = None
    projects = load_projects()
    linear = Linear(local.api_key)
    issue = linear.get_issue(identifier)
    if not no_agent:
        check_issue_structure(issue, local.issue_structure)
    project = resolve_project(projects, issue.project)
    repo = repository_path(local, project)
    global_settings = local
    # Validate workspace-only starts too: private files must never be copied
    # into a task checkout, even when no agent is requested.
    local = project_settings(global_settings, repo)
    if not no_agent:
        options = resolve_agent_options(local.agent, overrides)
        agent = adapter_for(options)
        agent.check_available()
    with ExitStack() as ownership:
        workspace = prepare_task(issue, project, linear, Git(repo), lambda: Herdr(repo), slice=slice,
                                 check_configuration=lambda: check_project_settings_unchanged(global_settings, repo, local),
                                 ownership=ownership)
        if agent:
            policy = (codex_repository_policy(local.codex_repository_profiles, project.repo_name)
                      if options.kind == "codex" else {})
            execution = AgentExecution(issue, repo, workspace, options,
                                       implementation_handoff(issue, workspace, assessment=local.task_assessment,
                                                              scope=local.implementation_scope), policy=policy)
            with observe_run() as metric:
                metric.start(ContextRegistry(), issue, options, "implementation")
                launched = launch_registered(agent, execution)
                metric.outcome("dispatched", context_id=getattr(launched, "context_id", None))
                status = launched.summary
        else:
            status = "skipped (--no-agent)"
        return (f"{issue.identifier}  {issue.title}\nRepo:   {repo}\nBranch: {workspace.branch}\n"
                f"Worktree: {workspace.path}\nHerdr:  {workspace.action}\nLinear: In Progress\nAgent:  {status}")


def cleanup(identifier: str, *, force: bool = False, branch: str | None = None) -> str:
    if branch is not None and not force:
        raise TaskError("Cleanup --branch requires --force and a proven merged execution")
    local = load_local(no_agent=True)
    issue = Linear(local.api_key).get_issue(identifier)
    project = resolve_project(load_projects(), issue.project)
    repo = repository_path(local, project)
    if force:
        from .cleanup import discard_execution
        return discard_execution(issue, project, repo, branch=branch)
    if issue.state_type != "completed":
        raise TaskError(f"{identifier} is not completed (Linear status: {issue.state_name}); nothing was removed")
    git, herdr = Git(repo), Herdr(repo)
    git.check_base(project.base_branch)
    with ExitStack() as locks:
        # The issue directory lock survives removal of the private Git directory.
        from .cleanup import DisposalStore
        locks.enter_context(DisposalStore(git, issue.identifier).locked())
        with ownership_gate(exclusive=True):
            ContextRegistry().remember_repository(repo)
            pending = git.load_retirement(issue.identifier, project.base_branch)
            target = herdr.resolve_task(git, issue.identifier, include_remotes=False)
            if target is not None:
                locks.enter_context(PublicationStore(target.path).locked())
        return cleanup_completed(issue, project, repo, git, herdr, pending, target)


def cleanup_completed(issue, project, repo, git, herdr, pending, target):
    if target is None:
        if pending is None:
            pending = herdr.stale_retirement(git, issue.identifier, project.base_branch)
            if pending is not None:
                with ownership_gate(exclusive=True):
                    ContextRegistry().remember_repository(repo)
                    git.save_retirement(pending)
        if pending is not None:
            try:
                if pending.workspace_id is not None:
                    retired = herdr.retire(git, pending)
                else:
                    herdr.revalidate_retirement(git, pending)
                    retired = False
            except TaskError as error:
                raise TaskError(f"Git cleanup is already complete, but Herdr workspace "
                                f"{pending.workspace_id!r} could not be confirmed retired: {error}. "
                                f"Rerun task cleanup {issue.identifier} after resolving the Herdr problem") from None
            with ownership_gate(exclusive=True):
                retire_contexts(issue.identifier, repo, pending.path, pending.workspace_id)
                retire_missing_contexts(issue.identifier, repo, herdr, pending.path)
                git.clear_retirement(pending)
            status = (f"Retired Herdr workspace: {pending.workspace_id}" if retired else
                      f"Herdr workspace already absent: {pending.workspace_id}" if pending.workspace_id else
                      "No open Herdr workspace was registered")
            return (f"{issue.identifier}: cleanup complete\nRepo: {repo}\n"
                    "Git worktree and local branch were already removed\n" + status)
        retire_missing_contexts(issue.identifier, repo, herdr)
        return (f"{issue.identifier}: no local task branch or registered Herdr worktree remains in {repo}. "
                "Nothing to clean up.")
    from .cleanup import check_normal_ownership, normal_stopped, removal_retirement
    registry = ContextRegistry()
    endpoint = None
    if target.open_workspace_id is not None and any(
            c["workspace_id"] == target.open_workspace_id for c in registry.list()):
        endpoint = HerdrContexts().endpoint()
    def check_claims():
        return check_normal_ownership(issue.identifier, repo, target, registry, endpoint=endpoint)
    record = None
    identities = HerdrContexts()
    def stopped():
        return normal_stopped(record, herdr, identities, project.base_branch, target.branch)
    git.check_cleanup_target(project.base_branch, target, issue.identifier, require_clean=False)
    record = check_claims()
    observed = None
    def verify_stopped():
        nonlocal observed
        current = stopped()
        if observed is not None and current != observed:
            raise TaskError("Selected execution runtime changed before cleanup; stop its agents and retry")
        observed = current
    git.discard_cleanup_artifacts(project.base_branch, target, issue.identifier, before_discard=verify_stopped)
    snapshot = git.check_cleanup(project.base_branch, target, issue.identifier)
    verify_stopped()
    if herdr.resolve_task(git, issue.identifier, include_remotes=False) != target:
        raise TaskError("Git/Herdr cleanup target changed during validation; nothing was removed")
    retirement = herdr.retirement(target, issue.identifier, project.base_branch)
    if pending is not None and (pending.identifier, pending.base, pending.branch, pending.path, pending.workspace_id) != (
            issue.identifier, project.base_branch, target.branch, target.path, target.open_workspace_id):
        raise TaskError("Pending Herdr retirement state does not match the exact current task workspace; "
                        "nothing was removed")
    reservation = removal_retirement(target, issue.identifier, project.base_branch)
    def admit_removal():
        check_claims()
        ContextRegistry().remember_repository(repo)
        git.save_retirement(reservation)
    if herdr.resolve_task(git, issue.identifier, include_remotes=False) != target:
        raise TaskError("Git/Herdr cleanup target changed before removal; nothing was removed")
    # Every removal retains a durable resource reservation through retirement.
    # Recursive deletion and Herdr closure must not retain the global gate.
    git.remove_task(project.base_branch, target, issue.identifier, snapshot,
                    before_remove=admit_removal, before_delete=verify_stopped)
    herdr_status = "No open Herdr workspace was registered"
    if retirement is not None:
        try:
            retired = herdr.retire(git, retirement)
        except TaskError as error:
            raise TaskError(f"Task cleanup is incomplete. Removed worktree: {target.path}. "
                            f"Removed local branch: {target.branch}. Herdr workspace "
                            f"{retirement.workspace_id!r} could not be confirmed retired: {error}. "
                            f"Rerun task cleanup {issue.identifier} to finish retirement") from None
        with ownership_gate(exclusive=True):
            retire_contexts(issue.identifier, repo, retirement.path, retirement.workspace_id)
            retire_missing_contexts(issue.identifier, repo, herdr, target.path)
            git.clear_retirement(reservation)
        herdr_status = (f"Retired Herdr workspace: {retirement.workspace_id}" if retired else
                        f"Herdr workspace already absent: {retirement.workspace_id}")
    else:
        retire_missing_contexts(issue.identifier, repo, herdr, target.path)
        with ownership_gate(exclusive=True):
            git.clear_retirement(reservation)
    return (f"{issue.identifier}: cleanup complete\nRepo: {repo}\n"
            f"Removed worktree: {target.path}\nRemoved local branch: {target.branch}\n{herdr_status}")


def retire_contexts(identifier, repo, path, workspace_id):
    registry = ContextRegistry()
    contexts = [c for c in registry.list(identifier) if c["repository"] == str(repo)
                and c["worktree"] == str(path) and c["workspace_id"] == workspace_id]
    if not contexts:
        return
    herdr = HerdrContexts()
    endpoint = herdr.endpoint()
    contexts = [c for c in contexts if c["endpoint"] == endpoint]
    terminals = {c["terminal_id"] for c in contexts if c["terminal_id"]}
    if terminals:
        for pane in herdr.snapshot():
            if pane["terminal_id"] in terminals:
                raise TaskError(f"A task context terminal still exists at {pane['pane_id']} after workspace "
                                "cleanup (possibly moved by a human); registry mappings were retained. "
                                f"Inspect it, then rerun task cleanup {identifier}")
    registry.retire(identifier, repo, path, endpoint=endpoint, workspace_id=workspace_id)


def retire_missing_contexts(identifier, repo, herdr, path=None):
    """Finish registry retirement after a workspace was manually closed or a retry.

    A missing pane alone is insufficient: require the exact checkout and workspace
    to be absent. Other servers' mappings remain untouched.
    """
    registry = ContextRegistry()
    contexts = [c for c in registry.list(identifier) if c["repository"] == str(repo)
                and c["worktree"] and (path is None or c["worktree"] == str(path))]
    if not contexts:
        return
    endpoint = HerdrContexts().endpoint()
    live = {w["workspace_id"] for w in herdr.workspaces()}
    for context in contexts:
        if context["endpoint"] != endpoint or context["workspace_id"] in live:
            continue
        try:
            Path(context["worktree"]).lstat()
        except FileNotFoundError:
            retire_contexts(identifier, repo, Path(context["worktree"]), context["workspace_id"])
        except OSError:
            raise TaskError("Cannot confirm context checkout absence; registry mappings were retained") from None


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "cleanup":
            if args.branch is not None and not args.force:
                raise TaskError("Cleanup --branch requires --force and a proven merged execution")
            print(cleanup(args.issue, force=args.force, branch=args.branch))
        elif args.command == "contexts":
            print(inspect_contexts(args.issue, include_retired=args.all))
        elif args.command == "status":
            if bool(args.issue) == args.all:
                raise TaskError("status requires either ISSUE or --all")
            result = task_status(args.issue)
            print(json.dumps(result.as_dict(), ensure_ascii=True, sort_keys=True) if args.json else result.render())
        elif args.command == "review":
            result = review(args.issue, resume=args.resume, agent_kind=args.agent_kind,
                            model=args.model, mode=args.mode, timeout=args.timeout)
            print(json.dumps(result.as_dict(), ensure_ascii=True) if args.json else result.render())
            return {"clean": 0, "findings": 2, "blocked": 3, "failed": 1}[result.state]
        elif args.command == "loop":
            # Interruption is not cancellation of the terminal-owned agent.
            # SIGKILL also leaves the durable claim for ordinary-command recovery.
            def interrupted(signum, frame):
                raise KeyboardInterrupt()
            previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGHUP)}
            try:
                result = loop(args.issue, action=args.action, agent_kind=args.r_agent_kind, model=args.r_model,
                              mode=args.r_mode, max_reviews=args.max_reviews, max_passes=args.max_passes,
                              timeout=args.timeout, from_review=args.from_review,
                              impl_agent_kind=args.i_agent_kind, impl_model=args.i_model, impl_mode=args.i_mode)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            print(json.dumps(result.as_dict(), ensure_ascii=True) if args.json else result.render())
            if args.action in {"pause", "status"}:
                return 0
            return {"clean": 0, "aborted": 0, "paused": 3, "escalated": 3, "interrupted": 130}.get(result.state, 1)
        elif args.command == "pr":
            print(publish(args.issue))
        elif args.command == "integrate":
            def interrupted(signum, frame):
                raise KeyboardInterrupt()
            previous = signal.signal(signal.SIGTERM, interrupted)
            try:
                if args.abandon is not None:
                    if any(v is not None for v in (args.agent_kind, args.model, args.mode, args.timeout)):
                        raise TaskError("--abandon uses recorded identity and cannot select an agent/model/mode/timeout")
                    print(abandon_integration(args.issue, args.abandon))
                else:
                    print(integrate(args.issue, agent_kind=args.agent_kind, model=args.model, mode=args.mode,
                                    timeout=args.timeout if args.timeout is not None else 1800))
            finally:
                signal.signal(signal.SIGTERM, previous)
        else:
            print(start(args.issue, no_agent=args.no_agent, slice=args.slice,
                        agent_kind=args.agent_kind, model=args.model, mode=args.mode))
    except TaskError as error:
        print(f"task: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if args.command == "loop" and args.action == "abort":
            print(f"task: abort interrupted; checkout preserved. Repeat task loop {args.issue} --abort "
                  "to finish reconciliation before resuming task agents", file=sys.stderr)
        elif args.command == "loop":
            print(f"task: controller interrupted; the original agent may still be working. "
                  f"Repeat task loop {args.issue} to reconcile its saved boundary", file=sys.stderr)
        else:
            print("task: interrupted; inspect workspace and issue state before retrying", file=sys.stderr)
        return 130
    return 0

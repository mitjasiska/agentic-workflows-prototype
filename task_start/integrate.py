"""Explicit, isolated agent assistance for reviewed uncommitted conflicts."""

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4

from . import TaskError
from .command import command_name
from .metrics import observe_run
from . import publish as publication
from .agent import AgentExecution, AgentOptions, AgentOverrides, adapter_for, codex_repository_policy, resolve_agent_options
from .config import (check_project_settings_unchanged, load_local, load_projects, project_settings,
                     repository_path, resolve_project)
from .contexts import ContextRegistry, HerdrContexts, allocate_launch, context_observer, context_reference, launch_allocated, now, reconcile
from .handoff import integration_handoff
from .implementation_pass import binding
from .integration_state import IntegrationStore, abandoned_context, check_integration, parse_result
from .linear import Linear
from .publication_rebase import (IntegrationConflict, review_required, check_ignored_obstructions,
                                checkout_index, continue_rebase, integration_plan, replay, validate_record)
from .publication_state import PublicationStore, verify_publication_history
from .review import resolve_review_workspace
from .review_result import publication_fingerprint
from .review_state import digest, snapshot, tree_content
from .sessions import SessionInvalid, has_immutable_identity
from .workspace import Git, run


def isolated_command(path, *args):
    return run(["git", "-C", str(path), "-c", f"core.hooksPath={path / '.git' / 'no-hooks'}",
                "-c", "commit.gpgSign=false", "-c", "rerere.enabled=false",
                "-c", "user.name=Task integration", "-c", "user.email=integration@localhost", *args])


def prepare_isolated(git, record, intent):
    """Reproduce the same replay in a durable repository with its OWN objects."""
    path = Path(record["checkout"])
    if path.is_relative_to(git.repo) or path.is_relative_to(Path(record["binding"]["repository"])):
        raise TaskError("Integration checkout must be outside the task and permanent checkouts")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    def command(*args):
        return isolated_command(path, *args)
    command("init", "--quiet", "--template=", "--object-format=" + git.command("rev-parse", "--show-object-format").strip())
    command("config", "core.hooksPath", str(path / ".git" / "no-hooks"))
    command("config", "rerere.enabled", "false")
    # Workflow-owned synthetic source commit; this does not update any real ref.
    tip = git.command("-c", "commit.gpgSign=false", "commit-tree", intent["tree"],
                      "-p", record["source"]["head"], "-m", intent["message"]).strip()
    command("fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules", "--refmap=", "--",
            str(git.repo), tip, record["base"])
    command("update-ref", "refs/workflow/source", tip)
    command("update-ref", "refs/workflow/base", record["base"])
    command("checkout", "--quiet", "--detach", tip)
    try:
        replay(command, record["base"], record["source"]["base_commit"])
    except IntegrationConflict:
        conflicts = command("ls-files", "--unmerged", "-z")
    else:
        raise TaskError("Durable integration did not reproduce the deterministic conflict; inspect it manually")
    # Leave the conflicted index/files, but no sequencer requiring an agent-owned
    # commit. The agent edits files; the workflow stages and constructs history.
    command("rebase", "--quit")
    if command("rev-parse", "HEAD").strip() != record["base"]:
        raise TaskError("Unsupported isolated replay history; inspect it manually")
    command("update-ref", "refs/heads/integration", record["base"])
    command("symbolic-ref", "HEAD", "refs/heads/integration")
    return conflicts


def isolated_index(path):
    git = Git(path)
    return dict(entries=digest(os.fsencode(git.command("--no-optional-locks", "ls-files", "--stage", "-z"))),
                flags=digest(os.fsencode(git.command("--no-optional-locks", "ls-files", "-v", "-z"))))


def completion_path(record):
    # Codex workspace-write permits its temporary roots, but protects .git and
    # does not grant writes beside a checkout. Retain this directory on every
    # exit: an interrupted controller must not delete an active agent's output.
    root = Path(tempfile.gettempdir()).resolve()
    if any(root.is_relative_to(Path(p)) for p in
           (record["checkout"], record["binding"]["repository"], record["binding"]["worktree"])):
        raise TaskError("Integration output must be outside all checkouts; check TMPDIR")
    directory = Path(tempfile.mkdtemp(prefix=f"task-integration-{record['pass_id']}-", dir=root))
    return directory / "result.json"


def history_identity(path):
    git = Git(path)
    metadata = Path(git.command("rev-parse", "--absolute-git-dir").strip())
    if (metadata != path / ".git" or metadata.is_symlink() or (metadata / "objects").is_symlink()
            or (metadata / "objects" / "info" / "alternates").exists()
            or git.command("remote").strip()):
        raise TaskError("Isolated Git identity changed; no integration is safe")
    logs = metadata / "logs"
    return dict(head=git.command("rev-parse", "HEAD").strip(),
                refs=git.command("show-ref"),
                config=digest((metadata / "config").read_bytes()),
                logs={str(p.relative_to(logs)): digest(p.read_bytes()) for p in sorted(logs.rglob("*")) if p.is_file()})


def agent_pass(issue, project, repo, workspace, anchor, local, options, adapter, registry, identities,
               record, state_store, verify_source, timeout, *, previous_run_id=None):
    path = Path(record["checkout"])
    pane = identities.split(anchor, path)
    isolated = replace(workspace, path=path, branch="integration", pane_id=pane["pane_id"], tab_id=pane["tab_id"])
    policy = codex_repository_policy(local.codex_repository_profiles, project.repo_name) if options.kind == "codex" else {}
    execution = AgentExecution(issue, repo, isolated, options, "Integration handoff pending", purpose="integration",
                               policy=dict(policy, session_reporting=True))
    context_id = allocate_launch(execution, registry, identities)
    record.update(context=dict(context_id=context_id), state="running")
    state_store.write(record)  # Durable delivery claim precedes any prompt.
    output = Path(record["output"])
    execution = replace(execution, handoff=integration_handoff(issue, record, output))
    persist = context_observer(registry, context_id)
    expected = None

    def observe(_=None):
        nonlocal expected
        context = registry.get(context_id)
        if (context["role"] != "integration" or context["worktree"] != str(path)
                or context["repository"] != str(repo) or context["endpoint"] != identities.endpoint()
                or context["workspace_id"] != workspace.workspace_id
                or any(context[k] != pane[k] for k in ("pane_id", "tab_id", "terminal_id"))
                or context["state"] not in {"launching", "awaiting_user", "active"} or context["retired_at"]
                or (context["agent"], context["model"], context["mode"]) != (options.kind, options.model, options.mode)):
            raise TaskError("Integration context identity changed")
        reference = context_reference(context)
        if expected is not None:
            if binding(context) != expected:
                raise TaskError("Integration conversation changed; no replacement is allowed")
            return
        if reference is None:
            return
        if options.kind == "codex":
            # The context guard already retains identity during trust/setup.
            # As in initial loop startup, adopt it only after the adapter's
            # bounded readiness/history check; completion verifies it again.
            if context["resumability"] != "yes":
                return
            verified = reference
        else:
            try:
                verified = adapter.verify_session(isolated, reference)
            except SessionInvalid:
                if has_immutable_identity(reference, options.kind):
                    raise
                return
        persist(dict(herdr_session=json.dumps(verified), resumability="yes"))
        expected = binding(registry.get(context_id))
        record["context"] = expected
        state_store.write(record)

    execution = replace(execution, runtime_observer=observe)
    verify_source()
    with observe_run() as metric:
        metric.start(registry, issue, options, "integration", run_id=record["pass_id"], context_id=context_id,
                     previous_run_id=previous_run_id)
        launch_allocated(adapter, execution, context_id, registry, identities, persist_observer=persist)
        def poll_observer(values):
            persist(values)
            observe()
        execution = replace(execution, runtime_observer=poll_observer)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            verify_source()
            observe()
            current, notes = reconcile(registry.get(context_id), identities.snapshot())
            if (current is None or current.get("agent") != options.kind
                    or Path(current.get("cwd", "")).resolve() != path
                    or current["terminal_id"] != pane["terminal_id"]
                    or any(n in {"agent mismatch", "session mismatch", "session identity invalid"} for n in notes)):
                raise TaskError("Integration runtime identity changed; inspect it before recovery")
            status = adapter.status(execution, pane["terminal_id"], expected["session"] if expected else None)
            observe()
            if status == "blocked":
                metric.outcome("blocked")
                raise TaskError("Integration agent requires a human decision; inspect its context")
            if status in {"idle", "done"} and output.exists():
                if expected is None:
                    raise TaskError("Integration has no verified immutable conversation identity")
                verified = adapter.verify_session(isolated, expected["session"])
                persist(dict(herdr_session=json.dumps(verified), resumability="yes"))
                observe()
                if (output.parent.resolve() != output.parent or output.is_symlink()
                        or not output.is_file() or output.stat().st_size > 1024 * 1024):
                    raise TaskError("Invalid integration result file")
                result = parse_result(output.read_text(encoding="utf-8"), record)
                if time.monotonic() >= deadline:
                    metric.outcome("timed_out")
                    raise TaskError("Timed out waiting for verified integration completion")
                metric.outcome(result["state"])
                return result
            time.sleep(0.25)
        metric.outcome("timed_out")
        raise TaskError("Timed out waiting for integration; inspect its retained context. No prompt replay is safe")


def prove_result(git, record, intent):
    path = Path(record["checkout"])
    isolated = Git(path)
    if history_identity(path) != record["history"]:
        raise TaskError("Integration agent changed Git history; only workflow-owned commits are allowed")
    if isolated_index(path) != record["isolated_index"]:
        raise TaskError("Integration agent changed the isolated index; only the workflow may stage resolutions")
    # Completion and immutable provider identity have been verified. Staging is
    # controller-owned so workspace-write agents need no protected .git writes.
    # This changes only the retained isolated index; source installation still
    # requires the complete tree/history proof and durable revocation below.
    try:
        isolated_command(path, "add", "--all", "--", ".")
    except TaskError:
        raise TaskError("Workflow could not stage the isolated resolution; inspect its retained Git state") from None
    try:
        isolated_command(path, "diff", "--cached", "--check")
    except TaskError:
        raise TaskError("Integrated result failed git diff --cached --check; inspect conflict markers, whitespace, "
                        "and the retained Git state") from None
    publication.check_publishable_index(isolated)
    before = snapshot(path, record["base"], "integration")
    with publication.prepared_index(isolated) as (_, tree, index):
        if tree_content(path, tree) != before.content:
            raise TaskError("Integrated tree does not represent the validated files/modes")
    if tree == isolated.command("rev-parse", f"{record['base']}^{{tree}}").strip():
        raise TaskError("Integrated result has no task changes; inspect requirements manually")
    if snapshot(path, record["base"], "integration") != before or history_identity(path) != record["history"]:
        raise TaskError("Isolated integration changed during proof")
    # Copy only proven tree/blob objects. No agent-owned history is imported and
    # no source refs, files, or index are changed by object transport.
    packed = run(["git", "-C", str(path), "pack-objects", "--stdout", "--revs"], input=(tree + "\n").encode())
    run(["git", "-C", str(git.repo), "index-pack", "--stdin"], input=os.fsencode(packed))
    if tree_content(git.repo, tree) != before.content:
        raise TaskError("Integrated object transfer did not preserve the proven tree")
    check_ignored_obstructions(git, tree)
    with checkout_index(git, intent["tree"], tree):
        pass
    plan = dict(version=1, binding=record["binding"], identity=record["identity"], source=record["source"],
                source_tree=intent["tree"], source_index=intent["index"], base=record["base"],
                steps=[dict(tree=tree, message=intent["message"], author=None)], commits=[], tree=tree,
                content=before.content, index=index, result=None,
                publication=dict(title=intent["title"], body=intent["body"]), integration_id=record["pass_id"])
    plan["publication_fingerprint"] = publication_fingerprint(plan["publication"])
    validate_record(plan)
    return plan


def abandonment_runtime(context, identities, adapter):
    """An absent terminal or its exact idle shell, never merely an idle agent."""
    if identities.endpoint() != context["endpoint"]:
        raise TaskError("Integration Herdr endpoint changed; abandonment refused")
    path = Path(context["worktree"])
    related = [p for p in identities.snapshot() if p["pane_id"] == context["pane_id"]
        or p["terminal_id"] == context["terminal_id"]
        or any(p.get(k) and Path(p[k]).resolve() == path for k in ("cwd", "foreground_cwd"))]
    if not related:
        return None
    if len(related) != 1:
        raise TaskError("Integration runtime is ambiguous; abandonment refused")
    pane = related[0]
    if (any(pane[k] != context[k] for k in ("pane_id", "terminal_id", "workspace_id", "tab_id"))
            or Path(pane.get("cwd", "")).resolve() != path
            or pane.get("agent_session") is not None or pane.get("agent") not in {None, context["agent"]}
            or pane.get("agent_status") not in {None, "idle", "done"}):
        raise TaskError("Integration runtime/session identity is live or uncertain; abandonment refused")
    try:
        info = adapter.command("pane", "process-info", "--pane", context["pane_id"])["process_info"]
        shell = info["shell_pid"]
        if (info["pane_id"] != context["pane_id"] or type(shell) is not int or shell <= 0
                or type(info["foreground_process_group_id"]) is not int
                or info["foreground_process_group_id"] <= 0
                or not isinstance(info["foreground_processes"], list)
                or any(type(p["pid"]) is not int or p["pid"] <= 0 for p in info["foreground_processes"])):
            raise ValueError("invalid process evidence")
    except (KeyError, TypeError, ValueError):
        raise TaskError("Cannot prove an idle integration shell; abandonment refused") from None
    if (info["foreground_process_group_id"] != shell
            or [p["pid"] for p in info["foreground_processes"]] != [shell]):
        pids = [p["pid"] for p in info["foreground_processes"]]
        raise TaskError(f"Integration execution is still live or uncertain in pane {context['pane_id']} "
                        f"(terminal {context['terminal_id']}, shell PID {shell}, foreground PIDs {pids}). "
                        "An idle agent is not an idle shell. Inspect and quit the agent in this exact pane yourself, "
                        "then rerun --abandon with the same context ID. Do not close/recreate the pane or delete evidence. "
                        "Abandonment still requires stopped-process and provider-history proof")
    return dict(pane=pane, shell_pid=shell)


def check_git_environment():
    if any(k in os.environ for k in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
                                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")):
        raise TaskError(f"Unset Git routing environment overrides before {command_name()} integrate")


def abandon_integration(identifier, context_id):
    """Explicitly abandon only an unproven, sessionless, stopped Codex attempt."""
    # Reject invalid inputs without creating lock state or loading configuration.
    # Exact workspace selection and the publication lock guard execution work.
    if not re.fullmatch(re.escape(identifier) + r"-G[1-9][0-9]*", context_id):
        raise TaskError("--abandon requires this issue's exact integration context ID, such as DEV-30-G1")
    check_git_environment()
    return _abandon_integration(identifier, context_id)


def _abandon_integration(identifier, context_id):
    local = load_local()
    issue = Linear(local.api_key).get_issue(identifier)
    project = resolve_project(load_projects(), issue.project)
    repo = repository_path(local, project)
    registry, identities = ContextRegistry(), HerdrContexts()
    workspace, _, _, endpoint = resolve_review_workspace(issue, project, repo, registry, identities)
    target = dict(issue=identifier, repository=str(repo), worktree=str(workspace.path), branch=workspace.branch,
                  base_branch=project.base_branch, endpoint=endpoint, workspace_id=workspace.workspace_id)
    with PublicationStore(workspace.path).locked() as store:
        state_store = IntegrationStore(workspace.path)
        record, saved = state_store.read(), store.read()
        if (record is None or record["binding"] != target or record["context"] != {"context_id": context_id}
                or record["state"] not in {"uncertain", "abandoned"}
                or record["plan"] is not None or "completion" in record or saved["rebase"] is not None):
            raise TaskError("Abandonment requires the exact uncertain integration without completion or installation proof")
        context = registry.get(context_id)
        if record["state"] == "abandoned" and context == abandoned_context(record):
            state_store.check_abandoned(record, registry, identifier)
            return f"{context_id} is already abandoned; evidence retained. Run {command_name()} integrate {identifier} explicitly for a fresh attempt."
        if (context["state"] != "uncertain" or context["retired_at"] is not None or context["agent"] != "codex"
                or context["resumability"] != "unknown"
                or any(context[k] is not None for k in ("session_id", "session_kind", "herdr_session"))
                or context["role"] != "integration" or context["worktree"] != record["checkout"]
                or any(context[k] != target[k] for k in ("issue", "repository", "endpoint", "workspace_id"))
                or any(context[k] != record["options"][v] for k, v in (("agent", "kind"), ("model", "model"), ("mode", "mode")))):
            raise TaskError("Abandonment requires an uncertain Codex context with no session identity or resumability")
        path = Path(record["checkout"])
        output = Path(record.get("output", path.parent / "result.json"))
        def check_evidence():
            if (path.resolve() != path or output.exists() or output.is_symlink()
                    or history_identity(path) != record["history"]
                    or not Git(path).command("ls-files", "--unmerged", "-z")):
                raise TaskError("Integration contains completion or changed/resolved history; inspect it manually before recovery")
        check_evidence()
        adapter = adapter_for(AgentOptions(**record["options"]))
        before = abandonment_runtime(context, identities, adapter)
        # Pre-sandbox-fix attempts did not record an output location, source scope,
        # or isolated index. Permit only proven readiness-only history for these
        # legacy claims; a missing receipt never permits an integration replay.
        legacy = not any(k in record for k in ("output", "slice", "isolated_index"))
        proof = adapter.verify_abandonment(path, before["shell_pid"] if before else None,
                                          allow_readiness=legacy and before is not None)
        if abandonment_runtime(context, identities, adapter) != before:
            raise TaskError("Integration runtime changed during abandonment checks")
        check_evidence()
        current, _, _, live_endpoint = resolve_review_workspace(issue, project, repo, registry, identities)
        if (current != workspace or live_endpoint != endpoint or state_store.read() != record
                or store.read() != saved or registry.get(context_id) != context):
            raise TaskError("Integration/task identity or evidence changed; abandonment refused")
        if record["state"] == "uncertain":
            record = dict(record, state="abandoned", abandonment=dict(at=now(), context=context, proof=proof))
            abandoned_context(record)
            state_store.write(record)  # A partial transition gates every new handoff.
        elif record["abandonment"]["context"] != context:
            raise TaskError("Abandonment context changed; inspect retained provenance")
        state_store.archive(record, create=True)  # Durable audit survives the next attempt's claim.
        registry.abandon_integration(context, record["abandonment"]["at"])
        return (f"Abandoned {context_id}; checkout, output location, context identity and provenance retained. "
                f"Run {command_name()} integrate {identifier} explicitly for a fresh attempt.")


def integrate(identifier, *, agent_kind=None, model=None, mode=None, timeout=1800):
    if timeout <= 0:
        raise TaskError("Integration timeout must be positive")
    check_git_environment()
    local = load_local()
    issue = Linear(local.api_key).get_issue(identifier)
    project = resolve_project(load_projects(), issue.project)
    repo = repository_path(local, project)
    global_settings = local
    local = project_settings(global_settings, repo)
    options = resolve_agent_options(local.agent, AgentOverrides(agent_kind, model, mode))
    if options.model is None or options.mode is None:
        raise TaskError("Integration requires explicit model and mode in [agent] or --model/--mode")
    adapter = adapter_for(options)
    adapter.check_available()
    registry, identities = ContextRegistry(), HerdrContexts()
    workspace, anchor, _, endpoint = resolve_review_workspace(issue, project, repo, registry, identities)
    with PublicationStore(workspace.path).locked() as store:
        state_store = IntegrationStore(workspace.path)
        previous = state_store.read()
        if previous is not None and previous["state"] == "abandoned":
            state_store.check_abandoned(previous, registry, identifier)
        elif previous is not None or any(c["role"] == "integration" and c["state"] != "retired"
                                         for c in registry.list(identifier, include_retired=True)):
            raise TaskError("Integration already has durable evidence; inspect its context and checkout. "
                            f"No replay is allowed; a proven pending installation continues through {command_name()} pr. "
                            f"For a sessionless stopped uncertain Codex attempt, inspect {command_name()} integrate --help for --abandon")
        saved = store.read()
        accepted = saved["acceptance"]
        publication.verify_acceptance(accepted, issue, project, repo, workspace, endpoint, registry)
        source = accepted["review_state"]
        if source["head"] != source["base_commit"] or saved["rebase"] is not None:
            raise TaskError("Integration v1 requires reviewed HEAD equal to the reviewed base and uncommitted changes; recover other histories manually")
        git, permanent = Git(workspace.path), Git(repo)
        publication.check_publishable_index(git)
        identity = publication.remote_identity(permanent, project.base_branch, workspace.branch)
        publication.verify_remote_identity(git, project.base_branch, workspace.branch, identity)
        publication.api_credential(required=True)
        target = dict(issue=issue.identifier, repository=str(repo), worktree=str(workspace.path),
                      branch=workspace.branch, base_branch=project.base_branch, endpoint=endpoint, workspace_id=workspace.workspace_id)
        history_binding = {k: target[k] for k in ("issue", "repository", "worktree", "branch", "base_branch")}
        verify_publication_history(saved["publication_history"], history_binding, identity)
        publication.require_never_published(saved["publication_history"])

        def record_published(head):
            # Retain positive evidence even when a later remote observation loses it.
            if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
                raise TaskError("Cannot identify observed publication; inspect remote history")
            saved["publication_history"] = dict(version=1, binding=history_binding, identity=identity, state="published", head=head)
            store.write(saved)

        def verify_source():
            current, _, _, live_endpoint = resolve_review_workspace(issue, project, repo, registry, identities)
            if current != workspace or live_endpoint != endpoint:
                raise TaskError("Source task identity changed during integration")
            publication.check_publishable_index(git)
            if snapshot(workspace.path, source["base_commit"], workspace.branch).as_dict() != source:
                raise TaskError("Source task state drifted since clean review; integration installs nothing")
            if store.read() != saved:
                raise TaskError("Source acceptance/publication evidence changed during integration")
            if saved["acceptance"] is not None:
                publication.verify_acceptance(saved["acceptance"], issue, project, repo, workspace, endpoint, registry)

        def verify_unpublished():
            publication.require_never_published(saved["publication_history"])
            publication.require_unpublished(git, permanent, identity, project.base_branch, workspace.branch, base,
                                            record_published=record_published)

        verify_source()
        base, remote_head = publication.remote_heads(workspace.path, identity, project.base_branch, workspace.branch)
        pulls = list(publication.pull_requests(identity["repository"], workspace.branch))
        if remote_head is not None or pulls:
            record_published(remote_head or pulls[0]["head"].get("sha"))
            raise TaskError("Task is already published; integration is forbidden. Recover manually without force-push")
        if base == source["base_commit"]:
            raise TaskError(f"Reviewed base has not advanced; use {command_name()} pr")
        title, body = publication.prepare_metadata(issue, accepted)
        with publication.prepared_index(git) as (_, tree, index):
            if tree_content(git.repo, tree) != source["content"]:
                raise TaskError("Source tree cannot represent all reviewed bytes/modes")
        intent = dict(tree=tree, index=index, title=title, body=body, message=f"{title}\n\nTask-Review: {accepted['pass_id']}")
        if saved["intent"] is not None:
            prior = saved["intent"]
            if (prior.get("publishing_head") or prior.get("reuse_head") or prior.get("pass_id") != accepted["pass_id"]
                    or any(prior.get(k) != v for k, v in intent.items())
                    or not publication.same_remote_identity(prior, identity)):
                raise TaskError("Unsupported publishing intent; inspect its original contract before integration")
        verify_source()
        publication.advance_base(git, permanent, identity, project.base_branch, workspace.branch, source["base_commit"], base)
        check_project_settings_unchanged(global_settings, repo, local)
        verify_unpublished()
        verify_source()
        try:
            integration_plan(git, accepted, intent, base)
        except IntegrationConflict:
            pass
        else:
            raise TaskError(f"Deterministic integration is conflict-free; run {command_name()} pr. No integration agent was launched")
        verify_source()
        verify_unpublished()
        pass_id = str(uuid4())
        path = (registry.path.parent / "integrations" / pass_id / "checkout").resolve()
        record = dict(version=1, pass_id=pass_id, state="preparing", binding=target, source=source,
                      base=base, identity=identity, options=asdict(options), checkout=str(path), context=None, plan=None,
                      slice=workspace.slice)
        state_store.write(record)
        try:
            record["conflicts"] = prepare_isolated(git, record, intent)
            record["history"] = history_identity(path)
            record["isolated_index"] = isolated_index(path)
            record["output"] = str(completion_path(record))
            state_store.write(record)
            result = agent_pass(issue, project, repo, workspace, anchor, local, options, adapter, registry, identities,
                                record, state_store, verify_source, timeout, previous_run_id=accepted["pass_id"])
            record["completion"] = result
            if result["state"] != "completed":
                record["state"] = "stopped"
                state_store.write(record)
                raise TaskError(f"Integration reported {result['state']}; inspect the retained context for human recovery")
            verify_source()
            verify_unpublished()
            plan = prove_result(git, record, intent)
            verify_source()
            verify_unpublished()
            record.update(state="proven", plan=plan)
            state_store.write(record)  # Complete proof is durable BEFORE revocation.
            saved.update(acceptance=None, intent=None, rebase=plan)
            store.write(saved)  # Revoke old authorization BEFORE real mutation.
            installation = check_integration(store, saved, registry, issue, project, repo, identities)

            try:
                continue_rebase(git, saved, store, publication.native_git, installation,
                                verify_identity=installation)
            except TaskError as error:
                if str(error) != review_required() or saved["rebase"]["result"] is None:
                    raise
            installation.finish()
            record["state"] = "installed"
            return review_required() + f" Integration checkout retained at {path}."
        except BaseException as error:
            if record["state"] not in {"proven", "installed", "stopped"}:
                record["state"] = "uncertain"
                state_store.write(record)
                if record["context"]:
                    registry.update(record["context"]["context_id"], state="uncertain")
            if isinstance(error, (OSError, UnicodeError)):
                raise TaskError(f"Integration files/result could not be read or saved; inspect the retained checkout at {path}. "
                                "No prompt replay is safe") from None
            raise

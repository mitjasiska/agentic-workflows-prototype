"""One fresh or explicitly resumed review pass, without routing fixes or reports."""

from dataclasses import asdict, replace
from contextlib import nullcontext
import json
from pathlib import Path
import re
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

from . import TaskError
from .metrics import observe_run
from .pass_delivery import object_digest
from .agent import AgentExecution, AgentOptions, AgentOverrides, adapter_for, codex_repository_policy, resolve_agent_options
from .config import load_local, load_projects, project_settings, repository_path, resolve_project
from .contexts import now, ContextRegistry, HerdrContexts, context_observer, context_reference, launch_registered, reconcile
from .handoff import review_handoff
from .linear import Linear
from .review_result import ReviewResult, parse_verdict, publication_fingerprint
from .publication_rebase import validate_commits, validate_record
from .publication_state import PublicationStore, prepare_review_continuation
from .integration_state import check_integration
from .review_state import snapshot
from .sessions import SessionInvalid, has_immutable_identity, merge_session, same_session
from .workspace import Git, Herdr, Workspace


def resolve_review_workspace(issue, project, repo, registry, identities, *, pending_implementation=None,
                             local_only=False, recovery_implementation=None, abort_implementation=None,
                             metrics_context=None):
    git, herdr = Git(repo), Herdr(repo)
    if local_only:
        git.check_repository()
    else:
        git.check_base(project.base_branch)
    target = herdr.resolve_task(git, issue.identifier, include_remotes=False)
    if target is None or not target.open_workspace_id:
        raise TaskError("Review requires the exact existing, open Herdr task worktree")
    # Reuse existing strict checkout/repository/scope validation, allowing dirty task files.
    git.check_cleanup_target(project.base_branch, target, issue.identifier, require_clean=False,
                             require_base=not local_only)
    endpoint, panes = identities.endpoint(), identities.snapshot()
    contexts = registry.list(issue.identifier)
    if any(c["repository"] != str(repo) or (c["role"] != "integration" and c["worktree"] != str(target.path))
           or c["endpoint"] != endpoint or c["workspace_id"] != target.open_workspace_id for c in contexts):
        raise TaskError("Issue context repository/worktree/Herdr mappings are inconsistent")
    bound = [c for c in contexts if recovery_implementation or abort_implementation or c["state"] in {"active", "reviewing"}]
    for index, context in enumerate(bound):
        for other in bound[:index]:
            shared_runtime = any(context[key] and context[key] == other[key] for key in ("pane_id", "terminal_id"))
            shared_session = (context["agent"] == other["agent"] and context["session_id"] and other["session_id"]
                and same_session(dict(agent=context["agent"], kind=context["session_kind"], value=context["session_id"]),
                                 dict(agent=other["agent"], kind=other["session_kind"], value=other["session_id"]), context["agent"]))
            if shared_runtime or shared_session:
                raise TaskError("Multiple workflow contexts claim the same reviewer/implementation identity")
    implementation_state = "launching" if pending_implementation else "active"
    implementations = [c for c in contexts if c["role"] == "implementation" and
                       (c["state"] == implementation_state or recovery_implementation == c["context_id"]
                        and c["state"] in {"launching", "uncertain"}
                        or abort_implementation == c["context_id"]
                        and c["state"] in {"launching", "uncertain", "awaiting_user"})]
    if len(implementations) != 1:
        raise TaskError("Review requires exactly one active implementation context mapping")
    if pending_implementation and (len(contexts) != 1 or implementations[0]["context_id"] != pending_implementation):
        raise TaskError("Pending implementation allocation changed; inspect before launching")
    anchor, notes = reconcile(implementations[0], panes)
    if anchor is None or any(n in {"agent mismatch", "session mismatch", "session identity invalid"} for n in notes):
        raise TaskError("Implementation pane identity is missing or inconsistent")
    # Position, focus and display label are deliberately not identity. A tab move
    # within this task workspace is safe when terminal identity still matches.
    if Path(anchor.get("cwd", "")).resolve() != target.path:
        raise TaskError("Implementation pane does not belong to the exact task checkout")
    workspace = Workspace(target.branch, target.path, target.open_workspace_id,
                          anchor["tab_id"], anchor["pane_id"], "existing task tab",
                          git.resolve_scope(target.path, target.branch, issue.identifier, None))
    herdr.retirement(target, issue.identifier, project.base_branch)  # Read-only workspace metadata validation.
    base = None if local_only else git.command("rev-parse", "--verify", f"refs/heads/{project.base_branch}^{{commit}}").strip()
    if metrics_context is not None:
        metrics_context["implementation_context_id"] = implementations[0]["context_id"]
    return workspace, anchor, base, endpoint


def validate_review_selector(context_id):
    if not isinstance(context_id, str) or not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*-R[1-9][0-9]*", context_id):
        raise TaskError("--resume requires an explicit review context ID such as DEV-20-R2")


def previously_verified(context):
    return (context["resumability"] == "yes"
            and has_immutable_identity(context_reference(context), context["agent"]))


def harmless_reviewer_note(context, note, pane):
    return (note in {"pane changed", "tab changed"} or note.startswith("renamed/unlabeled:")
            or (note == "session unknown/stale" and context["agent"] == "codex"
                and context["session_kind"] == "id")
            # Liveness/reporting absence is not contradictory identity evidence.
            # Only retained immutable identity plus prior verification permits it.
            or (note in {"agent absent; session unknown", "session unknown/stale"}
                and "agent" in pane and pane["agent"] in {None, context["agent"]}
                and previously_verified(context)))


def resolve_reviewer(context_id, issue, workspace, repo, endpoint, registry, identities):
    validate_review_selector(context_id)
    context = registry.get(context_id)
    if (context["issue"] != issue.identifier or context["role"] != "review"
            or context["state"] != "active" or context["retired_at"]
            or not previously_verified(context)):
        raise TaskError("Reviewer ID is mismatched, retired, stale, busy, or non-resumable")
    if (context["repository"] != str(repo) or context["worktree"] != str(workspace.path)
            or context["workspace_id"] != workspace.workspace_id or context["endpoint"] != endpoint):
        raise TaskError("Reviewer context does not match this task repository/worktree/Herdr server")
    pane, notes = reconcile(context, identities.snapshot(), allow_relocation=True)
    if pane is None:
        if notes != ["missing pane"]:
            raise TaskError("Reviewer pane is moved/stale; cannot safely recreate it in the task tab")
    elif (Path(pane.get("cwd", "")).resolve() != workspace.path
          or any(not harmless_reviewer_note(context, n, pane) for n in notes)):
        raise TaskError("Reviewer pane/session is stale or mismatched: " + "; ".join(notes))
    if pane and any(pane[k] != context[k] for k in ("pane_id", "tab_id")):
        registry.rebind_review_pane(context, pane)
        context = registry.get(context_id)
    return context, pane


def finalize_reviewer(context_id, workspace, adapter, registry, identities):
    """Release the pass claim using live identity evidence, independently of its verdict."""
    context = registry.get(context_id)
    retained = previously_verified(context)
    try:
        endpoint, panes = identities.endpoint(), identities.snapshot()
    except TaskError:
        # No new observation: preserve verified evidence, but fail this pass's
        # final identity check. Every later explicit resume must check it again.
        registry.update(context_id, state="active" if retained else "uncertain")
        raise
    pane, notes = reconcile(context, panes, allow_relocation=True)
    # A closed pane is not lost conversation identity. Check the retained provider
    # history below; an explicit resume must verify it again before recreation.
    missing = pane is None and notes == ["missing pane"] and retained
    if (context["endpoint"] != endpoint or (pane is None and not missing)
            or (pane is not None and (Path(pane.get("cwd", "")).resolve() != workspace.path
                or any(not harmless_reviewer_note(context, n, pane) and n != "session identity unknown" for n in notes)))):
        registry.update(context_id, state="uncertain", resumability="unknown")
        raise TaskError("Reviewer runtime identity is missing, ambiguous, or changed: " + "; ".join(notes))
    if pane and any(pane[k] != context[k] for k in ("pane_id", "tab_id")):
        registry.rebind_review_pane(context, pane)
    reference = context_reference(context)
    resumability = "yes" if retained else "unknown"
    note = None
    if reference:
        try:
            verified = adapter.verify_review_session(workspace, reference)
        except SessionInvalid:
            if retained:
                registry.update(context_id, state="uncertain", resumability="no")
                raise
            # A newly discovered runtime reference has not proved persistence yet.
        except (TaskError, OSError):
            note = ("Provider session verification is temporarily unavailable; "
                    + ("last verified resumability retained." if retained else "resumability remains unverified."))
        else:
            context_observer(registry, context_id)(dict(herdr_session=json.dumps(verified)))
            resumability = "yes"
    registry.update(context_id, state="active", resumability=resumability)
    return note


def review(identifier: str, *, resume: str | None = None, agent_kind: str | None = None,
           model: str | None = None, mode: str | None = None, timeout: float = 1800) -> ReviewResult:
    if timeout <= 0:
        raise TaskError("Review timeout must be positive")
    if resume is not None:
        validate_review_selector(resume)
    if resume is not None and any(v is not None for v in (agent_kind, model, mode)):
        raise TaskError("--resume preserves the original agent/model/mode; selection flags require a fresh review")
    local = load_local()
    issue = Linear(local.api_key).get_issue(identifier)
    project = resolve_project(load_projects(), issue.project)
    repo = repository_path(local, project)
    if resume is None:
        local = project_settings(local, repo)
    registry, identities = ContextRegistry(), HerdrContexts()
    metrics_context = {}
    workspace, anchor, base, endpoint = resolve_review_workspace(issue, project, repo, registry, identities,
                                                                local_only=True, metrics_context=metrics_context)
    with PublicationStore(workspace.path).locked() as store:
        return review_pass(issue, project, repo, registry, identities, workspace, anchor, base, endpoint,
                           local, resume, agent_kind, model, mode, timeout, store, metrics_context=metrics_context)


def review_pass(issue, project, repo, registry, identities, workspace, anchor, base, endpoint,
                local, resume, agent_kind, model, mode, timeout, store, *, loop_feedback=None,
                before_handoff=None, pass_observer=None, delivery=None, metrics_context=None):
    registry.check_pending_startup(issue.identifier, repo, workspace.path, "review")
    context, pane = (resolve_reviewer(resume, issue, workspace, repo, endpoint, registry, identities)
                     if resume is not None else (None, None))
    options = (AgentOptions(context["agent"], context["model"], context["mode"]) if context else
               resolve_agent_options(local.reviewer, AgentOverrides(agent_kind, model, mode), section="reviewer"))
    # Omitted provider defaults cannot be safely preserved across machine changes.
    if options.model is None or options.mode is None:
        raise TaskError("Review requires explicit model and mode in [reviewer] or --model/--mode so resume preserves them")
    adapter = adapter_for(options)
    adapter.check_available()
    reference = context_reference(context) if context else None
    if context:
        reference = adapter.verify_review_session(workspace, reference)
    saved = store.read()
    check_integration(store, saved, registry, issue, project, repo, identities)
    if saved["rebase"] is not None and saved["rebase"].get("result") is None:
        raise TaskError("Unpublished rebase is pending; rerun task pr to finish it before starting independent review")
    if saved["rebase"] is not None and saved["publication_history"] is None:
        # Review the installed basis, even if another task has advanced main.
        # Publication independently checks whether this base is still eligible.
        validate_record(saved["rebase"])
        base = saved["rebase"]["base"]
    elif base is None:
        permanent = Git(repo)
        permanent.check_base(project.base_branch)
        base = permanent.command("rev-parse", "--verify", f"refs/heads/{project.base_branch}^{{commit}}").strip()
    prepare_review_continuation(saved, Git(workspace.path),
        dict(issue=issue.identifier, repository=str(repo), worktree=str(workspace.path),
             branch=workspace.branch, base_branch=project.base_branch), base)
    # Starting another pass revokes earlier acceptance, including if interrupted.
    saved["acceptance"] = None
    saved["intent"] = None
    store.write(saved)
    before = snapshot(workspace.path, base, workspace.branch)
    frozen, frozen_fingerprint = None, None
    if saved["rebase"] is not None:
        # Exact completed integration provenance survives a fresh/failed review,
        # but intentional implementation edits establish a new contract.
        if saved["rebase"]["result"] != before.as_dict():
            saved["rebase"] = None
            store.write(saved)
        else:
            validate_record(saved["rebase"])
            if validate_commits(Git(workspace.path), saved["rebase"]) != before.head:
                raise TaskError("Rebased commit provenance differs from the current review state; inspect it before review")
            frozen = dict(saved["rebase"]["publication"])
            frozen_fingerprint = publication_fingerprint(frozen)
    pass_id, pass_kind = str(uuid4()), "resumed" if resume is not None else "fresh"
    standalone = None
    if delivery is None and loop_feedback is None:
        from .review_handoff_state import ReviewHandoffStore
        handoff_env = SimpleNamespace(issue=issue, project=project, repo=repo, registry=registry,
                                     workspace=workspace, endpoint=endpoint)
        standalone = ReviewHandoffStore(workspace.path)
        handoff_record = standalone.begin(handoff_env, before.as_dict())
    context_id, verdict, invalidated, post = resume, None, False, None
    state, summary, claimed = "failed", "Reviewer did not complete", False
    fresh_launch_failed = False

    def checkpoint():
        # Pass-local and sticky: restoration cannot undo an observed change.
        nonlocal invalidated, post
        try:
            post = snapshot(workspace.path, base, workspace.branch)
            invalidated |= post != before
            if frozen is not None:
                invalidated |= store.read()["rebase"] != saved["rebase"]
        except TaskError:
            post = None
            invalidated = True
        if delivery and invalidated and delivery.state.get("delivery"):
            delivery.invalidate_review()

    policy = codex_repository_policy(local.codex_repository_profiles, project.repo_name) if options.kind == "codex" else {}
    policy = dict(policy, read_only=True)
    if standalone:
        policy["standalone_review"] = True
    with observe_run() as metric:
        try:
            with (nullcontext(None) if delivery else tempfile.TemporaryDirectory(prefix="task-review-")) as directory:
                if directory is not None and Path(directory).resolve().is_relative_to(workspace.path):
                    raise TaskError("Temporary review output must be outside the task checkout; check TMPDIR")
                output = Path(directory) / "result.json" if directory is not None else None

                def handoff(allocated):
                    nonlocal context_id, claimed, output
                    context_id = allocated
                    claimed = True
                    if delivery:
                        output = delivery.create(allocated, pass_id,
                            review=dict(pass_kind=pass_kind, frozen=frozen, publication=object_digest(saved)))
                    elif pass_observer:
                        pass_observer(allocated, pass_id)
                    metric.start(registry, issue, options, "re-review" if resume else "review",
                                 run_id=pass_id, context_id=allocated, **(metrics_context or {}))
                    return review_handoff(issue, repo, workspace, project.base_branch, before,
                                          context_id, pass_kind, options, pass_id, output, frozen_publication=frozen,
                                          loop_feedback=loop_feedback, routable=standalone is not None, validation=local.review_validation,
                                          scope=local.implementation_scope) + (delivery.contract() if delivery else "")

                if before_handoff:
                    before_handoff()
                if context:
                    registry.claim_review(context)
                    claimed = True
                if pane is None:
                    # Use the verified live task anchor, independent of the reviewer's
                    # old tab. split rechecks this anchor before creating the pane.
                    pane = identities.split(anchor, workspace.path)
                workspace = replace(workspace, pane_id=pane["pane_id"], tab_id=pane["tab_id"])
                # The handoff records delivery coordinates from this binding.
                if context and pane["pane_id"] != context["pane_id"]:
                    registry.rebind_review_pane(context, pane)
                execution = AgentExecution(issue, repo, workspace, options,
                                           handoff(context_id) if context else "Review handoff pending allocation",
                                           purpose="review", policy=policy)
                if delivery:
                    execution = delivery.attach(execution, add_contract=False)
                elif standalone:
                    # Fresh allocation happens inside launch_registered. Resolve
                    # its selector when the adapter claims the actual prompt.
                    def claim_standalone(reference, message_id, prompt):
                        standalone.claim(handoff_env, handoff_record, options, context_id, reference, message_id, prompt)
                    execution = replace(execution, delivery_observer=claim_standalone)
                if context:
                    identities.label(pane, context_id)
                    execution = replace(execution, runtime_observer=context_observer(registry, context_id))
                    execution.runtime_observer(dict(herdr_session=json.dumps(reference)))
                    try:
                        # A stopped agent can restart in its original shell pane. A
                        # new pane binding is needed only for actual pane recreation.
                        launch = adapter.resume_review(execution, reference,
                            recreate=pane["pane_id"] != context["pane_id"] or pane.get("agent") is None)
                    finally:
                        checkpoint()
                else:
                    try:
                        launch = launch_registered(adapter, execution, registry=registry, herdr=identities,
                                                   handoff_factory=handoff)
                    except BaseException:
                        # launch_registered already retained the allocation and any
                        # observed identity as uncertain. There may be no runtime to
                        # finalize; preserve that primary failure and its evidence.
                        fresh_launch_failed = True
                        raise
                    finally:
                        checkpoint()
                    context_id = launch.context_id
                    context = registry.get(context_id)
                    reference = context_reference(context)
                    execution = replace(execution, runtime_observer=context_observer(registry, context_id))
                if delivery:
                    delivery.started()
                deadline = time.monotonic() + timeout
                while delivery or time.monotonic() < deadline:
                    if delivery:
                        delivery.warn_if_overdue()
                    checkpoint()
                    try:
                        # A pane ID is a location. Reconcile the same terminal before
                        # polling so a layout move cannot select or lose a reviewer.
                        current = registry.get(context_id)
                        try:
                            live, notes = reconcile(current, identities.snapshot(), allow_relocation=True)
                        except TaskError:
                            live, notes = None, ["observation unavailable"]
                        status = "unknown"
                        if live is None:
                            if notes not in (["missing pane"], ["observation unavailable"]):
                                raise TaskError("Reviewer runtime identity changed: " + "; ".join(notes))
                        else:
                            if (Path(live.get("cwd", "")).resolve() != workspace.path
                                    or any(n in {"agent mismatch", "session mismatch", "session identity invalid"} for n in notes)):
                                raise TaskError("Reviewer runtime identity changed: " + "; ".join(notes))
                            if any(live[k] != current[k] for k in ("pane_id", "tab_id")):
                                registry.rebind_review_pane(current, live)
                            pane = live
                            workspace = replace(workspace, pane_id=pane["pane_id"], tab_id=pane["tab_id"])
                            execution = replace(execution, workspace=workspace)
                            status = adapter.review_status(execution, pane["terminal_id"], reference)
                    finally:
                        checkpoint()
                    if delivery and invalidated:
                        metric.outcome("blocked")
                        raise TaskError("Checkout changed during review; retained pass is invalidated")
                    reference = context_reference(registry.get(context_id))
                    if status == "blocked":
                        metric.outcome("blocked")
                        state, summary = "blocked", "Reviewer needs human intervention; a new pass is required"
                        break
                    if status in {"idle", "done"} and output.exists():
                        if output.is_symlink() or not output.is_file() or output.stat().st_size > 1024 * 1024:
                            raise TaskError("Invalid reviewer result file")
                        raw = delivery.verify(adapter, execution) if delivery else output.read_text(encoding="utf-8")
                        if raw is None:
                            time.sleep(0.25)
                            continue
                        verdict = parse_verdict(raw, pass_id,
                                                frozen_fingerprint=frozen_fingerprint, identifier=issue.identifier,
                                                routing=loop_feedback is not None)
                        state, summary = verdict["state"], verdict["summary"]
                        break
                    # Herdr may infer idle from process/title detection even while Pi
                    # is working. No result means pending, regardless of an earlier
                    # working observation or elapsed startup time. The explicit
                    # standalone review deadline still bounds these observations;
                    # loop delivery uses an advisory warning and keeps observing.
                    time.sleep(0.25)
                else:
                    metric.outcome("timed_out")
                    summary = "Timed out waiting for validated reviewer output; inspect the pane before another pass"
        except (TaskError, OSError, UnicodeError) as error:
            if delivery and delivery.state.get("delivery"):
                raise  # Retain the active claim; only proven structured results route.
            state, summary = "failed", str(error)
            metric.outcome("failed")
        finally:
            if context_id and claimed and not fresh_launch_failed:
                try:
                    note = finalize_reviewer(context_id, workspace, adapter, registry, identities)
                    if note:
                        summary = f"{summary}. {note}"
                except TaskError as error:
                    state, summary = "failed", f"{summary}. Review context could not be finalized: {error}"
                    metric.outcome("failed")
            try:
                target = Herdr(repo).resolve_task(Git(repo), issue.identifier, include_remotes=False)
                if (target is None or target.path != workspace.path or target.branch != workspace.branch
                        or target.open_workspace_id != workspace.workspace_id):
                    invalidated = True
                else:
                    Git(repo).check_cleanup_target(project.base_branch, target, issue.identifier, require_clean=False)
            except TaskError:
                invalidated = True
            checkpoint()  # Last observation immediately before accepting the result.
        if invalidated:
            metric.outcome("blocked")
            state, summary = "blocked", ("Implementation changed during review, its state could not be verified, or frozen publication metadata changed. "
                                         "This pass is invalidated; a new pass is required. "
                                         "Reviewer-caused task changes also violate read-only review policy.")
        if (delivery and delivery.state.get("delivery")
                and (invalidated or verdict is None or state != verdict["state"])):
            raise TaskError(summary)
        if state == "clean":
            accept_review(store, saved, issue, repo, workspace, endpoint, project.base_branch,
                          context_id, pass_id, pass_kind, before.as_dict(), options,
                          context_reference(registry.get(context_id)), verdict, frozen)
        if verdict is not None or state != "failed":
            metric.outcome(state, findings=verdict["findings"] if verdict else None)
        result = ReviewResult(state, summary, context_id, pass_kind, asdict(options), before.as_dict(), pass_id,
                            verdict["findings"] if verdict else [], verdict["checks"] if verdict else [],
                            invalidated, post.fingerprint if post else None)
        if standalone:
            standalone.finish(handoff_env, handoff_record, result, store)
        return result


def accept_review(store, saved, issue, repo, workspace, endpoint, base_branch,
                  context_id, pass_id, pass_kind, before, options, reference, verdict, frozen):
    """Shared acceptance writer for live and reconciled loop results."""
    saved["acceptance"] = dict(version=1, issue=issue.identifier, repository=str(repo),
            worktree=str(workspace.path), branch=workspace.branch, workspace_id=workspace.workspace_id,
            endpoint=endpoint, base_branch=base_branch, context_id=context_id,
            pass_id=pass_id, pass_kind=pass_kind, verdict="clean", completed_at=now(),
            review_state=before, execution=asdict(options),
            session=merge_session(None, reference, options.kind),
            publication=verdict.get("publication") if frozen is None else None)
    if frozen is not None:
        saved["acceptance"]["publication_approval"] = verdict["publication_approval"]
    store.write(saved)

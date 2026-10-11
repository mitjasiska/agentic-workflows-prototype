"""Bounded implementation/review routing composed with the single-pass reviewer."""

from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
import copy
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from . import TaskError
from .ownership import ownership_gate, ownership_operation
from .agent import AgentExecution, AgentOptions, AgentOverrides, adapter_for, resolve_agent_options
from .metrics import observe_loop
from .config import (check_project_settings_unchanged, load_local, load_projects, project_settings,
                     repository_path, resolve_project)
from .contexts import ContextRegistry, HerdrContexts, allocate_launch, pending_launch, context_observer, context_reference
from .implementation_pass import binding, implementation_pass, implementation_target, initial_execution, parse_implementation
from .linear import Linear, LinearUnavailable
from .loop_state import LoopStore, PauseRequested, startup_recovery_blocker, unclaimed_existing_boundary
from .publication_state import PublicationStore, prepare_review_continuation
from .preparation import check_issue_structure, prepare_task
from .review import resolve_review_workspace, resolve_reviewer, review_pass, accept_review
from .review_result import parse_verdict, publication_fingerprint
from .pass_delivery import PassDelivery, dispose, object_digest, pass_timing
from .review_state import snapshot
from .review_handoff_state import ReviewHandoffStore, eligible_review, task_binding, requirements
from .workspace import Git, Herdr
from .cleanup import disposed_context_history


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def environment(identifier, *, allow_bootstrap=False, pending_implementation=None, recovery_implementation=None):
    local = load_local()
    issue = Linear(local.api_key).get_issue(identifier)
    project = resolve_project(load_projects(), issue.project)
    repo = repository_path(local, project)
    registry, identities = ContextRegistry(), HerdrContexts()
    env = SimpleNamespace(local=local, issue=issue, project=project, repo=repo, registry=registry,
                          identities=identities, workspace=None)
    if allow_bootstrap and not any(c["role"] == "implementation" for c in registry.list(issue.identifier)):
        env.disposed_history = disposed_context_history(registry, issue.identifier, repo)
        return env
    env.workspace, env.anchor, env.base, env.endpoint = resolve_review_workspace(
        issue, project, repo, registry, identities, pending_implementation=pending_implementation,
        recovery_implementation=recovery_implementation)
    return env


def bootstrap(env, options, global_settings, *, ownership=None):
    check_issue_structure(env.issue, env.local.issue_structure)
    adapter_for(options).check_available()
    target = Herdr(env.repo).resolve_task(Git(env.repo), env.issue.identifier, include_remotes=False)
    if target is not None and LoopStore(target.path).path.exists():
        raise TaskError("Loop checkpoint exists but its original implementation reservation is missing; inspect before recovery")
    env.workspace = prepare_task(env.issue, env.project, Linear(env.local.api_key), Git(env.repo),
                                 lambda: Herdr(env.repo), default_only=True,
                                 check_configuration=lambda: check_project_settings_unchanged(
                                     global_settings, env.repo, env.local), ownership=ownership)
    env.endpoint = env.identities.endpoint()
    env.base = Git(env.repo).command("rev-parse", "--verify", f"refs/heads/{env.project.base_branch}^{{commit}}").strip()


def idle_reviewers(env, expected=None):
    """Do not start edits alongside another live/uncertain reviewer turn."""
    for item in env.registry.list(env.issue.identifier):
        if item["role"] != "review":
            continue
        context, pane = resolve_reviewer(item["context_id"], env.issue, env.workspace, env.repo,
                                         env.endpoint, env.registry, env.identities)
        if expected and item["context_id"] == expected["context_id"] and binding(context) != expected:
            raise TaskError("Exact reviewer session/settings changed; no replacement is allowed")
        options = AgentOptions(context["agent"], context["model"], context["mode"])
        adapter = adapter_for(options)
        reference = adapter.verify_review_session(env.workspace, binding(context)["session"])
        if pane is not None and pane.get("agent") is not None:
            workspace = replace(env.workspace, pane_id=pane["pane_id"], tab_id=pane["tab_id"])
            execution = AgentExecution(env.issue, env.repo, workspace, options, "Loop boundary identity check",
                                       purpose="review", policy=dict(read_only=True))
            if adapter.review_status(execution, pane["terminal_id"], reference) not in {"idle", "done"}:
                raise TaskError("Reviewer is not confirmed idle; no implementation handoff is safe")
    if expected and expected["context_id"] not in {c["context_id"] for c in env.registry.list(env.issue.identifier)}:
        raise TaskError("Saved reviewer context is missing; no replacement is allowed")


@dataclass(frozen=True)
class LoopResult:
    """Versioned delivery seam for callers/future report persistence; no archive API."""
    data: dict

    @property
    def state(self):
        return self.data["state"]

    def as_dict(self):
        return self.data

    def render(self):
        value = self.data
        lines = [f"{value['issue']} loop: {value['state']} — {value['passes']} passes, {value['reviews']} reviews",
                 value["reason"],
                 f"Contexts: {value['implementation_context']} / {value['reviewer_context'] or 'reviewer not started'}"]
        for item in value["implementation"]:
            lines.append(f"Implementation ({item['phase']}): {item['summary']}")
            if "task_assessment" in item:
                assessment = item["task_assessment"]
                lines.append(f"Task assessment: {assessment['state']} — {assessment['summary']}")
                lines.extend(f"  Question: {question}" for question in assessment["questions"])
        for item in value["review_iterations"]:
            lines.append(f"Review {item['iteration']}: {item['state']} — {item['summary']}")
            for finding in item["findings"]:
                lines.append(f"  {finding['id']} [{finding['category']}/{finding['severity']}]: "
                             f"{finding['explanation']} ({finding['evidence']})")
        for item in value["resolutions"]:
            lines.append(f"Fix {item['finding_id']}: {item['summary']}")
        for check in value["validation"]:
            lines.append(f"Check {check['name']}: {check['result']} — {check['details']}")
        lines.append(f"Final review: {value['final_review_state']}; human action required: "
                     + ("yes" if value["human_action_required"] else "no"))
        if value["next_phase"]:
            lines.append(f"Next boundary: {value['next_phase']}")
        if value["pause_requested"]:
            lines.append("Pause-after-current requested (sticky).")
        if value.get("pass_timing"):
            timing = value["pass_timing"]
            lines.append(f"Pass warning threshold: {timing['threshold_seconds']:g}s "
                         f"({'exceeded' if timing['overdue'] else 'not yet exceeded'}; advisory only).")
        if value.get("abandonment"):
            claim = value["abandonment"]["claim"]
            lines.append(f"Abandoned: {claim['phase']} / {claim['context_id']} / {claim['pass_id']}")
            if value.get("implementation_session"):
                session = value["implementation_session"]
                lines.append(f"Implementation session to resume: {session['agent']} ({session['kind']}) {session['value']}")
        return "\n".join("".join(c if c.isprintable() else "?" for c in line) for line in lines)


def report(state, pause=False):
    implementations, reviews, resolutions, checks = [], [], [], []
    for record in state["records"]:
        if record["phase"] in {"review", "rereview"}:
            reviews.append(dict(iteration=len(reviews) + 1, state=record["state"], summary=record["summary"],
                                findings=record["findings"]))
        else:
            implementations.append(dict(phase=record["phase"], summary=record["summary"]))
            if "task_assessment" in record:
                implementations[-1]["task_assessment"] = record["task_assessment"]
        resolutions.extend(record.get("resolutions", []))
        for check in record["checks"]:
            if check not in checks:
                checks.append(check)
    return LoopResult(dict(version=1, run_id=state["run_id"], issue=state["binding"]["issue"],
        state=state["status"], reason=state["reason"], passes=state["pass_count"], reviews=state["review_count"],
        implementation_context=state["implementation"]["context_id"],
        reviewer_context=state["reviewer"]["context_id"] if state["reviewer"] else None,
        implementation=implementations, review_iterations=reviews, resolutions=resolutions, validation=checks,
        final_review_state=reviews[-1]["state"] if reviews else "not_run",
        human_action_required=state["status"] != "clean", next_phase=state["next_phase"],
        pause_requested=pause, active_pass=state["active_pass"], abandonment=state.get("abandonment"),
        pass_timing=pass_timing(state),
        implementation_session=state["implementation"].get("session") if state.get("abandonment") else None))


def new_state(env, reviewer_options, max_reviews, max_passes, timeout, *, from_review=False,
              initial_options=None, reserved_implementation=None):
    if initial_options is None:
        context, *_ = implementation_target(env)
        implementation = binding(context)
        idle_reviewers(env)
    elif reserved_implementation is not None:
        pending_launch(initial_execution(env, initial_options), reserved_implementation,
                       env.registry, env.identities, require_idle=True)
        implementation = dict(context_id=reserved_implementation)
    else:
        # Reserve before checkpoint creation so all controls can locate the run.
        # An interruption in this gap leaves a launching context, never a retry.
        history = disposed_context_history(env.registry, env.issue.identifier, env.repo)
        if history != getattr(env, "disposed_history", []):
            raise TaskError("Context history changed before initial allocation; inspect before starting")
        if LoopStore(env.workspace.path).path.exists():
            raise TaskError("Loop checkpoint already exists; no replacement implementation was reserved")
        execution = initial_execution(env, initial_options)
        implementation = dict(context_id=allocate_launch(execution, env.registry, env.identities))
    state = dict(version=1, run_id=str(uuid4()), binding=task_binding(env), requirements=requirements(env),
        implementation=implementation, reviewer=None, reviewer_options=asdict(reviewer_options),
        status="ready", reason="Implementation completion pending", next_phase="implementation", active_pass=None,
        pass_count=0, review_count=0, max_reviews=max_reviews, max_passes=max_passes, timeout=timeout,
        snapshot=snapshot(env.workspace.path, env.base, env.workspace.branch).as_dict(),
        findings=[], seen=[], records=[], delivery=None)
    if initial_options is not None:
        state.update(version=3, initial_phase="initial_implementation", next_phase="initial_implementation",
                     implementation_options=asdict(initial_options), reason="Initial implementation pending")
    if from_review:
        # Keep the default checkpoint path intact. Review-first runs need an
        # explicit origin so validation never infers a missing completion pass.
        state.update(version=2, initial_phase="review", next_phase="review",
                     reason="Explicit review start; independent review pending")
    return state


def route(state, result, after):
    """Only structured categories/IDs and snapshots drive transitions; no prose inference."""
    phase = state["next_phase"]
    record = dict(phase=phase, **result)
    state["records"].append(record)
    state.update(active_pass=None, status="ready")
    before = state["snapshot"]
    state["snapshot"] = after
    if result["state"] in {"failed", "blocked"}:
        state.update(status="escalated", reason=result["summary"])
        if result.get("task_assessment", {}).get("state") == "blocked" and before != after:
            state["reason"] = "Checkout changed during blocked task assessment; inspect before recovery"
    elif phase in {"initial_implementation", "implementation", "fixes"}:
        if result["state"] != "completed":
            raise TaskError("Implementation outcome cannot be routed")
        if before["head"] != after["head"]:
            state.update(status="escalated", reason="Implementation changed Git history; inspect before proceeding")
        elif phase == "fixes" and before["content"] == after["content"]:
            state.update(status="escalated", reason="Fix pass made no Git-visible progress on the review findings")
        else:
            state.update(next_phase="rereview" if phase == "fixes" else "review",
                         reason="Implementation result collected; independent review pending")
    elif before != after:
        state.update(status="escalated", reason="Checkout changed across the review boundary; inspect the invalid result")
    elif result["state"] == "clean":
        state.update(status="clean", next_phase=None, findings=[], reason="Implementation and independent review completed cleanly")
    elif result["state"] == "findings":
        findings = result["findings"]
        if not findings or any(f.get("category") != "implementation" or not f.get("id") for f in findings):
            state.update(status="escalated", reason="Review requires a human decision or cannot be safely classified")
        else:
            ids = sorted(f["id"] for f in findings)
            # Also detect identical substantive output with renamed finding IDs.
            signature = fingerprint(sorted((dict((k, v) for k, v in f.items() if k != "id") for f in findings),
                                           key=lambda f: json.dumps(f, sort_keys=True)))
            previous = {f["id"] for f in state["findings"]}
            repeated = any(s["ids"] == ids or s["signature"] == signature for s in state["seen"])
            if repeated or previous and previous <= set(ids):
                state.update(status="escalated", reason="Review findings repeated or made no structured progress")
            else:
                state.update(next_phase="fixes", reason="Actionable implementation findings pending fixes")
            state["seen"].append(dict(ids=ids, signature=signature))
        state["findings"] = findings
    else:
        raise TaskError("Review outcome cannot be routed")
    if state["status"] == "escalated":
        state["next_phase"] = None


class LoopRuntime:
    def __init__(self, identifier, publication, *, startup_recovery=False):
        self.identifier, self.publication = identifier, publication
        self.startup_recovery = startup_recovery
        self.env = None

    def prepare(self, state):
        initial = state["next_phase"] == "initial_implementation"
        env = environment(self.identifier, pending_implementation=state["implementation"]["context_id"] if initial else None)
        if task_binding(env) != state["binding"] or requirements(env) != state["requirements"]:
            raise TaskError("Task requirements or exact checkout binding changed; human inspection is required")
        if snapshot(env.workspace.path, env.base, env.workspace.branch).as_dict() != state["snapshot"]:
            raise TaskError("Checkout/base changed since the saved boundary; no completed pass will be replayed")
        if initial:
            if self.startup_recovery and env.workspace.slice is not None:
                raise TaskError("Reserved initial implementation no longer has its original unsliced task scope")
            options = AgentOptions(**state["implementation_options"])
            adapter_for(options).check_available()
            pending_launch(initial_execution(env, options), state["implementation"]["context_id"],
                           env.registry, env.identities, require_idle=self.startup_recovery)
            if self.startup_recovery:
                adapter_for(AgentOptions(**state["reviewer_options"])).check_available()
        else:
            implementation_target(env, state["implementation"])
        idle_reviewers(env, state["reviewer"])
        self.env = env

    def execute(self, state, before_handoff, pass_observer):
        env, phase = self.env, state["next_phase"]
        delivery = PassDelivery(state, pass_observer, env)
        metrics_context = dict(loop_id=state["run_id"],
            previous_run_id=state["records"][-1]["pass_id"] if state["records"] else None,
            implementation_context_id=state["implementation"]["context_id"],
            review_round=state["review_count"] + 1 if phase in {"review", "rereview"}
                         else state["review_count"] or None)
        if phase in {"initial_implementation", "implementation", "fixes"}:
            saved = self.publication.read()
            if saved["rebase"] is not None and saved["rebase"].get("result") is None:
                raise TaskError("Unpublished rebase is pending; inspect publication before implementation")
            prepare_review_continuation(saved, Git(env.workspace.path),
                {k: state["binding"][k] for k in ("issue", "repository", "worktree", "branch", "base_branch")}, env.base)
            saved.update(acceptance=None, intent=None)
            self.publication.write(saved)
            result = implementation_pass(env, state["implementation"], state["findings"], state["timeout"],
                                       before_handoff, pass_observer,
                                       initial_options=AgentOptions(**state["implementation_options"])
                                       if phase == "initial_implementation" else None, delivery=delivery,
                                       metrics_context=metrics_context)
            self.completed_snapshot = delivery.completed_snapshot
            return result
        options = state["reviewer_options"]
        feedback = ({} if phase == "review" else
                    dict(findings=state["findings"], resolutions=state["records"][-1]["resolutions"]))
        result = review_pass(env.issue, env.project, env.repo, env.registry, env.identities,
            env.workspace, env.anchor, env.base, env.endpoint, env.local,
            state["reviewer"]["context_id"] if state["reviewer"] else None,
            options["kind"] if phase == "review" else None,
            options["model"] if phase == "review" else None,
            options["mode"] if phase == "review" else None,
            state["timeout"], self.publication, loop_feedback=feedback,
            before_handoff=before_handoff, pass_observer=pass_observer, delivery=delivery,
            metrics_context=metrics_context)
        self.completed_snapshot = getattr(delivery, "completed_snapshot", None)
        if result.context_id and result.state in {"clean", "findings"}:
            state["reviewer"] = binding(env.registry.get(result.context_id))
        return dict(pass_id=result.pass_id, context_id=result.context_id, state=result.state,
                    summary=result.summary, findings=result.findings, checks=result.checks, resolutions=[])

    def recover(self, state, persist):
        """Collect the claimed pass without invoking any adapter delivery method."""
        if not state.get("delivery"):
            raise TaskError("Claim lacks durable recovery evidence (possibly a historical temporary result); "
                            "inspect task contexts; completion cannot be reconstructed or replayed")
        delivery = PassDelivery(state, persist, None)
        if delivery.evidence["review"] is not None:
            delivery.snapshot()
        env = environment(self.identifier, recovery_implementation=state["implementation"]["context_id"])
        if task_binding(env) != state["binding"] or requirements(env) != state["requirements"]:
            raise TaskError("Task requirements or exact checkout binding changed during interrupted pass")
        self.env = delivery.env = env
        current = delivery.snapshot()
        if (current["head"] != state["snapshot"]["head"] or env.base != state["snapshot"]["base_commit"]
                or state["next_phase"] in {"review", "rereview"} and current != state["snapshot"]):
            delivery.invalidate_review()
            raise TaskError("Git history/base or pinned review state changed during interrupted pass")
        value = delivery.evidence
        if env.workspace.slice != value["slice"]:
            raise TaskError("Task slice changed during interrupted pass; saved scope cannot be replaced")
        if value["review"] is None:
            idle_reviewers(env, state["reviewer"])
        else:
            implementation_target(env, state["implementation"])
        while True:
            delivery.warn_if_overdue()
            if value["review"] is not None:
                delivery.snapshot()
            try:
                adapter, execution, pane, reference = delivery.runtime()
                status = adapter.status(execution, pane["terminal_id"], reference)
            finally:
                delivery.checkpoint()
            if value["invalidated"]:
                raise TaskError("Checkout changed during review recovery; retained pass is invalidated")
            if status == "blocked":
                raise TaskError("Original agent needs human intervention; resolve it in the exact context, then repeat task loop")
            output = Path(value["directory"]) / "result.json"
            if status in {"idle", "done"} and output.exists():
                try:
                    raw = delivery.verify(adapter, execution)
                finally:
                    delivery.checkpoint()
                if value["invalidated"]:
                    raise TaskError("Checkout changed during review recovery; retained pass is invalidated")
                if raw is not None:
                    break
            time.sleep(0.25)
        # Observe identity again after the provider read and Git seal check.
        try:
            delivery.runtime()
        finally:
            delivery.checkpoint()
        self.completed_snapshot = delivery.completed_snapshot
        try:
            fresh = environment(self.identifier, recovery_implementation=state["implementation"]["context_id"])
        finally:
            delivery.checkpoint()
        after = delivery.snapshot()
        if (task_binding(fresh) != state["binding"] or requirements(fresh) != state["requirements"]
                or fresh.base != state["snapshot"]["base_commit"]
                or after != self.completed_snapshot):
            if fresh.base != state["snapshot"]["base_commit"] or after != self.completed_snapshot:
                delivery.invalidate_review()
            raise TaskError("Task requirements or checkout changed while observing the original pass")
        observed_reference = dict(context_reference(env.registry.get(value["context"]["context_id"])), **reference)
        if value["review"] is None:
            result = parse_implementation(raw, value["pass_id"], state["findings"],
                                          assessment_enabled=value["assessment"])
            context_observer(env.registry, value["context"]["context_id"])(
                dict(herdr_session=json.dumps(observed_reference), state="active", resumability="yes"))
            state["implementation"] = value["context"]
            implementation_target(env, state["implementation"])
            return dict(result, context_id=value["context"]["context_id"], findings=[])
        review = value["review"]
        saved = self.publication.read()
        if (saved["intent"] is not None
                or saved["acceptance"] is not None and saved["acceptance"].get("pass_id") != value["pass_id"]
                or object_digest(dict(saved, acceptance=None)) != review["publication"]):
            raise TaskError("Publication state changed during interrupted review")
        frozen = review["frozen"]
        verdict = parse_verdict(raw, value["pass_id"], identifier=env.issue.identifier, routing=True,
                                frozen_fingerprint=publication_fingerprint(frozen) if frozen else None)
        state["reviewer"] = value["context"]
        context_observer(env.registry, state["reviewer"]["context_id"])(
            dict(herdr_session=json.dumps(observed_reference), state="active", resumability="yes"))
        if verdict["state"] == "clean":
            accept_review(self.publication, saved, env.issue, env.repo, env.workspace, env.endpoint,
                env.project.base_branch, state["reviewer"]["context_id"], value["pass_id"], review["pass_kind"],
                state["snapshot"], execution.options, reference, verdict, frozen)
        return {k: verdict[k] for k in ("pass_id", "state", "summary", "findings", "checks")} | dict(
            context_id=state["reviewer"]["context_id"], resolutions=[])

    def snapshot(self):
        env = self.env
        return snapshot(env.workspace.path, env.base, env.workspace.branch).as_dict()

    def adopt_review(self, store, state):
        if state["next_phase"] is None:
            return  # Terminal consumption is bookkeeping outside drive's error routing.
        handoffs = ReviewHandoffStore(Path(state["binding"]["worktree"]))
        handoffs.consume(state)  # Repair only the post-checkpoint consumption gap.
        value = handoffs.read()
        if not value or value["consumed"] is not None or state["status"] != "ready":
            return
        if state["next_phase"] != "review" or not state["records"] or state["reviewer"] is not None:
            raise TaskError("Standalone review conflicts with the saved loop boundary; inspect before another handoff")
        self.prepare(state)
        candidate = eligible_review(self.env, self.publication, state)
        if candidate:
            adopt_review(state, candidate)
            store.save(state)
            if state["next_phase"] is not None:
                handoffs.consume(state)

    def revoke_active_acceptance(self, state):
        claim = state["active_pass"]
        if claim and claim["phase"] in {"review", "rereview"}:
            saved = self.publication.read()
            if saved["acceptance"] and saved["acceptance"].get("pass_id") == claim["pass_id"]:
                if saved["intent"] is not None:
                    raise TaskError("Publication intent conflicts with an unfinished loop review; inspect without resetting it")
                saved["acceptance"] = None
                self.publication.write(saved)

    def before_claim(self, state):
        if self.startup_recovery and state["next_phase"] == "initial_implementation":
            pending_launch(initial_execution(self.env, AgentOptions(**state["implementation_options"])),
                           state["implementation"]["context_id"], self.env.registry, self.env.identities,
                           require_idle=True)
            # The cached base object can still exist after its ref advances during
            # slow shell preflight. Recheck the saved ref before claiming delivery.
            base = Git(self.env.repo).command(
                "rev-parse", "--verify", f"refs/heads/{state['binding']['base_branch']}^{{commit}}").strip()
            if base != state["snapshot"]["base_commit"]:
                raise TaskError("Base changed before the handoff claim; inspect the saved boundary")


def drive(store, state, runtime, *, on_pass_result=None):
    """Single controller, deterministic bounds, no retries of uncertain delivery.

    on_pass_result is an optional future persistence seam, invoked only after the
    completed result and next boundary are checkpointed. It never owns routing.
    """
    def collect(result, *, recovered=False):
        delivery = PassDelivery(state, lambda *_: store.save(state), runtime.env) if state.get("delivery") else None
        try:
            after = runtime.snapshot()
        except TaskError:
            if delivery:
                delivery.invalidate_review()
            raise
        if state.get("delivery") and after != runtime.completed_snapshot:
            delivery.invalidate_review()
            raise TaskError("Checkout changed after completion verification; retained pass cannot be accepted")
        route(state, result, after)
        store.save(state)
        observe_loop(runtime.env, state, recovered_result=result if recovered else None)
        if on_pass_result:
            on_pass_result(copy.deepcopy(result))

    def clean_delivery():
        if state.get("delivery") and state["active_pass"] is None:
            dispose(state["delivery"])
            state["delivery"] = None
            store.save(state)

    try:
        if state["active_pass"] is not None:
            collect(runtime.recover(state, lambda *_: store.save(state)), recovered=True)
        clean_delivery()
        if unclaimed_existing_boundary(state) and state["status"] != "ready":
            state.update(status="ready", reason="Unclaimed existing-context boundary retained; revalidating before delivery")
            store.save(state)
        elif state["status"] in {"interrupted", "escalated"} and state["records"] and state["next_phase"] is not None:
            state.update(status="ready", reason="Completed pass retained; revalidating the next boundary")
            store.save(state)
        elif (state["status"] in {"interrupted", "escalated"} and state["records"]
                and state["records"][-1]["state"] == "clean" and state["next_phase"] is None):
            state.update(status="clean", reason="Implementation and independent review completed cleanly")
            store.save(state)
        runtime.adopt_review(store, state)
        while state["status"] == "ready":
            _, pause = store.read()
            if pause:
                raise PauseRequested()
            if state["pass_count"] >= state["max_passes"] or (
                    state["next_phase"] in {"review", "fixes", "rereview"}
                    and state["review_count"] >= state["max_reviews"]):
                state.update(status="escalated", reason="Autonomous pass/review limit reached; human inspection required")
                break
            runtime.prepare(state)

            def observe(context_id, pass_id, implementation=None):
                state["active_pass"].update(context_id=context_id, pass_id=pass_id)
                if implementation is not None:
                    if state["implementation"]["context_id"] != implementation["context_id"]:
                        raise TaskError("Implementation context changed during launch")
                    state["implementation"] = implementation
                if state["next_phase"] == "review":
                    state["reviewer"] = dict(context_id=context_id)
                store.save(state)

            def begin():
                # Recheck after potentially slow provider/session preflight.
                runtime.before_claim(state)
                if runtime.snapshot() != state["snapshot"]:
                    raise TaskError("Checkout changed before the handoff claim; inspect the saved boundary")
                store.begin(state)

            result = runtime.execute(state, begin, observe)
            if state["status"] != "running":
                raise TaskError("Agent pass returned without a recorded handoff claim")
            collect(result)
            clean_delivery()
    except PauseRequested:
        state.update(status="paused", reason="Paused after collecting the current pass; use --continue explicitly")
    except (KeyboardInterrupt, SystemExit):
        state.update(status="interrupted", reason=f"Controller interrupted; the original agent may still be working. "
                     f"Repeat task loop {runtime.identifier} to reconcile the saved pass; --continue only resumes a pause. "
                     "To abandon instead, quit task agents to their original shells, then run "
                     f"task loop {runtime.identifier} --abort from outside the task checkout")
        if state.get("delivery"):
            state["reason"] += f". Retained evidence: {state['delivery']['directory']}"
    except Exception as error:
        runtime.revoke_active_acceptance(state)
        state.update(status="escalated", reason=f"{error}. Inspect contexts before recovery; no handoff was retried")
        if state["active_pass"] is not None:
            state["reason"] += (f". Preserve the checkpoint/output; repeat task loop {runtime.identifier} after resolving "
                                "the reported evidence problem. To abandon, stop this controller with Ctrl+C, quit task "
                                "agents to their original shells, and run "
                                f"task loop {runtime.identifier} --abort from outside the checkout")
            if state.get("delivery"):
                state["reason"] += f". Retained evidence: {state['delivery']['directory']}"
        if isinstance(error, LinearUnavailable) and startup_recovery_blocker(state) is None:
            state["reason"] = (f"{error}. No initial handoff was claimed; rerun task loop {runtime.identifier} "
                               "to revalidate and reuse the original reservation")
    store.save(state)
    if state["next_phase"] is None:
        # A failed/interrupted marker write must leave the checkpointed terminal
        # verdict and reason intact. A later invocation repairs only this marker.
        ReviewHandoffStore(Path(state["binding"]["worktree"])).consume(state)
    observe_loop(runtime.env, state)
    return report(*store.read())


def adopt_review(state, candidate):
    """Enter the ordinary router without delivering another review prompt."""
    value, result = candidate
    if state["pass_count"] >= state["max_passes"] or state["review_count"] >= state["max_reviews"]:
        raise TaskError("Saved limits do not permit collecting the standalone review")
    state.update(reviewer=value["reviewer"], reviewer_options=value["result"]["execution"],
                 pass_count=state["pass_count"] + 1, review_count=state["review_count"] + 1)
    route(state, result, value["snapshot"])


@ownership_operation
def control_store(identifier, *, optional=False):
    """Pause/status/continue locate exact private state without Linear or local config."""
    contexts = ContextRegistry().list(identifier)
    # Integration contexts retain a separate checkout for recovery/provenance.
    # Only implementation/review contexts identify the task's loop checkpoint.
    paths = {c["worktree"] for c in contexts
             if c["role"] in {"implementation", "review"} and c["worktree"]}
    if optional and not paths:
        return None
    if len(paths) != 1:
        raise TaskError("Loop control requires exactly one registered task checkout")
    path = Path(paths.pop())
    store = LoopStore(path)
    if optional and not store.path.exists():
        return None
    state, _ = store.read()
    if state["binding"]["issue"] != identifier or state["binding"]["worktree"] != str(path):
        raise TaskError("Loop checkpoint does not match the exact issue/checkout")
    return store, path


def loop(identifier, *, action="run", agent_kind=None, model=None, mode=None,
         max_reviews=None, max_passes=None, timeout=None, from_review=False, on_pass_result=None,
         impl_agent_kind=None, impl_model=None, impl_mode=None):
    implementation_overrides = (impl_agent_kind, impl_model, impl_mode)
    overrides = (agent_kind, model, mode, max_reviews, max_passes, timeout, *implementation_overrides)
    if action not in {"run", "new", "continue", "pause", "status", "abort"}:
        raise TaskError("Unknown loop control action")
    if from_review and action not in {"run", "new"}:
        raise TaskError("--from-review only starts a new loop; controls preserve the saved boundary")
    if action not in {"run", "new"} and any(v is not None for v in overrides):
        raise TaskError("Loop controls preserve recorded settings/limits and do not accept selection overrides")
    if action in {"run", "new"}:
        if (max_reviews is not None and (type(max_reviews) is not int or not 1 <= max_reviews <= 20)
                or max_passes is not None and (type(max_passes) is not int or not 1 <= max_passes <= 40)
                or timeout is not None and (type(timeout) not in {int, float} or not 0 < timeout <= 86400)):
            raise TaskError("Loop requires 1..20 reviews, 1..40 passes, and a positive timeout up to 86400 seconds")
        if from_review and any(v is not None for v in implementation_overrides):
            raise TaskError("--i-* options require a from-scratch loop, not --from-review")
    # Argument-only refusals create no ownership/checkpoint state. All runtime
    # observation and mutation, including control resolution, remain guarded.
    if action == "abort":
        from .loop_abort import abort_loop
        try:
            return abort_loop(identifier)
        except TaskError as error:
            raise TaskError(f"{error}. If the original controller is still waiting, press Ctrl+C in its terminal "
                            "first; this stops only the controller. Then retry --abort after quitting task agents") from None
    return _loop(identifier, action=action, agent_kind=agent_kind, model=model, mode=mode,
                 max_reviews=max_reviews, max_passes=max_passes, timeout=timeout,
                 from_review=from_review, on_pass_result=on_pass_result,
                 impl_agent_kind=impl_agent_kind, impl_model=impl_model, impl_mode=impl_mode)


def _loop(identifier, *, action, agent_kind, model, mode, max_reviews, max_passes, timeout,
          from_review, on_pass_result, impl_agent_kind, impl_model, impl_mode):
    if action in {"pause", "status", "continue"}:
        with ownership_gate():
            store, path = control_store(identifier)
            if action == "pause":
                from .cleanup import check_unreserved
                target = store.read()[0]["binding"]
                check_unreserved(ContextRegistry(), target["repository"], target["worktree"])
                return report(*store.pause())
            if action == "status":
                return report(*store.read())
        with PublicationStore(path).locked() as publication:
            state = store.continue_paused()
            # A pause outlives the runtime that proved the reservation safe.
            # Every continued initial handoff must establish that proof again,
            # including the final runtime/base checks immediately before claim.
            runtime = LoopRuntime(identifier, publication,
                                  startup_recovery=state["next_phase"] == "initial_implementation")
            return drive(store, state, runtime, on_pass_result=on_pass_result)
    implementation_overrides = (impl_agent_kind, impl_model, impl_mode)
    existing = control_store(identifier, optional=True) if action == "run" else None
    if existing is not None:
        store, path = existing
        with PublicationStore(path).locked() as publication:
            state, _ = store.read()
            if state["status"] == "aborted":
                return report(*store.read())
            if state["status"] == "aborting":
                raise TaskError("Abort reconciliation is pending; rerun --abort, never recover the revoked pass")
            if (state["next_phase"] is None and state["active_pass"] is None and not state.get("delivery")
                    and ReviewHandoffStore(path).consume(state)):
                return report(*store.read())
            if state["active_pass"] is not None and not (state.get("delivery") or {}).get("receipt"):
                raise TaskError("Saved handoff claim has no durable delivery proof; inspect task contexts and retained "
                                "output. Never replay it or reconstruct historical temporary output. "
                                "To abandon without discarding work, stop the controller, quit task agents to their "
                                f"original shells, then run task loop {identifier} --abort outside the checkout")
            if (unclaimed_existing_boundary(state) or state["active_pass"] is not None
                    or "delivery" in state and state["records"] and state["status"] != "paused"
                    and (state["next_phase"] is not None or state["records"][-1]["state"] == "clean"
                         or state.get("delivery"))):
                if from_review and state.get("initial_phase") != "review":
                    raise TaskError("Recovery preserves the original boundary; --from-review conflicts")
                implementation_options = state.get("implementation_options") or {
                    "kind": state["implementation"].get("agent"), "model": state["implementation"].get("model"),
                    "mode": state["implementation"].get("mode")}
                for saved, supplied in ((implementation_options, implementation_overrides),
                                       (state["reviewer_options"], (agent_kind, model, mode))):
                    if any(v is not None and v != saved[k] for k, v in zip(("kind", "model", "mode"), supplied)):
                        raise TaskError("Recovery preserves recorded agent/model/mode; supplied settings conflict")
                if any(v is not None and v != state[k] for k, v in
                       (("max_reviews", max_reviews), ("max_passes", max_passes), ("timeout", timeout))):
                    raise TaskError("Recovery preserves recorded limits and original timeout; supplied bounds conflict")
                return drive(store, state, LoopRuntime(identifier, publication), on_pass_result=on_pass_result)
            blocker = startup_recovery_blocker(state)
            if blocker:
                raise TaskError("A loop checkpoint already exists; startup recovery blocked: " + blocker
                                + ". Use --status to inspect; --continue only resumes a pause")
            if from_review:
                raise TaskError("Startup recovery preserves initial implementation; --from-review conflicts")
            selections = dict(max_reviews=max_reviews, max_passes=max_passes, timeout=timeout)
            for saved, supplied, flags in (
                    (state["implementation_options"], implementation_overrides, ("--i-agent", "--i-model", "--i-mode")),
                    (state["reviewer_options"], (agent_kind, model, mode), ("--r-agent", "--r-model", "--r-mode"))):
                for key, value, flag in zip(("kind", "model", "mode"), supplied, flags):
                    if value is not None and value != saved[key]:
                        raise TaskError(f"Startup recovery preserves recorded settings; {flag} conflicts")
            for key, value in selections.items():
                if value is not None and value != state[key]:
                    raise TaskError(f"Startup recovery preserves recorded bounds; --{key.replace('_', '-')} conflicts")
            runtime = LoopRuntime(identifier, publication, startup_recovery=True)
            try:
                runtime.prepare(state)
            except LinearUnavailable as error:
                raise TaskError(f"{error}. Startup reservation retained; rerun task loop {identifier} when Linear recovers") from None
            except TaskError as error:
                raise TaskError(f"Startup recovery blocked: {error}. Inspect the original context; no handoff was sent") from None
            state = store.restart_initial(state)
            return drive(store, state, runtime, on_pass_result=on_pass_result)
    max_reviews = 3 if max_reviews is None else max_reviews
    max_passes = 6 if max_passes is None else max_passes
    reservation = None
    if action == "new":
        existing = control_store(identifier, optional=True)
        if existing:
            with PublicationStore(existing[1]).locked():
                previous, _ = existing[0].read()
                handoffs = ReviewHandoffStore(existing[1])
                retained = handoffs.read()
                if (previous["status"] not in {"aborting", "aborted"} and retained
                        and retained["consumed"] is None and handoffs.consume(previous)):
                    return report(*existing[0].read())
            if previous["status"] == "aborted" and set(previous["implementation"]) == {"context_id"}:
                if from_review:
                    raise TaskError("Initial implementation was never completed; use --new to start implementation")
                reservation = previous
    env = environment(identifier, allow_bootstrap=not from_review,
                      pending_implementation=reservation["implementation"]["context_id"] if reservation else None)
    global_settings = env.local
    env.local = project_settings(global_settings, env.repo)
    timeout = env.local.loop_timeout if timeout is None else timeout
    if env.workspace is not None:
        # Selection is revalidated under the publication lock below. Existing
        # R1 settings take precedence over defaults, never over conflicting flags.
        retained = ReviewHandoffStore(env.workspace.path).read()
        retained_options = (retained["result"]["execution"] if retained and retained["result"]
                            and retained["consumed"] is None else None)
    else:
        retained_options = None
    options = (AgentOptions(**retained_options) if retained_options else
               resolve_agent_options(env.local.reviewer, AgentOverrides(agent_kind, model, mode),
                                     section="reviewer", agent_flag="--r-agent"))
    if retained_options and any(v is not None and v != retained_options[k] for k, v in
                               zip(("kind", "model", "mode"), (agent_kind, model, mode))):
        raise TaskError("Standalone review adoption preserves the original reviewer settings; supplied flags conflict")
    if options.model is None or options.mode is None:
        raise TaskError("Loop reviewer requires explicit model and mode ([reviewer] or --r-model/--r-mode)")
    adapter_for(options).check_available()
    with ExitStack() as ownership:
        initial_options = None
        prepared = False
        if reservation:
            initial_options = AgentOptions(**reservation["implementation_options"])
            if any(v is not None and v != getattr(initial_options, k) for k, v in
                   zip(("kind", "model", "mode"), implementation_overrides)):
                raise TaskError("Aborted reservation retains its original implementation settings")
        elif env.workspace is None:
            initial_options = resolve_agent_options(env.local.agent, AgentOverrides(*implementation_overrides),
                                                    agent_flag="--i-agent")
            if initial_options.model is None or initial_options.mode is None:
                raise TaskError("Loop implementation requires explicit model and mode ([agent] or --i-model/--i-mode)")
            bootstrap(env, initial_options, global_settings, ownership=ownership)
            prepared = True
        elif any(v is not None for v in implementation_overrides):
            raise TaskError("--i-* options require a from-scratch loop; existing implementation settings are preserved")
        with PublicationStore(env.workspace.path).locked(allow_owned=prepared) as publication:
            store = LoopStore(env.workspace.path)
            retained = ReviewHandoffStore(env.workspace.path).read() if initial_options is None else None
            if action == "new" and store.path.exists():
                previous, _ = store.read()
                if previous["status"] == "aborting":
                    raise TaskError("Abort reconciliation is pending; rerun --abort before --new")
                if (previous["status"] != "aborted" and retained and retained["consumed"] is None
                        and ReviewHandoffStore(env.workspace.path).consume(previous)):
                    return report(*store.read())
                if previous["status"] == "aborted" and initial_options is None:
                    implementation_target(env, previous["implementation"])
                if previous["status"] == "aborted" and from_review:
                    if not previous["abandonment"]["review_ready"]:
                        raise TaskError("Implementation was interrupted; inspect and use --new without --from-review "
                                        "to explicitly continue implementation first")
                    if (task_binding(env) != previous["binding"]
                            or requirements(env) != previous["requirements"]
                            or snapshot(env.workspace.path, env.base, env.workspace.branch).as_dict() != previous["snapshot"]):
                        raise TaskError("Completed implementation boundary changed since abort; continue implementation "
                                        "with --new before requesting review")
            state = new_state(env, options, max_reviews, max_passes, timeout, from_review=from_review,
                              initial_options=initial_options,
                              reserved_implementation=reservation["implementation"]["context_id"] if reservation else None)
            if retained and retained["consumed"] is None:
                candidate = eligible_review(env, publication)
                if candidate:
                    options = AgentOptions(**candidate[0]["result"]["execution"])
                    if any(v is not None and v != getattr(options, k) for k, v in
                           zip(("kind", "model", "mode"), (agent_kind, model, mode))):
                        raise TaskError("Standalone reviewer settings changed before adoption; supplied flags conflict")
                    state.update(version=2, initial_phase="review", next_phase="review")
                    adopt_review(state, candidate)
            store.create(state, replace=action == "new")
            return drive(store, state, LoopRuntime(identifier, publication, startup_recovery=bool(reservation)),
                         on_pass_result=on_pass_result)

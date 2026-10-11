"""Explicit, non-destructive retirement of one stopped loop claim.

No provider input, process termination, checkout mutation, or Linear/config lookup.
The task publication lock excludes conflicting controllers; its durable abort
journal serializes revocation, context reconciliation and checkpoint release.
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

from . import TaskError
from .agent import AgentOptions, adapter_for
from .cleanup import check_unreserved
from .contexts import ContextRegistry, HerdrContexts, context_reference, now, reconcile
from .implementation_pass import binding
from .integration_process import shell_executable, stopped_loop_execution
from .integration_state import IntegrationStore
from .pass_delivery import PassDelivery, object_digest
from .publication_state import PublicationStore
from .review import resolve_review_workspace
from .review_state import snapshot
from .sessions import has_immutable_identity, merge_session, same_session
from .workspace import Git


def stopped_runtime(state, contexts, identities):
    target = state["binding"]
    if identities.endpoint() != target["endpoint"]:
        raise TaskError("Abort requires the original Herdr endpoint")
    panes = identities.snapshot()
    shells = {}
    selected = set()
    for context in contexts:
        pane, notes = reconcile(context, panes)
        if (pane is None or pane.get("agent") is not None
                or pane.get("launch_pending") is True
                or pane["tab_id"] != context["tab_id"]
                or pane.get("cwd") != target["worktree"]
                or any(n in {"session mismatch", "session identity invalid"} for n in notes)):
            raise TaskError(f"{context['context_id']} ({context['pane_id']}, {context['terminal_id']}) is live, "
                            "missing, or uncertain. Quit its agent to the original shell, keeping the pane open; "
                            "an idle session or missing pane does not prove a stopped turn")
        selected.add(pane["pane_id"])
        try:
            info = identities.command("pane", "process-info", "--pane", pane["pane_id"])["process_info"]
            pid = info["shell_pid"]
            foreground = info["foreground_processes"]
            if (info["pane_id"] != pane["pane_id"] or type(pid) is not int or pid <= 0 or pid in shells
                    or type(info["foreground_process_group_id"]) is not int
                    or info["foreground_process_group_id"] != pid
                    or not isinstance(foreground, list) or len(foreground) != 1
                    or type(foreground[0]["pid"]) is not int or foreground[0]["pid"] != pid):
                raise ValueError("not a unique shell")
            argv = foreground[0]["argv"]
            shell_executable(argv)
        except (KeyError, TypeError, ValueError):
            raise TaskError(f"Cannot prove {context['pane_id']} is a stopped shell; quit its agent and child jobs") from None
        shells[pid] = dict(cwd=Path(target["worktree"]), argv=argv)
    for pane in panes:
        if (pane["workspace_id"] == target["workspace_id"]
                or any(pane.get(k) and Path(pane[k]).is_relative_to(Path(target["worktree"]))
                       for k in ("cwd", "foreground_cwd"))):
            if pane["pane_id"] not in selected:
                raise TaskError("Unregistered task runtime may still use the checkout; inspect it before --abort")
    return stopped_loop_execution(Path(target["worktree"]), shells)


def verify_boundary(state, registry, identities, contexts, review_snapshot):
    """Recheck local checkout, exact claims, providers, shells and Git together."""
    target, delivery = state["binding"], state.get("delivery")
    if delivery and delivery["review"] is not None:
        review_snapshot()  # Drift is sticky even when a later identity check refuses abort.
    check_unreserved(registry, target["repository"], target["worktree"],
                     endpoint=target["endpoint"], workspace_id=target["workspace_id"])
    issue = SimpleNamespace(identifier=target["issue"])
    project = SimpleNamespace(base_branch=target["base_branch"])
    workspace, _, _, endpoint = resolve_review_workspace(issue, project, Path(target["repository"]), registry,
        identities, local_only=True, abort_implementation=state["implementation"]["context_id"])
    if (str(workspace.path) != target["worktree"] or workspace.branch != target["branch"]
            or workspace.workspace_id != target["workspace_id"] or endpoint != target["endpoint"]):
        raise TaskError("Exact task checkout binding changed; abort refused")
    history = registry.list(target["issue"], include_retired=True)
    if delivery and (delivery["contexts_sha256"] != object_digest(sorted(c["context_id"] for c in history))
                     or delivery["slice"] != workspace.slice or delivery["invalidated"]):
        raise TaskError("Delivery context/scope changed or review drift was recorded; abort refused")
    expected_ids = {state["implementation"]["context_id"]}
    if state["reviewer"]:
        expected_ids.add(state["reviewer"]["context_id"])
    if state["active_pass"]["context_id"]:
        expected_ids.add(state["active_pass"]["context_id"])
    if not expected_ids <= {c["context_id"] for c in contexts}:
        raise TaskError("Saved pass context is missing; abort refused")
    references = {}
    for context in contexts:
        if (context["role"] not in {"implementation", "review"}
                or context["state"] not in {"active", "reviewing", "uncertain", "launching", "awaiting_user", "abandoned"}
                or any(context[k] != target[k] for k in ("issue", "repository", "worktree", "workspace_id", "endpoint"))):
            raise TaskError("Conflicting task context claim; abort refused")
        reference = context_reference(context)
        saved = next((s for s in (state["implementation"], state["reviewer"],
                                  delivery["context"] if delivery else None)
                      if s and s["context_id"] == context["context_id"] and "session" in s), None)
        if saved is not None:
            try:
                reference = merge_session(reference, saved["session"], context["agent"]) if reference else None
            except ValueError:
                raise TaskError("Saved pass session/settings changed; abort refused") from None
            if (reference != saved["session"]
                    or any(context[k] != saved[k] for k in ("agent", "model", "mode"))):
                raise TaskError("Saved pass session/settings changed; abort refused")
        if delivery and context["context_id"] == state["active_pass"]["context_id"]:
            if any(context[k] != v for k, v in delivery["location"].items()):
                raise TaskError("Saved delivery terminal changed; abort refused")
        if reference is None:
            # The adapter persists its receipt before any submission. Only the
            # original allocation with no such receipt may remain sessionless.
            if saved or delivery and delivery["receipt"] or context["state"] not in {"launching", "uncertain", "awaiting_user"}:
                raise TaskError("Missing provider identity cannot prove non-delivery; abort refused")
        else:
            adapter = adapter_for(AgentOptions(context["agent"], context["model"], context["mode"]))
            receipt = (delivery["receipt"] if delivery and
                       context["context_id"] == state["active_pass"]["context_id"] else None)
            verified = adapter.verify_stopped_session(workspace, reference, receipt)
            if (not has_immutable_identity(reference, context["agent"])
                    or merge_session(None, verified, context["agent"]) != merge_session(None, reference, context["agent"])):
                raise TaskError("Provider session is ambiguous or changed; abort refused")
            references[context["context_id"]] = merge_session(None, verified, context["agent"])
    ids = {c["context_id"] for c in contexts}
    for other in registry.list():
        if other["context_id"] in ids:
            continue
        for context in contexts:
            shared = (other["worktree"] == target["worktree"]
                      or other["endpoint"] == target["endpoint"] and any(
                          other[k] and other[k] == context[k] for k in ("workspace_id", "pane_id", "terminal_id")))
            if (other["agent"] == context["agent"] and context_reference(other) and context_reference(context)
                    and same_session(context_reference(other), context_reference(context), context["agent"])):
                shared = True
            if shared:
                raise TaskError("Another context claims this checkout/runtime/session; abort refused")
    processes = stopped_runtime(state, contexts, identities)
    base = (state["snapshot"]["base_commit"] if delivery and delivery["review"] is not None else
            Git(Path(target["repository"])).command("rev-parse", "--verify",
                f"refs/heads/{target['base_branch']}^{{commit}}").strip())
    current = (review_snapshot() if delivery and delivery["review"] is not None else
               snapshot(workspace.path, state["snapshot"]["base_commit"], workspace.branch).as_dict())
    review = state["active_pass"]["phase"] in {"review", "rereview"}
    if (base != state["snapshot"]["base_commit"] or current["head"] != state["snapshot"]["head"]
            or review and current != state["snapshot"]):
        raise TaskError("Git history/base or reviewed content drifted; abort refused, work preserved")
    return current, processes, references


def abort_loop(identifier):
    from .loop import control_store, report, LoopRuntime
    store, path = control_store(identifier)
    with PublicationStore(path).locked() as publication:
        state, pause = store.read()
        if state["status"] == "aborted":
            return report(state, pause)
        if state["active_pass"] is None:
            raise TaskError("No unfinished pass to abort; use --continue for a pause or --new for a stopped boundary")
        if "delivery" not in state:
            raise TaskError("Historical claim lacks durable delivery bookkeeping; inspect it without inferring non-delivery")
        registry, identities = ContextRegistry(), HerdrContexts()
        integration = IntegrationStore(publication_store=publication).read()
        if integration and integration["state"] not in {"installed", "abandoned"}:
            raise TaskError("An integration claim is unresolved; inspect it before aborting the loop pass")
        journal = store.abort_journal(state["run_id"])
        if state["status"] == "aborting":
            if journal is None:
                raise TaskError("Abort journal is missing; no claim can be released")
            original = journal["original"]
            if state != dict(original, status="aborting", reason="Abort recorded; rerun --abort to finish reconciliation"):
                raise TaskError("Abort checkpoint conflicts with its original claim")
            contexts = [registry.get(c["context_id"]) for c in journal["contexts"]]
            if any(c not in (before, after) for c, before, after in
                   zip(contexts, journal["contexts"], journal["reconciled"])):
                raise TaskError("Context changed since abort began; preserve the archive and inspect it")
        else:
            if journal is not None:
                raise TaskError("Conflicting abandonment archive; no claim was released")
            original = copy.deepcopy(state)
            panes = identities.snapshot()
            contexts = [c for c in registry.list(identifier, include_retired=True)
                        if not c["retired_at"] or c["state"] == "abandoned" and any(
                            p["terminal_id"] == c["terminal_id"] or p["pane_id"] == c["pane_id"] for p in panes)]
            claimed = {c["context_id"] for c in (original["implementation"], original["reviewer"], original["active_pass"]) if c}
            if any(c["retired_at"] and c["context_id"] in claimed for c in contexts):
                raise TaskError("Saved pass context was already retired outside this abort; inspect the stale claim")
        def review_snapshot():
            # An abort journal already revokes recovery. Before that boundary,
            # use normal recovery's durable invalidation and acceptance revocation.
            delivery = PassDelivery(original, lambda *_: store.save(original) if state["status"] != "aborting" else None, None)
            try:
                return delivery.snapshot()
            except TaskError:
                LoopRuntime(identifier, publication).revoke_active_acceptance(original)
                raise

        current, processes, references = verify_boundary(original, registry, identities, contexts, review_snapshot)
        saved = publication.read()
        cleared = dict(saved, acceptance=None)
        if (saved["intent"] is not None or saved["rebase"] is not None and saved["rebase"].get("result") is None
                or saved["acceptance"] is not None
                and saved["acceptance"].get("pass_id") != original["active_pass"]["pass_id"]):
            raise TaskError("Publication state conflicts with the unfinished pass; abort refused")
        review = (original.get("delivery") or {}).get("review")
        if review and object_digest(cleared) != review["publication"]:
            raise TaskError("Publication provenance changed since the claimed review; abort refused")
        if journal is None:
            timestamp = now()
            reconciled = []
            for context in contexts:
                after = dict(context)
                verified = references.get(context["context_id"])
                reference = context_reference(context)
                if verified and merge_session(None, reference, context["agent"]) != verified:
                    after["herdr_session"] = json.dumps(dict(reference, **verified))
                if context["context_id"] == original["active_pass"]["context_id"] and context["role"] == "review":
                    after.update(state="abandoned", retired_at=timestamp)
                elif context["role"] == "implementation":
                    after.update(state="active" if context_reference(context) else "launching")
                    if context_reference(context):
                        after["resumability"] = "yes"
                reconciled.append(after)
            journal = dict(original=original, contexts=contexts, reconciled=reconciled, snapshot=current,
                           processes=processes, publication=saved, timestamp=timestamp)
            # Repeat slow observations before durably revoking the original pass.
            if verify_boundary(original, registry, identities, contexts, review_snapshot) != (current, processes, references):
                raise TaskError("Task activity changed during abort verification; nothing was abandoned")
            store.begin_abort(original, journal)
            state, _ = store.read()
        elif (current != journal["snapshot"] or processes != journal["processes"]
                or saved not in (journal["publication"], dict(journal["publication"], acceptance=None))):
            raise TaskError("Git/runtime/publication changed during abort; reconciliation refused")
        if publication.read() != saved:
            raise TaskError("Publication changed before abort reconciliation")
        if saved != cleared:
            publication.write(cleared)
        registry.reconcile_loop_abort(journal["contexts"], journal["reconciled"])
        review_ready = original["active_pass"]["phase"] in {"review", "rereview"}
        reason = ("Pass abandoned; checkout and session history preserved. Resume the exact implementation session "
                  "in its original pane, then use ")
        reason += (f"task loop {identifier} --new --from-review for fresh independent review" if review_ready else
                   f"task loop {identifier} --new to explicitly continue incomplete implementation before review")
        finished = dict(original, status="aborted", reason=reason, active_pass=None, delivery=None, next_phase=None,
                        snapshot=current, abandonment=dict(claim=original["active_pass"], review_ready=review_ready,
                                                           timestamp=journal["timestamp"]))
        implementation = registry.get(original["implementation"]["context_id"])
        if context_reference(implementation):
            finished["implementation"] = binding(implementation)
            if not review_ready and implementation["agent"] == "pi":
                finished["reason"] += (". If the Pi session has no messages yet, explicitly continue "
                                       "implementation in that native session first")
        else:
            finished["reason"] = (f"Undelivered initial pass abandoned; checkout preserved. "
                                  f"Use task loop {identifier} --new to reuse its original empty-shell reservation")
        store.finish_abort(state, finished)
        return report(*store.read())

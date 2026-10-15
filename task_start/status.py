"""Canonical, read-only local task projection. No controller or history writer.

The overview exports allowlisted identities and outcomes. Selected loop reports
reuse their owning renderer after verification; prompts, publication bodies and
provider transcripts never cross either read seam.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

from . import TaskError
from .command import command_name
from .config import load_projects, status_projects_root
from .contexts import ContextRegistry, HerdrContexts, context_reference
from .integration_state import IntegrationStore
from .loop_state import LoopStore, ITERATION_LIMIT, ITERATION_BYTES
from .pass_delivery import check_directory, object_digest
from .publication_rebase import validate_commits, validate_record as validate_rebase
from .publication_state import PublicationStore, verify_publication_history
from .publish import verify_acceptance, verify_commit
from .review_handoff_state import ReviewHandoffStore, standalone_current, verify_review_evidence
from .review_result import publication_fingerprint
from .review_state import snapshot
from .sessions import merge_session
from .workspace import Git, Herdr, belongs_to_issue


HISTORY_LIMIT = 64
BINDING_KEYS = ('issue', 'repository', 'worktree', 'branch', 'base_branch', 'endpoint', 'workspace_id')
READ_ERRORS = (TaskError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError)


class InvalidAcceptance(TaskError):
    """Positive local mismatch, distinct from an observation that failed."""


def problem(items, code, message):
    entry = dict(code=code, message=message)
    if entry not in items:
        items.append(entry)


def controller_state(directory):
    """Observe the existing lifetime lock without creating or modifying a file.

    A held lock proves a controller owns this checkout, not which operation it
    is performing. Absence of the lock file proves nothing about a saved pass.
    """
    if os.name != 'posix':
        return 'unknown'
    import fcntl
    try:
        fd = os.open(directory / 'agentic-workflows-publication.lock', os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'held'
            fcntl.flock(fd, fcntl.LOCK_UN)
            return 'unowned'
        finally:
            os.close(fd)
    except OSError:
        return 'unknown'


def project_identity(repo, base, projects, root):
    matches = [p for p in projects if root is not None
               and str((root / p.repo_name).resolve()) == repo
               and (base is None or p.base_branch == base)]
    if len(matches) != 1:
        return dict(state='unverified', linear_project=None, base_branch=base,
                    candidates=sorted(p.linear_project for p in matches))
    project = matches[0]
    return dict(state='verified', linear_project=project.linear_project,
                base_branch=project.base_branch, candidates=[])


def exact_binding(value, binding):
    if any(value.get(k) != binding[k] for k in BINDING_KEYS):
        raise TaskError('Saved evidence conflicts with task binding')


def checked_record(source, reader):
    """Supplement storage decoders at this read seam, without repairing evidence."""
    value = reader()
    if value is None:
        return value
    if source == 'loop':
        for record in value[0]['records']:
            allowed = ({'clean', 'findings', 'blocked', 'failed'} if record['phase'] in {'review', 'rereview'}
                       else {'completed', 'blocked', 'failed'})
            if record['state'] not in allowed:
                raise TaskError('Invalid saved pass outcome')
    if source in {'review', 'integration'}:
        binding = value['binding']
        if set(binding) != set(BINDING_KEYS) or any(not isinstance(v, str) or not v for v in binding.values()):
            raise TaskError('Invalid record binding')
    if source == 'review' and value['result'] is not None:
        result = value['result']
        if (result['state'] not in {'clean', 'findings', 'blocked', 'failed'}
                or result['state'] == 'clean' and result['invalidated']
                or not isinstance(result['findings'], list)
                or value['reviewer'] is not None and result['context_id'] != value['reviewer']['context_id']):
            raise TaskError('Invalid standalone review outcome')
    return value


def context_binding(saved, contexts, binding, role, *, worktree=None, abandoned=False):
    matches = [c for c in contexts if c['context_id'] == saved['context_id']]
    if len(matches) != 1:
        raise TaskError('Missing or ambiguous context')
    context = matches[0]
    if (context['role'] != role or context['retired_at'] and not abandoned
            or any(context[k] != binding[k] for k in ('issue', 'repository', 'endpoint', 'workspace_id'))
            or context['worktree'] != (worktree or binding['worktree'])):
        raise TaskError('Context binding changed')
    if 'session' in saved:
        reference = context_reference(context)
        if (reference is None or merge_session(reference, saved['session'], context['agent']) != saved['session']
                or any(context[k] != saved[k] for k in ('agent', 'model', 'mode'))):
            raise TaskError('Context session changed')
    return context


def retained_observation(state, contexts, binding):
    """Necessary local evidence for trying recovery, never a recovery guarantee."""
    delivery = state.get('delivery')
    if not state['active_pass'] or not delivery or not delivery['receipt'] or delivery['invalidated']:
        return False
    expected = delivery['context']
    context = context_binding(expected, contexts, binding,
                              'review' if delivery['review'] is not None else 'implementation')
    if (context['state'] not in {'active', 'reviewing', 'uncertain', 'launching'}
            or any(context[k] != v for k, v in delivery['location'].items())
            or object_digest(sorted(c['context_id'] for c in contexts if c['issue'] == binding['issue']))
               != delivery['contexts_sha256']):
        return False
    check_directory(delivery)
    # Mirror the local invariants in LoopRuntime.recover/PassDelivery.snapshot
    # without invoking their invalidation writes or provider/controller actions.
    before = state['snapshot']
    current = snapshot(Path(binding['worktree']), before['base_commit'], binding['branch']).as_dict()
    git = Git(Path(binding['repository']))
    base = git.command('rev-parse', '--verify', f"refs/heads/{binding['base_branch']}^{{commit}}").strip()
    if (current['head'] != before['head'] or base != before['base_commit']
            or (delivery['review'] is not None or state['next_phase'] in {'review', 'rereview'}) and current != before
            or git.resolve_scope(Path(binding['worktree']), binding['branch'], binding['issue'], None) != delivery['slice']):
        raise TaskError('Interrupted pass Git/base/scope evidence changed')
    return True


def registered_workspace(binding, common, entries, worktrees, endpoint):
    """Ignore display labels. Require both Herdr lists and exact Git identity."""
    if endpoint != binding['endpoint']:
        raise TaskError('Different Herdr endpoint')
    matches = [w for w in entries if w['workspace_id'] == binding['workspace_id']]
    locations = [w for w in entries if isinstance(w.get('worktree'), dict)
                 and w['worktree'].get('checkout_path') == binding['worktree']]
    if not matches and not locations:
        return False
    if len(matches) != 1 or locations != matches:
        raise TaskError('Ambiguous workspace identity')
    location = matches[0]['worktree']
    if (location.get('repo_root') != binding['repository'] or location.get('repo_key') != str(common)
            or location.get('checkout_path') != binding['worktree']
            or location.get('is_linked_worktree') is not True):
        raise TaskError('Workspace repository/checkout identity conflicts')
    if worktrees['source']['repo_root'] != binding['repository']:
        raise TaskError('Herdr worktree source conflicts')
    trees = worktrees['worktrees']
    candidates = [w for w in trees if w.get('path') == binding['worktree']
                  or w.get('open_workspace_id') == binding['workspace_id']]
    if len(candidates) != 1:
        raise TaskError('Herdr worktree is missing or ambiguous')
    tree = candidates[0]
    if (tree['path'] != binding['worktree'] or tree['branch'] != binding['branch']
            or tree.get('open_workspace_id') != binding['workspace_id']
            or tree.get('is_linked_worktree') is not True
            or any(tree.get(k) is not False for k in ('is_bare', 'is_detached', 'is_prunable'))):
        raise TaskError('Herdr worktree identity conflicts')
    return True


def event(source, phase, state, *, context_id=None, pass_id=None, timestamp=None, **details):
    return dict(source=source, phase=phase, state=state, context_id=context_id,
                pass_id=pass_id, timestamp=timestamp, **details)


def guidance(task, observable=False, blocked_retry=None, extension=None):
    """Hints only: no launch, resume, receipt polling, or lifecycle mutation."""
    issue, state = task['issue'], task['state']
    result = dict(kind='none', human_action_required=False, reconciliation_required=False,
                  message='No intervention indicated by the available local evidence.', command=None, options=[])
    if task['uncertainties'] or state == 'unknown':
        result.update(kind='inspect', human_action_required=True, reconciliation_required=True,
                      message='Inspect the conflicting or incomplete local evidence; no recovery action is inferred.')
    elif state == 'awaiting_user':
        result.update(kind='human_action', human_action_required=True,
                      message='Handle trust/setup in the identified pane, then acknowledge in the original controller.')
    elif state == 'paused':
        result.update(kind='continue', human_action_required=True, command=f'{command_name()} loop {issue} --continue',
                      message='The loop deliberately paused at a saved boundary; explicit continuation is required.')
    elif blocked_retry and task['controller'] == 'unowned':
        result.update(kind='retry_review', human_action_required=True,
                      command=f'{command_name()} loop {issue} --retry-review',
                      message='Resolve the external review blocker first, then explicitly retry with saved findings and checks. '
                              'Checkout and session verification may still refuse continuation.')
    elif extension and state == 'escalated' and task['controller'] == 'unowned':
        result.update(kind='extend' if extension['command'] else 'inspect', human_action_required=True,
                      command=extension['command'], message=extension['message'])
    elif task['controller'] == 'held' and state in {'integration_pending', 'publication_pending', 'cleanup_pending', 'aborting'}:
        result['message'] = 'A controller owns this checkout and its saved operation is pending; no intervention is indicated.'
    elif state == 'interrupted' and observable and task['controller'] == 'unowned':
        result.update(kind='observe', human_action_required=True, reconciliation_required=True,
                      command=f'{command_name()} loop {issue}',
                      message=f'Try observing the original saved pass by repeating {command_name()} loop. It never resends the pass; runtime/provider proof may still refuse recovery.')
    elif state in {'interrupted', 'escalated', 'aborting', 'aborted', 'uncertain', 'findings', 'blocked', 'failed',
                   'stale_review', 'integration_pending', 'publication_pending', 'cleanup_pending'}:
        result.update(kind='inspect', human_action_required=True, reconciliation_required=True,
                      message='Inspect the saved boundary and contexts before taking further lifecycle action.')
    if task['loop'] and task['loop']['active_pass'] and state not in {'running', 'awaiting_user'}:
        result['options'].append(dict(command=f'{command_name()} loop {issue} --abort', conditional=True,
            message='If deliberately abandoning: stop the controller, quit task agents to their original shells, stop child jobs, and run outside the checkout. Abort must prove safety and may refuse.'))
    if state in {'aborted', 'escalated', 'cleanup_pending'}:
        result['options'].append(dict(command=f'{command_name()} cleanup {issue} --force', conditional=True,
            message='Only if discarding this execution is intended. Run outside its checkouts after stopping active work; cleanup verifies ownership/publication and may refuse.'))
    return result


def empty_task(issue, repo, path, projects, root):
    return dict(issue=issue, repository=dict(path=repo, state='unverified'),
        project=project_identity(repo, None, projects, root),
        workspace=dict(path=path, branch=None, workspace_id=None, endpoint=None, registration='unknown'),
        state='unknown', phase=None, controller='unknown',
        contexts=dict(implementation=None, review=None, integration=None), loop=None,
        review=dict(state='not_recorded', acceptance='absent', context_id=None, pass_id=None),
        integration=None, publication=dict(state='not_recorded', head=None, pull_number=None, repository=None,
                                          merge='not_observed', current_acceptance_published=False),
        cleanup=[], uncertainties=[], earlier_iterations=unavailable_iterations(),
        history=dict(limit=HISTORY_LIMIT, ordering='source_order', truncated=False, entries=[]))


def unavailable_iterations():
    return dict(state='unavailable', limit=ITERATION_LIMIT, max_bytes=ITERATION_BYTES,
                ordering='oldest_first', entries=[],
                message='Earlier iterations unavailable. Runs discarded before retention cannot be reconstructed.')


def project_iterations(store, loop, binding, contexts, *, report_request=None):
    """Historical failures affect only this projection, never current guidance."""
    result = unavailable_iterations()
    try:
        retained = store.iterations()
        if not retained:
            return result
        entries, reports = [], {}
        for item in retained:
            entry = dict(order=item['order'], evidence='unavailable', run_id=None,
                         state='unknown', passes=None, reviews=None, implementation_context=None,
                         reviewer_context=None, reason=None, report_available=False)
            try:
                summary, checkpoint = item['summary'], item['checkpoint']
                if not summary or summary['run_id'] == loop['run_id']:
                    raise TaskError('Invalid prior identity')
                exact_binding(summary['binding'], binding)
                for key, role in (('implementation', 'implementation'), ('reviewer', 'review')):
                    saved = (checkpoint[key] if checkpoint else
                             dict(context_id=summary[key + '_context']) if summary[key + '_context'] else None)
                    if saved:
                        context_binding(saved, contexts, binding, role, abandoned=True)
                entry.update({k: v for k, v in summary.items() if k not in {'binding', 'state'}})
                entry['evidence'] = 'partial'
                if checkpoint:
                    try:
                        if checkpoint['status'] == 'aborted':
                            archive = store.abort_journal(checkpoint['run_id'])
                            if (not archive or archive['original']['binding'] != checkpoint['binding']
                                    or archive['original']['active_pass'] != checkpoint['abandonment']['claim']
                                    or archive['timestamp'] != checkpoint['abandonment']['timestamp']):
                                raise TaskError('Missing historical abandonment proof')
                        validate_loop_report(checkpoint)
                    except READ_ERRORS:
                        pass  # Unfiltered failure prose is never displayed.
                    else:
                        entry.update(evidence='verified', state=summary['state'], reason=checkpoint['reason'], report_available=True)
                        if report_request and report_request.get('prior_run_id') == summary['run_id']:
                            reports[summary['run_id']] = render_loop_report(checkpoint, item['pause'])
            except READ_ERRORS:
                pass
            entries.append(entry)
        if store.iterations() != retained:
            return result
        result.update(state='available' if all(e['evidence'] == 'verified' for e in entries) else 'partial',
                      entries=entries,
                      message=f'Up to {ITERATION_LIMIT} earlier iterations retained. Older or pre-feature runs may be unavailable; these are not lifetime totals.')
        if report_request and report_request.get('prior_run_id') in reports:
            report_request['text'] = reports[report_request['prior_run_id']]
    except READ_ERRORS:
        pass
    return result


def read_task(issue, repo, path, contexts, registry, projects, root, discovery, entries, endpoint, herdr_factory,
              *, loop_report=None):
    try:
        return _read_task(issue, repo, path, contexts, registry, projects, root, discovery, entries, endpoint,
                          herdr_factory, loop_report=loop_report)
    except READ_ERRORS:
        # Even malformed shapes in older, less strictly decoded records must be
        # represented as uncertainty, never an empty overview or a traceback.
        task = empty_task(issue, repo, path, projects, root)
        task['state'] = 'uncertain'
        problem(task['uncertainties'], 'evidence', 'Local evidence could not be decoded safely; inspect the saved task records.')
        task['guidance'] = guidance(task)
        return task


def _read_task(issue, repo, path, contexts, registry, projects, root, discovery, entries, endpoint, herdr_factory,
               *, loop_report=None):
    related = [c for c in contexts if c['issue'] == issue and c['repository'] == repo
               and c['worktree'] == path and c['role'] in {'implementation', 'review'}]
    active = [c for c in related if not c['retired_at']]
    task = empty_task(issue, repo, path, projects, root)
    errors, events, history_truncated = task['uncertainties'], [], False
    binding = dict(issue=issue, repository=repo, worktree=path, branch=None, base_branch=None,
                   workspace_id=None, endpoint=None)
    binding['base_branch'] = task['project']['base_branch']
    for key in ('endpoint', 'workspace_id'):
        values = {c[key] for c in active or related}
        if len(values) == 1:
            binding[key] = values.pop()
    store, loop, review, integration, saved, common = None, None, None, None, None, None
    readers = []

    def read(source, reader):
        try:
            value = checked_record(source, reader)
            readers.append((source, reader, value))
            return value
        except READ_ERRORS:
            problem(errors, source, f'{source} evidence is unavailable, malformed, or conflicts with identity.')
            return None

    try:
        for value in (repo, path):
            if not Path(value).is_absolute() or str(Path(value).resolve()) != value:
                raise TaskError('Aliased checkout')
        common = Git(Path(repo)).check_repository()
        task['repository']['state'] = 'verified'
        trees = Git(Path(repo)).worktrees()
        matches = [w for w in trees if w['worktree'] == path]
        if (len(matches) != 1 or path == repo or any(k in matches[0] for k in ('locked', 'prunable'))
                or not Path(path).is_dir()):
            raise TaskError('Task checkout unavailable')
        branch = matches[0].get('branch', '').removeprefix('refs/heads/')
        if (not belongs_to_issue(branch, issue)
                or sum(w.get('branch') == f'refs/heads/{branch}' for w in trees) != 1):
            raise TaskError('Task branch mismatch')
        git = Git(Path(path))
        if (git.command('rev-parse', '--show-toplevel').strip() != path
                or git.command('rev-parse', '--path-format=absolute', '--git-common-dir').strip() != str(common)
                or git.command('symbolic-ref', 'HEAD').strip() != f'refs/heads/{branch}'):
            raise TaskError('Checkout Git identity conflicts')
        binding['branch'] = task['workspace']['branch'] = branch
        scope = Git(Path(repo)).resolve_scope(Path(path), branch, issue, None)
        store = PublicationStore(Path(path))
        task['controller'] = controller_state(store.directory)
        def checkout_identity():
            return (Git(Path(repo)).check_repository(),
                    [w for w in Git(Path(repo)).worktrees() if w['worktree'] == path],
                    Git(Path(repo)).resolve_scope(Path(path), branch, issue, None),
                    store._identity(), Git(Path(path)).command('rev-parse', '--absolute-git-dir').strip())
        readers.append(('checkout', checkout_identity, (common, matches, scope, store.identity, str(store.directory))))
    except READ_ERRORS:
        problem(errors, 'checkout', 'Exact task checkout/branch/scope identity cannot be verified.')

    if store is not None:
        loop_store = LoopStore(Path(path))
        if loop_store.path.exists() or loop_store.path.is_symlink():
            pair = read('loop', loop_store.read)
            if pair:
                loop, pause = pair
                if loop['status'] in {'aborting', 'aborted'}:
                    archive = read('abandonment', lambda: loop_store.abort_journal(loop['run_id']))
                    if (not archive or archive['original']['binding'] != loop['binding']
                            or loop.get('abandonment') and (archive['timestamp'] != loop['abandonment']['timestamp']
                                or archive['original']['active_pass'] != loop['abandonment']['claim'])
                            or loop['status'] == 'aborted' and any(c not in contexts for c in archive['reconciled'])):
                        problem(errors, 'abandonment', 'Loop abandonment lacks matching archive/context evidence.')
        saved = read('publication', store.read)
        if saved is None:
            task['review']['acceptance'] = 'unverified'
            task['publication']['state'] = 'unknown'
        integration = read('integration', IntegrationStore(publication_store=store).read)
        review = read('review', ReviewHandoffStore(Path(path)).read)
        bindings = [s['binding'] for s in (loop, integration, review) if s]
        if saved:
            if saved['acceptance']:
                bindings.append({k: saved['acceptance'].get(k) for k in BINDING_KEYS})
            if saved['rebase']:
                bindings.append(saved['rebase'].get('binding', {}))
        # All sources must agree. Do not select the most recent or most convenient.
        candidates = active or related
        for key in ('endpoint', 'workspace_id'):
            values = {c[key] for c in candidates}
            values.update(b.get(key) for b in bindings)
            if len(values) == 1 and None not in values:
                binding[key] = values.pop()
            else:
                problem(errors, 'binding', 'Context and workflow workspace identities are missing or conflicting.')
        bases = {b.get('base_branch') for b in bindings}
        if len(bases) == 1 and None not in bases:
            binding['base_branch'] = bases.pop()
        elif bases:
            problem(errors, 'binding', 'Workflow base identities conflict.')
        task['project'] = project_identity(repo, binding['base_branch'], projects, root)
        if binding['base_branch'] is None:
            binding['base_branch'] = task['project']['base_branch']
        for source in bindings:
            try:
                exact_binding(source, binding)
            except READ_ERRORS:
                problem(errors, 'binding', 'Saved workflow evidence belongs to a different task/checkout/context binding.')

    task['workspace'].update({k: binding[k] for k in ('workspace_id', 'endpoint')})
    if task['project']['state'] != 'verified':
        problem(errors, 'project', 'Linear project membership is unverified in the configured repository/base mapping.')
    conflicts = [c for c in contexts if not c['retired_at']
        and (c['worktree'] == path or binding['workspace_id'] is not None
             and c['workspace_id'] == binding['workspace_id'] and c['endpoint'] == binding['endpoint'])
        and (c['issue'] != issue or c['repository'] != repo
             or c['role'] != 'integration' and c['worktree'] != path)]
    if conflicts:
        problem(errors, 'binding', 'Another context claims this checkout/workspace with a conflicting task or repository identity.')
    if discovery == 'available' and common is not None and binding['workspace_id']:
        try:
            # A truly absent workspace needs no per-repository Herdr query.
            present = any(w['workspace_id'] == binding['workspace_id'] or
                          (w.get('worktree') or {}).get('checkout_path') == path for w in entries)
            registered = registered_workspace(binding, common, entries,
                herdr_factory(Path(repo)).command('list') if present else {}, endpoint)
            task['workspace']['registration'] = 'registered' if registered else 'absent'
        except READ_ERRORS:
            problem(errors, 'workspace', 'Herdr registration is unavailable, ambiguous, or conflicts with the task checkout.')
    elif discovery != 'available':
        problem(errors, 'discovery', 'Herdr registration is temporarily unavailable; saved task evidence is diagnostic only.')
    if conflicts:
        task['workspace']['registration'] = 'unknown'

    # Identity disagreement makes all positive workflow conclusions unsafe.
    identity_ok = not any(e['code'] in {'checkout', 'binding'} for e in errors)
    current_review = False
    if identity_ok:
        try:
            current_review = standalone_current(review, loop)
        except READ_ERRORS:
            problem(errors, 'review', 'Standalone review consumption/origin conflicts with the saved loop outcome.')
    selected = {}
    if identity_ok:
        try:
            if loop:
                for key, role in (('implementation', 'implementation'), ('reviewer', 'review')):
                    if loop[key]:
                        selected[role] = context_binding(loop[key], contexts, binding, role,
                                                        abandoned=loop['status'] == 'aborted')
            if integration and integration.get('context') and integration['state'] != 'abandoned':
                selected['integration'] = context_binding(integration['context'], contexts, binding,
                    'integration', worktree=integration['checkout'])
            if current_review and review.get('reviewer'):
                selected['review'] = context_binding(review['reviewer'], contexts, binding, 'review')
            if saved and saved['acceptance'] and (not loop or current_review):
                accepted_context = context_binding(dict(context_id=saved['acceptance']['context_id']),
                                                   contexts, binding, 'review')
                selected.setdefault('review', accepted_context)
        except READ_ERRORS:
            problem(errors, 'context', 'Saved execution context/session no longer matches its registry binding.')
    integration_contexts = [c for c in contexts if c['role'] == 'integration' and not c['retired_at']
                           and c['issue'] == issue and c['repository'] == repo
                           and c['endpoint'] == binding['endpoint'] and c['workspace_id'] == binding['workspace_id']]
    if integration_contexts and (not integration or
            any(c['context_id'] != (integration.get('context') or {}).get('context_id') for c in integration_contexts)):
        problem(errors, 'integration', 'Active integration context lacks matching task-local provenance.')
    for role in ('implementation', 'review', 'integration'):
        choices = [c for c in active or related if c['role'] == role]
        if role == 'review' and loop and not loop['reviewer'] and not current_review:
            choices = []  # Earlier reviewers are historical, not the pending first reviewer.
        if role not in selected and len(choices) == 1:
            selected[role] = choices[0]
        elif role not in selected and len(choices) > 1:
            problem(errors, 'context', f'{role.capitalize()} context identity is ambiguous.')
        c = selected.get(role)
        task['contexts'][role] = ({k: c[k] for k in ('context_id', 'agent', 'model', 'mode', 'state',
            'worktree', 'workspace_id', 'endpoint', 'pane_id', 'terminal_id', 'session_id', 'session_kind', 'retired_at')}
            if c else None)
    for c in selected.values():
        events.append(event('context', c['role'], 'allocated', context_id=c['context_id'], timestamp=c['allocated_at']))
        if c['retired_at']:
            events.append(event('context', c['role'], c['state'], context_id=c['context_id'], timestamp=c['retired_at']))

    observable = False
    if identity_ok and loop:
        task['loop'] = dict(run_id=loop['run_id'], state=loop['status'], next_phase=loop['next_phase'],
                            passes=loop['pass_count'], reviews=loop['review_count'], pause_requested=pause,
                            active_pass=loop['active_pass'])
        task.update(state=loop['status'], phase=loop['next_phase'])
        for record in loop['records']:
            events.append(event('loop', record['phase'], record['state'], context_id=record['context_id'],
                pass_id=record['pass_id'], findings_count=len(record['findings']),
                resolutions_count=len(record['resolutions']),
                assessment_state=(record.get('task_assessment') or {}).get('state')))
        events.append(event('loop', loop['next_phase'], loop['status']))
        if loop.get('abandonment'):
            events.append(event('loop', loop['abandonment']['claim']['phase'], 'aborted',
                                timestamp=loop['abandonment']['timestamp']))
        if task['controller'] == 'unowned' and loop['active_pass']:
            try:
                observable = retained_observation(loop, contexts, binding)
            except READ_ERRORS:
                observable = False
                problem(errors, 'observation', 'Interrupted pass identity, Git/base, scope, or delivery evidence cannot be verified; inspect it manually.')
            if loop['status'] in {'running', 'interrupted'}:
                task['state'] = 'interrupted' if observable else 'uncertain'
        elif task['controller'] == 'unknown' and loop['status'] in {'ready', 'running'}:
            problem(errors, 'controller', 'Controller ownership cannot be observed; a saved running flag is not liveness proof.')
        elif task['controller'] == 'unowned' and loop['status'] == 'ready':
            task['state'] = 'uncertain'
        if loop['records']:
            reviews = [r for r in loop['records'] if r['phase'] in {'review', 'rereview'}]
            if reviews:
                task['review'].update(state=reviews[-1]['state'], context_id=reviews[-1]['context_id'],
                                      pass_id=reviews[-1]['pass_id'])
    if identity_ok and review:
        result = review['result']
        if result:
            if not loop or result['pass_id'] not in {r['pass_id'] for r in loop['records']}:
                events.append(event('review', 'review', result['state'], context_id=result['context_id'],
                                    pass_id=result['pass_id'], findings_count=len(result['findings'])))
            if current_review:
                task['review'].update(state=result['state'], context_id=result['context_id'], pass_id=result['pass_id'])
                if not loop or loop['status'] in {'clean', 'escalated', 'aborted'}:
                    task.update(state=result['state'], phase='review')
        elif current_review:
            task.update(state='running' if task['controller'] == 'held' else 'uncertain', phase='review')

    if identity_ok and saved:
        accepted, intent, history, rebase = (saved[k] for k in ('acceptance', 'intent', 'publication_history', 'rebase'))
        try:
            if rebase:
                validate_rebase(rebase)
                if rebase['result'] and validate_commits(Git(Path(path)), rebase) != rebase['result']['head']:
                    raise TaskError('Installed integration history changed')
                task['integration'] = dict(state='installed' if rebase['result'] else 'installing', context_id=None)
                events.append(event('rebase', 'integration', task['integration']['state']))
                if not accepted and task['state'] not in {'ready', 'running', 'paused', 'interrupted', 'escalated', 'aborting', 'aborted'}:
                    task.update(state='stale_review' if rebase['result'] else 'integration_pending', phase='integration')
            if history == 'unknown':
                raise TaskError('Legacy publication uncertain')
            if history:
                hb = {k: binding[k] for k in ('issue', 'repository', 'worktree', 'branch', 'base_branch')}
                verify_publication_history(history, hb, history.get('identity'))
                task['publication'].update(state=history['state'], head=history['head'],
                    pull_number=history.get('pull_number'), repository=history['identity']['repository'])
                cycles = history.get('cycles', [])
                if cycles and accepted:
                    last = cycles[-1]
                    if accepted['pass_id'] == last['acceptance']['pass_id']:
                        if accepted != last['acceptance'] or intent != last['intent']:
                            raise TaskError('Current acceptance/intent conflicts with publication lineage')
                    elif (last['state'] != 'complete' or accepted['review_state']['head'] != history['head']
                          or accepted['review_state']['base_commit'] != history['base_commit']):
                        raise TaskError('Follow-up acceptance conflicts with publication lineage')
                elif cycles and cycles[-1]['state'] != 'complete':
                    raise TaskError('Unfinished publication lost its acceptance/intent')
                history_truncated = len(cycles) > 12
                for cycle in cycles[-12:]:
                    events.append(event('publication', 'publication', cycle['state'], pass_id=cycle['acceptance']['pass_id']))
                if not cycles:
                    events.append(event('publication', 'publication', history['state']))
                if history['state'] == 'pending' or cycles and cycles[-1]['state'] != 'complete':
                    task.update(state='publication_pending', phase='publication')
                elif cycles and cycles[-1]['state'] == 'complete':
                    task['publication']['state'] = 'complete'
            elif intent:
                task['publication']['state'] = 'intent'
                task.update(state='publication_pending', phase='publication')
                events.append(event('publication', 'publication', 'intent'))
            if intent and (not accepted or intent['pass_id'] != accepted['pass_id'] or intent['version'] != 1
                    or any(not isinstance(intent[k], str) or not intent[k] for k in
                           ('tree', 'index', 'title', 'body', 'message', 'remote', 'repository'))
                    or isinstance(history, dict) and any(intent[k] != history['identity'][k] for k in ('remote', 'repository'))):
                raise TaskError('Publication intent has no matching acceptance')
        except READ_ERRORS:
            problem(errors, 'publication', 'Publication/integration evidence cannot be validated; no delivery or authorization is inferred.')
            task['publication']['state'] = 'unknown'
        if accepted and (not loop or current_review or any(r['pass_id'] == accepted.get('pass_id') for r in loop['records'])):
            task['review']['acceptance'] = 'unverified'
            if task['review']['pass_id'] is None:
                task['review'].update(context_id=accepted.get('context_id'), pass_id=accepted.get('pass_id'))
            try:
                verify_review_evidence(accepted, loop, review)
                if task['review']['pass_id'] != accepted.get('pass_id'):
                    raise TaskError('Current review outcome conflicts with retained acceptance')
                try:
                    verify_acceptance(accepted, SimpleNamespace(identifier=issue), SimpleNamespace(base_branch=binding['base_branch']),
                        Path(repo), SimpleNamespace(path=Path(path), branch=binding['branch'], workspace_id=binding['workspace_id']),
                        binding['endpoint'], registry, iteration_evidence=(loop, review))
                except TaskError:
                    raise InvalidAcceptance('Review context does not match acceptance') from None
                pinned = accepted['review_state']
                if intent:
                    verify_commit(Git(Path(path)), accepted, intent)
                elif snapshot(Path(path), pinned['base_commit'], binding['branch']).as_dict() != pinned:
                    raise InvalidAcceptance('Reviewed content changed')
                # Installed/published provenance pins a base independently of a moving local main.
                base = (rebase['base'] if rebase and rebase['result'] else
                        history['base_commit'] if isinstance(history, dict) and history['version'] == 2 else
                        Git(Path(repo)).command('rev-parse', '--verify', f"refs/heads/{binding['base_branch']}^{{commit}}").strip())
                if base != pinned['base_commit']:
                    raise InvalidAcceptance('Review base changed')
                if rebase and (rebase['result'] != pinned
                        or accepted.get('publication_approval') != publication_fingerprint(rebase['publication'])):
                    raise InvalidAcceptance('Review does not approve installed integration')
                task['review'].update(state='clean', acceptance='valid')
                # A completed older cycle remains PR evidence, but cannot attest
                # delivery of a newly accepted follow-up or changed content.
                cycles = history.get('cycles', []) if isinstance(history, dict) else []
                task['publication']['current_acceptance_published'] = bool(
                    task['publication']['state'] == 'complete' and cycles
                    and cycles[-1]['acceptance'] == accepted and cycles[-1]['intent'] == intent)
                events.append(event('acceptance', 'review', 'clean', context_id=accepted['context_id'],
                                    pass_id=accepted['pass_id'], timestamp=accepted['completed_at']))
                if task['state'] in {'unknown', 'clean'}:
                    published = task['publication']['current_acceptance_published']
                    task.update(state='published' if published else 'clean', phase='publication' if published else 'review')
            except InvalidAcceptance:
                task['review']['acceptance'] = 'invalid'
                task.update(state='stale_review', phase='review')
            except READ_ERRORS:
                problem(errors, 'acceptance', 'Acceptance cannot be reconciled with saved review outcomes/provenance and current local Git evidence; inspect it manually.')
        elif task['state'] == 'clean':
            task.update(state='stale_review', phase='review')

    if identity_ok and integration:
        task['integration'] = dict(state=integration['state'], context_id=(integration.get('context') or {}).get('context_id'),
                                   checkout=integration['checkout'], pass_id=integration['pass_id'])
        events.append(event('integration', 'integration', integration['state'],
                            context_id=task['integration']['context_id'], pass_id=integration['pass_id']))
        if integration['state'] not in {'installed', 'abandoned'}:
            task.update(state='running' if task['controller'] == 'held' and integration['state'] in {'preparing', 'running'}
                        else 'integration_pending', phase='integration')
        elif (integration['state'] == 'installed' and task['review']['acceptance'] != 'valid'
              and task['state'] not in {'ready', 'running', 'paused', 'interrupted', 'escalated', 'aborting', 'aborted'}):
            task.update(state='stale_review', phase='review')
    if not loop and not review and not integration and task['state'] == 'unknown' and active:
        task.update(state='implementation', phase='implementation')
    if any(c['state'] == 'awaiting_user' for c in selected.values()):
        task['state'] = 'awaiting_user' if task['controller'] == 'held' else 'uncertain'
    elif any(c['state'] == 'uncertain' for c in selected.values()) and not observable:
        task['state'] = 'uncertain'

    # Cleanup constructors create directories: inspect only the existing read paths.
    if common is not None:
        from .cleanup import DisposalStore, validate_record
        try:
            for file in Git(Path(repo)).disposal_files(issue):
                record = DisposalStore.read_path(file)
                if record['issue'] != issue or record['repository'] != repo:
                    raise TaskError('Cleanup issue/repository conflict')
                if record['path'] != path:
                    continue
                recorded_ids = {c['context_id'] for c in record['contexts']}
                if active and not {c['context_id'] for c in active} <= recorded_ids:
                    if record['state'] == 'complete':
                        continue  # A disposed predecessor may have used this same path.
                    raise TaskError('Pending cleanup conflicts with the current execution')
                validate_record(record, SimpleNamespace(identifier=issue), SimpleNamespace(base_branch=record['base']), Path(repo), registry)
                item = dict(state=record['state'], workspace=record['workspace_state'],
                            worktree=record['worktree_state'], branch=record['branch_state'], timestamp=record['at'],
                            execution_id=record.get('execution_id'))
                task['cleanup'].append(item)
                if record.get('merged'):
                    task['publication']['merge'] = 'recorded'
                events.append(event('cleanup', 'cleanup', item['state'], timestamp=item['timestamp']))
                if record['state'] == 'pending':
                    task.update(state='cleanup_pending', phase='cleanup')
                elif not os.path.lexists(path) and task['workspace']['registration'] == 'absent' and not active:
                    errors[:] = [e for e in errors if e['code'] != 'checkout']
                    task['workspace']['branch'] = record['branch']
                    if task['state'] != 'cleanup_pending':
                        task.update(state='cleaned', phase='cleanup')
                else:
                    problem(errors, 'cleanup', 'Completed cleanup conflicts with remaining checkout/context/workspace evidence.')
            if binding['base_branch']:
                retired = Git(Path(repo)).load_retirement(issue, binding['base_branch'])
                if retired and str(retired.path) == path:
                    task['cleanup'].append(dict(state='retirement_pending', workspace_id=retired.workspace_id))
                    task.update(state='cleanup_pending', phase='cleanup')
                    events.append(event('cleanup', 'cleanup', 'retirement_pending'))
        except READ_ERRORS:
            problem(errors, 'cleanup', 'Cleanup evidence is unavailable or conflicts with the task binding.')
    for source, reader, before in readers:
        try:
            if reader() != before:
                raise TaskError('Evidence changed')
        except READ_ERRORS:
            problem(errors, 'changed', f'{source} evidence changed during inspection; refresh before drawing conclusions.')
    if store and controller_state(store.directory) != task['controller']:
        problem(errors, 'changed', 'Controller ownership changed during inspection; refresh status.')
    paths = {c['worktree'] for c in contexts if c['issue'] == issue and not c['retired_at']
             and c['role'] in {'implementation', 'review'}}
    if len(paths) > 1:
        problem(errors, 'selection', 'Multiple active checkouts require manual selection; issue-only loop controls are ambiguous.')
    if task['repository']['state'] != 'verified' or any(e['code'] == 'binding' for e in errors):
        project = task['project']
        project['candidates'] = sorted(set(project['candidates'] + ([project['linear_project']] if project['linear_project'] else [])))
        project.update(state='unverified', linear_project=None)
    if errors:
        task['state'] = 'uncertain'
        task['publication']['current_acceptance_published'] = False
        if task['review']['acceptance'] == 'valid':
            task['review']['acceptance'] = 'unverified'
    task['history'] = dict(limit=HISTORY_LIMIT, ordering='source_order',
        truncated=len(events) > HISTORY_LIMIT or history_truncated,
        entries=events[-HISTORY_LIMIT:])
    blocked_retry = extension = None
    if loop:
        from .loop_state import blocked_review_retry, extension_guidance
        try:
            blocked_retry = blocked_review_retry(loop)
        except TaskError:
            pass
        try:
            extension = extension_guidance(loop)
        except TaskError:
            pass
    task['guidance'] = guidance(task, observable, blocked_retry, extension)
    if (loop and store and task['workspace']['registration'] == 'registered'
            and not any(e['code'] in {'checkout', 'binding', 'workspace', 'discovery', 'project', 'changed'} for e in errors)):
        task['earlier_iterations'] = project_iterations(loop_store, loop, binding, contexts, report_request=loop_report)
    if (loop_report is not None and not loop_report.get('prior_run_id')
            and loop and task['workspace']['registration'] == 'registered'
            and not any(e['code'] not in {'controller', 'observation', 'selection'} for e in errors)
            and task['state'] != 'stale_review'
            and (loop['status'] != 'clean' or task['review']['acceptance'] == 'valid')):
        # Render only the selected task, from the same checkpoint pair that the
        # canonical reader verified and re-read above. Never add prose to status.
        try:
            loop_report['text'] = render_loop_report(loop, pause, failure_guidance=task['guidance']['message'])
        except READ_ERRORS:
            pass  # The overview remains unchanged; the selected report is unavailable.
    return task


@dataclass(frozen=True)
class StatusReport:
    data: dict

    def as_dict(self):
        return self.data

    def render(self):
        return render_status(self.data)


def task_status(identifier=None, *, registry=None, projects=None, projects_root=None,
                identities=None, herdr_factory=Herdr):
    """None selects the dashboard overview; ISSUE retains broader diagnostics."""
    return _task_status(identifier, registry=registry, projects=projects, projects_root=projects_root,
                        identities=identities, herdr_factory=herdr_factory)


def _task_status(identifier=None, *, registry=None, projects=None, projects_root=None,
                 identities=None, herdr_factory=Herdr, report_target=None, loop_report=None):
    registry, identities = registry or ContextRegistry(), identities or HerdrContexts()
    errors = []
    if projects is None or projects_root is None:
        try:
            projects = load_projects() if projects is None else projects
            projects_root = status_projects_root() if projects_root is None else projects_root
        except READ_ERRORS:
            problem(errors, 'configuration', 'Configured project mappings/repository root are unavailable; project grouping is unverified.')
    projects = projects or []
    try:
        contexts = registry.list(include_retired=True)
        for context in contexts:
            issue = context['issue']
            prefix = {'implementation': 'I', 'review': 'R', 'integration': 'G'}[context['role']]
            if (not re.fullmatch(r'[A-Z][A-Z0-9]*-[1-9][0-9]*', issue)
                    or not re.fullmatch(re.escape(issue) + f'-{prefix}[1-9][0-9]*', context['context_id'])
                    or any(context[k] is not None and (not isinstance(context[k], str) or not context[k])
                           for k in ('repository', 'worktree', 'endpoint', 'workspace_id', 'retired_at'))):
                raise TaskError('Malformed context identity')
    except READ_ERRORS:
        contexts = None
        problem(errors, 'registry', 'Context registry is unavailable; local task discovery is uncertain.')
    entries, endpoint, discovery = None, None, 'available'
    try:
        endpoint = identities.endpoint()
        entries = herdr_factory(Path.cwd()).workspaces()
    except READ_ERRORS:
        discovery = 'unavailable'
        problem(errors, 'herdr', 'Herdr discovery is temporarily unavailable; this is not an empty workspace list.')
    tasks = []
    if contexts is not None:
        if any(c['role'] in {'implementation', 'review'} and (not c['repository'] or not c['worktree'])
               and (c['issue'] == identifier if identifier else not c['retired_at']) for c in contexts):
            problem(errors, 'binding', 'Some saved task contexts lack repository/checkout identity; discovery is incomplete.')
        candidates = sorted({(c['issue'], c['repository'], c['worktree']) for c in contexts
            if c['role'] in {'implementation', 'review'} and c['repository'] and c['worktree']
            and (c['issue'] == identifier if identifier else not c['retired_at'])})
        for issue, repo, path in candidates:
            if report_target is not None and (issue, repo, path) != (
                    report_target['issue'], report_target['repository']['path'], report_target['workspace']['path']):
                continue
            task = read_task(issue, repo, path, contexts, registry, projects, projects_root, discovery,
                             entries, endpoint, herdr_factory, loop_report=loop_report)
            if identifier or task['workspace']['registration'] == 'registered':
                tasks.append(task)
            elif task['workspace']['registration'] == 'unknown' and discovery == 'available':
                problem(errors, 'excluded', f'{issue}: an execution could not be matched to an exact registered workspace.')
        try:
            if registry.list(include_retired=True) != contexts:
                raise TaskError('Registry changed')
            if discovery == 'available' and (identities.endpoint() != endpoint
                    or herdr_factory(Path.cwd()).workspaces() != entries):
                raise TaskError('Herdr registration changed')
        except READ_ERRORS:
            discovery = 'uncertain'
            problem(errors, 'changed', 'Discovery evidence changed during inspection; refresh before replacing an overview.')
            for task in tasks:
                problem(task['uncertainties'], 'changed', 'Task discovery changed during inspection.')
                task['state'] = 'uncertain'
                task['publication']['current_acceptance_published'] = False
                if task['review']['acceptance'] == 'valid':
                    task['review']['acceptance'] = 'unverified'
                task['guidance'] = guidance(task)
    if contexts is None:
        discovery = 'unavailable'
    elif discovery == 'available' and errors:
        discovery = 'uncertain'
    groups = sorted({t['project']['linear_project'] for t in tasks if t['project']['state'] == 'verified'})
    return StatusReport(dict(version=1, selection='issue' if identifier else 'all', issue=identifier,
        discovery=dict(state=discovery, registered_workspaces=len(entries) if entries is not None else None,
                       uncertainties=errors),
        projects=[dict(linear_project=p, tasks=[n for n, t in enumerate(tasks) if t['project']['linear_project'] == p])
                  for p in groups],
        unverified_project_tasks=[n for n, t in enumerate(tasks) if t['project']['state'] != 'verified'],
        tasks=None if not identifier and discovery == 'unavailable' else tasks))


def report_identity(task):
    return (task['issue'], task['repository']['path'], *(task['workspace'][key]
            for key in ('path', 'branch', 'workspace_id', 'endpoint')))


def validate_loop_report(state, *, failure_guidance=None):
    """Validate all reported evidence; optionally redact a later failed boundary.

    Only current reports supply canonical guidance. Historical reports retain
    their strict, original-text contract. The projection never changes saved
    evidence, counts, routing, or acceptance.
    """
    from .implementation_pass import parse_implementation
    from .review_result import parse_verdict

    # Controller exceptions and failed review primitives can persist arbitrary
    # exception bodies. A verified preceding pass does not authorize that prose.
    controller_failure = ('. Inspect contexts before recovery; no handoff was retried' in state['reason']
            or any(f'. No initial handoff was claimed; rerun {name} loop ' in state['reason']
                   for name in ('task', 'awl', 'awp')))
    can_redact = (failure_guidance is not None and state['records']
                  and state['status'] in {'escalated', 'interrupted'})
    if controller_failure and not can_redact:
        raise TaskError('Unfiltered controller failure')
    findings, records = [], []
    failed_review = False
    for index, record in enumerate(state['records']):
        value = {key: record[key] for key in ('pass_id', 'state', 'summary', 'checks')}
        if record['phase'] in {'review', 'rereview'}:
            if record['resolutions']:
                raise TaskError('Unfiltered review failure')
            value['findings'] = record['findings']
            parse_verdict(json.dumps(value), record['pass_id'], routing=True)
            findings = record['findings']
            if record['state'] == 'failed':
                if (not can_redact or index == 0 or index != len(state['records']) - 1
                        or state['active_pass'] is not None or state['next_phase'] is not None):
                    raise TaskError('Unfiltered review failure')
                failed_review = True
                # Even structurally valid failed results may contain exception
                # output in every prose field. Keep only the verified outcome.
                record = dict(record, summary='Review failed; failure details withheld.', findings=[], checks=[])
        else:
            if record['findings']:
                raise TaskError('Unexpected implementation findings')
            value['resolutions'] = record['resolutions']
            if 'task_assessment' in record:
                value['task_assessment'] = record['task_assessment']
            parse_implementation(json.dumps(value), record['pass_id'], findings,
                                 assessment_enabled='task_assessment' in record)
        records.append(record)
    if controller_failure or failed_review:
        reason = ('Review failed' if failed_review else 'Controller operation failed')
        reason += '; failure details withheld. ' + failure_guidance
        if state['active_pass'] and state['active_pass']['phase'] in {'review', 'rereview'}:
            reason += (' Review delivery/completion is uncertain; final review below describes only completed records.')
        elif not any(r['phase'] in {'review', 'rereview'} for r in records):
            reason += ' Independent review has not run; no review handoff was claimed.'
        return dict(state, reason=reason, records=records)
    return state


def render_loop_report(state, pause, *, failure_guidance=None):
    from .loop import report
    projected = validate_loop_report(state, failure_guidance=failure_guidance)
    result = report(projected, pause)
    if projected is not state:
        # A redacted boundary has only canonical attention guidance, never a
        # recovery suggestion derived from the failed result's private fields.
        result.data['next_action'] = None
    return result.render()


def unavailable_report(message, task=None, discovery=None):
    """Only canonical, allowlisted diagnostics; never exception/provider text."""
    lines = [message]
    if task:
        lines.extend([f"{task['issue']} — {task['workspace']['path']}",
                      f"Last observed state: {task['state']}; phase: {task['phase'] or 'unknown'}",
                      f"Recorded guidance: {task['guidance']['message']}"])
        if task['guidance']['command']:
            lines.append(task['guidance']['command'])
        for option in task['guidance']['options']:
            lines.extend([f"Conditional guidance: {option['message']}", option['command']])
        lines.extend(e['message'] for e in task['uncertainties'])
    lines.extend(e['message'] for e in (discovery or {}).get('uncertainties', []))
    return dict(kind='diagnostics' if len(lines) > 1 else 'unavailable', message=message,
                text='\n'.join(lines) if len(lines) > 1 else None)


def task_loop_report(selected, *, prior_run_id=None, **kwargs):
    """Read a server-selected execution through canonical identity validation.

    selected must come from the dashboard's status cache, never client paths.
    No registry/checkpoint writes, provider history reads, or loop control calls.
    """
    if prior_run_id is not None and not any(e['run_id'] == prior_run_id and e['report_available']
            for e in selected.get('earlier_iterations', {}).get('entries', [])):
        return unavailable_report('Earlier iteration report unavailable: refresh the retained selection.')
    rendered = dict(prior_run_id=prior_run_id)
    status = _task_status(selected['issue'], report_target=selected, loop_report=rendered, **kwargs).as_dict()
    matches = [task for task in status['tasks'] if report_identity(task) == report_identity(selected)]
    if len(matches) != 1:
        return unavailable_report('Task report unavailable: the selected execution could not be verified.',
                                  discovery=status['discovery'])
    task = matches[0]
    if status['discovery']['state'] != 'available':
        return unavailable_report('Task report unavailable: discovery is unverified; refresh before using recorded guidance.',
                                  task, status['discovery'])
    if prior_run_id is None and task['loop'] and task['loop']['run_id'] != (selected.get('loop') or {}).get('run_id'):
        return unavailable_report('Current iteration changed; refresh before inspecting its report.')
    if 'text' in rendered:
        return dict(kind='report', message=('Earlier iteration report (CLI format). Historical guidance and acceptance do not apply to the current iteration.'
                    if prior_run_id else 'Saved loop report (CLI format). Recorded outcomes do not prove current agent activity.'),
                    text=rendered['text'])
    if prior_run_id:
        return unavailable_report('Earlier iteration report unavailable: retained evidence is missing, partial, or unverified.')
    missing = task['loop'] is None and not task['uncertainties']
    return unavailable_report('No saved loop report exists for this task.' if missing else
                              'Task report unavailable: saved evidence is unreadable, stale, or cannot be verified.', task)


def render_status(value):
    lines = [f"Local task status (v{value['version']}); discovery: {value['discovery']['state']}"]
    for error in value['discovery']['uncertainties']:
        lines.append(error['message'])
    if value['tasks'] == []:
        lines.append('No matching local task evidence.' if value['selection'] == 'issue' else
                     'No verified registered task workspaces.' if value['discovery']['state'] != 'available' else
                     'No registered AWP task workspaces.')
    for task in value['tasks'] or []:
        publication = task['publication']['state']
        if publication == 'complete' and not task['publication']['current_acceptance_published']:
            publication += ' (saved cycle; current acceptance delivery unconfirmed)'
        lines.extend([f"{task['issue']}: {task['state']} ({task['phase'] or 'phase unknown'})",
            f"  Project: {task['project']['linear_project'] or 'unverified'}; repository: {task['repository']['path']}",
            f"  Checkout: {task['workspace']['path']} [{task['workspace']['branch'] or '?'}]; Herdr: {task['workspace']['registration']}",
            '  Contexts: ' + ', '.join(c['context_id'] for c in task['contexts'].values() if c),
            f"  Review: {task['review']['state']}; acceptance: {task['review']['acceptance']}; publication: {publication}",
            '  ' + task['guidance']['message']])
        if task['guidance']['command']:
            lines.append('  Suggested command: ' + task['guidance']['command'])
        for option in task['guidance']['options']:
            lines.append(f"  Conditional option: {option['command']}. {option['message']}")
        lines.extend('  Uncertain: ' + e['message'] for e in task['uncertainties'])
        if value['selection'] == 'issue':
            if task['loop']:
                lines.append(f"  Current iteration: {task['loop']['run_id']} — {task['loop']['passes']} passes / {task['loop']['reviews']} reviews")
            earlier = task['earlier_iterations']
            lines.append('  Earlier iterations: ' + earlier['message'])
            for entry in earlier['entries']:
                lines.append(f"    {entry['run_id'] or 'unavailable'}: {entry['state']} — "
                             f"{entry['passes']} passes / {entry['reviews']} reviews; "
                             f"{entry['implementation_context']} / {entry['reviewer_context']}; evidence: {entry['evidence']}")
            lines.append('  Saved history (ordered within each source; missing timestamps remain unknown):')
            lines.extend(f"    {e['source']}: {e['phase'] or 'boundary'} / {e['state']}"
                         + (f" [{e['context_id']}]" if e['context_id'] else '') for e in task['history']['entries'])
    return '\n'.join(''.join(c if c.isprintable() else '?' for c in line) for line in lines)

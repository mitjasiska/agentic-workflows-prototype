"""Synthetic service boundaries for installed CLI output tests (loaded as sitecustomize)."""

from pathlib import Path
from types import SimpleNamespace

from task_start import cli, dashboard_server
from task_start.cleanup import cleanup_recovery
from task_start.config import IssueStructureConfig
from task_start.preparation import check_issue_structure
from task_start.publication_rebase import review_required
from task_start.publish import prepare_metadata
from task_start.status import StatusReport, empty_task, guidance


def status_report(identifier=None):
    tasks = []
    for number, state in enumerate(('paused', 'interrupted', 'escalated', 'aborted'), 16):
        task = empty_task(f'DEV-{number}', '/fixture/task review/repo', '/fixture/task loop/worktree', [], None)
        task.update(state=state, controller='unowned', loop=dict(active_pass=dict(phase='implementation')))
        task['guidance'] = guidance(task, observable=True)
        tasks.append(task)
    return StatusReport(dict(version=1, selection='all', issue=None,
        discovery=dict(state='available', registered_workspaces=len(tasks), uncertainties=[]),
        projects=[], unverified_project_tasks=list(range(len(tasks))), tasks=tasks))


def publication_refusal(identifier):
    # Use the real publication validation and error, without touching Git or APIs.
    return prepare_metadata(SimpleNamespace(identifier=identifier), {})


def start_warning(identifier, **kwargs):
    check_issue_structure(SimpleNamespace(identifier=identifier, description=''),
                          IssueStructureConfig(block_name='task review is literal user text'))
    return 'task review is literal user text'


def interrupted_loop(identifier, **kwargs):
    raise KeyboardInterrupt()


cli.task_status = dashboard_server.task_status = status_report
cli.publish = publication_refusal
cli.cleanup = lambda identifier, **kwargs: cleanup_recovery(identifier, Path('/fixture/task review/repo'))
cli.start = start_warning
cli.loop = interrupted_loop
dashboard_server.task_loop_report = lambda selected: dict(kind='report', message='Fixture', text=review_required())

"""Explicit, actor-scoped browser preparation at the workflow handoff boundary."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shlex
from shutil import which
import subprocess
from subprocess import run as run_browser
import sys

from . import TaskError


def prepare_browser(execution, context, registry):
    """Prepare only opted-in actors, after allocation and before prompt delivery.

    Context IDs are durable across turns and pane recreation. A caller launching
    subagents must allocate a distinct context/actor before invoking this seam.
    """
    retained = registry.browser_enabled(context['context_id']) is True
    enabled = execution.policy.get('browser', False) or retained
    if not enabled:
        return execution
    context_id = context['context_id']
    if (execution.context_id not in {None, context_id}
            or context['agent'] != execution.options.kind
            or Path(context['worktree']).resolve() != execution.workspace.path.resolve()):
        raise TaskError('Browser handoff context/agent/worktree mismatch; no browser was prepared')
    # Persist capability/transport before delivery, including failed preparation.
    directory = (registry.browser_runtime(context_id) if retained else
                 os.environ.get('AWL_BROWSER_RUNTIME_DIR') or
                 str(Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}') / 'agent-browser/awl'))
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise TaskError('Browser runtime requires the original absolute directory; inspect this actor assignment')
    directory = str(Path(directory))
    try:
        if Path(directory).resolve() != Path(directory):
            raise TaskError('Browser runtime must be a physical directory; resolve symlinks before opting in')
    except (OSError, RuntimeError):
        raise TaskError('Cannot resolve browser runtime directory; correct it before opting in') from None
    root = str(execution.workspace.path.resolve())
    digest = hashlib.sha256(b'\0'.join(os.fsencode(x) for x in (root, execution.options.kind, context_id)) + b'\0').hexdigest()[:32]
    session = f'awl-{execution.options.kind}-{digest}'
    if len(os.fsencode(Path(directory) / (session + '.sock'))) >= 108:
        raise TaskError('Browser runtime socket path is too long; choose a shorter private directory before opting in')
    if not retained:
        registry.enable_browser(context_id, directory)
    helper = which('ab-worktree')
    if not helper:
        raise TaskError(f'Browser {context_id} opt-in requires ab-worktree on controller PATH; run python3.12 scripts/install_browser.py from an AWL checkout')
    command = [helper, execution.options.kind, '--instance', context_id, '--actor', context_id]
    try:
        result = run_browser([*command, '--prepare'], cwd=execution.workspace.path,
                                capture_output=True, text=True, timeout=75,
                                env=dict(os.environ, AWL_BROWSER_RUNTIME_DIR=directory))
    except (OSError, subprocess.SubprocessError) as error:
        raise TaskError(f'Browser preparation failed for {context_id}; on the host in {execution.workspace.path}, run '
                        f'{shlex.join(["env", f"AWL_BROWSER_RUNTIME_DIR={directory}", *command, "--prepare"])} ({type(error).__name__})') from None
    if result.returncode:
        raise TaskError(f'Browser preparation failed for {context_id}; no agent handoff sent. {result.stderr.strip()}')
    try:
        prepared = json.loads(result.stdout)
        expected = dict(root=root, agent=execution.options.kind, instance=context_id, actor=context_id,
                        runtime_dir=directory, session=session)
        if any(prepared[k] != v for k, v in expected.items()):
            raise ValueError('identity mismatch')
    except (ValueError, KeyError, TypeError):
        raise TaskError(f'Browser preparation returned an unverified identity for {context_id}; update the managed helper and retry on the host') from None
    assignments = dict(AWL_BROWSER_INSTANCE=context_id, AWL_BROWSER_ACTOR=context_id,
                       AWL_BROWSER_AGENT=execution.options.kind, AWL_BROWSER_ROOT=expected['root'],
                       AWL_BROWSER_RUNTIME_DIR=directory, AWL_BROWSER_CONNECT_ONLY='1', AWL_BROWSER_CONCURRENT='1')
    connect = shlex.join(['env', *(f'{k}={v}' for k, v in assignments.items()), *command, '--connect-only'])
    print(f'Browser prepared: actor={context_id}, instance={context_id}, session={prepared.get("session")}', file=sys.stderr)
    handoff = execution.handoff + f'''

BROWSER CAPABILITY (explicit host preparation)
Your actor and browser instance are both {context_id}. The controller prepared this exact
session outside your sandbox. Use this exact prefix from the task checkout on EVERY call:
{connect} COMMAND [ARGS...]
Start with snapshot -i to verify actual agent-side access. This is connect-only: never
invent an ID, use the default session, call raw agent-browser, or launch Chrome inside
your sandbox. If the session/socket is missing or inaccessible, report the helper's
host recovery instruction and block browser-dependent acceptance. Host preflight alone
is not a passed browser acceptance check. Prefer snapshots, DOM queries, console and
network diagnostics; screenshots only for requested visual/layout checks.
Each internally spawned subagent requires a DIFFERENT stable actor/instance from its
parent and separate host preparation through ab-worktree --prepare. Do not pass your
own browser assignment to subagents. If your framework cannot arrange unique IDs and
host preparation, report that limitation and block their browser use. Close only your
assigned instance with the same connect-only prefix when browser work is finished.
'''
    return replace(execution, handoff=handoff, context_id=context_id,
                   policy=dict(execution.policy, browser=True))

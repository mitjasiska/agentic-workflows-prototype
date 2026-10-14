"""Controlled host preparation, leaving real workflow allocation/delivery intact."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch


def install(test, *, fail=False, mismatch=False):
    calls = []
    def run(command, **kwargs):
        actor = command[command.index('--instance') + 1]
        test.assertEqual(command[command.index('--actor') + 1], actor)
        test.assertEqual(command[-1], '--prepare')
        root, agent = str(kwargs['cwd'].resolve()), command[1]
        calls.append((actor, agent, root))
        digest = hashlib.sha256(b'\0'.join(v.encode() for v in (root, agent, actor)) + b'\0').hexdigest()[:32]
        result = dict(root=root, agent=agent, actor=actor, instance='wrong' if mismatch else actor,
                      session=f'awl-{agent}-{digest}', runtime_dir=kwargs['env']['AWL_BROWSER_RUNTIME_DIR'])
        return SimpleNamespace(returncode=1 if fail else 0, stdout=json.dumps(result), stderr='Host recovery: exact instance unavailable')
    test.enterContext(patch('task_start.browser.which', return_value='/managed/ab-worktree'))
    test.enterContext(patch('task_start.browser.run_browser', side_effect=run))
    return calls

"""Socket-level regression tests; the daemon is fake, never live Chrome evidence."""

import importlib.util
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_browser import BrowserFixture, HELPER, INSTALLER, ROOT

spec = importlib.util.spec_from_file_location('browser_session', ROOT / 'scripts/ab_worktree_session.py')
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)

# Independent state per process, real private Unix sockets and peer credentials.
DAEMON = r'''
import json, os, socket, sys
from pathlib import Path
sock = Path(sys.argv[1])
state = dict(url='about:blank', cookies={}, local={}, session={}, tabs=['about:blank'])
tab_ids = ['t1']
active_tab = 't1'
next_tab = 2
with socket.socket(socket.AF_UNIX) as server:
    server.bind(str(sock))
    server.listen()
    while True:
        with server.accept()[0] as connection:
            raw = connection.makefile('rb').readline()
            if not raw: continue
            request = json.loads(raw)
            action = request['action']
            with sock.with_suffix('.requests').open('a') as log:
                log.write(json.dumps(request) + '\n')
            control_path = sock.with_suffix('.test-control.json')
            control = json.loads(control_path.read_text()) if control_path.exists() else {}
            data = dict(state)
            if action == 'stream_status':
                data = dict(enabled=False, port=None, connected=control.get('connected', True), screencasting=False)
            elif action == 'navigate':
                state['url'] = request['url']
                state['tabs'][tab_ids.index(active_tab)] = request['url']
            elif action == 'snapshot': data = dict(snapshot=json.dumps(state))
            elif action == 'url': data = dict(url=state['url'])
            elif action == 'cookies_set':
                for cookie in request['cookies']: state['cookies'][cookie['name']] = cookie['value']
            elif action == 'cookies_get': data = dict(cookies=state['cookies'])
            elif action == 'cookies_clear': state['cookies'] = {}
            elif action == 'storage_set': state[request['type']][request['key']] = request['value']
            elif action == 'storage_get': data = dict(storage=state[request['type']])
            elif action == 'storage_clear': state[request['type']] = {}
            elif action == 'tab_new':
                active_tab = 't' + str(next_tab)
                next_tab += 1
                tab_ids.append(active_tab)
                state['url'] = request.get('url', 'about:blank')
                state['tabs'].append(state['url'])
            elif action == 'tab_switch':
                active_tab = request['tabId'].lower()
                state['url'] = state['tabs'][tab_ids.index(active_tab)]
            elif action == 'tab_close':
                target = request.get('tabId', active_tab).lower()
                state['tabs'].pop(tab_ids.index(target))
                tab_ids.remove(target)
                if target == active_tab: active_tab = tab_ids[0]
                state['url'] = state['tabs'][tab_ids.index(active_tab)]
            elif action == 'tab_list':
                data = dict(tabs=[dict(tabId=key, url=url, active=key == active_tab) for key, url in zip(tab_ids, state['tabs'])])
            elif action == 'close': data = dict(closed=True)
            data.update(control.get('responses', {}).get(action, {}))
            response = dict(id=request['id'], success=True, data=data)
            connection.sendall(json.dumps(response).encode() + b'\n')
            if action == 'close': break
sock.unlink()
'''


class PreparedBrowserTests(BrowserFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # Some agent sandboxes allow connecting to host sockets but prohibit
        # bind(). Keep this real-socket suite available on a permitted host;
        # in-process regressions below still run in those restricted agents.
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.bind(str(self.base / 'probe.sock'))
            except OSError as error:
                if error.errno not in {errno.EPERM, errno.EACCES}:
                    raise
                self.skipTest(f'Sandbox denies Unix socket bind: {error}')
        self.runtime = self.base / 'rt'
        self.env['AWL_BROWSER_RUNTIME_DIR'] = str(self.runtime)
        self.daemon = self.base / 'daemon.py'
        self.daemon.write_text(DAEMON)
        self.bin.joinpath('agent-browser').write_text(f'''#!{sys.executable}
import json, os, subprocess, sys, time
from pathlib import Path
if sys.argv[1:] == ['--version']:
    print('agent-browser 0.39.0'); sys.exit(0)
with open(os.environ['BROWSER_TEST_LOG'], 'a') as out:
    out.write(json.dumps(dict(args=sys.argv[1:], env={{k:v for k,v in os.environ.items() if k.startswith('AGENT_BROWSER_')}})) + '\\n')
if os.environ.get('BROWSER_TEST_EXIT'): sys.exit(int(os.environ['BROWSER_TEST_EXIT']))
session = sys.argv[sys.argv.index('--session')+1]
sock = Path(os.environ['AGENT_BROWSER_SOCKET_DIR']) / (session + '.sock')
sock.with_suffix('.config').write_text('native config fingerprint')
p = subprocess.Popen([{sys.executable!r}, {str(self.daemon)!r}, str(sock)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
Path(str(sock) + '.testpid').write_text(str(p.pid))
for _ in range(100):
    if sock.exists(): sys.exit(1 if os.environ.get('BROWSER_TEST_FAIL_AFTER_START') else 0)
    time.sleep(.01)
sys.exit(1)
''')
        self.addCleanup(self.cleanup_daemons)

    def cleanup_daemons(self):
        for path in self.runtime.glob('*.testpid'):
            try:
                os.kill(int(path.read_text()), 15)
            except ProcessLookupError:
                pass

    def prepare(self, actor='main', agent='codex', instance=None, env=None, cwd=None):
        result = self.call(agent, '--instance', instance or actor, '--actor', actor, '--prepare', env=env, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def connect(self, actor, *args, agent='codex', instance=None, env=None, cwd=None):
        result = self.call(agent, '--instance', instance or actor, '--actor', actor, '--connect-only', *args, env=env, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def daemon_control(self, prepared, **values):
        (self.runtime / (prepared['session'] + '.test-control.json')).write_text(json.dumps(values))

    def daemon_requests(self, prepared):
        return [json.loads(line) for line in (self.runtime / (prepared['session'] + '.requests')).read_text().splitlines()]

    def test_main_and_two_same_type_subagents_have_independent_state_and_close(self):
        actors = ['main', 'main-sub1', 'main-sub2']
        prepared = [self.prepare(actor) for actor in actors]
        self.assertEqual(len({p['session'] for p in prepared}), 3)
        for actor in actors:
            self.connect(actor, 'open', 'http://localhost/' + actor)
            self.connect(actor, 'cookies', 'set', 'actor', actor)
            self.connect(actor, 'storage', 'local', 'set', 'actor', actor)
            self.connect(actor, 'storage', 'session', 'set', 'actor', actor)
            self.connect(actor, 'tab', 'new', 'http://localhost/tab/' + actor)
            self.connect(actor, 'tab', 't1')
        for actor in actors:
            value = self.connect(actor, 'snapshot')
            self.assertEqual(value['url'], 'http://localhost/' + actor)
            self.assertEqual(value['cookies'], dict(actor=actor))
            self.assertEqual(value['local'], dict(actor=actor))
            self.assertEqual(value['session'], dict(actor=actor))
            self.assertEqual(value['tabs'], ['http://localhost/' + actor, 'http://localhost/tab/' + actor])
        self.connect('main-sub1', 'close')
        self.assertEqual(self.connect('main', 'get', 'url')['url'], 'http://localhost/main')
        self.assertEqual(self.connect('main-sub2', 'cookies')['cookies'], dict(actor='main-sub2'))
        result = self.call('codex', '--instance', 'main-sub1', '--actor', 'main-sub1', '--connect-only', 'snapshot')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.records()), 3)  # Closing/missing session never launches.

    def test_prepare_reuses_exact_actor_without_resetting_state(self):
        first = self.prepare()
        self.connect('main', 'open', 'http://localhost/retained')
        second = self.prepare()
        self.assertEqual(first, second)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.connect('main', 'get', 'url')['url'], 'http://localhost/retained')

    def test_custom_pi_worktree_and_same_actor_are_separated(self):
        identities = [self.prepare(agent=agent) for agent in ('codex', 'pi', 'custom-agent')]
        identities.append(self.prepare(cwd=self.worktree))
        self.assertEqual(len({v['session'] for v in identities}), 4)
        self.connect('main', 'open', 'http://localhost/custom', agent='custom-agent')
        self.assertEqual(self.connect('main', 'get', 'url')['url'], 'about:blank')

    def test_concurrent_preparations_launch_once(self):
        command = ['/bin/sh', str(HELPER), 'codex', '--instance', 'main', '--actor', 'main', '--prepare']
        children = [subprocess.Popen(command, cwd=self.repo, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        results = [child.communicate(timeout=15) for child in children]
        self.assertEqual([p.returncode for p in children], [0, 0, 0], results)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len({out for out, _ in results}), 1)

    def test_duplicate_instance_for_different_actors_is_refused(self):
        self.prepare('main', instance='fixed')
        self.connect('main', 'open', 'http://localhost/retained', instance='fixed')
        assignments = {p: p.read_bytes() for p in self.runtime.glob('actor-*.json')}
        result = self.call('codex', '--instance', 'fixed', '--actor', 'sub', '--prepare')
        self.assertEqual(result.returncode, 2)
        self.assertIn('different actor', result.stderr)
        self.assertEqual({p: p.read_bytes() for p in self.runtime.glob('actor-*.json')}, assignments)
        result = self.call('codex', '--instance', 'fixed', '--actor', 'sub', '--connect-only', 'snapshot')
        self.assertEqual(result.returncode, 2)
        self.assertIn('no host-prepared instance assignment', result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.prepare('sub', instance='unique')
        self.assertEqual(self.connect('sub', 'get', 'url', instance='unique')['url'], 'about:blank')
        self.assertEqual(self.connect('main', 'get', 'url', instance='fixed')['url'], 'http://localhost/retained')
        self.assertEqual(len(self.records()), 2)

    def test_competing_actors_reserve_only_the_owned_instance(self):
        command = ['/bin/sh', str(HELPER), 'codex', '--instance', 'shared', '--actor']
        children = [subprocess.Popen([*command, actor, '--prepare'], cwd=self.repo, env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for actor in ('main', 'sub')]
        results = [child.communicate(timeout=15) for child in children]
        self.assertEqual(sorted(p.returncode for p in children), [0, 2], results)
        winner, loser = ('main', 'sub') if children[0].returncode == 0 else ('sub', 'main')
        self.assertEqual(len(list(self.runtime.glob('actor-*.json'))), 1)
        self.assertEqual(len(self.records()), 1)
        self.prepare(loser, instance='unique')
        self.prepare(winner, instance='shared')
        self.assertEqual(len(self.records()), 2)

    def test_stable_tab_references_survive_close_without_affecting_other_actors(self):
        self.prepare('main')
        self.prepare('sub')
        self.connect('main', 'open', 'http://localhost/first')
        self.connect('main', 'tab', 'new', 'http://localhost/second')
        self.connect('main', 'tab', 'new', 'http://localhost/third')
        tabs = self.connect('main', 'tab', 'list')['tabs']
        self.assertEqual([tab['tabId'] for tab in tabs], ['t1', 't2', 't3'])
        self.connect('main', 'tab', tabs[0]['tabId'])
        self.assertEqual(self.connect('main', 'get', 'url')['url'], 'http://localhost/first')
        self.connect('main', 'tab', 'close', 't2')
        self.assertEqual([tab['tabId'] for tab in self.connect('main', 'tab')['tabs']], ['t1', 't3'])
        self.connect('main', 'tab', 't3')
        self.assertEqual(self.connect('main', 'get', 'url')['url'], 'http://localhost/third')
        self.connect('main', 'tab', 'close')
        self.assertEqual(self.connect('main', 'get', 'url')['url'], 'http://localhost/first')
        self.assertEqual(self.connect('sub', 'get', 'url')['url'], 'about:blank')

    def test_browser_loss_with_live_daemon_blocks_commands_until_host_recovery(self):
        value = self.prepare()
        self.prepare('other')
        self.connect('other', 'open', 'http://localhost/retained')
        self.daemon_control(value, connected=False)
        before = len(self.daemon_requests(value))
        for args in [('snapshot',), ('open', 'http://localhost/should-not-navigate')]:
            result = self.call('codex', '--instance', 'main', '--actor', 'main', '--connect-only', *args)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(result.stdout, '')
            self.assertIn('browser is unavailable', result.stderr)
            self.assertIn('--instance main --actor main --prepare', result.stderr)
        self.assertEqual([r['action'] for r in self.daemon_requests(value)[before:]], ['stream_status', 'stream_status'])
        self.assertTrue((self.runtime / (value['session'] + '.sock')).exists())
        self.assertEqual(self.connect('other', 'get', 'url')['url'], 'http://localhost/retained')
        self.daemon_control(value, connected=True)
        self.prepare()
        self.connect('main', 'snapshot')
        self.assertEqual([r['action'] for r in self.daemon_requests(value)[-3:]], ['snapshot', 'stream_status', 'snapshot'])
        self.assertEqual(len(self.records()), 2)

    def test_liveness_probe_requires_an_explicit_boolean_connection(self):
        value = self.prepare()
        for connected in (None, 'true', 1, {}):
            with self.subTest(connected=connected):
                self.daemon_control(value, connected=connected)
                result = self.call('codex', '--instance', 'main', '--actor', 'main', '--connect-only', 'snapshot')
                self.assertEqual(result.returncode, 2)
                self.assertIn('browser is unavailable', result.stderr)
        self.assertEqual([r['action'] for r in self.daemon_requests(value)[1:]], ['stream_status'] * 4)

    def test_unexpected_launch_evidence_is_rejected_before_snapshot_output(self):
        value = self.prepare()
        for action in ('snapshot', 'navigate', 'stream_status'):
            for flag in ('launched', 'relaunchedBrowser', 'restartedBackground'):
                for nested in (False, True):
                    with self.subTest(action=action, flag=flag, nested=nested):
                        evidence = {flag: True}
                        if nested: evidence = dict(lifecycle=evidence)
                        self.daemon_control(value, responses={action: evidence})
                        args = ['open', 'http://localhost/race'] if action == 'navigate' else ['snapshot']
                        before = len(self.daemon_requests(value))
                        result = self.call('codex', '--instance', 'main', '--actor', 'main', '--connect-only', *args)
                        self.assertEqual(result.returncode, 2, result.stdout)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('Unexpected browser launch or restart', result.stderr)
                        self.assertIn('--instance main --actor main --prepare', result.stderr)
                        actions = [r['action'] for r in self.daemon_requests(value)[before:]]
                        self.assertEqual(actions, ['stream_status'] if action == 'stream_status' else ['stream_status', action])
        self.assertEqual(len(self.records()), 1)

    def test_browser_loss_still_allows_closing_only_the_owned_daemon(self):
        value = self.prepare()
        self.prepare('other')
        self.daemon_control(value, connected=False)
        self.assertTrue(self.connect('main', 'close')['closed'])
        self.assertEqual([r['action'] for r in self.daemon_requests(value)], ['snapshot', 'close'])
        self.connect('other', 'snapshot')
        self.assertEqual(len(self.records()), 2)

    def test_actor_cannot_create_second_instance_under_another_id(self):
        self.prepare('main', instance='original')
        result = self.call('codex', '--instance', 'invented', '--actor', 'main', '--prepare')
        self.assertEqual(result.returncode, 2)
        self.assertIn('already assigned to another instance', result.stderr)
        self.assertIn('--instance original --actor main --prepare', result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.connect('main', 'get', 'url', instance='original')['url'], 'about:blank')

    def test_mismatched_assigned_instance_agent_root_actor_never_launches(self):
        self.prepare('main')
        for variable, value in [('AWL_BROWSER_INSTANCE', 'other'), ('AWL_BROWSER_ACTOR', 'other'),
                                ('AWL_BROWSER_AGENT', 'pi'), ('AWL_BROWSER_ROOT', str(self.worktree))]:
            with self.subTest(variable=variable):
                result = self.call('codex', '--instance', 'main', '--actor', 'main', '--connect-only', 'snapshot', env=dict(self.env, **{variable:value}))
                self.assertEqual(result.returncode, 2)
                self.assertIn('mismatch', result.stderr)
        self.assertEqual(len(self.records()), 1)

    def test_known_concurrency_missing_identity_and_unprepared_session_refused(self):
        for args in [('snapshot',), ('--instance', 'one', 'snapshot')]:
            result = self.call('codex', *args, env=dict(self.env, AWL_BROWSER_CONCURRENT='1'))
            self.assertEqual(result.returncode, 2)
            self.assertIn('concurrent', result.stderr)
        for args in [('--connect-only', 'snapshot'), ('--instance', 'missing', '--actor', 'missing', '--connect-only', 'snapshot')]:
            result = self.call('codex', *args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Host recovery', result.stderr)
        self.assertEqual(self.records(), [])

    def test_assignment_implies_connect_only_and_forbids_prepare(self):
        self.prepare()
        env = dict(self.env, AWL_BROWSER_INSTANCE='main', AWL_BROWSER_ACTOR='main', AWL_BROWSER_CONNECT_ONLY='1')
        result = self.call('codex', 'snapshot', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.call('codex', '--prepare', env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn('forbidden', result.stderr)
        self.assertEqual(len(self.records()), 1)

    def test_missing_agent_cli_does_not_block_connect_only(self):
        self.prepare()
        (self.bin / 'agent-browser').unlink()
        self.assertIn('url', self.connect('main', 'get', 'url'))

    def test_connect_only_can_read_readonly_metadata_and_lock(self):
        value = self.prepare()
        for path in [*self.runtime.glob('*.json'), *self.runtime.glob('*.lock')]:
            path.chmod(0o400)
        self.assertIn('url', self.connect('main', 'get', 'url'))

    def test_unprepared_owned_daemon_blocks_actions_but_allows_safe_close(self):
        value = self.prepare()
        record = self.runtime / (value['session'] + '.awl.json')
        data = json.loads(record.read_text())
        data['ready'] = False
        record.write_text(json.dumps(data))
        result = self.call('codex', '--instance', 'main', '--actor', 'main', '--connect-only', 'snapshot')
        self.assertEqual(result.returncode, 2)
        self.assertIn('not passed host preflight', result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertTrue(self.connect('main', 'close')['closed'])

    def test_host_can_finish_preflight_of_same_owned_daemon(self):
        value = self.prepare()
        record = self.runtime / (value['session'] + '.awl.json')
        data = json.loads(record.read_text())
        data['ready'] = False
        record.write_text(json.dumps(data))
        self.assertEqual(self.prepare(), value)
        self.assertTrue(json.loads(record.read_text())['ready'])
        self.assertEqual(len(self.records()), 1)

    def test_host_restarts_closed_actor_without_resetting_others(self):
        self.prepare('main')
        other = self.prepare('other')
        self.connect('other', 'open', 'http://localhost/retained')
        self.connect('main', 'close')
        for _ in range(50):
            if not list(self.runtime.glob('*.sock')) or len(list(self.runtime.glob('*.sock'))) == 1: break
            time.sleep(.01)
        self.prepare('main')
        self.assertEqual(self.connect('main', 'get', 'url')['url'], 'about:blank')
        self.assertEqual(self.connect('other', 'get', 'url')['url'], 'http://localhost/retained')
        self.assertEqual(len(self.records()), 3)

    def test_stale_or_changed_daemon_is_refused_not_replaced(self):
        value = self.prepare()
        record = self.runtime / (value['session'] + '.awl.json')
        data = json.loads(record.read_text())
        data['peer']['started'] = 'wrong'
        record.write_text(json.dumps(data))
        for mode in ('--prepare', '--connect-only'):
            args = ['snapshot'] if mode == '--connect-only' else []
            result = self.call('codex', '--instance', 'main', '--actor', 'main', mode, *args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('identity changed', result.stderr)
        self.assertEqual(len(self.records()), 1)

    def test_unowned_socket_is_not_adopted_or_closed(self):
        value = self.prepare()
        record = self.runtime / (value['session'] + '.awl.json')
        record.unlink()
        result = self.call('codex', '--instance', 'main', '--actor', 'main', '--prepare')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Unowned', result.stderr)
        self.assertTrue((self.runtime / (value['session'] + '.sock')).exists())
        self.assertEqual(len(self.records()), 1)

    def test_user_browser_settings_cannot_redirect_host_session(self):
        inherited = dict(self.env, AGENT_BROWSER_CDP='9999', AGENT_BROWSER_PROFILE='/user/profile',
                         AGENT_BROWSER_NAMESPACE='other', AGENT_BROWSER_ARGS='--no-sandbox',
                         AGENT_BROWSER_SESSION='default', AGENT_BROWSER_PROVIDER='remote')
        self.prepare(env=inherited)
        values = self.records()[0]['env']
        self.assertEqual(set(values), {'AGENT_BROWSER_SOCKET_DIR', 'AGENT_BROWSER_EXECUTABLE_PATH'})
        self.assertIn('--config', self.records()[0]['args'])

    def test_startup_failure_no_fallback_or_unsafe_suggestion(self):
        result = self.call('codex', '--instance', 'main', '--actor', 'main', '--prepare', env=dict(self.env, BROWSER_TEST_EXIT='37'))
        self.assertEqual(result.returncode, 2)
        self.assertIn('Host Chrome startup failed', result.stderr)
        self.assertIn('--prepare', result.stderr)
        self.assertNotIn('--no-sandbox', result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(list(self.runtime.glob('*.awl.json')), [])

    def test_failed_browser_start_keeps_daemon_ownership_but_blocks_agent_work(self):
        result = self.call('codex', '--instance', 'main', '--actor', 'main', '--prepare',
                           env=dict(self.env, BROWSER_TEST_FAIL_AFTER_START='1'))
        self.assertEqual(result.returncode, 2)
        record = next(self.runtime.glob('*.awl.json'))
        self.assertFalse(json.loads(record.read_text())['ready'])
        result = self.call('codex', '--instance', 'main', '--actor', 'main', '--connect-only', 'snapshot')
        self.assertEqual(result.returncode, 2)
        self.assertIn('not passed host preflight', result.stderr)
        self.prepare()
        self.assertTrue(json.loads(record.read_text())['ready'])
        self.assertEqual(len(self.records()), 1)

    def test_unsafe_runtime_and_ownership_links_are_refused(self):
        self.runtime.mkdir(mode=0o755)
        result = self.call('codex', '--instance', 'main', '--actor', 'main', '--prepare')
        self.assertEqual(result.returncode, 2)
        self.assertIn('mode 700', result.stderr)
        self.runtime.chmod(0o700)
        value = self.prepare()
        record = self.runtime / (value['session'] + '.awl.json')
        elsewhere = self.base / 'elsewhere'
        record.rename(elsewhere)
        record.symlink_to(elsewhere)
        result = self.call('codex', '--instance', 'main', '--actor', 'main', '--connect-only', 'close')
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.runtime / (value['session'] + '.sock')).exists())

    def test_unsupported_unsafe_commands_never_reach_daemon(self):
        self.prepare()
        for args in [('connect', '9222'), ('launch',), ('--cdp', '9222', 'snapshot'),
                     ('--profile', '/user', 'snapshot'), ('session', 'list'), ('install',)]:
            result = self.call('codex', '--instance', 'main', '--actor', 'main', '--connect-only', *args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Unsupported', result.stderr)
        self.assertEqual(len(self.records()), 1)


class PreparedBrowserInProcessTests(unittest.TestCase):
    """Exercise real ownership files/locks and protocol framing without bind()."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='awl-protocol-')
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / 'rt'
        self.root = str(Path(self.temporary.name) / 'checkout')
        self.peers = {}
        self.connected = {}
        self.responses = {}
        self.requests = []
        self.starts = []
        self.probe_error = None
        patches = [patch.dict(os.environ, {'AWL_BROWSER_RUNTIME_DIR': str(self.runtime)}, clear=True),
                   patch.object(transport, 'check_host'),
                   patch.object(transport.shutil, 'which', return_value='/fake/agent-browser'),
                   patch.object(transport.subprocess, 'run', side_effect=self.launch),
                   patch.object(transport, 'connection_to', side_effect=self.connection)]
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)

    def launch(self, args, **kwargs):
        if args[1:] == ['--version']:
            return subprocess.CompletedProcess(args, 0, 'agent-browser 0.39.0\n', '')
        session = args[args.index('--session') + 1]
        self.starts.append(session)
        sock = self.runtime / (session + '.sock')
        sock.touch()
        self.peers[sock] = dict(pid=len(self.starts), started='1', inode=len(self.starts))
        self.connected[sock] = True
        return subprocess.CompletedProcess(args, 0, '', '')

    @contextmanager
    def connection(self, path, expected=None):
        peer = self.peers[path]
        if expected is not None:
            self.assertEqual(expected, peer)
        connection = Mock()

        def send(payload):
            command = json.loads(payload)
            self.requests.append(command)
            action = command['action']
            if action == 'stream_status' and self.probe_error is not None:
                response = dict(id=command['id'], success=False, error=self.probe_error)
            else:
                if action == 'stream_status':
                    data = dict(connected=self.connected[path], enabled=False, port=None)
                elif action == 'close':
                    data = dict(closed=True)
                    self.connected[path] = False
                    path.unlink()
                else:
                    # Model the upstream hazard: ordinary commands can launch
                    # Chrome again while the same daemon remains available.
                    launched = self.connected[path] is not True
                    self.connected[path] = True
                    data = dict(lifecycle=dict(launched=launched, relaunchedBrowser=launched))
                    if action == 'snapshot': data['snapshot'] = 'prepared page'
                data.update(self.responses.get(action, {}))
                response = dict(id=command['id'], success=True, data=data)
            connection.makefile.return_value = io.BytesIO(json.dumps(response).encode() + b'\n')

        connection.sendall.side_effect = send
        yield connection, peer

    def session(self, instance):
        return 'awl-codex-' + hashlib.sha256(
            b'\0'.join(os.fsencode(value) for value in (self.root, 'codex', instance)) + b'\0').hexdigest()[:32]

    def run_helper(self, *args, actor='main', instance=None, prepare=False):
        instance = instance or actor
        session = self.session(instance)
        argv = ['ab-worktree', self.root, 'codex', instance, actor, session, 'prepare' if prepare else 'connect', *args]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(out), redirect_stderr(err):
            status = transport.main()
        return status, out.getvalue(), err.getvalue()

    def prepare(self, actor='main', instance=None):
        status, out, err = self.run_helper(actor=actor, instance=instance, prepare=True)
        self.assertEqual(status, 0, err)
        return self.runtime / (json.loads(out)['session'] + '.sock')

    def test_rejected_instance_does_not_reserve_actor_and_corrected_retry_succeeds(self):
        self.prepare(instance='shared')
        assignments = {p: p.read_bytes() for p in self.runtime.glob('actor-*.json')}
        status, out, err = self.run_helper(actor='sub', instance='shared', prepare=True)
        self.assertEqual(status, 2)
        self.assertEqual(out, '')
        self.assertIn('different actor', err)
        self.assertEqual({p: p.read_bytes() for p in self.runtime.glob('actor-*.json')}, assignments)
        self.prepare('sub', 'unique')
        self.assertEqual(len(self.starts), 2)
        self.assertEqual(self.run_helper('snapshot', actor='sub', instance='unique')[:2], (0, 'prepared page\n'))
        self.assertEqual(self.run_helper('snapshot', instance='shared')[:2], (0, 'prepared page\n'))
        # Successful reservations remain stable, including after host reuse.
        self.prepare('sub', 'unique')
        status, _, err = self.run_helper(actor='sub', instance='invented', prepare=True)
        self.assertEqual(status, 2)
        self.assertIn('--instance unique --actor sub --prepare', err)
        self.assertEqual(len(self.starts), 2)

    def test_unowned_socket_refusal_also_leaves_new_actor_unassigned(self):
        self.runtime.mkdir(mode=0o700)
        unowned = self.runtime / (self.session('unowned') + '.sock')
        unowned.touch()
        status, _, err = self.run_helper(instance='unowned', prepare=True)
        self.assertEqual(status, 2)
        self.assertIn('Unowned', err)
        self.assertEqual(list(self.runtime.glob('actor-*.json')), [])
        self.assertEqual(self.starts, [])
        self.prepare(instance='unique')
        self.assertTrue(unowned.exists())
        self.assertEqual(len(self.starts), 1)

    def test_browser_loss_is_detected_without_sending_auto_launching_actions(self):
        sock = self.prepare()
        self.connected[sock] = False
        before = len(self.requests)
        for args in [('snapshot',), ('open', 'http://localhost/recovery')]:
            status, out, err = self.run_helper(*args)
            self.assertEqual(status, 2)
            self.assertEqual(out, '')
            self.assertIn('browser is unavailable', err)
            self.assertIn('--instance main --actor main --prepare', err)
        self.assertFalse(self.connected[sock])  # No implicit relaunch happened.
        self.assertTrue(sock.exists())  # The same daemon/socket survived.
        self.assertEqual([r['action'] for r in self.requests[before:]], ['stream_status'] * 2)
        # Only the explicit host path can re-establish browser availability.
        self.prepare()
        self.assertTrue(self.connected[sock])
        self.assertEqual(self.run_helper('snapshot')[:2], (0, 'prepared page\n'))
        self.assertEqual(len(self.starts), 1)

    def test_missing_or_invalid_probe_response_never_sends_dependent_action(self):
        sock = self.prepare()
        for connected in (False, None, 1, 'true', {}):
            with self.subTest(connected=connected):
                self.connected[sock] = connected
                before = len(self.requests)
                status, out, err = self.run_helper('snapshot')
                self.assertEqual(status, 2)
                self.assertEqual(out, '')
                self.assertIn('browser is unavailable', err)
                self.assertEqual([r['action'] for r in self.requests[before:]], ['stream_status'])
        self.probe_error = 'Chrome connection was lost'
        status, _, err = self.run_helper('snapshot')
        self.assertEqual(status, 2)
        self.assertIn(self.probe_error, err)
        self.assertIn('Host recovery', err)

    def test_unexpected_relaunch_is_rejected_before_returning_command_data(self):
        self.prepare()
        for action in ('stream_status', 'snapshot', 'navigate'):
            for flag in ('launched', 'relaunchedBrowser', 'restartedBackground'):
                for nested in (False, True):
                    with self.subTest(action=action, flag=flag, nested=nested):
                        evidence = {flag: True}
                        self.responses = {action: dict(lifecycle=evidence) if nested else evidence}
                        before = len(self.requests)
                        args = ('open', 'http://localhost/race') if action == 'navigate' else ('snapshot',)
                        status, out, err = self.run_helper(*args)
                        self.assertEqual(status, 2)
                        self.assertEqual(out, '')
                        self.assertIn('Unexpected browser launch or restart', err)
                        self.assertIn('--instance main --actor main --prepare', err)
                        self.assertEqual([r['action'] for r in self.requests[before:]],
                                         ['stream_status'] if action == 'stream_status' else ['stream_status', action])
        self.assertEqual(len(self.starts), 1)

    def test_close_does_not_require_live_browser_or_affect_other_actor(self):
        sock = self.prepare()
        other = self.prepare('other')
        self.connected[sock] = False
        before = len(self.requests)
        status, out, _ = self.run_helper('close')
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(out)['closed'])
        self.assertEqual([r['action'] for r in self.requests[before:]], ['close'])
        self.assertFalse(sock.exists())
        self.assertTrue(other.exists())
        self.assertEqual(self.run_helper('snapshot', actor='other')[:2], (0, 'prepared page\n'))
        self.assertEqual(len(self.starts), 2)


class BrowserProtocolTests(unittest.TestCase):
    def test_command_mapping_preserves_data_and_rejects_unknowns(self):
        cases = [(['eval', 'document.title'], dict(action='evaluate', script='document.title')),
                 (['fill', '#name', 'a b'], dict(action='fill', selector='#name', value='a b')),
                 (['type', '#name', 'a b'], dict(action='type', selector='#name', text='a b')),
                 (['find', 'role', 'button', 'click', '--name', 'Submit'], dict(action='getbyrole', role='button', subaction='click', name='Submit')),
                 (['snapshot', '-i', '-d', '3', '-s', 'main'], dict(action='snapshot', interactive=True, maxDepth=3, selector='main')),
                 (['network', 'requests'], dict(action='requests', clear=False)),
                 (['find', 'testid', 'submit', 'click'], dict(action='getbytestid', testId='submit', subaction='click')),
                 (['tab', 't2'], dict(action='tab_switch', tabId='t2')),
                 (['tab', 'T1'], dict(action='tab_switch', tabId='T1')),
                 (['tab', 'close', 't1'], dict(action='tab_close', tabId='t1')),
                 (['tab', 'close'], dict(action='tab_close'))]
        for args, expected in cases:
            self.assertEqual(transport.parse_command(args), expected)
        for args in [['snapshot', '--session', 'x'], ['snapshot', '-d', 'bad'], ['launch'], ['connect', '1']]:
            with self.assertRaises(ValueError): transport.parse_command(args)

    def test_tab_ids_follow_the_pinned_positive_u32_protocol(self):
        for tab in ('t1', 't4294967295', 'T2'):
            for operation in ([], ['close']):
                self.assertEqual(transport.parse_command(['tab', *operation, tab])['tabId'], tab)
        for tab in ('1', '0', '-1', 't0', 't4294967296', 't-1', 't1.5', 't１', ''):
            for operation in ([], ['close']):
                with self.subTest(tab=tab, operation=operation), self.assertRaises(ValueError):
                    transport.parse_command(['tab', *operation, tab])

    def test_upstream_unsafe_automatic_sandbox_fallback_is_refused(self):
        with patch.object(transport.os, 'geteuid', return_value=0), self.assertRaisesRegex(ValueError, 'automatic --no-sandbox'):
            transport.check_host()


class GuidedSetupTests(BrowserFixture, unittest.TestCase):
    def install(self, *args, env=None):
        return subprocess.run([sys.executable, str(INSTALLER), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=15)

    def test_missing_browser_requires_consent_and_does_not_write_helpers(self):
        (self.bin / 'agent-browser').unlink()
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn('--install-deps', result.stderr)
        self.assertFalse((Path(self.env['HOME']) / '.local/bin/ab-worktree').exists())

    def test_missing_node_npm_has_actionable_diagnostic(self):
        (self.bin / 'agent-browser').unlink()
        result = self.install('--install-deps')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Node.js 24+', result.stderr)
        self.assertFalse((Path(self.env['HOME']) / '.local/bin/ab-worktree').exists())

    def test_missing_chrome_and_bad_override_do_not_launch_browser(self):
        env = dict(self.env)
        env.pop('AGENT_BROWSER_EXECUTABLE_PATH')
        result = self.install(env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Chrome is missing', result.stderr)
        result = self.install(env=dict(self.env, AGENT_BROWSER_EXECUTABLE_PATH='/missing/chrome'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('not an executable file', result.stderr)
        self.assertEqual(self.records(), [])

    def test_supported_user_dependency_installation_uses_private_prefix_without_sudo(self):
        (self.bin / 'agent-browser').unlink()
        for name, code in {
            'node': "print('v24.1.0')",
            'npm': r'''
import os, sys
from pathlib import Path
prefix = Path(sys.argv[sys.argv.index('--prefix') + 1])
assert sys.argv[1:3] == ['install', '--global']
assert sys.argv[-1] == 'agent-browser@0.39.0'
p = prefix / 'bin/agent-browser'
p.parent.mkdir(parents=True)
p.write_text('#!' + sys.executable + '\n' + "import os, sys\nfrom pathlib import Path\nif sys.argv[1:] == ['--version']: print('agent-browser 0.39.0')\nelif sys.argv[1:] == ['install']:\n p=Path.home()/'.agent-browser/browsers/chrome-test/chrome-linux64/chrome'\n p.parent.mkdir(parents=True)\n p.write_text('#!/bin/sh\\nexit 0\\n')\n p.chmod(0o755)\nelse: raise Exception('unexpected browser launch')\n")
p.chmod(0o755)
'''
        }.items():
            path = self.bin / name
            path.write_text(f'#!{sys.executable}\n' + code + '\n')
            path.chmod(0o755)
        env = dict(self.env)
        env.pop('AGENT_BROWSER_EXECUTABLE_PATH')
        result = self.install('--install-deps', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = Path(self.env['HOME']) / '.local/bin'
        self.assertTrue((installed / 'agent-browser').is_symlink())
        self.assertTrue((installed / 'ab-worktree').is_file())
        result = self.install(env=dict(env, PATH=str(installed) + ':' + self.env['PATH']))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records(), [])

    def test_managed_upgrade_and_check_preserve_personal_bytes(self):
        self.assertEqual(self.install().returncode, 0)
        home = Path(self.env['HOME'])
        helper = home / '.local/bin/ab-worktree'
        manifest = home / '.local/share/awl-browser/install-manifest.json'
        import hashlib
        old = b'#!/bin/sh\n# previous managed release\n'
        helper.write_bytes(old)
        values = json.loads(manifest.read_text())
        values[str(helper)] = hashlib.sha256(old).hexdigest()
        manifest.write_text(json.dumps(values))
        rules = home / '.codex/AGENTS.md'
        rules.write_bytes(b'Personal prefix\n' + rules.read_bytes() + b'\nPersonal suffix')
        before = rules.read_bytes()
        result = self.install('--check')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(helper.read_bytes(), old)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(helper.read_bytes(), HELPER.read_bytes())
        self.assertEqual(rules.read_bytes(), before)
        result = self.install('--check', '--agent-path', str(helper.parent) + ':' + self.env['PATH'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Supplied agent PATH', result.stdout)
        result = self.install('--check', '--agent-path', '/missing')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Agent PATH missing', result.stderr)

    def test_edited_managed_companion_refuses_all_writes(self):
        self.assertEqual(self.install().returncode, 0)
        home = Path(self.env['HOME'])
        companion = home / '.local/bin/ab_worktree_session.py'
        companion.write_text('# personal modification\n')
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Preserving different existing file', result.stderr)
        self.assertEqual(companion.read_text(), '# personal modification\n')

    def test_symlinked_manifest_is_preserved_and_never_used_to_authorize_updates(self):
        self.assertEqual(self.install().returncode, 0)
        home = Path(self.env['HOME'])
        manifest = home / '.local/share/awl-browser/install-manifest.json'
        external = self.base / 'external-manifest'
        manifest.rename(external)
        manifest.symlink_to(external)
        before = external.read_bytes()
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Refusing non-regular file', result.stderr)
        self.assertEqual(external.read_bytes(), before)

    def test_custom_agents_can_skip_native_instruction_writes(self):
        result = self.install('--skip-instructions')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.env['HOME']) / '.codex').exists())
        self.assertFalse((Path(self.env['HOME']) / '.pi').exists())

    def test_version_incompatibility_is_reported_without_replacing_existing_install(self):
        browser = self.bin / 'agent-browser'
        browser.write_text('#!/bin/sh\nprintf "agent-browser 0.1.0\\n"\n')
        before = browser.read_bytes()
        result = self.install('--install-deps')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Existing agent-browser preserved', result.stderr)
        self.assertEqual(browser.read_bytes(), before)

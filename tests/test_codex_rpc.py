"""Exercise the actual stdio framing/lifecycle against a disposable fake server."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from task_start import TaskError
from task_start.codex_rpc import CodexRPC


class CodexRPCTests(unittest.TestCase):
    def server(self, response):
        source = '''
import json, sys, time
request = json.loads(sys.stdin.readline())
assert request['method'] == 'initialize'
assert request['params']['clientInfo']['name'] == 'agentic_workflows'
print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
assert json.loads(sys.stdin.readline()) == {'method': 'initialized'}
request = json.loads(sys.stdin.readline())
''' + response
        real_popen = subprocess.Popen
        processes = []

        def spawn(argv, **kwargs):
            self.assertEqual(argv, ['codex', 'app-server', '--listen', 'stdio://'])
            process = real_popen([sys.executable, '-u', '-c', source], **kwargs)
            processes.append(process)
            return process

        self.processes = processes
        return patch('task_start.codex_rpc.subprocess.Popen', side_effect=spawn)

    def test_handshake_notifications_unicode_and_matching_response(self):
        with tempfile.TemporaryDirectory() as directory, self.server('''
print(json.dumps({'method': 'thread/started', 'params': {}}), flush=True)
print(json.dumps({'id': request['id'], 'result': request['params']}), flush=True)
sys.stdin.read()
'''):
            with CodexRPC(Path(directory)) as rpc:
                params = {'threadId': 'exact-id', 'text': '\nα\r\n$(literal)\n'}
                self.assertEqual(rpc.request('thread/read', params), params)
            self.assertIsNotNone(self.processes[0].poll())

    def test_helper_does_not_inherit_github_tokens_but_keeps_provider_and_ssh_auth(self):
        with patch.dict(os.environ, {"GH_TOKEN": "synthetic-controller", "GITHUB_TOKEN": "synthetic-fallback",
                                     "GH_ENTERPRISE_TOKEN": "synthetic-enterprise", "GITHUB_ENTERPRISE_TOKEN": "synthetic-enterprise",
                                     "OPENAI_API_KEY": "synthetic-provider", "SSH_AUTH_SOCK": "/tmp/test-agent.sock"}, clear=True), \
                self.server("import os; print(json.dumps({'id': request['id'], 'result': dict(os.environ)}), flush=True)"):
            with CodexRPC(Path('/tmp')) as rpc:
                environment = rpc.request('thread/list', {})
            for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
                self.assertNotIn(key, environment)
            self.assertEqual(environment['OPENAI_API_KEY'], 'synthetic-provider')
            self.assertEqual(environment['SSH_AUTH_SOCK'], '/tmp/test-agent.sock')
            self.assertEqual(os.environ['GH_TOKEN'], 'synthetic-controller')

    def test_api_error_is_sanitized(self):
        with self.server("print(json.dumps({'id': request['id'], 'error': {'message': 'secret'}}), flush=True)"):
            with CodexRPC(Path('/tmp')) as rpc, self.assertRaises(TaskError) as raised:
                rpc.request('thread/items/list', {})
        self.assertIn('thread/items/list', str(raised.exception))
        self.assertNotIn('secret', str(raised.exception))

    def test_unexpected_response_id_is_rejected(self):
        with self.server("print(json.dumps({'id': 999, 'result': {}}), flush=True)"):
            with CodexRPC(Path('/tmp')) as rpc, self.assertRaisesRegex(TaskError, 'ID mismatch'):
                rpc.request('thread/read', {})

    def test_malformed_json_and_eof_are_errors(self):
        for response in ["print('invalid JSON', flush=True)", "pass", "print('[]', flush=True)"]:
            with self.subTest(response=response), self.server(response):
                with CodexRPC(Path('/tmp')) as rpc, self.assertRaisesRegex(TaskError, 'malformed'):
                    rpc.request('thread/read', {})

    def test_requests_for_approval_are_never_accepted(self):
        with self.server("print(json.dumps({'id': 9, 'method': 'item/commandExecution/requestApproval'}), flush=True)"):
            with CodexRPC(Path('/tmp')) as rpc, self.assertRaisesRegex(TaskError, 'client action'):
                rpc.request('thread/start', {})

    def test_read_timeout_reaps_only_owned_helper(self):
        with self.server("sys.stdin.read()"):
            with CodexRPC(Path('/tmp')) as rpc, self.assertRaisesRegex(TaskError, 'timed out'):
                rpc.request('thread/read', {}, timeout=0.02)
        self.assertIsNotNone(self.processes[0].poll())

    def test_failed_initialize_closes_helper(self):
        real_popen = subprocess.Popen
        processes = []

        def spawn(*args, **kwargs):
            process = real_popen([sys.executable, '-c', 'pass'], **kwargs)
            processes.append(process)
            return process

        with patch('task_start.codex_rpc.subprocess.Popen', side_effect=spawn), self.assertRaises(TaskError):
            with CodexRPC(Path('/tmp')):
                self.fail('must not initialize')
        self.assertIsNotNone(processes[0].poll())


if __name__ == '__main__':
    unittest.main()

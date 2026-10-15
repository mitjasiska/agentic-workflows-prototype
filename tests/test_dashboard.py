"""Focused offline transport/cache checks; browser review stays developer tooling."""

import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from task_start import TaskError, cli, config
from task_start import dashboard_server as dashboard
from task_start.status import StatusReport, empty_task, guidance, report_identity, task_loop_report, task_status
import test_status as status_fixture
from task_start.workspace import Herdr


ROOT = Path(__file__).resolve().parents[1]
TRUSTED_ORIGIN = 'https://dashboard.example.invalid'
TRUSTED_HOST = 'dashboard.example.invalid'
REPORT_URL = '/api/report?revision=1&task=0'


def report(state='available', *, tasks=None):
    tasks = [] if tasks is None else tasks
    projects = sorted({t['project']['linear_project'] for t in tasks if t['project']['state'] == 'verified'})
    return dict(version=1, selection='all', issue=None,
                discovery=dict(state=state, registered_workspaces=None if state == 'unavailable' else len(tasks),
                               uncertainties=[] if state == 'available' else [dict(code='herdr', message='Discovery is unverified.')]),
                projects=[dict(linear_project=p, tasks=[n for n, t in enumerate(tasks) if t['project']['linear_project'] == p]) for p in projects],
                unverified_project_tasks=[n for n, t in enumerate(tasks) if t['project']['state'] != 'verified'],
                tasks=None if state == 'unavailable' else tasks)


def task(issue='DEV-7', project='Example Project', state='implementation'):
    value = empty_task(issue, '/projects/repository', f'/worktrees/{issue}', [], None)
    value['project'] = dict(state='verified', linear_project=project, base_branch='main', candidates=[])
    value['workspace'].update(registration='registered', branch=issue.lower(), workspace_id=issue)
    value.update(state=state, phase='implementation')
    value['guidance'] = guidance(value)
    return value


class CollectorTests(unittest.TestCase):
    def test_default_reader_is_canonical_all_tasks_api(self):
        with patch.object(dashboard, 'task_status', return_value=StatusReport(report())) as reader:
            collector = dashboard.SnapshotCollector()
            collector.refresh()
            reader.assert_called_once_with()
            self.assertEqual(collector.snapshot()['projection'], report())

    def test_only_projection_changes_invalidate_not_collection_timestamps(self):
        source = report(tasks=[task()])
        collector = dashboard.SnapshotCollector(lambda: StatusReport(source))
        with patch.object(dashboard, 'utc_now', side_effect=['one', 'two', 'three']):
            collector.refresh()
            collector.refresh()
            unchanged = collector.snapshot()
            self.assertEqual(unchanged['revision'], 1)
            self.assertEqual(unchanged['checked_at'], 'two')
            self.assertEqual(unchanged['changed_at'], 'one')
            source['tasks'][0]['state'] = 'clean'
            collector.refresh()
        self.assertEqual(collector.snapshot()['revision'], 2)
        self.assertEqual(collector.snapshot()['changed_at'], 'three')
        unchanged['projection']['tasks'].clear()
        self.assertEqual(len(collector.snapshot()['projection']['tasks']), 1)

    def test_discovery_failure_or_ambiguity_retains_then_confirmed_empty_removes(self):
        source = report(tasks=[task(state='clean'), task('DEV-8', state='published')])
        source['tasks'][1]['publication']['merge'] = 'recorded'
        reader = Mock(return_value=StatusReport(source))
        collector = dashboard.SnapshotCollector(reader)
        collector.refresh()
        for uncertain in [report('unavailable'), report('uncertain'), report('uncertain', tasks=[task()])]:
            reader.return_value = StatusReport(uncertain)
            collector.refresh()
            cached = collector.snapshot()
            self.assertTrue(cached['stale'])
            self.assertEqual(cached['projection'], source)
            self.assertEqual(cached['discovery'], uncertain['discovery'])
        reader.return_value = StatusReport(report())
        collector.refresh()
        self.assertFalse(collector.snapshot()['stale'])
        self.assertEqual(collector.snapshot()['projection']['tasks'], [])
        reader.return_value = StatusReport(report('unavailable'))
        collector.refresh()
        self.assertEqual(collector.snapshot()['projection']['tasks'], [], 'No permanent task archive')

    def test_initial_failure_partial_snapshot_and_exception_do_not_claim_empty(self):
        reader = Mock(return_value=StatusReport(report('unavailable')))
        collector = dashboard.SnapshotCollector(reader)
        collector.refresh()
        self.assertIsNone(collector.snapshot()['projection']['tasks'])
        partial = report('uncertain', tasks=[task()])
        reader.return_value = StatusReport(partial)
        collector.refresh()
        self.assertEqual(collector.snapshot()['projection'], partial)
        reader.side_effect = RuntimeError('PRIVATE secret transcript')
        collector.refresh()
        result = collector.snapshot()
        self.assertTrue(result['stale'])
        self.assertEqual(result['projection'], partial, 'Retain partial evidence when no complete discovery exists yet')
        self.assertIsNotNone(result['error'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        reader.side_effect = None
        reader.return_value = StatusReport(report(tasks=[task()]))
        collector.refresh()
        self.assertFalse(collector.snapshot()['stale'])
        self.assertIsNone(collector.snapshot()['error'])

    def test_task_uncertainty_does_not_remove_healthy_tasks_or_rewrite_guidance(self):
        bad = task('DEV-8', state='uncertain')
        bad['uncertainties'] = [dict(code='evidence', message='Cannot safely decode task evidence.')]
        bad['guidance'] = guidance(bad)
        source = report(tasks=[task(), bad])
        collector = dashboard.SnapshotCollector(lambda: StatusReport(source))
        collector.refresh()
        self.assertEqual(collector.snapshot()['projection'], source)
        self.assertIsNone(collector.snapshot()['projection']['tasks'][1]['guidance']['command'])

    def test_one_periodic_worker_no_overlap_and_shutdown_wakes_waiters(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def reader():
            calls.append(time.monotonic())
            entered.set()
            release.wait(2)
            return StatusReport(report())
        collector = dashboard.SnapshotCollector(reader, interval=.03)
        collector.start()
        try:
            self.assertTrue(entered.wait(1))
            for _ in range(30):
                collector.snapshot()
            self.assertEqual(len(calls), 1)
            release.set()
            self.assertEqual(collector.wait(0, timeout=1), 1)
            waiter = threading.Thread(target=lambda: collector.wait(1, timeout=60))
            waiter.start()
            collector.close()
            waiter.join(1)
            self.assertFalse(waiter.is_alive())
            self.assertFalse(collector.thread.is_alive())
        finally:
            release.set()
            collector.close()


class HTTPTests(unittest.TestCase):
    trusted_origin = None

    def setUp(self):
        self.source = report(tasks=[task()])
        self.reader = Mock(side_effect=lambda: StatusReport(self.source))
        self.collector = dashboard.SnapshotCollector(self.reader)
        self.collector.refresh()
        self.report_reader = Mock(return_value=dict(kind='report', message='Saved report', text='Complete report\nFinal line'))
        self.server = dashboard.DashboardServer(self.collector, port=0, trusted_origin=self.trusted_origin,
                                               report_reader=self.report_reader)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs=dict(poll_interval=.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.collector.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)

    def connect(self):
        connection = http.client.HTTPConnection(dashboard.HOST, self.server.server_port, timeout=2)
        self.addCleanup(connection.close)
        return connection

    def request(self, path, method='GET', headers=None):
        connection = self.connect()
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response, response.read()

    def event(self, response):
        lines = []
        while True:
            line = response.readline()
            self.assertTrue(line, 'SSE unexpectedly closed')
            if line == b'\n':
                return b''.join(lines)
            lines.append(line)

    def test_loopback_snapshot_and_multiple_clients_only_use_cache(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        for _ in range(3):
            response, body = self.request('/api/snapshot')
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(body)['projection'], self.source)
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))
        self.assertEqual(self.reader.call_count, 1)
        self.report_reader.assert_not_called()

    def test_selected_report_is_complete_separate_and_keeps_security_headers(self):
        content = ('Check full: not_run — recorded limitation\n' * 3000) + 'Final review: clean'
        self.report_reader.return_value['text'] = content
        for method in ('GET', 'HEAD'):
            response, body = self.request(REPORT_URL, method)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))
            self.assertIn("script-src 'self'", response.getheader('Content-Security-Policy'))
            if method == 'GET':
                self.assertEqual(json.loads(body), dict(self.report_reader.return_value, revision=1, task=0,
                                                       identity=list(report_identity(self.source['tasks'][0]))))
            else:
                self.assertEqual(body, b'')
        self.report_reader.assert_called_with(self.source['tasks'][0])
        self.assertEqual(self.reader.call_count, 1, 'Report does not alter collector schedule')
        self.assertNotIn(content, json.dumps(self.collector.snapshot()))

    def test_report_selector_cannot_redirect_to_files_or_another_revision(self):
        for query in ('', '?path=/etc/passwd', '?revision=1&task=/etc/passwd', '?revision=1&task=-1',
                      '?revision=1&task=0&path=/etc/passwd', '?revision=1&task=0&task=1',
                      '?revision=1&task=0.0', '?revision=01&task=0', '?revision=1&task=%2e%2e',
                      '?revision=1&task=0&iteration=/etc/passwd', '?revision=1&task=0&iteration=-1',
                      '?revision=1&task=0&iteration=',
                      '?revision=1&task=0&iteration=0&iteration=1', '?revision=1&task=0&iteration=01',
                      '?revision=1&task=9999999999999999999999', '?revision=&task=0'):
            response, _ = self.request('/api/report' + query)
            self.assertEqual(response.status, 400, query)
        for query in ('?revision=0&task=0', '?revision=2&task=0', '?revision=1&task=1'):
            response, _ = self.request('/api/report' + query)
            self.assertEqual(response.status, 409, query)
        self.report_reader.assert_not_called()

    def test_historical_report_selector_is_bound_to_cached_run_and_read_permissions(self):
        selected = self.source['tasks'][0]
        selected['earlier_iterations'] = dict(entries=[dict(run_id='retained-run', report_available=True),
                                                       dict(run_id=None, report_available=False)])
        self.collector.refresh()
        response, body = self.request('/api/report?revision=2&task=0&iteration=0')
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)['prior_run_id'], 'retained-run')
        self.report_reader.assert_called_with(selected, prior_run_id='retained-run')
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))
        for selector in ('1', '2', '9999999999999999'):
            response, _ = self.request('/api/report?revision=2&task=0&iteration=' + selector)
            self.assertEqual(response.status, 409)
        for method in ('POST', 'PUT', 'DELETE'):
            response, _ = self.request('/api/report?revision=2&task=0&iteration=0', method)
            self.assertEqual(response.status, 405)
        response, _ = self.request('/api/report?revision=2&task=0&iteration=0', headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)

    def test_report_stale_discovery_and_read_failure_return_safe_diagnostics(self):
        self.reader.return_value = None
        self.source = report('unavailable')
        self.collector.refresh()
        response, body = self.request('/api/report?revision=2&task=0')
        result = json.loads(body)
        self.assertEqual(response.status, 200)
        self.assertEqual(result['kind'], 'diagnostics')
        self.assertIn('unverified', result['text'])
        self.assertIn('Discovery is unverified.', result['text'])
        self.report_reader.assert_not_called()
        self.source = report(tasks=[task()])
        self.collector.refresh()
        self.report_reader.side_effect = RuntimeError('PRIVATE exception transcript')
        response, body = self.request('/api/report?revision=3&task=0')
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)['kind'], 'diagnostics')
        self.assertNotIn(b'PRIVATE', body)

    def test_report_in_flight_selection_change_or_removal_discards_old_content(self):
        def change(_):
            self.source = report(tasks=[task('DEV-8')])
            self.collector.refresh()
            return dict(kind='report', text='OLD REPORT', message='Old')
        self.report_reader.side_effect = change
        response, body = self.request(REPORT_URL)
        self.assertEqual(response.status, 409)
        self.assertNotIn(b'OLD REPORT', body)
        self.source = report()
        self.collector.refresh()
        response, _ = self.request('/api/report?revision=3&task=0')
        self.assertEqual(response.status, 409)

    def test_report_concurrency_is_bounded(self):
        with patch.object(self.server, 'report_reads') as reads:
            reads.acquire.return_value = False
            response, _ = self.request(REPORT_URL)
            self.assertEqual(response.status, 503)
            self.report_reader.assert_not_called()

    def test_sse_invalidation_reconnect_ignores_missed_events_and_fetches_full_snapshot(self):
        connections = [self.connect(), self.connect()]
        responses = []
        for connection in connections:
            connection.request('GET', '/api/events')
            response = connection.getresponse()
            self.assertIn(b'event: invalidate\ndata: 1', self.event(response))
            responses.append(response)
        self.source = report(tasks=[task(state='paused')])
        self.collector.refresh()
        for response in responses:
            self.assertEqual(self.event(response), b'event: invalidate\ndata: 2\n')
        for connection in connections:
            connection.close()
        self.source = report()
        self.collector.refresh()
        reconnected = self.connect()
        reconnected.request('GET', '/api/events', headers={'Last-Event-ID': '1'})
        self.assertIn(b'data: 3', self.event(reconnected.getresponse()))
        response, body = self.request('/api/snapshot')
        self.assertEqual(json.loads(body)['projection'], self.source)
        self.assertEqual(self.reader.call_count, 3)

    def test_sse_heartbeat_has_no_workflow_payload_and_clients_are_bounded(self):
        original = self.collector.wait
        with patch.object(self.collector, 'wait', side_effect=lambda revision: original(revision, timeout=.02)):
            connection = self.connect()
            connection.request('GET', '/api/events')
            response = connection.getresponse()
            self.event(response)
            self.assertEqual(self.event(response), b': heartbeat\n')
            self.collector.close()
        with patch.object(self.server, 'streams') as streams:
            streams.acquire.return_value = False
            response, _ = self.request('/api/events')
            self.assertEqual(response.status, 503)

    def test_shutdown_closes_active_stream_and_releases_capacity(self):
        connection = self.connect()
        connection.request('GET', '/api/events')
        response = connection.getresponse()
        self.event(response)
        self.collector.close()
        self.assertEqual(response.readline(), b'')
        acquired = 0
        try:
            for _ in range(16):
                self.assertTrue(self.server.streams.acquire(blocking=False))
                acquired += 1
        finally:
            for _ in range(acquired):
                self.server.streams.release()

    def test_read_only_fixed_routes_no_arbitrary_files_or_commands(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            for path in ('/api/snapshot', '/api/events', REPORT_URL, '/task/start'):
                response, _ = self.request(path, method)
                self.assertEqual(response.status, 405)
        for path in ('/../task_start/config.py', '/%2e%2e/config/local.toml', '/config/projects.toml', '/task', '/api/tasks/DEV-7', '/web/', '//example.invalid/secret'):
            response, _ = self.request(path)
            self.assertEqual(response.status, 404, path)
        self.assertEqual(self.reader.call_count, 1)

    def test_cross_origin_and_rebinding_reads_refused(self):
        for headers in ({'Host': 'attacker.invalid'}, {'Origin': 'https://attacker.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
            for path in ('/api/snapshot', REPORT_URL):
                response, _ = self.request(path, headers=headers)
                self.assertEqual(response.status, 403)
        response, _ = self.request('/api/snapshot', headers={'Host': f'localhost:{self.server.server_port}', 'Origin': f'http://localhost:{self.server.server_port}'})
        self.assertEqual(response.status, 200)
        response, _ = self.request('/api/snapshot', headers={'Host': 'localhost:9876', 'Origin': 'http://localhost:9876'})
        self.assertEqual(response.status, 200, 'SSH forwarding may use a different local port')

    def test_extra_host_requires_explicit_opt_in_for_all_routes(self):
        for route in (*dashboard.ASSETS, '/api/snapshot', '/api/events', REPORT_URL):
            with self.subTest(route=route):
                response, _ = self.request(route, 'HEAD', headers={'Host': TRUSTED_HOST})
                self.assertEqual(response.status, 403 if self.trusted_origin is None else 200)

    def test_forwarded_headers_do_not_authorize_a_host_or_origin(self):
        forwarded = {'Forwarded': f'host={TRUSTED_HOST};proto=https',
                     'X-Forwarded-Host': TRUSTED_HOST, 'X-Forwarded-Proto': 'https',
                     'Tailscale-User-Login': 'synthetic@example.invalid'}
        for headers in ({'Host': 'attacker.invalid'},
                        {'Host': 'localhost', 'Origin': TRUSTED_ORIGIN},
                        {'Host': 'localhost', 'Origin': 'https://attacker.invalid'}):
            response, _ = self.request('/api/snapshot', headers=forwarded | headers)
            self.assertEqual(response.status, 403)
        response, _ = self.request('/api/snapshot', headers=forwarded | {'Host': 'localhost'})
        self.assertEqual(response.status, 200, 'Forwarded headers have no effect on local access')

    def test_missing_duplicate_and_ambiguous_security_headers_refused(self):
        host = TRUSTED_HOST if self.trusted_origin else 'localhost'
        origin = self.trusted_origin or 'http://localhost'
        header_sets = [[], [('Host', host), ('Host', host)],
                       [('Host', host), ('Host', 'attacker.invalid')],
                       [('Host', 'attacker.invalid'), ('Host', host)],
                       [('Host', host), ('Origin', origin), ('Origin', origin)],
                       [('Host', host), ('Origin', origin), ('Origin', 'https://attacker.invalid')],
                       [('Host', host), ('Sec-Fetch-Site', 'same-origin'), ('Sec-Fetch-Site', 'cross-site')],
                       [('Host', host), ('Sec-Fetch-Site', 'same-origin, cross-site')],
                       [('Host', host + ',attacker.invalid')],
                       [('Host', host), ('Origin', origin + ', https://attacker.invalid')]]
        for headers in header_sets:
            with self.subTest(headers=headers):
                connection = self.connect()
                connection.putrequest('GET', '/api/snapshot', skip_host=True)
                for name, value in headers:
                    connection.putheader(name, value)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()

    def test_known_assets_and_head_independent_of_working_directory(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                Path('app.js').write_text('UNTRUSTED cwd asset')
                for route, (name, mime) in dashboard.ASSETS.items():
                    response, body = self.request(route)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(body, (ROOT / 'task_start' / 'web' / name).read_bytes())
                    self.assertEqual(response.getheader('Content-Type'), mime)
                    self.assertIn("script-src 'self'", response.getheader('Content-Security-Policy'))
                    response, body = self.request(route, 'HEAD')
                    self.assertEqual(body, b'')
                self.assertEqual(dashboard.asset_root(), ROOT / 'task_start' / 'web')
            finally:
                os.chdir(previous)


class TrustedOriginHTTPTests(HTTPTests):
    # Run the existing loopback/cache/read-only/SSE checks with opt-in too.
    trusted_origin = TRUSTED_ORIGIN

    def test_proxy_assets_snapshot_and_head_keep_security_headers(self):
        for origin in (None, TRUSTED_ORIGIN):
            headers = {'Host': TRUSTED_HOST, 'Sec-Fetch-Site': 'same-origin'}
            if origin is not None:
                headers['Origin'] = origin
            for route in (*dashboard.ASSETS, '/api/snapshot', REPORT_URL):
                for method in ('GET', 'HEAD'):
                    with self.subTest(route=route, method=method, origin=origin):
                        response, body = self.request(route, method, headers)
                        self.assertEqual(response.status, 200)
                        self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))
                        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                        self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
                        self.assertEqual(response.getheader('Referrer-Policy'), 'no-referrer')
                        self.assertIn("connect-src 'self'", response.getheader('Content-Security-Policy'))
                        self.assertIn("frame-ancestors 'none'", response.getheader('Content-Security-Policy'))
                        if method == 'HEAD':
                            self.assertEqual(body, b'')
                        elif route in dashboard.ASSETS:
                            self.assertEqual(body, (ROOT / 'task_start' / 'web' / dashboard.ASSETS[route][0]).read_bytes())
                        elif route == '/api/snapshot':
                            self.assertEqual(json.loads(body)['projection'], self.source)
                        else:
                            self.assertEqual(json.loads(body)['text'], self.report_reader.return_value['text'])
        for host in (TRUSTED_HOST + ':443', TRUSTED_HOST.upper()):
            response, _ = self.request('/', headers={'Host': host, 'Origin': TRUSTED_ORIGIN})
            self.assertEqual(response.status, 200)
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.reader.call_count, 1)

    def test_proxy_rejects_wrong_host_scheme_port_origin_and_cross_site(self):
        headers_to_reject = [
            {'Host': host} for host in ('other.example.invalid', 'other.example.ts.net',
                'sub.' + TRUSTED_HOST, TRUSTED_HOST + '.attacker.invalid', TRUSTED_HOST + '.',
                TRUSTED_HOST + ':444', TRUSTED_HOST + ':8765', TRUSTED_HOST + ':',
                TRUSTED_HOST + '@attacker.invalid', TRUSTED_HOST + '/path')]
        headers_to_reject += [{'Host': TRUSTED_HOST, 'Origin': origin} for origin in (
            'https://attacker.invalid', 'http://' + TRUSTED_HOST, 'http://localhost',
            TRUSTED_ORIGIN + ':444', TRUSTED_ORIGIN + '/', 'null', '',
            TRUSTED_ORIGIN + ' https://attacker.invalid')]
        headers_to_reject += [{'Host': TRUSTED_HOST, 'Origin': TRUSTED_ORIGIN, 'Sec-Fetch-Site': 'cross-site'},
                              {'Host': 'localhost', 'Origin': TRUSTED_ORIGIN}]
        for headers in headers_to_reject:
            for route in ('/', '/app.js', '/api/snapshot', '/api/events', REPORT_URL):
                with self.subTest(headers=headers, route=route):
                    response, body = self.request(route, headers=headers)
                    self.assertEqual(response.status, 403)
                    self.assertNotIn(b'projection', body)
                    self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))

    def test_proxy_sse_changes_heartbeat_reconnect_and_snapshot(self):
        headers = {'Host': TRUSTED_HOST, 'Origin': TRUSTED_ORIGIN, 'Sec-Fetch-Site': 'same-origin'}
        connection = self.connect()
        connection.request('GET', '/api/events', headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn('text/event-stream', response.getheader('Content-Type'))
        self.assertEqual(self.event(response), b'retry: 2000\nevent: invalidate\ndata: 1\n')
        original_wait = self.collector.wait
        with patch.object(self.collector, 'wait', side_effect=lambda revision: original_wait(revision, timeout=.02)):
            self.source = report(tasks=[task(state='paused')])
            self.collector.refresh()
            self.assertEqual(self.event(response), b'event: invalidate\ndata: 2\n')
            self.assertEqual(self.event(response), b': heartbeat\n')
        response.close()
        connection.close()
        self.source = report()
        self.collector.refresh()
        reconnected = self.connect()
        reconnected.request('GET', '/api/events', headers=headers | {'Last-Event-ID': '1'})
        self.assertEqual(self.event(reconnected.getresponse()), b'retry: 2000\nevent: invalidate\ndata: 3\n')
        response, body = self.request('/api/snapshot', headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)['projection'], self.source)
        self.assertEqual(self.reader.call_count, 3)

    def test_explicit_https_port_does_not_trust_other_ports(self):
        server = dashboard.DashboardServer(self.collector, port=0, trusted_origin=TRUSTED_ORIGIN + ':8443')
        self.addCleanup(server.server_close)
        with patch.object(self.server, 'trusted_origin', server.trusted_origin):
            for host, origin, expected in (
                    (TRUSTED_HOST + ':8443', TRUSTED_ORIGIN + ':8443', 200),
                    (TRUSTED_HOST + ':8443', None, 200),
                    (TRUSTED_HOST, TRUSTED_ORIGIN, 403),
                    (TRUSTED_HOST + ':443', TRUSTED_ORIGIN, 403),
                    (TRUSTED_HOST + ':8443', TRUSTED_ORIGIN, 403)):
                headers = {'Host': host}
                if origin is not None:
                    headers['Origin'] = origin
                response, _ = self.request('/api/snapshot', headers=headers)
                self.assertEqual(response.status, expected)


class CanonicalAdaptationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = status_fixture.StatusTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_current_http_report_preserves_initial_evidence_after_launch_failure(self):
        fixture = status_fixture.initial_report_failure(self)
        collector = dashboard.SnapshotCollector(lambda: fixture.report(None))
        collector.refresh()
        server = dashboard.DashboardServer(collector, port=0, report_reader=lambda task: task_loop_report(task,
            registry=fixture.registry, identities=fixture.identities, projects=fixture.projects,
            projects_root=fixture.repo.parent))
        thread = threading.Thread(target=server.serve_forever, kwargs=dict(poll_interval=.01))
        thread.start()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            connection.request('GET', REPORT_URL)
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(body['kind'], 'report', body)
            self.assertEqual(body['identity'], list(report_identity(fixture.detail())))
            self.assertEqual((body['revision'], body['task']), (1, 0))
            for text in ('Verified initial implementation', '139 tests passed', 'full regression: not_run',
                         'Final review: not_run; human action required: yes', 'failure details withheld'):
                self.assertIn(text, body['text'])
            self.assertNotIn('SECRET_DEV113', json.dumps(body))
            self.assertNotIn('<script>', json.dumps(body))
            # An available partial report does not bypass cache selection or
            # uncertainty when discovery subsequently fails.
            connection.request('GET', '/api/report?revision=0&task=0')
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            response.read()
            with patch.object(Herdr, 'workspaces', side_effect=TaskError(status_fixture.UNTRUSTED_FAILURE)):
                collector.refresh()
            revision = collector.snapshot()['revision']
            connection.request('GET', f'/api/report?revision={revision}&task=0')
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(body['kind'], 'diagnostics')
            self.assertIn('snapshot is unverified', body['message'])
            self.assertNotIn('Verified initial implementation', body['text'])
            self.assertNotIn('SECRET_DEV113', json.dumps(body))
        finally:
            connection.close()
            collector.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_selected_http_report_uses_real_checkpoint_then_missing_checkpoint_diagnostics(self):
        fixture = self.fixture
        expected = fixture.run_loop().render()
        collector = dashboard.SnapshotCollector(lambda: fixture.report(None))
        collector.refresh()
        server = dashboard.DashboardServer(collector, port=0, report_reader=lambda task: task_loop_report(task,
            registry=fixture.registry, identities=fixture.identities, projects=fixture.projects,
            projects_root=fixture.repo.parent))
        thread = threading.Thread(target=server.serve_forever, kwargs=dict(poll_interval=.01))
        thread.start()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            connection.request('GET', REPORT_URL)
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(body['kind'], 'report')
            self.assertEqual(body['text'], expected)
            self.assertNotIn(expected, json.dumps(collector.snapshot()))
            fixture.store.path.unlink()
            connection.request('GET', REPORT_URL)
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(body['kind'], 'diagnostics')
            self.assertIn('No saved loop report exists', body['text'])
            self.assertFalse(fixture.store.path.exists())
        finally:
            connection.close()
            collector.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_prior_report_from_real_replacement_and_pruned_cached_selection(self):
        fixture = self.fixture
        expected = fixture.run_loop().render()
        old = fixture.store.read()[0]
        fixture.run_loop(action='new', from_review=True)
        collector = dashboard.SnapshotCollector(lambda: fixture.report(None))
        collector.refresh()
        server = dashboard.DashboardServer(collector, port=0, report_reader=lambda task, **kw: task_loop_report(task,
            registry=fixture.registry, identities=fixture.identities, projects=fixture.projects,
            projects_root=fixture.repo.parent, **kw))
        thread = threading.Thread(target=server.serve_forever, kwargs=dict(poll_interval=.01))
        thread.start()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            connection.request('GET', REPORT_URL + '&iteration=0')
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(body['kind'], 'report')
            self.assertEqual(body['prior_run_id'], old['run_id'])
            self.assertEqual(body['text'], expected)
            self.assertNotIn(expected, json.dumps(collector.snapshot()))
            # Retention changes before the collector refreshes. Never substitute
            # the current run for this cached historical selection.
            with fixture.store.connection(write=True) as db:
                db.execute('DELETE FROM iterations')
            connection.request('GET', REPORT_URL + '&iteration=0')
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(body['kind'], 'unavailable')
            self.assertIsNone(body['text'])
        finally:
            connection.close()
            collector.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_real_canonical_guidance_history_and_no_sensitive_report_prose(self):
        fixture = self.fixture
        fixture.run_loop()
        expected = fixture.report(None).as_dict()
        collector = dashboard.SnapshotCollector(lambda: fixture.report(None))
        with patch('task_start.config.load_local', side_effect=AssertionError('No credentials')):
            collector.refresh()
        current = collector.snapshot()['projection']
        self.assertEqual(current, expected)
        self.assertEqual(current['tasks'][0]['state'], 'clean')
        self.assertEqual(current['tasks'][0]['contexts']['review']['context_id'], 'DEV-7-R1')
        self.assertTrue(current['tasks'][0]['history']['entries'])
        for secret in ('Current requirements', 'Checked requirements', 'prompt_sha256', 'api_key'):
            self.assertNotIn(secret, json.dumps(current))
        with patch.object(Herdr, 'workspaces', side_effect=TaskError('unreachable')):
            collector.refresh()
        self.assertTrue(collector.snapshot()['stale'])
        self.assertEqual(collector.snapshot()['projection'], expected)
        with patch.object(Herdr, 'workspaces', return_value=[]):
            collector.refresh()
        self.assertEqual(collector.snapshot()['projection']['tasks'], [])

    def test_second_process_persisted_context_update_reaches_snapshot(self):
        fixture = self.fixture
        collector = dashboard.SnapshotCollector(lambda: fixture.report(None), interval=.02)
        collector.refresh()
        before = collector.snapshot()
        subprocess.run([sys.executable, '-c',
            'from pathlib import Path; import sys; from task_start.contexts import ContextRegistry; '
            'ContextRegistry(Path(sys.argv[1])).update(sys.argv[2], state="uncertain")',
            str(fixture.registry.path), fixture.implementation], cwd=ROOT, check=True, capture_output=True)
        collector.start()
        try:
            self.assertGreater(collector.wait(before['revision'], timeout=5), before['revision'])
            current = collector.snapshot()['projection']['tasks'][0]
            self.assertEqual(current['state'], 'uncertain')
            self.assertEqual(current['guidance']['kind'], 'inspect')
            self.assertIsNone(current['guidance']['command'])
        finally:
            collector.close()

    def configure_project_registry(self):
        fixture = self.fixture
        path = fixture.repo / 'config/projects.toml'
        path.parent.mkdir()
        path.write_text('[projects.synthetic]\nlinear_project=' + json.dumps(fixture.projects[0].linear_project)
                        + '\nrepo_name=' + json.dumps(fixture.repo.name) + '\nbase_branch="main"\n')
        return path

    def configured_report(self, identifier=None):
        fixture = self.fixture
        return task_status(identifier, registry=fixture.registry, identities=fixture.identities,
                           projects_root=fixture.repo.parent)

    def test_permanent_and_worktree_config_agree_in_status_cli_and_dashboard(self):
        fixture = self.fixture
        fixture.run_loop()
        self.configure_project_registry()
        expected = fixture.report(None).as_dict()
        for source in (fixture.repo, fixture.path):
            with self.subTest(source=source), patch.object(config, 'REGISTRY', source / 'config/projects.toml'), \
                    patch.object(cli, 'task_status', side_effect=self.configured_report), \
                    patch('task_start.config.load_local', side_effect=AssertionError('No credentials')), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(cli.main(['status', '--all', '--json']), 0)
                self.assertEqual(json.loads(output.getvalue()), expected)
                collector = dashboard.SnapshotCollector(self.configured_report)
                collector.refresh()
                self.assertFalse(collector.snapshot()['stale'])
                self.assertEqual(collector.snapshot()['projection'], expected)
                self.assertEqual(expected['tasks'][0]['state'], 'clean')
                self.assertEqual(expected['tasks'][0]['review']['acceptance'], 'valid')
                server = dashboard.DashboardServer(collector, port=0)
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
                thread.start()
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                try:
                    connection.request('GET', '/api/snapshot')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())['projection'], expected)
                finally:
                    connection.close()
                    collector.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(2)
        self.assertFalse((fixture.path / 'config/projects.toml').exists())

    def test_registry_loss_and_competing_configuration_remain_uncertain(self):
        fixture = self.fixture
        fixture.run_loop()
        path = self.configure_project_registry()
        original = path.read_text()
        with patch.object(config, 'REGISTRY', fixture.path / 'config/projects.toml'):
            collector = dashboard.SnapshotCollector(self.configured_report)
            collector.refresh()
            verified = collector.snapshot()['projection']
            path.unlink()
            missing = self.configured_report().as_dict()
            self.assertEqual(missing['discovery']['state'], 'uncertain')
            self.assertEqual(missing['tasks'][0]['project']['state'], 'unverified')
            self.assertEqual(missing['tasks'][0]['state'], 'uncertain')
            collector.refresh()
            self.assertTrue(collector.snapshot()['stale'])
            self.assertEqual(collector.snapshot()['projection'], verified)
            path.write_text(original)
            (fixture.path / 'config').mkdir()
            competing = fixture.path / 'config/projects.toml'
            competing.write_text(original.replace(fixture.projects[0].linear_project, 'Conflicting project'))
            value = self.configured_report().as_dict()
            self.assertEqual(value['discovery']['state'], 'uncertain')
            self.assertEqual(value['tasks'][0]['project']['state'], 'unverified')
            self.assertEqual(value['tasks'][0]['guidance']['kind'], 'inspect')
            competing.unlink()
            (fixture.path / 'config').rmdir()
            collector.refresh()
            self.assertFalse(collector.snapshot()['stale'])
            self.assertEqual(collector.snapshot()['projection'], verified)


class EntryPointTests(unittest.TestCase):
    def test_cli_defaults_port_validation_and_dispatch_without_workflow_config(self):
        for args, expected in [(['dashboard'], 8765), (['dashboard', '--port', '0'], 0)]:
            with patch.object(cli, 'serve_dashboard') as serve, patch.object(cli, 'load_local', side_effect=AssertionError('No workflow config')):
                self.assertEqual(cli.main(args), 0)
                serve.assert_called_once_with(port=expected, trusted_origin=None)
        for args in (['dashboard', '--host', '0.0.0.0'], ['dashboard', '--port', '-1'], ['dashboard', '--port', '65536'], ['dashboard', '--port', 'x']):
            with self.subTest(args=args), patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(args)

    def test_trusted_origin_cli_validation_normalization_and_dispatch(self):
        for supplied, expected in ((TRUSTED_ORIGIN, TRUSTED_ORIGIN),
                                   (TRUSTED_ORIGIN + '/', TRUSTED_ORIGIN),
                                   ('https://DASHBOARD.EXAMPLE.INVALID:443/', TRUSTED_ORIGIN),
                                   (TRUSTED_ORIGIN + ':8443', TRUSTED_ORIGIN + ':8443')):
            with self.subTest(supplied=supplied), patch.object(cli, 'serve_dashboard') as serve, \
                    patch.object(cli, 'load_local', side_effect=AssertionError('No workflow config')):
                self.assertEqual(cli.main(['dashboard', '--trusted-origin', supplied]), 0)
                serve.assert_called_once_with(port=8765, trusted_origin=expected)
        invalid = ['', 'http://' + TRUSTED_HOST, TRUSTED_HOST, 'https://',
                   'https://*.example.invalid', 'https://user@' + TRUSTED_HOST,
                   TRUSTED_ORIGIN + '/path', TRUSTED_ORIGIN + '?', TRUSTED_ORIGIN + '#',
                   TRUSTED_ORIGIN + ':0', TRUSTED_ORIGIN + ':65536', TRUSTED_ORIGIN + ':bad',
                   TRUSTED_ORIGIN + ':', TRUSTED_ORIGIN + ',https://other.example.invalid',
                   TRUSTED_ORIGIN + ' https://other.example.invalid',
                   ' ' + TRUSTED_ORIGIN, TRUSTED_ORIGIN + '\n', TRUSTED_ORIGIN + '\t',
                   'https://dash\tboard.example.invalid', 'https://dashboard.example.invalid\\path',
                   'https://dashboard..invalid', 'https://-dashboard.invalid', 'https://dashboard-.invalid',
                   'https://dash_board.invalid', 'https://%64ashboard.invalid', 'https://dáshboard.invalid',
                   'https://[::1]', TRUSTED_ORIGIN + '.', 'https://' + 'a' * 64 + '.invalid',
                   'https://' + '.'.join(['a' * 63] * 4)]
        for value in invalid:
            with self.subTest(value=value), patch('sys.stderr', new=io.StringIO()) as error, \
                    patch.object(cli, 'serve_dashboard') as serve:
                with self.assertRaises(SystemExit) as failure:
                    cli.main(['dashboard', '--trusted-origin', value])
                self.assertEqual(failure.exception.code, 2)
                self.assertIn('Trusted origin must be one HTTPS hostname', error.getvalue())
                serve.assert_not_called()
            with self.subTest(server_value=value), patch.object(dashboard.ThreadingHTTPServer, '__init__') as bind:
                with self.assertRaises(TaskError):
                    dashboard.DashboardServer(dashboard.SnapshotCollector(), port=0, trusted_origin=value)
                bind.assert_not_called()

    def test_distribution_layout_serves_package_assets_and_fails_clearly_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            shutil.copytree(ROOT / 'task_start' / 'web', bundle / 'task_start' / 'web')
            copied_module = bundle / 'task_start' / 'dashboard_server.py'
            shutil.copy(ROOT / 'task_start' / 'dashboard_server.py', copied_module)
            spec = importlib.util.spec_from_file_location('task_start.dashboard_resource_fixture', copied_module)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assets = module.load_assets()
            self.assertEqual(assets['/app.js'][0], (ROOT / 'task_start' / 'web' / 'app.js').read_bytes())
            (bundle / 'task_start' / 'web' / 'app.js').unlink()
            with self.assertRaisesRegex(TaskError, 'reinstall agentic-workflows-lite'):
                module.load_assets()
            (bundle / 'task_start' / 'web' / 'app.js').symlink_to(ROOT / 'task_start' / 'config.py')
            with self.assertRaises(TaskError):
                module.load_assets()

    def test_actual_entrypoint_from_other_directory_and_clean_sigterm(self):
        for trusted_origin in (None, TRUSTED_ORIGIN):
            with self.subTest(trusted_origin=trusted_origin):
                self.check_actual_entrypoint(trusted_origin)

    def check_actual_entrypoint(self, trusted_origin):
        with tempfile.TemporaryDirectory() as directory:
            # Inject only synthetic status data, never inspect user configuration.
            fixture = Path(directory) / 'sitecustomize.py'
            fixture.write_text('from task_start import dashboard_server as d\n'
                'from task_start.status import StatusReport\n'
                'd.task_status = lambda: StatusReport(' + repr(report()) + ')\n')
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((directory, str(ROOT))))
            args = [sys.executable, str(ROOT / 'task'), 'dashboard', '--port', '0']
            if trusted_origin is not None:
                args += ['--trusted-origin', trusted_origin]
            process = subprocess.Popen(args,
                cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                import select
                ready, _, _ = select.select([process.stdout], [], [], 5)
                self.assertTrue(ready, 'Dashboard did not print its URL')
                line = process.stdout.readline()
                self.assertIn('Dashboard: http://127.0.0.1:', line)
                port = int(line.split('127.0.0.1:')[1].split()[0])
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                try:
                    connection.request('GET', '/')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b'Local task dashboard', response.read())
                    connection.request('GET', '/api/snapshot', headers={'Host': TRUSTED_HOST, 'Origin': TRUSTED_ORIGIN})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403 if trusted_origin is None else 200)
                    body = response.read()
                    if trusted_origin is not None:
                        self.assertEqual(json.loads(body)['projection'], report())
                finally:
                    connection.close()
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
                self.assertEqual(stderr, '')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['__pycache__', 'sitecustomize.py'])

    def test_bind_failure_is_actionable_and_missing_assets_fail_before_collecting(self):
        with patch.object(dashboard, 'DashboardServer', side_effect=OSError(98, 'Address already in use')):
            with self.assertRaisesRegex(TaskError, '127.0.0.1:8765'):
                dashboard.serve_dashboard()
        with patch.object(dashboard, 'load_assets', side_effect=TaskError('Missing assets')), patch.object(dashboard.SnapshotCollector, 'start') as start:
            with self.assertRaises(TaskError):
                dashboard.serve_dashboard()
            start.assert_not_called()


if __name__ == '__main__':
    unittest.main()

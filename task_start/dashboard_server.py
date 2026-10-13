"""Read-only HTTP transport for status.task_status, with one shared collector.

The cache is disposable: uncertain discovery retains one previous projection,
never a task archive. No lifecycle interpretation belongs here.
"""

from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import signal
import threading
from urllib.parse import urlsplit

from . import TaskError
from .status import task_status


HOST = '127.0.0.1'
DEFAULT_PORT = 8765
REFRESH_SECONDS = 5
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/index.html': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/styles.css': ('styles.css', 'text/css; charset=utf-8')}


def trusted_https_origin(value):
    """Accept one HTTPS DNS authority, normalizing it to a browser origin."""
    match = re.fullmatch(r'https://([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?/?', value)
    if match:
        host, port = match.groups()
        labels = host.split('.')
        if (len(host) <= 253
                and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                        for label in labels)
                and (port is None or 1 <= int(port) <= 65535)):
            authority = host.lower()
            if port is not None and int(port) != 443:
                authority += f':{int(port)}'
            return f'https://{authority}'
    raise TaskError('Trusted origin must be one HTTPS hostname with an optional port (1-65535); '
                    'no credentials, wildcards, path, query, or fragment.')


def asset_root():
    # Distribution contract: ship web/ beside task_start/, in the same release.
    # Never fall back to cwd or allow a request to choose a resource location.
    return Path(__file__).resolve().parent.parent / 'web'


def load_assets():
    root = asset_root().resolve()
    assets = {}
    try:
        for route, (name, content_type) in ASSETS.items():
            path = root / name
            if path.resolve().parent != root:
                raise OSError('Asset outside bundle')
            assets[route] = (path.read_bytes(), content_type)
    except OSError:
        raise TaskError('Dashboard assets are missing or invalid; install web/ beside task_start/.') from None
    return assets


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class SnapshotCollector:
    def __init__(self, reader=None, *, interval=REFRESH_SECONDS):
        if interval <= 0:
            raise ValueError('Refresh interval must be positive')
        self.reader = reader or task_status
        self.interval = interval
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.thread = None
        self._retained = None
        self._fingerprint = None
        self._snapshot = dict(revision=0, projection=None, discovery=None, stale=True,
                              error=None, checked_at=None, verified_at=None, changed_at=None,
                              refresh_seconds=interval)

    def refresh(self):
        """One caller only; HTTP clients read the cache and never invoke this."""
        error, report = None, None
        try:
            report = self.reader().as_dict()
        except Exception:
            # Exceptions can contain subprocess output or private paths/content.
            error = 'Status collection failed; retained data is unverified. The collector will retry.'
        now = utc_now()
        discovery = report['discovery'] if report is not None else None
        verified = discovery is not None and discovery['state'] == 'available'
        if verified or self._retained is None and report is not None and report['tasks'] is not None:
            self._retained = deepcopy(report)
        projection = self._retained if self._retained is not None else report
        semantic = dict(projection=projection, discovery=discovery, stale=not verified, error=error)
        fingerprint = json.dumps(semantic, sort_keys=True, ensure_ascii=True)
        with self.condition:
            changed = fingerprint != self._fingerprint
            self._snapshot.update(deepcopy(semantic), checked_at=now)
            if verified:
                self._snapshot['verified_at'] = now
            if changed:
                self._fingerprint = fingerprint
                self._snapshot['revision'] += 1
                self._snapshot['changed_at'] = now
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return deepcopy(self._snapshot)

    def wait(self, revision, timeout=10):
        with self.condition:
            self.condition.wait_for(lambda: self.stopped.is_set() or self._snapshot['revision'] != revision,
                                    timeout=timeout)
            return self._snapshot['revision']

    def start(self):
        def collect():
            while not self.stopped.is_set():
                self.refresh()
                if self.stopped.wait(self.interval):
                    break
        self.thread = threading.Thread(target=collect, name='dashboard-status', daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            # The canonical reader uses bounded external calls. A slow read must
            # not prevent local HTTP shutdown; it owns no workflow locks/writes.
            self.thread.join(timeout=2)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, collector, *, port=DEFAULT_PORT, trusted_origin=None):
        self.trusted_origin = trusted_https_origin(trusted_origin) if trusted_origin is not None else None
        self.collector = collector
        self.assets = load_assets()
        self.streams = threading.BoundedSemaphore(16)
        super().__init__((HOST, port), DashboardHandler)

    @property
    def url(self):
        return f'http://{HOST}:{self.server_port}'


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = 'AWL'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args):
        pass  # Do not log task identities, request paths, or browser data.

    def send_headers(self, status, content_type, length=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if length is not None:
            self.send_header('Content-Length', str(length))
        if status == 405:
            self.send_header('Allow', 'GET, HEAD')
        self.end_headers()

    def respond(self, status, body, content_type='text/plain; charset=utf-8'):
        self.send_headers(status, content_type, len(body))
        if self.command != 'HEAD':
            self.wfile.write(body)

    def trusted_request(self):
        # Repeated security headers are ambiguous, even when the first is valid.
        if (len(self.headers.get_all('Host', [])) != 1
                or len(self.headers.get_all('Origin', [])) > 1
                or len(self.headers.get_all('Sec-Fetch-Site', [])) > 1):
            return False
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'same-site', 'none'):
            return False
        # Restrict browser reads as well as binding: no CORS or DNS rebinding.
        # A user's SSH tunnel can have a different local port from this server.
        local = re.fullmatch(r'(127\.0\.0\.1|localhost)(:[0-9]{1,5})?', host)
        if local is not None and (origin is None or origin == f'http://{host}'):
            return True
        # Tailscale Serve preserves Host and Origin when proxying to loopback.
        # Forwarded/identity headers never establish trust. Navigation and some
        # same-origin GETs omit Origin, so require it to match only when present.
        trusted = self.server.trusted_origin
        if trusted is None:
            return False
        authority = trusted.removeprefix('https://')
        hosts = {authority} if ':' in authority else {authority, f'{authority}:443'}
        return host.lower() in hosts and (origin is None or origin == trusted)

    def do_GET(self):
        if not self.trusted_request():
            self.respond(403, b'Trusted same-origin requests only.\n')
            return
        path = urlsplit(self.path).path
        if path in self.server.assets:
            body, content_type = self.server.assets[path]
            self.respond(200, body, content_type)
        elif path == '/api/snapshot':
            body = json.dumps(self.server.collector.snapshot(), ensure_ascii=True).encode('utf-8')
            self.respond(200, body, 'application/json; charset=utf-8')
        elif path == '/api/events':
            if self.command == 'HEAD':
                self.send_headers(200, 'text/event-stream', 0)
            else:
                self.events()
        else:
            self.respond(404, b'Not found.\n')

    do_HEAD = do_GET

    def events(self):
        if not self.server.streams.acquire(blocking=False):
            self.respond(503, b'Too many event streams; retry later.\n')
            return
        try:
            self.send_headers(200, 'text/event-stream; charset=utf-8')
            collector = self.server.collector
            revision = collector.snapshot()['revision']
            # Always invalidate on connection, ignoring Last-Event-ID. Current
            # snapshots, not event replay, recover missed changes/restarts.
            self.wfile.write(f'retry: 2000\nevent: invalidate\ndata: {revision}\n\n'.encode())
            self.wfile.flush()
            while not collector.stopped.is_set():
                current = collector.wait(revision)
                if collector.stopped.is_set():
                    break
                if current != revision:
                    self.wfile.write(f'event: invalidate\ndata: {current}\n\n'.encode())
                    revision = current
                else:
                    self.wfile.write(b': heartbeat\n\n')
                self.wfile.flush()
        except (OSError, ConnectionError):
            pass
        finally:
            self.server.streams.release()

    def reject_write(self):
        self.respond(405, b'Read-only dashboard.\n')

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = reject_write


def serve_dashboard(*, port=DEFAULT_PORT, trusted_origin=None):
    collector = SnapshotCollector()
    try:
        server = DashboardServer(collector, port=port, trusted_origin=trusted_origin)
    except OSError as error:
        raise TaskError(f'Cannot listen on {HOST}:{port}: {error.strerror}') from None
    previous = {}
    try:
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, interrupted)
        collector.start()
        print(f'Dashboard: {server.url} (read-only; Ctrl+C to stop)', flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        collector.close()
        server.server_close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)

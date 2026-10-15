#!/usr/bin/env python3
"""Private Linux transport for ab-worktree's prepared sessions (agent-browser 0.39.0)."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
from uuid import uuid4

SUPPORTED_VERSION = '0.39.0'


def runtime_directory():
    value = os.environ.get('AWL_BROWSER_RUNTIME_DIR')
    return Path(value) if value else Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}') / 'agent-browser/awl'


def private_directory(path, *, create=False):
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError(f'Runtime directory must be an absolute physical path: {path}')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f'Runtime directory must be owned by you with mode 700: {path}')


def private_file(path, *, create=False):
    flags = (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError(f'Unsafe browser ownership file: {path}')
    return os.fdopen(fd, 'r+' if create else 'r')


def ownership_record(source):
    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Ambiguous browser ownership record: duplicate field')
            value[key] = item
        return value
    return json.load(source, object_pairs_hook=unique_fields)


def namespace_identity(kind):
    info = Path(f'/proc/self/ns/{kind}').stat()
    return dict(device=info.st_dev, inode=info.st_ino)


def socket_identity(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError('Browser socket is not an owned local Unix socket')
    return dict(device=info.st_dev, inode=info.st_ino, ctime_ns=info.st_ctime_ns,
                uid=info.st_uid, gid=info.st_gid, mode=info.st_mode)


def listener_identity(path, info):
    """Pin the kernel listener, not just a potentially recycled filesystem inode."""
    try:
        rows = [line.split(maxsplit=7) for line in Path('/proc/net/unix').read_text().splitlines()[1:]]
        matches = [row for row in rows if len(row) == 8 and row[7] == str(path)
                   and row[3:6] == ['00010000', '0001', '01']]
        if len(matches) != 1:
            raise ValueError('Missing or ambiguous kernel browser listener; check sandbox network/procfs visibility')
        inode = int(matches[0][6])
        if not 0 < inode <= 0xffffffff:
            raise ValueError('Unsupported kernel browser listener inode')
        # Linux SOCK_DIAG_BY_FAMILY / unix_diag_req, requesting NAME and VFS.
        payload = struct.pack('=BBHIIIII', socket.AF_UNIX, 0, 0, 0xffffffff, inode, 3, 0xffffffff, 0xffffffff)
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4) as diag:
            diag.settimeout(5)
            diag.sendto(struct.pack('=IHHII', 16 + len(payload), 20, 1, 1, 0) + payload, (0, 0))
            data, sender = diag.recvfrom(4096)
        return parse_listener(data, sender, inode, path, info)
    except OSError as error:
        raise ValueError(f'Kernel browser listener proof inaccessible: {error}; '
                         'sandbox must expose the prepared listener through /proc/net/unix and NETLINK_SOCK_DIAG') from error


def parse_listener(data, sender, inode, path, info):
    if sender != (0, 0) or len(data) < 32:
        raise ValueError('Invalid kernel browser listener response')
    length, kind, flags, sequence, _ = struct.unpack_from('=IHHII', data)
    if length != len(data) or kind != 20 or flags != 0 or sequence != 1:
        raise ValueError('Kernel browser listener lookup failed; check NETLINK_SOCK_DIAG access and session continuity')
    family, kind, state, _, found, low, high = struct.unpack_from('=BBBBIII', data, 16)
    if (family, kind, state, found) != (socket.AF_UNIX, socket.SOCK_STREAM, 10, inode) or (low, high) in ((0, 0), (0xffffffff, 0xffffffff)):
        raise ValueError('Prepared browser kernel listener identity changed')
    attributes = {}
    offset = 32
    while offset < len(data):
        if offset + 4 > len(data):
            raise ValueError('Truncated kernel browser listener attribute')
        size, kind = struct.unpack_from('=HH', data, offset)
        if size < 4 or offset + size > len(data) or kind in attributes:
            raise ValueError('Invalid or ambiguous kernel browser listener attributes')
        attributes[kind] = data[offset + 4:offset + size]
        offset += (size + 3) & ~3
    # unix_diag_vfs uses the kernel's 12:20 device encoding, not stat's encoding.
    device = os.major(info['device']) << 20 | os.minor(info['device'])
    # UNIX_DIAG_VFS projects i_ino to __u32. Keep the full filesystem inode in
    # the separate socket continuity proof; only this ABI comparison truncates.
    if (offset != len(data) or attributes.get(0) != os.fsencode(path) + b'\0'
            or len(attributes.get(1, b'')) != 8
            or struct.unpack('=II', attributes[1]) != (info['inode'] & 0xffffffff, device)):
        raise ValueError('Kernel listener does not match the owned browser socket')
    return dict(inode=inode, cookie=[low, high])


def process_start(pid):
    # Never interpret a PID from another procfs numbering domain as this peer.
    if int(Path('/proc/self/stat').read_text().split(' ', 1)[0]) != os.getpid():
        raise ValueError('Browser identity requires procfs for the calling PID namespace')
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        started = fields[19]
        if not started.isascii() or not started.isdigit():
            raise ValueError('Invalid process start time')
    except (IndexError, ValueError) as error:
        raise ValueError('Invalid browser peer procfs identity') from error
    if fields[0] in {'Z', 'X', 'x'}:
        raise ValueError('Prepared browser daemon has stopped')
    return started


def validate_peer_record(peer):
    legacy = {'pid', 'started', 'inode'}
    if (not isinstance(peer, dict) or set(peer) not in (legacy, legacy | {'proof'})
            or type(peer['pid']) is not int or peer['pid'] <= 0
            or not isinstance(peer['started'], str) or not peer['started'].isascii()
            or not peer['started'].isdigit() or type(peer['inode']) is not int or peer['inode'] <= 0):
        raise ValueError('Prepared browser daemon identity changed or invalid host peer record')
    if 'proof' not in peer:
        return
    proof = peer['proof']
    if (not isinstance(proof, dict)
            or set(proof) != {'version', 'socket', 'boot_id', 'pid_ns', 'uid', 'gid', 'process', 'listener'}
            or type(proof['version']) is not int or proof['version'] != 1
            or not isinstance(proof['boot_id'], str)
            or not re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', proof['boot_id'])
            or any(type(proof[k]) is not int or proof[k] < 0 for k in ('uid', 'gid'))):
        raise ValueError('Invalid host browser identity proof; host preparation is required')
    for key, fields in [('socket', {'device', 'inode', 'ctime_ns', 'uid', 'gid', 'mode'}),
                        ('pid_ns', {'device', 'inode'})]:
        value = proof[key]
        if (not isinstance(value, dict) or set(value) != fields
                or any(type(v) is not int or v < 0 for v in value.values())):
            raise ValueError('Invalid host browser identity proof; host preparation is required')
    listener = proof['listener']
    if (not isinstance(listener, dict) or set(listener) != {'inode', 'cookie'}
            or type(listener['inode']) is not int or not 0 < listener['inode'] <= 0xffffffff
            or not isinstance(listener['cookie'], list) or len(listener['cookie']) != 2
            or any(type(v) is not int or not 0 <= v <= 0xffffffff for v in listener['cookie'])
            or listener['cookie'] in ([0, 0], [0xffffffff, 0xffffffff])):
        raise ValueError('Invalid host kernel listener proof; host preparation is required')
    process = proof['process']
    if (proof['socket']['inode'] != peer['inode'] or proof['socket']['uid'] != proof['uid']
            or not isinstance(process, dict) or type(process.get('pid')) is not int
            or process != dict(pid=peer['pid'], started=peer['started'])):
        raise ValueError('Inconsistent host browser identity proof; refusing another session')


@contextmanager
def live_peer(connection):
    # Linux SO_PEERPIDFD (6.5+): obtained from the connected socket, never from
    # a supplied PID. Poll works even when SO_PEERCRED translates the PID to 0.
    # Do not use fstat(pidfd) as identity: pre-pidfs kernels share an anon inode.
    option = getattr(socket, 'SO_PEERPIDFD', None)
    if option is None:
        # Python 3.12 lacks this name. Only use asm-generic's numeric ABI on
        # platforms with its socket constants, never interpret another option
        # (e.g. on SPARC) as a returned file descriptor.
        if socket.SOL_SOCKET != 1 or socket.SO_PEERCRED != 17:
            raise ValueError('Browser identity requires Python exposing SO_PEERPIDFD on this socket ABI')
        option = 77
    try:
        fd = connection.getsockopt(socket.SOL_SOCKET, option)
    except OSError as error:
        raise ValueError(f'Browser peer identity inaccessible (SO_PEERPIDFD: {error}); '
                         'host/kernel and sandbox must permit peer pidfds') from error
    try:
        poll = select.poll()
        poll.register(fd, select.POLLIN)
        if poll.poll(0):
            raise ValueError('Prepared browser daemon has stopped; host preparation is required')
        yield
    finally:
        os.close(fd)


@contextmanager
def connection_to(path, expected=None):
    if expected is not None:
        validate_peer_record(expected)
    try:
        info = socket_identity(path)
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(35)
            connection.connect(str(path))
            pid, uid, gid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid() or pid < 0 or gid < 0:
                raise ValueError('Prepared browser daemon identity changed: incompatible peer credentials')
            if expected is not None and info['inode'] != expected['inode']:
                raise ValueError('Prepared browser daemon identity changed; refusing another session')
            proof = dict(version=1, socket=info, boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                         pid_ns=namespace_identity('pid'), uid=uid, gid=gid, listener=listener_identity(path, info))
            stored = expected.get('proof') if expected is not None else None
            same_namespace = stored is None or stored['pid_ns'] == proof['pid_ns']
            if stored is not None and any(stored[key] != proof[key] for key in proof if key != 'pid_ns'):
                raise ValueError('Prepared browser daemon identity changed; host socket/credential proof differs')
            if pid == 0:
                if stored is None or same_namespace:
                    raise ValueError('Browser peer PID is not visible and host namespace proof is missing or incompatible; '
                                     'rerun host preparation with the upgraded helper')
                peer = expected
            else:
                # A visible process never falls back after a failed PID/start check.
                started = process_start(pid)
                if expected is not None and (started != expected['started'] or same_namespace and pid != expected['pid']):
                    raise ValueError('Prepared browser daemon identity changed; refusing another session')
                proof['process'] = dict(pid=pid, started=started)
                peer = expected if stored is not None else dict(pid=pid, started=started, inode=info['inode'], proof=proof)
            # Recheck the pathname after connect/identity inspection, before any
            # protocol bytes. Bind replacement requires unlinking the old inode.
            if socket_identity(path) != info:
                raise ValueError('Prepared browser socket changed during connection')
            if pid == 0:
                with live_peer(connection):
                    yield connection, peer
            else:
                yield connection, peer
    except OSError as error:
        raise ValueError(f'Host-prepared browser socket/process evidence is inaccessible: {error}; '
                         'check the exact runtime mount, Unix-socket policy and procfs visibility on the host; '
                         'no launch fallback was attempted') from error


def request(path, command, expected=None, *, connect_only=False):
    with connection_to(path, expected) as (connection, peer):
        command = dict(command, id=str(uuid4()))
        connection.sendall(json.dumps(command).encode() + b'\n')
        with connection.makefile('rb') as source:
            data = source.readline(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024 or not data.endswith(b'\n'):
            raise ValueError('Incomplete or oversized browser response')
        result = json.loads(data)
        if not isinstance(result, dict) or result.get('id') != command['id'] or type(result.get('success')) is not bool:
            raise ValueError('Unexpected browser protocol response; rerun setup for the supported version')
        if not result['success']:
            raise ValueError(f"Browser command failed: {result.get('error', 'unknown error')}")
        data = result.get('data')
        if connect_only and isinstance(data, dict):
            # Inspect before extracting snapshot text: a browser can disappear
            # between the liveness probe and the action's native dispatch.
            for evidence in (data, data.get('lifecycle')):
                if isinstance(evidence, dict) and any(evidence.get(key) is True for key in
                                                      ('launched', 'relaunchedBrowser', 'restartedBackground')):
                    raise ValueError('Unexpected browser launch or restart in connect-only mode; host preparation is required')
        return data, peer


def tab_id(value):
    # Native 0.39.0 uses stable positive u32 IDs, never positional integers.
    if not re.fullmatch(r'[tT][0-9]+', value) or not 1 <= int(value[1:]) <= 0xffffffff:
        raise ValueError('Expected a stable tab ID such as t1 (use tab list); positional integers are not supported')
    return value


def parse_command(args):
    """A deliberately bounded connect-only surface; no launch/connect/provider actions."""
    command, *rest = args
    if command in {'open', 'goto'} and len(rest) == 1:
        return dict(action='navigate', url=rest[0])
    if command in {'close', 'back', 'forward', 'reload'} and not rest:
        return dict(action=command)
    if command == 'snapshot':
        result = dict(action='snapshot')
        flags = {'-i': 'interactive', '--interactive': 'interactive', '-c': 'compact', '--compact': 'compact',
                 '-C': 'cursor', '--cursor': 'cursor', '--urls': 'urls'}
        while rest:
            option = rest.pop(0)
            if option in flags:
                result[flags[option]] = True
            elif option in {'-s', '--selector', '-d', '--depth'} and rest:
                key = 'selector' if option in {'-s', '--selector'} else 'maxDepth'
                result[key] = rest.pop(0) if key == 'selector' else int(rest.pop(0))
            else:
                raise ValueError(f'Unsupported snapshot option: {option}')
        return result
    if command == 'eval' and rest:
        return dict(action='evaluate', script=sys.stdin.read() if rest == ['--stdin'] else ' '.join(rest))
    if command in {'click', 'dblclick', 'hover', 'focus', 'check', 'uncheck'} and len(rest) == 1:
        return dict(action=command, selector=rest[0])
    if command in {'fill', 'type'} and len(rest) == 2:
        return dict(action=command, selector=rest[0], **{'value' if command == 'fill' else 'text': rest[1]})
    if command == 'press' and len(rest) == 1:
        return dict(action='press', key=rest[0])
    if command == 'get' and rest:
        sub, *values = rest
        if sub in {'url', 'title'} and not values:
            return dict(action=sub)
        actions = {'text': 'gettext', 'html': 'innerhtml', 'value': 'inputvalue', 'count': 'count', 'box': 'boundingbox'}
        if sub in actions and len(values) == 1:
            return dict(action=actions[sub], selector=values[0])
        if sub == 'attr' and len(values) == 2:
            return dict(action='getattribute', selector=values[0], attribute=values[1])
    if command == 'find' and len(rest) >= 2 and rest[0] in {'role', 'text', 'label', 'placeholder', 'testid'}:
        locator, value, *tail = rest
        result = dict(action='getby' + locator, **{'testId' if locator == 'testid' else locator: value})
        result['subaction'] = tail.pop(0) if tail and not tail[0].startswith('--') else 'click'
        if result['subaction'] not in {'click', 'fill', 'text', 'check', 'uncheck', 'hover'}:
            raise ValueError('Unsupported find action')
        while tail:
            option = tail.pop(0)
            if option == '--exact':
                result['exact'] = True
            elif option == '--name' and tail:
                result['name'] = tail.pop(0)
            elif 'value' not in result:
                result['value'] = option
            else:
                raise ValueError('Unsupported find arguments')
        return result
    if command in {'console', 'errors'} and rest in ([], ['--clear']):
        return dict(action=command, clear=bool(rest))
    if command == 'network' and rest in (['requests'], ['requests', '--clear']):
        return dict(action='requests', clear=len(rest) == 2)
    if command == 'cookies':
        if rest in ([], ['get'], ['clear']):
            return dict(action='cookies_clear' if rest == ['clear'] else 'cookies_get')
        if len(rest) in {3, 5} and rest[0] == 'set':
            cookie = dict(name=rest[1], value=rest[2])
            if len(rest) == 5:
                if rest[3] != '--url':
                    raise ValueError('cookies set supports --url')
                cookie['url'] = rest[4]
            return dict(action='cookies_set', cookies=[cookie])
    if command == 'storage' and rest and rest[0] in {'local', 'session'}:
        kind, *tail = rest
        if not tail or tail[0] == 'get' and len(tail) <= 2:
            return dict(action='storage_get', type=kind, **({'key': tail[1]} if len(tail) == 2 else {}))
        if len(tail) == 3 and tail[0] == 'set':
            return dict(action='storage_set', type=kind, key=tail[1], value=tail[2])
        if tail == ['clear']:
            return dict(action='storage_clear', type=kind)
    if command == 'tab':
        if rest in ([], ['list']):
            return dict(action='tab_list')
        if rest and rest[0] == 'new' and len(rest) <= 2:
            return dict(action='tab_new', **({'url': rest[1]} if len(rest) == 2 else {}))
        if rest and rest[0] == 'close' and len(rest) <= 2:
            return dict(action='tab_close', **({'tabId': tab_id(rest[1])} if len(rest) == 2 else {}))
        if len(rest) == 1:
            return dict(action='tab_switch', tabId=tab_id(rest[0]))
    if command == 'screenshot' and len(rest) in {1, 2}:
        if len(rest) == 1 or rest[1] == '--full':
            return dict(action='screenshot', path=str(Path(rest[0]).absolute()), fullPage=len(rest) == 2)
    raise ValueError(f'Unsupported connect-only command: {command}; see the browser guide. No fallback was attempted')


def check_host():
    # agent-browser 0.39.0 automatically disables Chrome sandboxing here. Refuse
    # these environments rather than accepting that upstream fallback.
    cgroup = Path('/proc/1/cgroup').read_text()
    if (os.geteuid() == 0 or 'CI' in os.environ or Path('/.dockerenv').exists()
            or Path('/run/.containerenv').exists() or any(x in cgroup for x in ('docker', 'kubepods', 'lxc'))):
        raise ValueError('This host would trigger upstream automatic --no-sandbox; prepare from a supported non-root Linux host')


def prepare(identity, directory, record_path, sock, actor_path):
    previous = None
    if record_path.exists():
        with private_file(record_path) as source:
            previous = ownership_record(source)
        if not isinstance(previous, dict) or previous.get('identity') != identity:
            raise ValueError('Instance is assigned to a different actor; supply a distinct stable instance ID')
    # The caller holds both actor and instance locks. Reserve only after the
    # instance's ownership is validated, before any launch/recovery attempt.
    if previous is not None and sock.exists():
        if not actor_path.exists():
            save_record(actor_path, identity)
        _, peer = request(sock, dict(action='snapshot'), previous['peer'])
        previous['peer'] = peer
        previous['ready'] = True
        save_record(record_path, previous)
        return previous
    # Never attach to or close a daemon for which ownership was not recorded.
    if sock.exists() or sock.is_symlink():
        raise ValueError('Unowned or stale socket exists; inspect it on the host, do not adopt another browser')
    sidecars = [directory / (identity['session'] + suffix) for suffix in ('.pid', '.version', '.config', '.stream')]
    for path in sidecars:
        if not (path.exists() or path.is_symlink()):
            continue
        info = path.lstat()
        # The native daemon removes its socket/PID/version on close but leaves
        # the CLI config fingerprint. Reusing this owned regular file is safe.
        if (previous is not None and path.suffix == '.config' and stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid() and info.st_nlink == 1):
            continue
        raise ValueError('Daemon sidecars remain without a socket; inspect this exact session on the host before cleanup/retry')
    check_host()
    executable = shutil.which('agent-browser')
    if not executable:
        raise ValueError('agent-browser missing from controller PATH; run python3.12 scripts/install_browser.py')
    env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_BROWSER_')}
    version = subprocess.run([executable, '--version'], text=True, capture_output=True, timeout=10, env=env)
    if version.returncode or version.stdout.strip() != f'agent-browser {SUPPORTED_VERSION}':
        raise ValueError(f'Prepared sessions require agent-browser {SUPPORTED_VERSION}; rerun setup or install that version explicitly')
    # Explicit empty config and a clean browser environment preserve user config
    # without allowing CDP/profile/provider/session defaults to collapse isolation.
    config = directory / (identity['session'] + '.empty.json')
    with private_file(config, create=True) as output:
        if output.read() not in ('', '{}\n'):
            raise ValueError('Managed empty browser config was edited; inspect it on the host')
        output.seek(0)
        output.write('{}\n')
        output.truncate()
    env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_BROWSER_')}
    env['AGENT_BROWSER_SOCKET_DIR'] = str(directory)
    # An explicitly selected Chrome binary is supported, but no launch flags.
    if os.environ.get('AGENT_BROWSER_EXECUTABLE_PATH'):
        env['AGENT_BROWSER_EXECUTABLE_PATH'] = os.environ['AGENT_BROWSER_EXECUTABLE_PATH']
    if not actor_path.exists():
        save_record(actor_path, identity)
    result = subprocess.run([executable, '--config', str(config), '--session', identity['session'],
                             'open', 'about:blank'], env=env, capture_output=True, text=True, timeout=60)
    # Record the exact daemon even if Chrome startup failed, so the host can
    # retry or close that owned daemon without treating it as an unrelated one.
    value = None
    if sock.exists():
        with connection_to(sock) as (_, peer):
            value = dict(identity=identity, peer=peer, ready=False)
        save_record(record_path, value)
    if result.returncode:
        reason = next((reason for phrase, reason in (
            ('Read-only file system', 'read-only browser storage'),
            ('Permission denied', 'permission denied'),
            ('Operation not permitted', 'operation not permitted by the host/sandbox'),
            ('error while loading shared libraries', 'missing Chrome shared libraries'),
            ('No usable sandbox', 'Chrome user-namespace sandbox unavailable')) if phrase in result.stderr), 'launch rejected')
        raise ValueError(f'Host Chrome startup failed ({reason}); inspect Chrome libraries, executable and user-namespace/AppArmor permissions on the host. No sandbox bypass or session fallback was attempted')
    if value is None:
        raise ValueError('Host startup returned without an available local browser socket')
    request(sock, dict(action='snapshot'), value['peer'])
    value['ready'] = True
    save_record(record_path, value)
    return value


def save_record(path, value):
    with private_file(path, create=True) as output:
        output.seek(0)
        json.dump(value, output)
        output.truncate()


def main():
    root, agent, instance, actor, session, mode, *args = sys.argv[1:]
    directory = runtime_directory()
    recovery = shlex.join([str(Path(__file__).resolve().with_name('ab-worktree')), agent, '--instance', instance, '--actor', actor, '--prepare'])
    try:
        if sys.platform != 'linux':
            raise ValueError('Prepared browsers are supported only on Linux')
        if not instance or not actor:
            raise ValueError('Prepared browser use requires both a stable instance and actor identity')
        for key, value in [('AWL_BROWSER_INSTANCE', instance), ('AWL_BROWSER_ACTOR', actor),
                           ('AWL_BROWSER_AGENT', agent), ('AWL_BROWSER_ROOT', root)]:
            if key in os.environ and os.environ[key] != value:
                raise ValueError(f'{key} mismatch: requested identity differs from the assigned browser')
        if mode == 'prepare' and os.environ.get('AWL_BROWSER_CONNECT_ONLY') == '1':
            raise ValueError('Agent environment is connect-only; preparation belongs to the host controller')
        if mode == 'prepare' and args:
            raise ValueError('--prepare does not accept a browser command')
        command = parse_command(args) if mode != 'prepare' else None
        private_directory(directory, create=mode == 'prepare')
        record_path = directory / (session + '.awl.json')
        sock = directory / (session + '.sock')
        if len(os.fsencode(sock)) >= 108:
            raise ValueError('Runtime socket path is too long; select a shorter private AWL_BROWSER_RUNTIME_DIR')
        identity = dict(root=root, agent=agent, instance=instance, actor=actor, session=session)
        actor_key = hashlib.sha256(b'\0'.join(os.fsencode(value) for value in (root, agent, actor)) + b'\0').hexdigest()[:32]
        actor_path = directory / ('actor-' + actor_key + '.json')
        with private_file(directory / ('actor-' + actor_key + '.lock'), create=mode == 'prepare') as actor_lock:
            fcntl.flock(actor_lock, fcntl.LOCK_EX)
            if actor_path.exists() or actor_path.is_symlink():
                with private_file(actor_path) as source:
                    assigned = ownership_record(source)
                if assigned != identity:
                    if isinstance(assigned, dict) and all(assigned.get(k) == identity[k] for k in ('root', 'agent', 'actor')):
                        recovery = shlex.join([str(Path(__file__).resolve().with_name('ab-worktree')), agent,
                                               '--instance', assigned['instance'], '--actor', actor, '--prepare'])
                    raise ValueError('Actor is already assigned to another instance; reuse its original assignment')
            elif mode != 'prepare':
                raise ValueError('Actor has no host-prepared instance assignment')
            with private_file(directory / (session + '.lock'), create=mode == 'prepare') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if mode == 'prepare':
                    prepare(identity, directory, record_path, sock, actor_path)
                    print(json.dumps(dict(identity, runtime_dir=str(directory))))
                else:
                    with private_file(record_path) as source:
                        prepared = ownership_record(source)
                    if not isinstance(prepared, dict) or prepared.get('identity') != identity:
                        raise ValueError('Prepared/requested browser identity mismatch')
                    if prepared.get('ready') is not True and command['action'] != 'close':
                        raise ValueError('Actor browser has not passed host preflight; preparation is required')
                    if command['action'] != 'close':
                        # Unlike ordinary actions, stream_status is in the
                        # pinned daemon's skip_launch list and probes Chrome
                        # with Browser.getVersion without enabling streaming.
                        status, _ = request(sock, dict(action='stream_status'), prepared['peer'], connect_only=True)
                        if not isinstance(status, dict) or status.get('connected') is not True:
                            raise ValueError('Prepared browser is unavailable; host preparation is required')
                    data, _ = request(sock, command, prepared['peer'], connect_only=True)
                    if isinstance(data, dict) and 'snapshot' in data:
                        print(data['snapshot'])
                    else:
                        print(json.dumps(data, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if isinstance(error, OSError):
            error = f'{error}; verify host preparation and sandbox access to the exact runtime mount and ownership files'
        print(f'ab-worktree: instance {instance or "<missing>"}, actor {actor or "<missing>"}: {error}. '
              f'Host recovery (same checkout): AWL_BROWSER_RUNTIME_DIR={shlex.quote(str(directory))} {recovery}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

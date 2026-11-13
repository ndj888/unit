#!/usr/bin/env python3
"""Paired Unit/PHP 8.5 microbenchmark; run in an isolated Linux Docker job."""

import argparse
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import random
import re
import signal
import socket
import statistics
import subprocess
import tempfile
import time


HOST = '127.0.0.1'
PATH = '/cloud/remote.php/dav/files/synthetic'
QUERY = 'download=0&format=json'
TARGET = f'{PATH}?{QUERY}'
HEADERS = {
    'Host': 'cloud.example.test',
    'User-Agent': 'Synthetic-Unit-Bench/1',
    'Accept': 'application/json',
    'Accept-Language': 'en-US',
    'Cookie': 'bench_session=synthetic',
    'X-Forwarded-Proto': 'https',
    'X-Forwarded-For': '192.0.2.30',
    'X-Requested-With': 'XMLHttpRequest',
    'Connection': 'keep-alive',
}

PHP_PROBE = r'''<?php
if (isset($_GET['probe'])) {
    $p = $_SERVER['UNIT_REWRITTEN_PATH'] ?? null;
    $uri = $_SERVER['REQUEST_URI'] ?? null;
    $_SERVER['UNIT_REWRITTEN_PATH'] = '/changed-in-php';
    $_SERVER['REQUEST_URI'] = '/changed-in-php';
    $result = [
        'pid' => getmypid(),
        'version' => PHP_VERSION,
        'version_id' => PHP_VERSION_ID,
        'path' => $p,
        'uri' => $uri,
        'filter_available' => function_exists('filter_input'),
        'filter_path' => filter_input(INPUT_SERVER, 'UNIT_REWRITTEN_PATH'),
        'filter_uri' => filter_input(INPUT_SERVER, 'REQUEST_URI'),
        'opcache_enabled' => (bool) ini_get('opcache.enable'),
        'opcache_cached' => function_exists('opcache_is_script_cached')
            && opcache_is_script_cached(__FILE__),
        'opcache_validate_timestamps' => (bool) ini_get('opcache.validate_timestamps'),
        'auto_globals_jit' => (bool) ini_get('auto_globals_jit'),
    ];
    $body = json_encode($result, JSON_THROW_ON_ERROR);
    header('Content-Type: application/json');
    header('Content-Length: ' . strlen($body));
    echo $body;
    return;
}
'''

PHP_SERVERVARS = r'''
$server = $_SERVER;
if ($server['REQUEST_METHOD'] !== 'GET'
    || $server['REQUEST_URI'] !== '/cloud/remote.php/dav/files/synthetic?download=0&format=json'
    || $server['UNIT_REWRITTEN_PATH'] !== '/cloud/remote.php/dav/files/synthetic'
    || $server['QUERY_STRING'] !== 'download=0&format=json'
    || $server['HTTP_HOST'] !== 'cloud.example.test'
    || $server['HTTP_COOKIE'] !== 'bench_session=synthetic'
    || $server['HTTP_X_FORWARDED_PROTO'] !== 'https'
    || $server['HTTP_X_REQUESTED_WITH'] !== 'XMLHttpRequest'
    || !isset($server['SCRIPT_FILENAME'], $server['DOCUMENT_ROOT'], $server['PHP_SELF'])) {
    http_response_code(500);
    echo 'unexpected synthetic server variables';
    return;
}
'''

PHP_RESPONSE = r'''
header('Content-Type: text/plain');
header('Content-Length: 2');
header('X-Bench-Worker: ' . getmypid());
echo 'ok';
'''


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def topology(cpu):
    path = Path(f'/sys/devices/system/cpu/cpu{cpu}/topology')
    return read_text(path / 'physical_package_id'), read_text(path / 'core_id')


def select_cpus(explicit, allowed):
    if explicit is not None:
        try:
            cpus = tuple(int(v) for v in explicit.split(','))
        except ValueError as e:
            raise RuntimeError('--cpus must be ROUTER,WORKER,CLIENT') from e
        require(len(cpus) == 3 and set(cpus) <= allowed,
                f'--cpus must have three entries from allowed CPUs {sorted(allowed)}')
    else:
        require(len(allowed) >= 2, f'need at least two available CPUs: {sorted(allowed)}')
        groups = {}
        for cpu in sorted(allowed):
            core = topology(cpu)
            groups.setdefault(core if None not in core else (None, cpu), []).append(cpu)
        cores = list(groups.values())
        if len(cores) >= 3:
            cpus = (cores[0][0], cores[1][0], cores[2][0])
        elif len(cores) == 2:
            router_core = max(cores, key=len)
            worker_core = next(g for g in cores if g is not router_core)
            cpus = (router_core[0], worker_core[0], router_core[-1])
        else:
            router, worker, *other = sorted(allowed)
            cpus = (router, worker, other[0] if other else router)
    router, worker, client = cpus
    require(worker not in (router, client), 'the PHP worker needs its own logical CPU')
    return cpus


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def capture(argv):
    return subprocess.check_output(argv, stderr=subprocess.STDOUT, text=True, timeout=15).strip()


def inspect_build(path, source_sha):
    path = path.resolve()
    exe = path / 'sbin/unitd'
    modules = path / 'lib/unit/modules'
    php = sorted(modules.glob('php*.unit.so'))
    require(exe.is_file() and os.access(exe, os.X_OK) and len(php) == 1,
            f'{path}: expected build/sbin/unitd and exactly one php*.unit.so')
    version = capture([str(exe), '--version'])
    require('--debug' not in version and '-fsanitize=' not in version,
            f'{path}: use a release build without ASan/UBSan')
    linkage = capture(['ldd', str(php[0])])
    require('not found' not in linkage, f'{path}: unresolved PHP module dependency:\n{linkage}')
    lib = re.findall(r'^\s*libphp[^\s]*\s+=>\s+(\S+)', linkage, re.MULTILINE)
    require(len(lib) == 1 and Path(lib[0]).is_file(), f'{path}: could not prove libphp linkage')
    embed = Path(lib[0]).resolve()
    return {'source_sha': source_sha, 'build': str(path), 'unitd': str(exe),
            'modules': str(modules), 'unitd_sha256': sha256(exe),
            'module': str(php[0]), 'module_sha256': sha256(php[0]),
            'libphp': str(embed), 'libphp_sha256': sha256(embed), 'version': version}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=6)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.close()
            raise


def configure(control, port, app_root, timestamps):
    config = {
        'listeners': {f'{HOST}:{port}': {'pass': 'applications/bench'}},
        'applications': {'bench': {
            'type': 'php', 'processes': 1,
            'root': str(app_root), 'working_directory': str(app_root),
            'script': 'index.php',
            'options': {'admin': {
                'opcache.enable': '1', 'opcache.validate_timestamps': str(int(timestamps)),
                'opcache.revalidate_freq': '2', 'opcache.file_update_protection': '0',
                'opcache.revalidate_path': '0', 'opcache.restrict_api': '',
                'opcache.jit': 'disable', 'opcache.jit_buffer_size': '0',
                'auto_globals_jit': '1', 'display_errors': '0', 'expose_php': '0',
            }},
        }},
    }
    with contextlib.closing(UnixHTTPConnection(control)) as conn:
        conn.request('PUT', '/config', json.dumps(config), {'Content-Type': 'application/json'})
        response = conn.getresponse()
        body = response.read()
        require(response.status == 200 and 'success' in json.loads(body),
                f'Unit rejected synthetic config: HTTP {response.status}: {body!r}')


def request(conn, target):
    sock = conn.sock
    conn.request('GET', target, headers=HEADERS)
    response = conn.getresponse()
    body = response.read()
    require(response.status == 200, f'{target}: HTTP {response.status}, body={body[:256]!r}')
    require(not response.will_close and conn.sock is not None and (sock is None or conn.sock is sock),
            f'{target}: server closed or reconnected the single keep-alive socket')
    return response, body


def probe(conn, timestamps, group_pid, path=PATH, url=None):
    target = url or f'{TARGET}&probe=1'
    _, body = request(conn, target)
    data = json.loads(body)
    require(data['path'] == path and data['uri'] == target,
            f'path/URI compatibility failed for {target}: {data!r}')
    require(data['filter_available'] and data['filter_path'] == path and data['filter_uri'] == target,
            f'filter_input(INPUT_SERVER) snapshot changed: {data!r}')
    require(80500 <= data['version_id'] < 80600 and data['auto_globals_jit'],
            f'expected PHP 8.5 and auto_globals_jit: {data!r}')
    require(data['opcache_enabled'] and data['opcache_validate_timestamps'] is timestamps,
            f'OPcache settings mismatch: {data!r}')
    pid = data['pid']
    require(type(pid) is int and pid > 1 and os.getpgid(pid) == group_pid,
            f'PHP PID {pid!r} is not in the private Unit process group {group_pid}')
    return data


def schedstat(pid):
    # /proc/PID/stat's second field may contain spaces or parentheses.
    # The suffix after the last ')' starts at field 3; starttime is field 22.
    root = Path(f'/proc/{pid}')
    start = int((root / 'stat').read_text().rsplit(')', 1)[1].split()[19])
    values = tuple(map(int, (root / 'schedstat').read_text().split()[:3]))
    require(len(values) == 3, f'could not read schedstat for PHP worker {pid}')
    return start, values


def measured_request(conn, pid):
    response, body = request(conn, TARGET)
    require(body == b'ok' and response.getheader('X-Bench-Worker') == str(pid),
            f'wrong PHP response or a different worker: {response.getheader("X-Bench-Worker")}, {body[:256]!r}')


def percentile(values, p):
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (index - lower) * (ordered[upper] - ordered[lower])


def stop_unit(process):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def log_tail(path):
    text = read_text(path)
    return '\n'.join(text.splitlines()[-20:])[-3500:] if text else ''


def trial(build, tmp, app_root, timestamps, cpus, args):
    router_cpu, worker_cpu, _ = cpus
    control = tmp / 'control.sock'
    state = tmp / 'state'
    state.mkdir()
    unit_log = tmp / 'unit.log'
    console = tmp / 'console.log'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free_port:
        free_port.bind((HOST, 0))
        port = free_port.getsockname()[1]

    cmd = [build['unitd'], '--no-daemon', '--modulesdir', build['modules'],
           '--statedir', str(state), '--pid', str(tmp / 'unit.pid'),
           '--log', str(unit_log), '--control', f'unix:{control}',
           '--tmpdir', str(tmp), '--user', 'nobody']
    with console.open('wb') as output:
        # The driver has no threads. Pin all daemon descendants on fork;
        # migrate only the verified PHP worker before warm-up.
        process = subprocess.Popen(cmd, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True,
                                   preexec_fn=lambda: os.sched_setaffinity(0, {router_cpu}))
    try:
        deadline = time.monotonic() + 10
        while not control.exists():
            require(process.poll() is None, f'Unit exited during startup: {process.returncode}')
            require(time.monotonic() < deadline, 'private Unit control socket did not appear')
            time.sleep(0.02)
        configure(control, port, app_root, timestamps)

        with contextlib.closing(http.client.HTTPConnection(HOST, port, timeout=6)) as conn:
            initial = probe(conn, timestamps, process.pid)
            pid = initial['pid']
            try:
                os.sched_setaffinity(pid, {worker_cpu})
            except PermissionError as e:
                raise RuntimeError('pinning the nobody PHP worker needs CAP_SYS_NICE in the benchmark container') from e
            require(os.sched_getaffinity(pid) == {worker_cpu}, 'PHP worker affinity was not applied')
            normalized = probe(conn, timestamps, process.pid, path='/cloud/a b',
                               url='/cloud/a%20b?x=1&probe=1')
            require(normalized['pid'] == pid, 'worker changed on the normalized-path check')
            for _ in range(args.warmup):
                measured_request(conn, pid)
            hot = probe(conn, timestamps, process.pid)
            require(hot['pid'] == pid and hot['opcache_cached'], 'warm-up did not establish a hot OPcache worker')

            latencies = []
            worker_start, before = schedstat(pid)
            wall_start = time.perf_counter_ns()
            for _ in range(args.requests):
                req_start = time.perf_counter_ns()
                measured_request(conn, pid)
                latencies.append(time.perf_counter_ns() - req_start)
            wall_ns = time.perf_counter_ns() - wall_start
            after_start, after = schedstat(pid)
            final = probe(conn, timestamps, process.pid)
            require(after_start == worker_start and final['pid'] == pid and final['opcache_cached'],
                    'worker restarted or lost its hot script during measurement')
            require(os.sched_getaffinity(pid) == {worker_cpu}, 'PHP worker affinity changed during measurement')
            cpu_ns, wait_ns, slices = (b - a for a, b in zip(before, after))
            require(cpu_ns > 0 and wait_ns >= 0 and slices >= 0 and wall_ns > 0,
                    f'invalid schedstat delta: before={before}, after={after}')
            return {'worker_pid': pid, 'worker_start_ticks': worker_start,
                    'requests': args.requests, 'warmup_requests': args.warmup,
                    'worker_cpu_ns': cpu_ns, 'worker_runqueue_wait_ns': wait_ns,
                    'worker_timeslices': slices, 'wall_ns': wall_ns,
                    'worker_cpu_ns_per_req': cpu_ns / args.requests,
                    'rps': args.requests * 1e9 / wall_ns,
                    'p50_us': percentile(latencies, .50) / 1000,
                    'p95_us': percentile(latencies, .95) / 1000,
                    'opcache_and_filter_probe': hot}
    except Exception as e:
        raise RuntimeError(f'{e}\nUnit log:\n{log_tail(unit_log)}\nConsole:\n{log_tail(console)}') from e
    finally:
        stop_unit(process)


METRICS = ('worker_cpu_ns_per_req', 'rps', 'p50_us', 'p95_us')


def distribution(values):
    middle = statistics.median(values)
    return {'median': middle, 'mean': statistics.mean(values),
            'stdev': statistics.stdev(values), 'variance': statistics.variance(values),
            'mad': statistics.median(abs(v - middle) for v in values),
            'min': min(values), 'max': max(values)}


def summarize(rows, seed):
    rng = random.Random(seed)
    out = {}
    for scenario in sorted({row['scenario'] for row in rows}):
        pairs = [row for row in rows if row['scenario'] == scenario]
        if len(pairs) < 2:
            continue
        summary = {'pairs': len(pairs), 'orders': {o: sum(row['order'] == o for row in pairs) for o in ('AB', 'BA')}}
        for metric in METRICS:
            a = [row['samples']['A'][metric] for row in pairs]
            b = [row['samples']['B'][metric] for row in pairs]
            changes = [(candidate / baseline - 1) * 100 for baseline, candidate in zip(a, b)]
            boot = [statistics.median(rng.choices(changes, k=len(changes))) for _ in range(2000)]
            summary[metric] = {'baseline': distribution(a), 'candidate': distribution(b),
                               'paired_candidate_minus_baseline_percent': distribution(changes),
                               'paired_bootstrap_95pct': [percentile(boot, .025), percentile(boot, .975)],
                               'paired_percent_by_order': {
                                   order: statistics.median(values)
                                   for order in ('AB', 'BA')
                                   if (values := [v for row, v in zip(pairs, changes) if row['order'] == order])}}
        out[scenario] = summary
    return out


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-build', required=True, type=Path)
    parser.add_argument('--candidate-build', required=True, type=Path)
    parser.add_argument('--baseline-sha', default='2c1aaa01d7a631c733cc939d827cbf07d1a04b93')
    parser.add_argument('--candidate-sha')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cpus', help='ROUTER,WORKER,CLIENT CPU IDs; default selects from process affinity')
    parser.add_argument('--pairs', type=int, default=8, help='even pair count per scenario; default: 8')
    parser.add_argument('--requests', type=int, default=25000, help='measured requests per side per pair')
    parser.add_argument('--warmup', type=int, default=2500, help='warm-up requests per side per pair')
    parser.add_argument('--seed', type=int, default=829)
    args = parser.parse_args()
    if args.pairs < 4 or args.pairs % 2 or args.requests < 1000 or args.warmup < 1:
        parser.error('--pairs must be even and >=4; --requests >=1000 and --warmup >=1')
    return args


def main():
    args = parse_args()
    require(platform.system() == 'Linux' and hasattr(os, 'sched_getaffinity'),
            'run in the isolated Linux Docker CI environment')
    a = inspect_build(args.baseline_build, args.baseline_sha)
    b = inspect_build(args.candidate_build, args.candidate_sha)
    require((a['libphp'], a['libphp_sha256']) == (b['libphp'], b['libphp_sha256']),
            'A and B are not linked to the same PHP embed library')
    require(a['version'] == b['version'],
            'A and B unitd --version differs; use identical release version and configure options')
    allowed = os.sched_getaffinity(0)
    cpus = select_cpus(args.cpus, allowed)
    os.sched_setaffinity(0, {cpus[2]})
    rng = random.Random(args.seed)
    scenarios = [(f'{profile}-timestamps-{label}', profile, timestamps)
                 for profile in ('servervars', 'light')
                 for label, timestamps in (('on', True), ('off', False))]
    orders = {name: ['AB', 'BA'] * (args.pairs // 2) for name, _, _ in scenarios}
    for order in orders.values():
        rng.shuffle(order)
    report = {'status': 'running', 'seed': args.seed, 'pairs_per_scenario': args.pairs,
              'requests_per_sample': args.requests, 'warmup_per_sample': args.warmup,
              'builds': {'A': a, 'B': b}, 'rows': [],
              'environment': {'platform': platform.platform(), 'python': platform.python_version(),
                              'allowed_cpus': sorted(allowed),
                              'cpus': dict(zip(('router', 'worker', 'client'), cpus)),
                              'physical_core': dict(zip(('router', 'worker', 'client'), map(topology, cpus))),
                              'cpu_max': read_text('/sys/fs/cgroup/cpu.max'),
                              'cpuset': read_text('/sys/fs/cgroup/cpuset.cpus.effective')},
              'interpretation': 'Primary: PHP worker CPU ns/request (lower better). RPS is higher better. '
                                'Percent is paired 100*(candidate/baseline-1). Synthetic localhost; '
                                'shared-runner noise and actual app/DB throughput are outside scope.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')

    try:
        with tempfile.TemporaryDirectory(prefix='unit-php-ab-', dir='/tmp') as root:
            root = Path(root)
            root.chmod(0o755)
            for profile, content in (('servervars', PHP_SERVERVARS), ('light', '')):
                app_root = root / profile
                app_root.mkdir(mode=0o755)
                (app_root / 'index.php').write_text(PHP_PROBE + content + PHP_RESPONSE)
            trial_no = 0
            for pair in range(args.pairs):
                wave = scenarios.copy()
                rng.shuffle(wave)
                for name, profile, timestamps in wave:
                    order = orders[name][pair]
                    row = {'scenario': name, 'pair': pair + 1, 'order': order, 'samples': {}}
                    report['rows'].append(row)
                    for side in order:
                        trial_no += 1
                        with tempfile.TemporaryDirectory(prefix=f't{trial_no:03d}-', dir=root) as tmp:
                            row['samples'][side] = trial({'A': a, 'B': b}[side], Path(tmp),
                                                         root / profile, timestamps, cpus, args)
                    av = row['samples']['A']['worker_cpu_ns_per_req']
                    bv = row['samples']['B']['worker_cpu_ns_per_req']
                    print(f'{name} pair {pair + 1}/{args.pairs} {order}: '
                          f'A {av:.2f}, B {bv:.2f} worker ns/req ({(bv/av-1)*100:+.2f}%)', flush=True)
                    save()
            require(len({sample['opcache_and_filter_probe']['version']
                         for row in report['rows'] for sample in row['samples'].values()}) == 1,
                    'the PHP worker runtime version changed across samples')
            report['summary'] = summarize(report['rows'], args.seed)
            report['status'] = 'passed'
    except BaseException as e:
        report['status'] = 'failed'
        report['failure'] = str(e)
        raise
    finally:
        save()

    for scenario, values in report['summary'].items():
        cpu = values['worker_cpu_ns_per_req']
        pct = cpu['paired_candidate_minus_baseline_percent']
        ci = cpu['paired_bootstrap_95pct']
        print(f'{scenario}: A {cpu["baseline"]["median"]:.2f}, B {cpu["candidate"]["median"]:.2f} ns/req; '
              f'paired median {pct["median"]:+.2f}%, SD {pct["stdev"]:.2f}pp, '
              f'bootstrap 95% [{ci[0]:+.2f}%, {ci[1]:+.2f}%]', flush=True)


if __name__ == '__main__':
    def terminate(_signum, _frame):
        raise KeyboardInterrupt('benchmark terminated')

    signal.signal(signal.SIGTERM, terminate)
    main()

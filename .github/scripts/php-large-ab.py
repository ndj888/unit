#!/usr/bin/env python3
"""Compare large PHP responses on paired Unit builds in isolated Linux Docker CI."""

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

PHP_DISPATCH = r'''<?php
if (isset($_GET['probe'])) {
    require __DIR__ . '/probe.php';
    return;
}
'''

PHP_PROBE = r'''<?php
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
        && opcache_is_script_cached(__DIR__ . '/index.php'),
    'opcache_validate_timestamps' => (bool) ini_get('opcache.validate_timestamps'),
    'auto_globals_jit' => (bool) ini_get('auto_globals_jit'),
    'output_buffering' => (int) ini_get('output_buffering'),
    'zlib_output_compression' => (bool) ini_get('zlib.output_compression'),
];
$body = json_encode($result, JSON_THROW_ON_ERROR);
header('Content-Type: application/json');
header('Content-Length: ' . strlen($body));
echo $body;
'''

RESPONSE_SIZES = (16 * 1024, 256 * 1024, 1024 * 1024)

PHP_RESPONSE = r'''
if (filter_input(INPUT_SERVER, 'UNIT_REWRITTEN_PATH') !== '/cloud/remote.php/dav/files/synthetic'
    || filter_input(INPUT_SERVER, 'REQUEST_URI') !== '/cloud/remote.php/dav/files/synthetic?download=0&format=json') {
    http_response_code(500);
    echo 'unexpected PHP filter input';
    return;
}
header('Content-Type: application/octet-stream');
header('Content-Length: __BODY_SIZE__');
header('X-Bench-Worker: ' . getmypid());
echo '__BODY__';
'''


def create_fixture(root, size):
    # ASCII bytes make the literal PHP-safe. A distinct deterministic byte
    # sequence also catches out-of-order, missing and duplicated chunks.
    body = random.Random(size).randbytes(size // 2).hex().encode('ascii')
    script = PHP_RESPONSE.replace('__BODY_SIZE__', str(size)).replace('__BODY__', body.decode('ascii'))
    root.mkdir(mode=0o755)
    (root / 'index.php').write_text(PHP_DISPATCH + script, encoding='ascii')
    (root / 'probe.php').write_text(PHP_PROBE, encoding='ascii')
    return body


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


def configure(control, port, app_root):
    config = {
        'listeners': {f'{HOST}:{port}': {'pass': 'applications/bench'}},
        'applications': {'bench': {
            'type': 'php', 'processes': 1,
            'root': str(app_root), 'working_directory': str(app_root),
            'script': 'index.php',
            'options': {'admin': {
                'opcache.enable': '1', 'opcache.validate_timestamps': '0',
                'opcache.revalidate_freq': '2', 'opcache.file_update_protection': '0',
                'opcache.revalidate_path': '0', 'opcache.restrict_api': '',
                'opcache.jit': 'disable', 'opcache.jit_buffer_size': '0',
                'auto_globals_jit': '1', 'display_errors': '0', 'expose_php': '0',
                'output_buffering': '0', 'zlib.output_compression': '0',
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


def probe(conn, group_pid, path=PATH, url=None):
    target = url or f'{TARGET}&probe=1'
    _, body = request(conn, target)
    data = json.loads(body)
    require(data['path'] == path and data['uri'] == target,
            f'path/URI compatibility failed for {target}: {data!r}')
    require(data['filter_available'] and data['filter_path'] == path and data['filter_uri'] == target,
            f'filter_input(INPUT_SERVER) snapshot changed: {data!r}')
    require(80500 <= data['version_id'] < 80600 and data['auto_globals_jit'],
            f'expected PHP 8.5 and auto_globals_jit: {data!r}')
    require(data['opcache_enabled'] and not data['opcache_validate_timestamps'],
            f'OPcache settings mismatch: {data!r}')
    require(data['output_buffering'] == 0 and not data['zlib_output_compression'],
            f'PHP output buffering or compression is enabled: {data!r}')
    pid = data['pid']
    require(type(pid) is int and pid > 1 and os.getpgid(pid) == group_pid,
            f'PHP PID {pid!r} is not in the private Unit process group {group_pid}')
    return data


def router_pid(group_pid):
    routers = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        pid = int(proc.name)
        try:
            if os.getpgid(pid) != group_pid:
                continue
            title = (proc / 'cmdline').read_bytes().split(b'\0', 1)[0].strip()
        except (ProcessLookupError, FileNotFoundError):
            continue
        if title == b'unit: router':
            routers.append(pid)
    require(len(routers) == 1, f'expected one router in Unit process group {group_pid}: {routers}')
    return routers[0]


def schedstat(pid, cpu):
    # Include all threads. Linux /proc/PID/schedstat covers only the leader.
    threads = {}
    for task in Path(f'/proc/{pid}/task').iterdir():
        tid = int(task.name)
        require(os.sched_getaffinity(tid) == {cpu},
                f'PID {pid} TID {tid} did not stay pinned to CPU {cpu}')
        # The suffix after the last ')' starts at field 3. Starttime is field 22.
        start = int((task / 'stat').read_text().rsplit(')', 1)[1].split()[19])
        values = tuple(map(int, (task / 'schedstat').read_text().split()))
        require(len(values) == 3, f'could not read schedstat for PID {pid} TID {tid}')
        threads[tid] = (start, values)
    require(pid in threads, f'process leader {pid} missing from schedstat snapshot')
    return threads


def schedstat_delta(role, pid, before, after, requests):
    require(before.keys() == after.keys(), f'{role} thread set changed: {before.keys()} -> {after.keys()}')
    deltas = []
    for tid, (start, counts) in before.items():
        new_start, new_counts = after[tid]
        require(start == new_start, f'{role} TID {tid} changed identity')
        delta = tuple(end - begin for begin, end in zip(counts, new_counts))
        require(min(delta) >= 0, f'{role} TID {tid} schedstat decreased: {delta}')
        deltas.append(delta)
    cpu_ns, wait_ns, slices = map(sum, zip(*deltas))
    require(cpu_ns > 0, f'{role} PID {pid} recorded zero scheduler CPU time')
    return {f'{role}_pid': pid, f'{role}_thread_start_ticks': {tid: start for tid, (start, _) in before.items()},
            f'{role}_cpu_ns': cpu_ns, f'{role}_runqueue_wait_ns': wait_ns,
            f'{role}_timeslices': slices, f'{role}_cpu_ns_per_req': cpu_ns / requests}


def measured_request(conn, pid, expected):
    response, body = request(conn, TARGET)
    worker = response.getheader('X-Bench-Worker')
    length = response.getheader('Content-Length')
    require(worker == str(pid), f'PHP worker changed: expected {pid}, got {worker}')
    require(length == str(len(expected)) and len(body) == len(expected),
            f'response length changed: header={length}, actual={len(body)}, expected={len(expected)}')
    if body != expected:
        raise RuntimeError(f'wrong response bytes: SHA256 {hashlib.sha256(body).hexdigest()}')


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


def trial(build, tmp, app_root, body, cpus, args):
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
        configure(control, port, app_root)

        with contextlib.closing(http.client.HTTPConnection(HOST, port, timeout=6)) as conn:
            router = router_pid(process.pid)
            initial = probe(conn, process.pid)
            pid = initial['pid']
            require(pid != router, 'PHP worker and router must be different processes')
            try:
                os.sched_setaffinity(pid, {worker_cpu})
            except PermissionError as e:
                raise RuntimeError('pinning the nobody PHP worker needs CAP_SYS_NICE in the benchmark container') from e
            require(os.sched_getaffinity(pid) == {worker_cpu}, 'PHP worker affinity was not applied')
            normalized = probe(conn, process.pid, path='/cloud/a b',
                               url='/cloud/a%20b?x=1&probe=1')
            require(normalized['pid'] == pid, 'worker changed on the normalized-path check')
            for _ in range(args.warmup):
                measured_request(conn, pid, body)
            hot = probe(conn, process.pid)
            require(hot['pid'] == pid and hot['opcache_cached'], 'warm-up did not establish a hot OPcache worker')

            latencies = []
            router_before = schedstat(router, router_cpu)
            worker_before = schedstat(pid, worker_cpu)
            wall_start = time.perf_counter_ns()
            for _ in range(args.requests):
                req_start = time.perf_counter_ns()
                measured_request(conn, pid, body)
                latencies.append(time.perf_counter_ns() - req_start)
            wall_ns = time.perf_counter_ns() - wall_start
            # Read both CPU counters before the final JSON probe can add work.
            worker_after = schedstat(pid, worker_cpu)
            router_after = schedstat(router, router_cpu)
            final = probe(conn, process.pid)
            require(final['pid'] == pid and final['opcache_cached'],
                    'worker restarted or lost its hot script during measurement')
            require(router_pid(process.pid) == router, 'router changed during measurement')
            worker = schedstat_delta('worker', pid, worker_before, worker_after, args.requests)
            router_stats = schedstat_delta('router', router, router_before, router_after, args.requests)
            total = worker['worker_cpu_ns'] + router_stats['router_cpu_ns']
            return {**worker, **router_stats, 'total_cpu_ns': total,
                    'total_cpu_ns_per_req': total / args.requests,
                    'requests': args.requests, 'warmup_requests': args.warmup,
                    'bytes_per_response': len(body), 'verified_timed_bytes': len(body) * args.requests,
                    'wall_ns': wall_ns, 'rps': args.requests * 1e9 / wall_ns,
                    'p50_us': percentile(latencies, .50) / 1000,
                    'p95_us': percentile(latencies, .95) / 1000,
                    'opcache_and_filter_probe': hot}
    except Exception as e:
        raise RuntimeError(f'{e}\nUnit log:\n{log_tail(unit_log)}\nConsole:\n{log_tail(console)}') from e
    finally:
        stop_unit(process)


CPU_METRICS = ('router_cpu_ns_per_req', 'worker_cpu_ns_per_req', 'total_cpu_ns_per_req')
METRICS = CPU_METRICS + ('rps', 'p50_us', 'p95_us')


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
    parser.add_argument('--requests', type=int, default=800, help='measured requests per side per pair')
    parser.add_argument('--sizes', type=int, nargs='+', choices=RESPONSE_SIZES,
                        default=RESPONSE_SIZES, help='response sizes in bytes')
    parser.add_argument('--trial-dir', type=Path, default=Path('/dev/shm'),
                        help='scratch directory for isolated processes and fixtures')
    parser.add_argument('--warmup', type=int, default=100, help='warm-up requests per side per pair')
    parser.add_argument('--seed', type=int, default=829)
    args = parser.parse_args()
    if args.pairs < 4 or args.pairs % 2 or args.requests < 600 or args.warmup < 1:
        parser.error('--pairs must be even and >=4; --requests >=600 and --warmup >=1')
    if len(set(args.sizes)) != len(args.sizes):
        parser.error('--sizes entries must be unique')
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
    scenarios = {f'{size // 1024}KiB': size for size in args.sizes}
    orders = {name: ['AB', 'BA'] * (args.pairs // 2) for name in scenarios}
    for order in orders.values():
        rng.shuffle(order)
    report = {'status': 'running', 'seed': args.seed, 'pairs_per_scenario': args.pairs,
              'requests_per_sample': args.requests, 'warmup_per_sample': args.warmup,
              'builds': {'A': a, 'B': b}, 'rows': [], 'fixtures': {},
              'environment': {'platform': platform.platform(), 'python': platform.python_version(),
                              'allowed_cpus': sorted(allowed),
                              'cpus': dict(zip(('router', 'worker', 'client'), cpus)),
                              'physical_core': dict(zip(('router', 'worker', 'client'), map(topology, cpus))),
                              'cpu_max': read_text('/sys/fs/cgroup/cpu.max'),
                              'cpuset': read_text('/sys/fs/cgroup/cpuset.cpus.effective')},
              'interpretation': 'Primary: router CPU ns/request (lower better). Total CPU sums '
                                'router and PHP worker threads. RPS is higher better. Percent is '
                                'paired 100*(candidate/baseline-1). Each serial keep-alive request '
                                'checks response size and bytes. Synthetic localhost PHP responses.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')

    try:
        with tempfile.TemporaryDirectory(prefix='unit-php-large-ab-', dir=args.trial_dir) as root:
            root = Path(root)
            root.chmod(0o755)
            bodies = {name: create_fixture(root / name, size) for name, size in scenarios.items()}
            report['fixtures'] = {
                name: {'bytes_per_response': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                for name, body in bodies.items()
            }
            trial_no = 0
            for pair in range(args.pairs):
                wave = list(scenarios)
                rng.shuffle(wave)
                for name in wave:
                    order = orders[name][pair]
                    row = {'scenario': name, 'pair': pair + 1, 'order': order, 'samples': {}}
                    report['rows'].append(row)
                    for side in order:
                        trial_no += 1
                        with tempfile.TemporaryDirectory(prefix=f't{trial_no:03d}-', dir=root) as tmp:
                            row['samples'][side] = trial({'A': a, 'B': b}[side], Path(tmp),
                                                         root / name, bodies[name], cpus, args)
                    changes = ' '.join(
                        f'{role} {(row["samples"]["B"][metric] / row["samples"]["A"][metric] - 1) * 100:+.2f}%'
                        for role, metric in zip(('router', 'worker', 'total'), CPU_METRICS)
                    )
                    print(f'{name} pair {pair + 1}/{args.pairs} {order}: {changes}', flush=True)
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
        for metric in METRICS:
            result = values[metric]
            pct = result['paired_candidate_minus_baseline_percent']
            low, high = result['paired_bootstrap_95pct']
            print(f'{scenario} {metric}: A {result["baseline"]["median"]:.2f}, '
                  f'B {result["candidate"]["median"]:.2f}; paired median {pct["median"]:+.2f}%, '
                  f'SD {pct["stdev"]:.2f}pp, bootstrap 95% [{low:+.2f}%, {high:+.2f}%]', flush=True)


if __name__ == '__main__':
    def terminate(_signum, _frame):
        raise KeyboardInterrupt('benchmark terminated')

    signal.signal(signal.SIGTERM, terminate)
    main()

"""Storage audit regressions: temporary state and synthetic operational effects only."""
import os
import json
import sqlite3
import threading
import tarfile
import weakref
import asyncio
import time
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from src.tests import _isolation
_isolation.isolate()
import pytest
from src import config, state_db, updater, log_db
from src.state_store import StateStore

@pytest.fixture(autouse=True)
def lifecycle():
    from src import update_checker, drain
    updater.start()
    update_checker.start()
    drain.reset_for_tests()
    yield
    updater.start()
    update_checker.start()
    drain.reset_for_tests()


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = StateStore(str(tmp_path / 'runtime.json'), str(tmp_path / 'durable.json'))
    s.start()
    monkeypatch.setattr(state_db, '_store', s)
    yield s
    s.close()

@pytest.mark.parametrize('coordinated', [False, True])
def test_affinity_rename_preserves_persisted_rows(store, coordinated, monkeypatch):
    state_db.affinity_upsert('fingerprint', 'api:old', 'model', prompt_cache_key='cache-preserved')
    state_db.client_affinity_upsert('client', 'api:old', 'model')
    if coordinated:
        state_db.rename_runtime_channel_state('api:old', 'api:new')
    else:
        state_db.affinity_rename_channel('api:old', 'api:new')
        state_db.client_affinity_rename_channel('api:old', 'api:new')
    assert state_db.affinity_load('fingerprint')['channel_key'] == 'api:new'
    assert state_db.affinity_load('fingerprint')['prompt_cache_key'] == 'cache-preserved'
    assert len(state_db.client_affinity_load_all()) == 1
    assert state_db.client_affinity_load_all()[0]['channel_key'] == 'api:new'
    state_db.flush(strict=True)
    assert len(StateStore.read_snapshot(store._paths['runtime'], 'runtime')[1]['cache_affinities']) == 1
    store.close()
    reopened = StateStore(store._paths['runtime'], store._paths['durable'])
    reopened.start()
    monkeypatch.setattr(state_db, '_store', reopened)
    try:
        assert state_db.affinity_load('fingerprint')['channel_key'] == 'api:new'
        assert state_db.affinity_load('fingerprint')['prompt_cache_key'] == 'cache-preserved'
        assert len(state_db.client_affinity_load_all()) == 1
    finally:
        reopened.close()


def test_legacy_metadata_touch_preserves_new_durable_state(tmp_path, monkeypatch):
    legacy = tmp_path / 'legacy.db'
    with sqlite3.connect(legacy) as c:
        c.execute('CREATE TABLE performance_stats(channel_key TEXT, model TEXT, value INTEGER)')
        c.execute("INSERT INTO performance_stats VALUES('api:old', 'model', 1)")
    paths = {'stateDbPath': str(legacy), 'runtimeStatePath': str(tmp_path/'runtime.json'),
             'durableStatePath': str(tmp_path/'durable.json')}
    monkeypatch.setattr(config, 'get', lambda: paths)
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(state_db, '_store', None)
    state_db.init()
    state_db.workbuddy_action_begin('synthetic-intent', {'owner':'owner', 'attempt_id':'attempt', 'action':'checkin'})
    assert state_db.workbuddy_action_load('synthetic-intent') is not None
    state_db.close()
    contents = legacy.read_bytes()
    st = legacy.stat()
    os.utime(legacy, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert legacy.read_bytes() == contents
    try:
        state_db.init()
        assert state_db.workbuddy_action_load('synthetic-intent') is not None
        assert state_db.migration_report()['status'] == 'unchanged'
        state_db.close()
        with sqlite3.connect(legacy) as c:
            c.execute('UPDATE performance_stats SET value=2')
        state_db.init()
        assert state_db.migration_report()['status'] == 'healthy'
        assert state_db.perf_load_all()[0]['value'] == 2
        assert state_db.workbuddy_action_load('synthetic-intent') is not None
    finally:
        state_db.close()


def test_pruning_preserves_newest_backup_at_version_digit_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, '_backup_root', lambda: str(tmp_path))
    monkeypatch.setattr(updater, '_cfg', lambda: {'keepBackups': 1})
    old = 'src-0.9.0-20260928-120000'
    newest = 'src-0.10.0-20260929-120000'
    for name in [old, newest]:
        (tmp_path/(name+'.json')).write_text(json.dumps({'ref':name}))
        (tmp_path/(name+'.tar.gz')).write_bytes(b'synthetic archive')
    updater._prune_backups()
    assert not (tmp_path/(old+'.json')).exists()
    assert (tmp_path/(newest+'.json')).exists()
    assert (tmp_path/(newest+'.tar.gz')).exists()


@pytest.mark.parametrize('phase', ['restarting', 'verifying'])
@pytest.mark.parametrize('result,expected', [('ROLLBACK','rolled_back'), ('ROLLBACK_FAILED','failed')])
def test_verifying_state_resumes(store, monkeypatch, phase, result, expected):
    state_db.updater_save({'stage':phase, 'mode':'docker', 'target_tag':'v999.0.0'})
    monkeypatch.setattr(updater, '_read_update_result', lambda **kw: result)
    monkeypatch.setattr(updater, '_notify_cross_process', lambda *a, **kw: None)
    updater.resume_after_restart()
    updater._resume_thread.join(2)
    assert not updater._resume_thread.is_alive()
    assert state_db.updater_load()['stage'] == expected


@pytest.mark.parametrize('mode', ['systemd', 'docker'])
def test_healthy_wrong_version_is_not_success(store, monkeypatch, mode):
    state_db.updater_save({'stage':'restarting', 'mode':mode, 'from_version':'0.1.0', 'target_tag':'v999.0.0'})
    monkeypatch.setattr(updater, '_cfg', lambda: {'healthTimeoutSeconds':1})
    monkeypatch.setattr(updater, 'wait_healthy', lambda timeout: (True, 'synthetic health 200'))
    monkeypatch.setattr(updater, '_notify_cross_process', lambda *a, **kw: None)
    monkeypatch.setattr(updater, '_src_rollback', lambda *a: (False, 'synthetic missing backup'))
    monkeypatch.setattr(updater, '_docker_rollback', lambda *a: (False, 'synthetic missing backup'))
    monkeypatch.setattr(updater, '_read_update_result', lambda **kw: 'OK')
    updater.resume_after_restart()
    updater._resume_thread.join(2)
    assert state_db.updater_load()['stage'] == 'failed'


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(log_db, '_log_dir', str(tmp_path))
    local = threading.local()
    registry = {}
    monkeypatch.setattr(log_db, '_local', local)
    monkeypatch.setattr(log_db, '_write_conn_registry', registry)
    monkeypatch.setattr(log_db, '_retired_log_paths', set())
    monkeypatch.setattr(log_db, '_request_handles', weakref.WeakValueDictionary())
    yield tmp_path
    for conns in registry.values():
        for c in conns:
            try: c.close()
            except Exception: pass


def test_stale_cleanup_preserves_live_request_handle(logs):
    import time
    h = log_db.insert_pending('live', '127.0.0.1', 'synthetic', 'model', True, 1, 0, {}, {}, created_at=time.time()-1900)
    assert log_db._request_handles.get('live') == h
    assert log_db.cleanup_stale_pending(1800) == 0
    assert log_db._request_handles['live'] is h
    c = sqlite3.connect(h.db.path)
    try:
        assert c.execute("SELECT status,error_message FROM request_log WHERE request_id='live'").fetchone() == ('pending', None)
    finally: c.close()


def test_boundary_retention_preserves_live_root(logs):
    import time
    now = time.time()
    h = log_db.insert_pending('live-root', '127.0.0.1', 'synthetic', 'model', True, 1, 0, {}, {}, created_at=now-2*86400)
    with log_db._write_lock:
        result = log_db._trim_retention_month({'path':h.db.path, 'month':h.db.month, 'cutoff':now-86400})
    assert result['ok']
    assert result['deleted_requests'] == 0
    assert log_db._request_handles.get('live-root') == h
    c = sqlite3.connect(h.db.path)
    try:
        assert c.execute('SELECT count(*) FROM request_log').fetchone()[0] == 1
    finally: c.close()


def test_source_archive_and_fallback_exclude_live_data(tmp_path, monkeypatch):
    app = tmp_path / 'app'
    app.mkdir()
    backups = app / 'backups'
    backups.mkdir()
    (app/'server.py').write_text('# synthetic source\n')
    (app/'config.json').write_text('{"version":"before"}')
    (app/'durable-state.json').write_text('{"version":"before"}')
    monkeypatch.setattr(updater, '_app_dir', lambda: str(app))
    monkeypatch.setattr(updater, '_backup_root', lambda: str(backups))
    monkeypatch.setattr(updater, '_src_current_commit', lambda: '')
    monkeypatch.setattr(updater, '_src_is_git_repo', lambda: False)
    ok, ref, _ = updater._src_backup('v999.0.0')
    assert ok
    with tarfile.open(backups/(ref+'.tar.gz')) as t:
        assert 'config.json' not in t.getnames()
        assert 'durable-state.json' not in t.getnames()
    (app/'config.json').write_text('{"version":"new"}')
    (app/'durable-state.json').write_text('{"version":"new"}')
    assert updater._src_rollback(ref)[0]
    assert json.loads((app/'config.json').read_text())['version'] == 'new'
    assert json.loads((app/'durable-state.json').read_text())['version'] == 'new'


def test_drain_deadline_bounds_uvicorn_shutdown_and_flushes(monkeypatch, store):
    import asyncio
    import uvicorn
    import server
    from src import drain
    async def scenario():
        drain.reset_for_tests()
        monkeypatch.setattr(drain, 'shutdown_timeout_seconds', lambda: 0)
        srv = server._DrainAwareServer(uvicorn.Config(server.app, host='127.0.0.1', port=0, log_level='critical'))
        assert srv.config.timeout_graceful_shutdown == 0
        srv.servers = []
        closed = []
        async def close_lifespan():
            assert state_db.affinity_load('request-finalizer') is not None
            store.close()
            closed.append(True)
        srv.lifespan = SimpleNamespace(shutdown=close_lifespan)
        async def request():
            try:
                await asyncio.Event().wait()
            finally:
                state_db.affinity_upsert('request-finalizer', 'api:synthetic', 'model')
        pending = asyncio.create_task(request())
        await asyncio.sleep(0)
        srv.server_state.tasks.add(pending)
        lease = await drain.enter('synthetic-hung-request')
        try:
            drain.begin('test')
            await srv._stop_after_drain('SIGTERM')
            assert srv.should_exit
            await asyncio.wait_for(srv.shutdown(), timeout=0.25)
            assert closed
            assert pending.done()
            assert 'request-finalizer' in StateStore.read_snapshot(store._paths['runtime'], 'runtime')[1]['cache_affinities']
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await lease.aclose()
            drain.reset_for_tests()
    asyncio.run(scenario())


def test_sidecar_uses_health_port_and_probe_deadline(monkeypatch, tmp_path):
    settings = {'composeService':'parrot', 'containerName':'parrot', 'image':'example/parrot:latest',
                'composeDir':str(tmp_path), 'healthTimeoutSeconds':30, 'gracefulStopSeconds':20}
    monkeypatch.setattr(updater, '_cfg', lambda: settings)
    script = updater._compose_up_inner(backup_digest='sha256:synthetic', health_port=23456)
    health_line = next(line for line in script.splitlines() if line.startswith('health_ok()'))
    assert ':23456/health' in health_line and ':22122/health' not in health_line
    assert '--max-time' in health_line and '--connect-timeout' in health_line
    assert 'timeout -s TERM' in health_line
    assert subprocess.run(['sh', '-n'], input=script, text=True, capture_output=True).returncode == 0


def test_image_quick_restart_closes_orphan(tmp_path, monkeypatch):
    from src import image_db
    clock = [10000.0]
    monkeypatch.setattr(image_db.time, 'time', lambda: clock[0])
    monkeypatch.setattr(image_db, '_resolve_db_path', lambda: str(tmp_path/'synthetic-images.db'))
    monkeypatch.setattr(image_db, '_conn', None)
    image_db.init()
    row_id = image_db.start_call(request_id='interrupted-image', api_key_name='synthetic',
                                action='generate', main_model='model', tool_model='model',
                                size=None, prompt_preview='synthetic', prompt_hash='hash')
    image_db._conn.close()
    monkeypatch.setattr(image_db, '_conn', None)
    clock[0] += 10
    image_db.init()  # restart before 30-minute threshold
    clock[0] += 1900
    try:
        assert image_db.summary()['running_count'] == 0
        assert image_db.get_log(row_id)['status'] == 'failed'
    finally:
        image_db._conn.close()


def test_shutdown_joins_real_sync_update_worker_before_state_close(store, monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    from src import update_checker
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def synthetic_update_worker(**kw):
        started.set()
        release.wait(3)
        try:
            state_db.updater_save({'stage':'staged'})
        except Exception as exc:
            errors.append(str(exc))
        finally:
            finished.set()
    monkeypatch.setattr(update_checker, '_check_once_impl', synthetic_update_worker)
    update_checker.start()
    async def scenario():
        task = asyncio.create_task(_ORIG_TO_THREAD(update_checker._check_once))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not finished.is_set()
        join = asyncio.create_task(update_checker.stop())
        await asyncio.sleep(0.03)
        assert not join.done()
        release.set()
        await asyncio.wait_for(join, 1)
        assert finished.is_set()
        assert state_db.updater_load()['stage'] == 'staged'
        store.close()
    try:
        asyncio.run(scenario())
        assert errors == []
    finally:
        release.set()
        update_checker.start()

@pytest.mark.parametrize('outcome', ['import_error', 'wrong_version', 'missing_dependency', 'success'])
def test_independent_monitor_recovers_unstartable_synthetic_app(tmp_path, outcome):
    """Real monitor/child processes, file readiness only: no sockets or installs."""
    from src import update_supervisor as monitor
    app = tmp_path / 'app'
    app.mkdir()
    lib = tmp_path / 'synthetic-site-packages'
    lib.mkdir()
    module = lib / 'synthetic_dependency.py'
    module.write_text('READY = True\n')
    health, pidfile = tmp_path / 'health.json', tmp_path / 'pid'
    source = app / 'server.py'
    template = '''import sys,os,json,time,signal
from pathlib import Path
sys.path.insert(0, {lib!r})
from synthetic_dependency import READY
health=Path({health!r})
Path({pid!r}).write_text(str(os.getpid()))
def stop(*args):
    health.unlink(missing_ok=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
health.write_text(json.dumps({{"status":"ok","version":VERSION}}))
while True: time.sleep(.02)
'''.format(lib=str(lib), health=str(health), pid=str(pidfile))
    source.write_text('VERSION="1.0"\n' + template)
    archive = tmp_path / 'code.tar.gz'
    monitor.backup_code(str(app), str(archive))
    deps = monitor.snapshot_dependencies(str(tmp_path / 'deps'), [str(lib)])
    (app / 'config.json').write_text('{"business":"original"}')
    old = subprocess.Popen([sys.executable, '-S', str(source)])
    launched_pid = None
    runner = None
    try:
        deadline = time.monotonic() + 3
        while not health.exists() and time.monotonic() < deadline: time.sleep(.01)
        assert health.exists()
        if outcome == 'import_error':
            source.write_text('raise ImportError("synthetic early import failure")\n')
        else:
            source.write_text('VERSION=' + repr('2.0' if outcome != 'wrong_version' else '3.0') + '\n' + template)
        if outcome == 'missing_dependency': module.unlink()
        monitor.seal_dependencies(deps)
        # Business data changes after the code backup must survive rollback.
        (app / 'config.json').write_text('{"business":"new"}')
        plan = dict(app=str(app), archive=str(archive), dependencies=str(tmp_path / 'deps/snapshot.json'),
                    mode='bare', old_pid=old.pid, old_identity=monitor.process_identity(old.pid),
                    command=[sys.executable, '-S', str(source)], health_url=health.as_uri(),
                    health_timeout=.5, stop_timeout=2, target_version='2.0', from_version='1.0',
                    target_code_digest=monitor.code_digest(str(app)), dispatch_delay=0,
                    update_id='synthetic-run', result_path=str(tmp_path / 'result.json'))
        planfile = tmp_path / 'plan.json'
        monitor.write_json(planfile, plan)
        standalone = tmp_path / 'standalone.py'
        standalone.write_text(Path(monitor.__file__).read_text())
        with open(tmp_path/'monitor.log', 'wb') as output:
            runner = subprocess.Popen([sys.executable, '-S', str(standalone), str(planfile)],
                                      stdout=output, stderr=output)
        runner.wait(timeout=5)
        assert runner.returncode == 0, (tmp_path/'monitor.log').read_text()
        assert json.loads(Path(plan['result_path']).read_text()) == {
            'update_id':'synthetic-run', 'result':'OK' if outcome == 'success' else 'ROLLBACK'}
        launched_pid = int(pidfile.read_text())
        assert json.loads(health.read_text())['version'] == ('2.0' if outcome == 'success' else '1.0')
        assert json.loads((app / 'config.json').read_text())['business'] == 'new'
        assert module.read_text() == 'READY = True\n'
        assert not planfile.exists()
    finally:
        if runner is not None and runner.poll() is None:
            runner.kill(); runner.wait()
        if pidfile.exists():
            launched_pid = int(pidfile.read_text())
            monitor.stop_process(launched_pid, monitor.process_identity(launched_pid), 1)
        if old.poll() is None: old.terminate()
        old.wait(timeout=2)


def test_dependency_restore_reverts_only_own_changes(tmp_path):
    from src import update_supervisor as monitor
    lib = tmp_path / 'lib'
    lib.mkdir()
    (lib / 'module.py').write_text('old')
    snap = monitor.snapshot_dependencies(str(tmp_path / 'deps'), [str(lib)])
    (lib / 'module.py').write_text('new')
    (lib / 'added.py').write_text('new')
    monitor.seal_dependencies(snap)
    (lib / 'operator-data').write_text('keep')
    monitor.restore_dependencies(str(tmp_path / 'deps/snapshot.json'))
    assert (lib / 'module.py').read_text() == 'old'
    assert not (lib / 'added.py').exists()
    assert (lib / 'operator-data').read_text() == 'keep'


def test_live_request_lease_retains_string_only_caller_and_releases_abandonment(logs):
    from src import drain
    from src.tests.conftest import _ORIG_TO_THREAD

    def insert_without_retaining_handle():
        # A string-only caller discards the handle. Returning it through the
        # executor lets its Future/worker pin it even after the await resumes;
        # one event-loop tick does not synchronize that worker's teardown.
        log_db.insert_pending('string-only', '127.0.0.1', 'test', 'm', True,
                              1, 0, {}, {}, created_at=time.time()-1900)

    async def scenario():
        lease = await drain.enter('synthetic-request')
        await _ORIG_TO_THREAD(insert_without_retaining_handle)
        assert lease.log_handles['string-only'] is log_db._request_handles['string-only']
        assert log_db.cleanup_stale_pending(1800) == 0
        await lease.aclose()
        assert lease.closed and not lease.log_handles
        assert 'string-only' not in log_db._request_handles
        assert log_db.cleanup_stale_pending(1800) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('tier', ['flex', 'future-unknown'])
@pytest.mark.parametrize('actual', [False, True])
def test_unknown_search_tier_is_unpriced_unless_actual(logs, monkeypatch, tier, actual):
    from src import model_pricing
    monkeypatch.setattr(model_pricing, 'estimate_cost', lambda *a, **kw: pytest.fail('unknown tier must not estimate'))
    monkeypatch.setattr(model_pricing, 'estimate_cost_from_binding', lambda *a, **kw: pytest.fail('unknown tier must not TTL-estimate'))
    h = log_db.record_search_call(call_id='tier', attempt_no=1, source_id='xai', source_type='xai',
                                 operation='search', model='grok-4.6')
    body = {'service_tier':tier, 'usage':{'input_tokens':10, 'output_tokens':5}}
    if actual: body['usage']['cost_in_usd_ticks'] = 123
    log_db.finish_search_call(h, status='success', response_body=body, model='grok-4.6', provider='xai')
    with sqlite3.connect(h.db.path) as c:
        row = c.execute('SELECT cost_source,cost_ticks FROM search_call_log WHERE id=?',(h.row_id,)).fetchone()
    assert row == (('actual',123) if actual else ('unpriced',None))


def test_pruning_protects_current_anchor_even_if_older(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, '_backup_root', lambda: str(tmp_path))
    monkeypatch.setattr(updater, '_cfg', lambda: {'keepBackups':1})
    monkeypatch.setattr(updater, 'load_state', lambda: {'backup_ref':'old', 'stage':updater.STAGE_STAGED})
    for ref, created in [('old',1),('new',2)]:
        (tmp_path / (ref+'.json')).write_text(json.dumps({'ref':ref,'created_at':created}))
    updater._prune_backups()
    assert (tmp_path/'old.json').exists()
    assert [m['ref'] for m in updater.list_backups()] == ['new','old']


def test_sidecar_blackhole_probe_has_real_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, '_cfg', lambda: {'composeService':'parrot','containerName':'parrot',
        'image':'test:latest','composeDir':str(tmp_path),'healthTimeoutSeconds':1})
    script = updater._compose_up_inner(health_port=23456)
    # Run only health functions. Executable docker is a temp no-I/O sleeping stub.
    fake = tmp_path / 'docker'
    fake.write_text('#!/bin/sh\nsleep 10\n')
    fake.chmod(0o700)
    functions = script[script.index('health_ok()'):script.index('container_running()')]
    start = time.monotonic()
    result = subprocess.run(['sh','-c', 'NAME=test; EXPECTED_VERSION=1; EXPECTED_DIGEST=fake;\n'+functions+'\nwait_health'],
                            env={**os.environ,'PATH':str(tmp_path)+':'+os.environ['PATH']}, timeout=4)
    assert result.returncode == 1
    assert time.monotonic()-start < 3


def test_cancelled_updater_command_is_reaped_before_stop_returns(tmp_path, monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    started = tmp_path / 'started'
    async def scenario():
        def worker():
            with updater._op_lock:
                return updater._run([sys.executable,'-S','-c',
                     f'from pathlib import Path; import time; Path({str(started)!r}).write_text("yes"); time.sleep(30)'])
        task = asyncio.create_task(_ORIG_TO_THREAD(worker))
        while not started.exists(): await asyncio.sleep(.005)
        updater.begin_shutdown()
        await asyncio.wait_for(updater.stop(), 3)
        assert (await task)[0] == 130
    asyncio.run(scenario())

@pytest.mark.parametrize('version,rc', [('2.0',0), ('3.0',1)])
def test_sidecar_exec_probe_verifies_reported_version(tmp_path, monkeypatch, version, rc):
    monkeypatch.setattr(updater, '_cfg', lambda: {'composeService':'parrot','containerName':'parrot',
        'image':'test:latest','composeDir':str(tmp_path),'healthTimeoutSeconds':1})
    script = updater._compose_up_inner(health_port=23456)
    functions = script[script.index('health_ok()'):script.index('wait_health()')]
    (tmp_path/'docker').write_text('#!/bin/sh\nif [ "$1" = exec ]; then shift 2; exec "$@"; fi\necho sha256:expected\n')
    (tmp_path/'curl').write_text('#!/bin/sh\nprintf \'%s\\n\' "$SYNTHETIC_HEALTH"\n')
    for name in ['docker','curl']: (tmp_path/name).chmod(0o700)
    (tmp_path/'python').symlink_to(sys.executable)
    env = {**os.environ, 'PATH':str(tmp_path)+':'+os.environ['PATH'],
           'SYNTHETIC_HEALTH':json.dumps({'status':'ok','version':version})}
    result = subprocess.run(['sh','-c', 'NAME=test; EXPECTED_VERSION=2.0; EXPECTED_DIGEST=sha256:expected; PROBE_TIMEOUT=2;\n'+functions+'\nhealth_ok'],
                            env=env, timeout=4)
    assert result.returncode == rc


@pytest.mark.parametrize('mode', ['bare','systemd'])
def test_source_restart_dispatches_standalone_monitor_before_stopping(tmp_path, monkeypatch, store, mode):
    monkeypatch.setattr(updater, '_app_dir', lambda: str(tmp_path))
    backups = tmp_path / 'backups'
    backups.mkdir()
    monkeypatch.setattr(updater, '_backup_root', lambda: str(backups))
    monkeypatch.setattr(updater, '_src_current_commit', lambda: '')
    (tmp_path/'server.py').write_text('# synthetic app')
    ok, ref, _ = updater._src_backup('v2.0')
    assert ok
    saved_monitor = (backups/(ref+'.monitor.py')).read_bytes()
    updater.save_state(stage='restarting', mode=mode, backup_ref=ref, target_tag='v2.0', from_version='1.0')
    calls = []
    monkeypatch.setattr(updater.subprocess, 'Popen', lambda args, **kw: calls.append(args))
    monkeypatch.setattr(updater, '_run', lambda args, **kw: (calls.append(args) or (0,'')))
    assert updater._src_restart()[0]
    plan = json.loads((backups/(ref+'.plan.json')).read_text())
    assert plan['old_pid'] == os.getpid()
    assert plan['target_version'] == 'v2.0'
    assert plan['update_id'] == updater.load_state()['supervisor_id']
    assert (backups/(ref+'.plan.json')).stat().st_mode & 0o777 == 0o600
    assert (backups/(ref+'.monitor.py')).read_bytes() == saved_monitor
    assert '-S' in calls[0]
    if mode == 'systemd':
        assert calls[0][0] == 'systemd-run'
        assert any(arg.startswith('--unit=parrot-update-') for arg in calls[0])
    else:
        assert calls[0][0] == sys.executable
    assert all('restart' not in args for args in calls)


@pytest.mark.parametrize('result,version,expected', [('OK','2.0','success'), ('ROLLBACK','1.0','rolled_back'), ('OK','3.0','failed')])
def test_source_verifying_consumes_identity_bound_monitor_result(tmp_path, monkeypatch, store, result, version, expected):
    path = tmp_path/'result.json'
    path.write_text(json.dumps({'update_id':'synthetic','result':result}))
    updater.save_state(stage='verifying', mode='bare', target_tag='v2.0', from_version='1.0',
                       supervisor_id='synthetic', supervisor_result=str(path))
    monkeypatch.setattr(updater, '__version__', version)
    monkeypatch.setattr(updater, '_notify_cross_process', lambda *a, **kw: None)
    updater.resume_after_restart()
    updater._resume_thread.join(2)
    assert updater.load_state()['stage'] == expected


def test_archive_and_git_arguments_exclude_custom_runtime_paths(tmp_path, monkeypatch):
    app = tmp_path/'app'
    app.mkdir()
    (app/'src').mkdir()
    backups = app/'backups'
    backups.mkdir()
    cfgfile = app/'src/operator.json'
    cfgfile.write_text('business')
    (app/'src/code.py').write_text('old-code')
    monkeypatch.setattr(config, 'DATA_DIR', str(app))
    monkeypatch.setattr(config, 'path', lambda: str(cfgfile))
    monkeypatch.setattr(config, 'get', lambda: {'runtimeStatePath':str(app/'src/runtime.json'),
                                              'durableStatePath':str(app/'src/durable.json')})
    monkeypatch.setattr(updater, '_app_dir', lambda: str(app))
    monkeypatch.setattr(updater, '_backup_root', lambda: str(backups))
    monkeypatch.setattr(updater, '_src_current_commit', lambda: '')
    monkeypatch.setattr(updater, '_src_is_git_repo', lambda: False)
    for name in ['runtime.json','durable.json']:
        (app/'src'/name).write_text('business')
    ok, ref, _ = updater._src_backup('v2.0')
    assert ok
    with tarfile.open(backups/(ref+'.tar.gz')) as archive:
        assert 'src/code.py' in archive.getnames()
        assert 'src/operator.json' not in archive.getnames()
        assert 'src/runtime.json' not in archive.getnames()
        assert 'src/durable.json' not in archive.getnames()
    cfgfile.write_text('new-business')
    (app/'src/code.py').write_text('new-code')
    assert updater._src_rollback(ref)[0]
    assert cfgfile.read_text() == 'new-business'
    assert (app/'src/code.py').read_text() == 'old-code'
    args = updater._source_restore_args('old', updater._source_data_excludes())
    assert ':(exclude)src/operator.json' in args
    assert ':(exclude)src/runtime.json' in args


def test_docker_result_is_bound_to_current_update_id(tmp_path, monkeypatch, store):
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    updater.save_state(stage='verifying', result_id='synthetic-current')
    (tmp_path/'.update_result').write_text('OK')
    (tmp_path/'.update_result.synthetic-current').write_text('ROLLBACK')
    assert updater._read_update_result(wait_seconds=1) == 'ROLLBACK'
    script = updater._compose_up_inner()
    assert '/.update_result.synthetic-current"' in script

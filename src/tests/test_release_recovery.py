"""Real temporary Git + simulated external effects for release recovery boundaries."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from scripts import release as r


@pytest.fixture
def run(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    (repo / 'src/tests').mkdir(parents=True)
    (repo / 'src/__init__.py').write_text('__version__ = "0.34.5"\n')
    (repo / 'server.py').write_text('import json\n')
    (repo / 'requirements.txt').write_text('')
    (repo / 'src/profile.json').write_text('{"current": "old"}\n')
    (repo / 'src/instructions.txt').write_text('old instructions\n')
    (repo / 'src/tests/isolated_pytest.py').write_text('print("fixture regression")\n')
    (repo / '.gitignore').write_text('.release-runs/\n')
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=repo, text=True, stderr=subprocess.PIPE).strip()
    git('init', '-b', 'main'); git('config', 'user.name', 'fixture'); git('config', 'user.email', 'fixture@fake.invalid')
    git('add', '.'); git('commit', '-m', 'fixture')
    monkeypatch.setenv('GH_TOKEN', 'fixture-not-a-real-token')
    state = {'schema': 1, 'run_id': '0.34.5-0123456789ab', 'repository': 'fake/repo',
             'version': '0.34.5', 'old_version': '0.34.4', 'notes': 'Fixture\n\nfrom v0.34.4 to v0.34.5\n\nChanges\n',
             'files': ['server.py', 'src/profile.json', 'src/instructions.txt'], 'python': sys.executable,
             'workers': 1, 'timeout': 3, 'service': 'fixture.service', 'health_url': 'http://example.invalid',
             'candidate': git('rev-parse', 'HEAD'), 'branch': 'release-candidate/fixture', 'stages': {}}
    gh = Mock(repository='fake/repo')
    gh.request.return_value = None
    runner = r.ReleaseRun(repo, repo / '.release-runs' / state['run_id'], state, github=gh)
    state['runtime_fingerprint'] = runner.runtime_fingerprint()
    monkeypatch.setattr(runner, 'environment_fingerprint', lambda: 'env')
    state['stages']['local'] = {'status': 'passed', 'commit': state['candidate'], 'environment': 'env'}
    yield runner, git
    runner.cleanup()


def test_changed_worktree_is_rejected_before_any_service_command(run, monkeypatch):
    x, git = run
    monkeypatch.setattr(x, 'preflight', lambda: None)
    def validation(*args):
        (x.repo / 'server.py').write_text('raise RuntimeError("not validated")\n')
    monkeypatch.setattr(x, 'validation', validation)
    service = Mock(side_effect=AssertionError('No service query/restart before source gate'))
    monkeypatch.setattr(x, 'service_instance', service)
    with pytest.raises(r.ReleaseError, match='Source changed'):
        x.run(approve_restart=True)
    service.assert_not_called()


@pytest.mark.parametrize('filename', ['src/profile.json', 'src/instructions.txt'])
def test_runtime_resources_invalidate_previous_restart(run, filename):
    x, git = run
    before = x.runtime_fingerprint()
    x.state['stages']['restart'] = {'status': 'passed', 'runtime': before}
    (x.repo / filename).write_text('changed runtime resource\n')
    x.prepare()
    assert x.runtime_fingerprint() != before
    assert 'restart' not in x.state['stages']


def test_old_restart_intent_cannot_certify_corrected_candidate(run, monkeypatch):
    x, git = run
    old_runtime = x.state['runtime_fingerprint']
    x.state['restart_intent'] = {'old_instance': {'pid': '11', 'start_ticks': '1'},
                                 'runtime': old_runtime, 'started_at': 1}
    (x.repo / 'server.py').write_text('import json\nimport time\n')
    x.prepare()
    x.state['stages']['local'] = {'status': 'passed', 'commit': x.state['candidate'], 'environment': 'env'}
    monkeypatch.setattr(x, 'service_instance', lambda: {'pid': '22', 'start_ticks': '2'})
    with pytest.raises(r.ReleaseError, match='candidate changed'):
        x.restart(True)
    assert x.state['stages'].get('restart', {}).get('status') != 'passed'


def test_explicit_restart_reconciliation_checks_pending_job_and_restarts(run, monkeypatch):
    x, git = run
    old = {'pid': '11', 'start_ticks': '1'}
    new = {'pid': '22', 'start_ticks': '2'}
    x.state['restart_intent'] = {'old_instance': old, 'runtime': x.state['runtime_fingerprint'], 'started_at': 1}
    calls = []
    def execute(stage, args, **kwargs):
        calls.append(args)
        output = 'a' * 32 if 'InvocationID' in args else ''
        return subprocess.CompletedProcess(args, 0, output, '')
    monkeypatch.setattr(x, 'assert_candidate', lambda: None)
    monkeypatch.setattr(x, 'execute', execute)
    monkeypatch.setattr(x, 'service_instance', Mock(side_effect=[old, new, new]))
    monkeypatch.setattr(x, 'health', lambda: {'status': 'ok', 'version': '0.34.5'})
    x.restart(True, reconcile=True)
    assert sum(c[:2] == ['systemctl', 'restart'] for c in calls) == 1
    assert x.state['stages']['restart']['instance'] == new


@pytest.mark.parametrize('case, error', [
    ('old-shutdown-error', None),
    ('current-error', 'Startup errors detected'),
    ('missing-invocation', 'Cannot identify'),
    ('changed-instance', 'Service instance changed'),
])
def test_restart_recovery_scopes_logs_to_current_invocation(run, monkeypatch, case, error):
    x, git = run
    old = {'pid': '11', 'start_ticks': '1'}
    new = {'pid': '22', 'start_ticks': '2'}
    later = {'pid': '33', 'start_ticks': '3'} if case == 'changed-instance' else new
    x.state['restart_intent'] = {'old_instance': old, 'runtime': x.state['runtime_fingerprint'],
                                 'environment': 'env', 'started_at': 1}
    monkeypatch.setattr(x, 'service_instance', Mock(side_effect=[new, new, later]))
    monkeypatch.setattr(x, 'health', lambda: {'status': 'ok', 'version': '0.34.5'})
    original, calls = x.execute, []
    invocation = 'a' * 32

    def execute(stage, args, **kwargs):
        if args[0] not in ('systemctl', 'journalctl'):
            return original(stage, args, **kwargs)
        calls.append(args)
        assert args[:2] != ['systemctl', 'restart'], 'confirmed new instance must not be restarted'
        if args[0] == 'systemctl':
            assert 'InvocationID' in args
            output = '' if case == 'missing-invocation' else invocation
        elif '_SYSTEMD_INVOCATION_ID=' + invocation not in args:
            output = 'ERROR: old shutdown\nTraceback: drain deadline exceeded\n'
        else:
            output = 'ERROR: current startup failed\n' if case == 'current-error' else 'new instance ready\n'
        return subprocess.CompletedProcess(args, 0, output, '')

    monkeypatch.setattr(x, 'execute', execute)
    if error:
        with pytest.raises(r.ReleaseError, match=error):
            x.restart()
        assert 'restart_intent' in x.state
        assert x.state['stages'].get('restart', {}).get('status') != 'passed'
    else:
        x.restart()
        assert x.state['stages']['restart']['instance'] == new
        assert 'restart_intent' not in x.state
    journals = [args for args in calls if args[0] == 'journalctl']
    assert bool(journals) == (case != 'missing-invocation')
    assert all('_SYSTEMD_INVOCATION_ID=' + invocation in args for args in journals)


def test_pending_systemd_job_cannot_be_replayed(run, monkeypatch):
    x, git = run
    monkeypatch.setattr(x, 'assert_candidate', lambda: None)
    monkeypatch.setattr(x, 'service_instance', lambda: {'pid': '11', 'start_ticks': '1'})
    x.state['restart_intent'] = {'runtime': 'old'}
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, '123\n', ''))
    monkeypatch.setattr(x, 'execute', execute)
    with pytest.raises(r.ReleaseError, match='pending'):
        x.restart(True, reconcile=True)
    assert execute.call_count == 1


@pytest.mark.parametrize('key', ['retry_intent', 'publish_retry_intent'])
def test_definitely_rejected_retry_does_not_poison_next_attempt(run, key):
    x, git = run
    cloud = {'id': 10, 'run_attempt': 1}
    x.github.request.side_effect = r.ReleaseError('cloud', 'HTTP 403', details={'request_rejected': True})
    with pytest.raises(r.ReleaseError):
        x.request_retry(key, cloud)
    assert key not in x.state
    x.github.request.side_effect = None
    x.request_retry(key, cloud)
    assert x.github.request.call_count == 2
    assert x.state[key]['run_id'] == 10


def test_unknown_retry_preserves_intent_for_reconciliation(run):
    x, git = run
    x.github.request.side_effect = r.ReleaseError('cloud', 'connection lost')
    with pytest.raises(r.ReleaseError):
        x.request_retry('retry_intent', {'id': 10, 'run_attempt': 1})
    assert x.state['retry_intent']['attempt'] == 1


def test_stale_owned_snapshot_is_reclaimed_before_clone(run):
    x, git = run
    (x.work / 'source').mkdir(parents=True)
    (x.work / 'source/stale').write_text('previous terminated invocation')
    x.start_local()
    assert x.local_process.wait(timeout=10) == 0
    assert not (x.work / 'source/stale').exists()


def test_rehearsal_uses_isolated_index_and_never_bumps_or_moves_main(run):
    x, git = run
    x.state['mode'] = 'rehearsal'
    x.state.pop('candidate')
    before = (git('rev-parse', 'HEAD'), git('write-tree'), r.read_version(x.repo))
    (x.repo / 'server.py').write_text('import os\n')
    x.prepare()
    first = x.state['candidate']
    assert first != before[0]
    assert (git('rev-parse', 'HEAD'), git('write-tree'), r.read_version(x.repo)) == before
    x.prepare()
    assert x.state['candidate'] == first
    (x.repo / 'server.py').write_text('import time\n')
    x.prepare()
    assert git('rev-parse', x.state['candidate'] + '^') == first
    assert (git('rev-parse', 'HEAD'), git('write-tree'), r.read_version(x.repo)) == before
    with pytest.raises(r.ReleaseError, match='rehearsal'):
        x.assert_candidate()


def test_rehearsal_run_cannot_enter_formal_stages(run, monkeypatch):
    x, git = run
    x.state['mode'] = 'rehearsal'
    for name in ('preflight', 'reconcile_commit', 'prepare', 'validation'):
        monkeypatch.setattr(x, name, lambda *a: None)
    for name in ('restart', 'git_publish', 'publication'):
        monkeypatch.setattr(x, name, Mock(side_effect=AssertionError('formal side effect forbidden')))
    def finish():
        x.state['completed'] = True
        x.state['stages']['publication'] = {'url': 'https://example.invalid/rehearsal'}
    monkeypatch.setattr(x, 'finish_rehearsal', finish)
    x.run(approve_restart=True)
    assert json.loads((x.directory / 'timings.json').read_text())['mode'] == 'rehearsal'


def test_candidate_cleanup_never_deletes_a_formally_tagged_digest(run):
    x, git = run
    def request(path, **kwargs):
        if path == '/user': return {'login': 'fake'}
        return [{'id': 123, 'metadata': {'container': {'tags': [f"candidate-{x.state['candidate']}-amd64", 'latest']}}}]
    x.github.request.side_effect = request
    with pytest.raises(r.ReleaseError, match='non-rehearsal'):
        x.cleanup_rehearsal_registry()
    assert not any(call.kwargs.get('method') == 'DELETE' for call in x.github.request.call_args_list)


def test_rehearsal_cleanup_includes_failed_ancestors_but_not_base_commit(run):
    x, git = run
    base = x.state.pop('candidate')
    x.state.update(mode='rehearsal', run_id='rehearsal-0123456789ab', branch='release-candidate/rehearsal-0123456789ab')
    (x.repo / 'server.py').write_text('import os\n')
    x.prepare()
    first = x.state['candidate']
    (x.repo / 'server.py').write_text('import time\n')
    x.prepare()
    second = x.state['candidate']
    versions = [{'id': i, 'metadata': {'container': {'tags': [f'candidate-{sha}-amd64']}}}
                for i, sha in enumerate((first, second, base), 1)]
    def request(path, *, method='GET', **kwargs):
        if method == 'DELETE': return None
        if '?' in path: return versions
        return next(v for v in versions if str(v['id']) == path.rsplit('/', 1)[1])
    x.github.request.side_effect = request
    x.cleanup_rehearsal_registry()
    assert x.state['deleted_rehearsal_versions'] == [1, 2]
    deleted = [c.args[0] for c in x.github.request.call_args_list if c.kwargs.get('method') == 'DELETE']
    assert all(not p.endswith('/3') for p in deleted)


def test_repository_preflight_uses_canonical_api_url():
    gh = r.GitHub('fake/repo', 'fixture')
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"permissions":{"push":true}}'
    gh.opener = Mock()
    gh.opener.open.return_value = response
    assert gh.request('')['permissions']['push']
    assert gh.opener.open.call_args.args[0].full_url == 'https://api.github.com/repos/fake/repo'


def test_missing_candidate_receipt_is_detected_before_formal_publication(monkeypatch):
    gh = r.GitHub('fake/repo', None)
    sha = 'a' * 40
    monkeypatch.setattr(gh, 'prepare_runs', lambda sha: [{'id': 1, 'status': 'completed', 'conclusion': 'success'}])
    monkeypatch.setattr(gh, 'jobs', lambda run_id: [{'name': n, 'conclusion': 'success'} for n in ('quality', 'build (amd64)', 'build (arm64)')])
    monkeypatch.setattr(gh, 'artifacts', lambda run_id: [{'name': f'parrot-candidate-{sha}-amd64', 'expired': True}])
    with pytest.raises(r.ReleaseError, match='expired'):
        gh.verified_candidate(sha)

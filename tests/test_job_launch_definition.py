from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from mn_api.job_launch_definition import require_prepared_launch_models


def models():
    return {'inspect': {'workers': [{'id': 'vision', 'model': 'edge:bf16'}]}}


def job():
    return {'job_id': 'job-1', 'blueprint_id': 'camera', 'mcp': {'profile': 'mn.mcp.supervision.v1'},
            'workflow_definition': {'runtime': {'bindings': models()}}}


def test_identical_recipes_preserve_the_prepared_job():
    current = job()
    before = deepcopy(current)
    require_prepared_launch_models(current, {'runtime': {'bindings': models()}})
    assert current == before


@pytest.mark.parametrize('field', ['model', 'worker_id', 'step_id', 'missing_worker'])
def test_stale_owner_cannot_change_prepared_model_bindings(field):
    candidate = models()
    if field == 'model':
        candidate['inspect']['workers'][0]['model'] = 'old-recipe'
    elif field == 'worker_id':
        candidate['inspect']['workers'][0]['id'] = 'old-recipe'
    elif field == 'step_id':
        candidate['old-recipe'] = candidate.pop('inspect')
    else:
        candidate['inspect']['workers'] = []
    with pytest.raises(HTTPException) as error:
        require_prepared_launch_models(job(), {'runtime': {'bindings': candidate}})
    assert error.value.status_code == 409
    assert 'Update its blueprint catalog' in error.value.detail
    assert 'old-recipe' not in error.value.detail


@pytest.mark.parametrize('current', [{}, {'workflow_definition': {}}, {'workflow_definition': {'runtime': None}}])
def test_missing_prepared_definition_requires_preparation(current):
    with pytest.raises(HTTPException) as error:
        require_prepared_launch_models(current, {'runtime': {'bindings': models()}})
    assert error.value.status_code == 409


def test_model_free_job_does_not_acquire_a_model_during_credential_delivery():
    current = {'workflow_definition': {'runtime': {}}}
    require_prepared_launch_models(current, {'runtime': {}})
    with pytest.raises(HTTPException):
        require_prepared_launch_models(current, {'runtime': {'bindings': models()}})


@pytest.fixture
def launch(monkeypatch):
    from mn_api import job_supervision, operations, blueprints
    from mn_api.routes import blueprints as legacy
    from mn_api.routes.v1 import blueprints as routes, jobs

    current = job()
    calls = []
    runtime = SimpleNamespace(get_job=lambda *a, **kw: deepcopy(current), get_run=lambda *_: {},
        pause_run=lambda *_: {}, resume_run=lambda *_: {}, cancel_run=lambda *_: {})
    monkeypatch.setattr(job_supervision, 'RuntimeService', lambda _: runtime)
    monkeypatch.setattr(job_supervision, 'JobSupervisor', lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(job_supervision, 'OperationStore', lambda _: None)
    monkeypatch.setattr(job_supervision, 'RuntimeMCPAdapter', lambda _: None)
    monkeypatch.setattr(blueprints, 'find_blueprint', lambda *_: ('repo', {'id': 'camera'}))
    monkeypatch.setattr(blueprints, 'validate_blueprint_bundle', lambda *_: 'bundle')
    candidate = {'runtime': {'bindings': models()}}
    monkeypatch.setattr(legacy, 'read_manifest_for_launch', lambda _: candidate)

    def start(kind, options, work, **kw):
        calls.append(('operation', options))
        return work(lambda **_: None)

    def start_secret(blueprint_id, request, *args):
        calls.append(('secret_launch', request.secret_environment))
        return JSONResponse({'run_id': 'run-1'})

    def start_plain(*args):
        calls.append(('plain_launch',))
        return JSONResponse({'run_id': 'run-2'})

    monkeypatch.setattr(operations, 'start_local_operation', start)
    monkeypatch.setattr(routes, 'create_blueprint_run', start_secret)
    monkeypatch.setattr(jobs, 'create_job_run_operation', start_plain)
    supervisor = job_supervision.BoundJobSupervision(None, lambda: 'job-1')._supervisor()
    return supervisor.launch, candidate, calls


def test_secure_owner_launch_rejects_downgrade_before_preparation_or_secret_dispatch(launch):
    start, candidate, calls = launch
    candidate['runtime']['bindings']['inspect']['workers'][0]['model'] = 'nano:old'
    with pytest.raises(HTTPException):
        start('request-1', {'secret_environment': {'CAMERA_SECRET': 'private-camera'}})
    assert calls == []


def test_secure_launch_allows_matching_recipe_and_keeps_secrets_out_of_operation(launch):
    start, _, calls = launch
    assert start('request-1', {'secret_environment': {'CAMERA_SECRET': 'private-camera'}}) == {'run_id': 'run-1'}
    assert calls[0] == ('operation', {'job_id': 'job-1'})
    assert calls[1][0] == 'secret_launch'
    assert calls[1][1]['CAMERA_SECRET'] == 'private-camera'


def test_launch_without_credentials_uses_existing_prepared_run_path(launch):
    start, candidate, calls = launch
    candidate['runtime']['bindings'] = {}
    assert start('request-1', {}) == {'run_id': 'run-2'}
    assert calls == [('plain_launch',)]

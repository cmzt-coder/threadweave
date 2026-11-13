"""验证 O12 失败来源、确定性、重试提示与恢复建议。"""
import copy
import json
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from failure import (  # noqa: E402
    build_failure_record,
    failure_summary,
    latest_unresolved_failure,
    unresolved_failures,
)
from next_actions import build_next_actions  # noqa: E402
from recovery import build_recovery_plan  # noqa: E402
from state_schema import validate_state  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def rejected(name, fn):
    try:
        fn()
    except ValueError:
        check(name, True)
    else:
        check(name, False)


auth = build_failure_record(
    'host_api', 'authentication_failed', 'create_worker', 'HTTP 401')
check('source-recorded', auth['source'] == 'host_api')
check('stage-recorded', auth['stage'] == 'create_worker')
check('certainty-confirmed', auth['resultCertainty'] == 'confirmed_failure')
check('auth-not-retryable', auth['retryable'] is False)
check('auth-recommendation', auth['recommendation'] == 'verify_credentials')
check('raw-message-preserved', auth['message'] == 'HTTP 401')
model_validation = build_failure_record(
    'host_api', 'model_validation_failed', 'create_worker',
    'create_thread could not validate model')
check('request-validation-correction',
      model_validation['recommendation'] == 'correct_request_before_retry')

rate = build_failure_record(
    'provider', 'rate_limited', 'model_call', '429', retryable=True,
    recommendation=None)
check('same-source-different-code', rate['recommendation'] != auth['recommendation'])
check('rate-retryable', rate['retryable'] is True)
check('rate-backoff-recommendation', rate['recommendation'] == 'retry_after_backoff_after_leader_decision')

unknown = build_failure_record(
    'host_api', 'send_result_unknown', 'dispatch', 'connection reset',
    result_certainty='unknown', retryable=True)
check('unknown-forces-no-retry', unknown['retryable'] is False)
check('unknown-reconcile-first', unknown['recommendation'] == 'reconcile_result')

setup = build_failure_record(
    'workspace_setup', 'dependency_install_failed', 'setup', 'npm install failed')
check('setup-recommendation', setup['recommendation'] == 'verify_idempotency_then_rerun_setup')
model = build_failure_record(
    'model_output', 'report_schema_invalid', 'report', 'missing head')
check('model-output-rework', model['recommendation'] == 'rerequest_or_rework_existing_worker')
acceptance = build_failure_record(
    'acceptance', 'verification_failed', 'integration', 'step exited 17')
check('acceptance-repair', acceptance['recommendation'] == 'repair_and_reverify')
worker_process = build_failure_record(
    'worker_process', 'worker_exited', 'execute', 'process exited 1',
    evidence='host process table recorded child PID exit status 1')
check('worker-process-explicit', worker_process['source'] == 'worker_process')
supervisor_process = build_failure_record(
    'supervisor_process', 'supervisor_crashed', 'dispatch', 'control process signal 9',
    evidence='dedicated control process PID 11203 exited by SIGKILL')
check('supervisor-process-explicit', supervisor_process['source'] == 'supervisor_process')

rejected('invalid-source-rejected', lambda: build_failure_record(
    'network', 'x', 'stage', 'message'))
rejected('invalid-certainty-rejected', lambda: build_failure_record(
    'host_api', 'x', 'stage', 'message', result_certainty='maybe'))
rejected('retryable-type-rejected', lambda: build_failure_record(
    'host_api', 'x', 'stage', 'message', retryable=1))
rejected('process-no-evidence-rejected', lambda: build_failure_record(
    'worker_process', 'worker_exited', 'execute', 'process gone'))

resolved = dict(unknown, resolved=True)
state_errors = {'errors': [auth, resolved, dict(unknown, scope='a#1')]}
check('latest-unresolved-skips-resolved',
      latest_unresolved_failure(state_errors, role='a', attempt=1)['code'] == 'send_result_unknown')
check('unresolved-count', len(unresolved_failures(state_errors)) == 2)
check('summary-has-guidance',
      failure_summary(unknown)['recommendation'] == 'reconcile_result')

base = 'a' * 40
worker = {
    'role': 'a', 'attempt': 1, 'state': 'pending', 'threadId': None,
    'branch': 'tw/a', 'baseSha': base,
}
state = {'workers': [worker], 'errors': [dict(unknown, scope='a#1')]}
observed = {
    'thread_exists': False, 'thread_status': None,
    'worktree_exists': True, 'branch': 'tw/a', 'head': base,
    'status_porcelain': '',
}
plan = build_recovery_plan(state, {'a#1': observed}, replacement_limit=1)
check('unknown-error-prevents-recreate', plan['a#1']['action'] == 'revalidate')
check('recovery-includes-failure', plan['a#1']['failure']['source'] == 'host_api')

next_state = {
    'runId': 'failure-next', 'status': 'observing',
    'workers': [dict(worker, errors=[])],
    'limits': {'replacementPerRole': 1},
    'cleanup': {'status': 'pending', 'outstandingResources': []},
    'errors': [dict(unknown, scope='a#1')],
}
next_plan = build_next_actions(next_state, {})
check('next-unresolved-failure-listed', len(next_plan['unresolvedFailures']) == 1)
check('next-failure-guidance',
      next_plan['unresolvedFailures'][0]['recommendation'] == 'reconcile_result')
check('next-action-carries-failure',
      next_plan['actions'][0]['failure']['source'] == 'host_api')
check('next-readonly', next_plan['readOnly'] is True)

legacy = json.loads((project / 'references/state.example.json').read_text())
legacy = copy.deepcopy(legacy)
legacy['errors'] = [{
    'at': '2026-11-13T00:00:00Z', 'scope': 'run', 'code': 'legacy',
    'message': 'old error shape', 'resolved': False,
}]
validate_state(legacy)
check('legacy-error-shape-compatible', True)

normalized_state = copy.deepcopy(legacy)
normalized_state['errors'] = [dict(unknown, scope='run')]
validate_state(normalized_state)
check('normalized-error-schema-compatible', True)
invalid_source_state = copy.deepcopy(normalized_state)
invalid_source_state['errors'][0]['source'] = 'bogus'
try:
    validate_state(invalid_source_state)
except ValueError:
    check('invalid-source-schema-rejected', True)
else:
    check('invalid-source-schema-rejected', False)
invalid_certainty_state = copy.deepcopy(normalized_state)
invalid_certainty_state['errors'][0]['resultCertainty'] = 'maybe'
try:
    validate_state(invalid_certainty_state)
except ValueError:
    check('invalid-certainty-schema-rejected', True)
else:
    check('invalid-certainty-schema-rejected', False)

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O12 local failure classification and read-only recovery guidance',
}, ensure_ascii=False, indent=2))

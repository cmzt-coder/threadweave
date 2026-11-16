"""验证 O04 只读恢复建议与 next CLI，不调用宿主 API 或执行清理。"""
import io
import json
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from next_actions import build_next_actions  # noqa: E402
from state_store import run_state_path, write_state_atomic  # noqa: E402
from threadweave import main, next_plan  # noqa: E402

results = []
BASE = 'a' * 40
HEAD = 'b' * 40


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def make_worker(state='ready', **overrides):
    worker = {
        'role': 'a',
        'attempt': 1,
        'state': state,
        'threadId': 'thread-a',
        'branch': 'tw/a-1',
        'baseSha': BASE,
        'createdAt': now(),
        'deadline': now(),
        'lastObservation': now(),
        'errors': [],
    }
    worker.update(overrides)
    return worker


def recovery_observation(worker, **overrides):
    observed = {
        'observedAt': now(),
        'acceptance_verified': True,
        'thread_exists': True,
        'thread_status': 'idle',
        'worktree_exists': True,
        'branch': worker['branch'],
        'head': worker['baseSha'],
        'status_porcelain': '',
    }
    if worker.get('state') in ('running', 'blocked', 'reported'):
        observed['head'] = HEAD
        observed['base_is_ancestor'] = True
    if worker.get('state') == 'reported':
        observed['report_sha'] = HEAD
    observed.update(overrides)
    return observed


def cleanup_observation(worker, **overrides):
    observed = {
        'observedAt': now(),
        'acceptance_verified': True,
        'thread_exists': True,
        'thread_status': 'archived',
        'worktree_exists': True,
        'head': BASE,
        'status_porcelain': '',
        'branch': {
            'name': worker['branch'],
            'branch_exists': True,
            'owned_by_run': True,
            'checked_out': False,
            'merged_into_integration': True,
        },
        'worktree': {
            'worktree_exists': True,
            'is_shared': False,
            'is_primary': False,
            'owned_by_run': True,
            'status_porcelain': '',
            'unmerged_commits': False,
            'worker_status': 'archived',
        },
        'report_archived': True,
        'stop': {
            'thread_idle': True,
            'final_answer_present': True,
            'no_active_turn': True,
        },
    }
    observed.update(overrides)
    return observed


def state_with(workers, status='observing', cleanup_status='pending', **extra):
    state = {
        'runId': 'run-next',
        'status': status,
        'workers': workers,
        'cleanup': {'status': cleanup_status},
    }
    state.update(extra)
    return state


# 完整观察下可复用，且不把请求配置误当成观测身份。
healthy = make_worker()
plan = build_next_actions(state_with([healthy]), {'a#1': recovery_observation(healthy)})
check('healthy-reuse-action', plan['actions'][0]['action'] == 'reuse')
check('read-only-marker', plan['readOnly'] is True)
check('role-attempt-key', plan['actions'][0]['worker'] == 'a#1')
check('observed-at-preserved', bool(plan['actions'][0]['observedAt']))
check('no-lost-resume-actions',
      all(a['action'] not in ('lost', 'resume') for a in plan['actions']))

legacy_observation = recovery_observation(healthy)
legacy_observation.pop('observedAt')
legacy_plan = build_next_actions(state_with([healthy]), {'a#1': legacy_observation})
check('legacy-recovery-format-reuse', legacy_plan['actions'][0]['action'] == 'reuse')

# 缺失外部观察只能要求重新核对，不能建议 recreation/reuse/send。
missing = build_next_actions(state_with([healthy]))
check('missing-observation-revalidate', missing['actions'][0]['action'] == 'revalidate')
check('missing-evidence-listed',
      'thread_exists' in missing['actions'][0]['missingEvidence'])
check('missing-observation-no-direct-action',
      all(a['action'] not in ('recreate', 'reuse', 'send', 'create')
          for a in missing['actions']))

# pending/unknown dispatch 必须优先核对。
pending_worker = make_worker(
    role='b',
    threadId='thread-b',
    branch='tw/b-1',
    dispatchIntent={
        'dispatchId': 'd1', 'message': 'dispatch', 'at': now(), 'result': 'pending',
    },
)
ordered = build_next_actions(
    state_with([healthy, pending_worker]),
    {'a#1': recovery_observation(healthy),
     'b#1': recovery_observation(pending_worker)},
)
check('pending-dispatch-first', ordered['actions'][0]['worker'] == 'b#1')
check('pending-dispatch-revalidate', ordered['actions'][0]['action'] == 'revalidate')
check('pending-dispatch-priority', ordered['actions'][0]['priority'] < ordered['actions'][1]['priority'])
check('pending-dispatch-does-not-recreate',
      all(a['action'] != 'recreate' for a in ordered['actions'] if a['worker'] == 'b#1'))

# integrationBlocked 必须生成运行级重验建议，并全局禁止 worker 恢复执行。
blocked_state = state_with(
    [healthy], status='integrating', cleanup_status='pending',
    integrationBlocked={
        'status': 'blocked', 'reason': 'integration verification failed', 'at': now(),
    },
)
blocked_plan = build_next_actions(
    blocked_state, {'a#1': recovery_observation(healthy)})
blocked_actions = blocked_plan['actions']
check('integration-blocked-top-level', blocked_plan['integrationBlocked'] is True)
check('integration-blocked-execution-flag', blocked_plan['runExecutionBlocked'] is True)
check('integration-blocked-global-action',
      any(a['action'] == 'recover_integration' and a['worker'] == 'run#1'
          for a in blocked_actions))
check('integration-blocked-required-action',
      any(a.get('requiredAction') == 'recover_integration' for a in blocked_actions))
check('integration-blocked-no-execution',
      all(a['action'] not in ('reuse', 'recreate') for a in blocked_actions))
check('integration-blocked-worker-recheck',
      any(a['worker'] == 'a#1' and a['action'] == 'revalidate'
          and a.get('recoveryExecutionBlocked') is True for a in blocked_actions))

empty_block_plan = build_next_actions(
    state_with(
        [healthy], status='integrating', cleanup_status='pending',
        integrationBlocked={},
    ),
    {'a#1': recovery_observation(healthy)},
)
check('empty-block-matches-controller-falsy-semantics',
      all(a['action'] != 'recover_integration' for a in empty_block_plan['actions']))

resolved_block_plan = build_next_actions(
    state_with(
        [healthy], status='observing', cleanup_status='pending',
        integrationBlocked={'status': 'resolved'},
    ),
    {'a#1': recovery_observation(healthy)},
)
check('resolved-label-cannot-clear-controller-block',
      any(a['action'] == 'recover_integration' for a in resolved_block_plan['actions']))

blocked_pending = build_next_actions(
    state_with(
        [pending_worker], status='integrating', cleanup_status='pending',
        integrationBlocked=True,
    ),
    {'b#1': recovery_observation(pending_worker)},
)
check('integration-blocked-pending-dispatch-first',
      blocked_pending['actions'][0]['worker'] == 'b#1'
      and blocked_pending['actions'][0].get('dispatchResult') == 'pending')
check('integration-blocked-recover-second',
      any(a['action'] == 'recover_integration'
          for a in blocked_pending['actions'][1:]))

# failed / aborted / cleanup_pending 只允许停止与清理复核，不得恢复执行。
for halted_status in ('failed', 'aborted', 'cleanup_pending'):
    halted_plan = build_next_actions(
        state_with([healthy], status=halted_status, cleanup_status='pending'),
        {'a#1': recovery_observation(healthy)},
    )
    check(f'{halted_status}-no-execution',
          all(a['action'] not in ('reuse', 'recreate')
              for a in halted_plan['actions']))
    check(f'{halted_status}-blocked-recheck',
          any(a['action'] == 'revalidate'
              and a.get('recoveryExecutionBlocked') is True
              for a in halted_plan['actions']))
    check(f'{halted_status}-no-integration-recover',
          all(a['action'] != 'recover_integration' for a in halted_plan['actions']))

halted_with_block = build_next_actions(
    state_with(
        [healthy], status='failed', cleanup_status='pending',
        integrationBlocked={'status': 'blocked', 'reason': 'stale integration failure'},
    ),
    {'a#1': recovery_observation(healthy)},
)
check('failed-run-suppresses-integration-recover',
      all(a['action'] != 'recover_integration'
          for a in halted_with_block['actions']))
check('failed-run-only-stop-cleanup',
      any('stop' in a['reason'].lower() for a in halted_with_block['actions']))

# reported 报告 SHA 不一致必须阻断。
reported = make_worker(state='reported')
reported_bad = recovery_observation(reported, report_sha='c' * 40)
reported_plan = build_next_actions(state_with([reported]), {'a#1': reported_bad})
check('reported-sha-mismatch-block', reported_plan['actions'][0]['action'] == 'block')

# 线程确认不存在时才允许 recreate；缺少工作树或存在未保护成果时不得直接 recreate。
gone = make_worker(threadId='thread-a')
gone_obs = recovery_observation(
    gone, thread_exists=False, thread_status=None, worktree_exists=False,
    thread_absence_confirmed=True,
    thread_absence_evidence='list_threads exact lookup returned not found',
    branch_preserved=True, report_present=False, notes_present=False,
)
gone_obs.pop('status_porcelain')
preserve_plan = build_next_actions(state_with([gone]), {'a#1': gone_obs})
check('confirmed-gone-preserve-first', preserve_plan['actions'][0]['action'] == 'preserve_then_recreate')
check('preserve-action-no-direct-recreate', preserve_plan['actions'][0]['action'] != 'recreate')

unknown_worktree = {
    'observedAt': now(),
    'thread_exists': False,
    'thread_status': None,
}
unknown_plan = build_next_actions(state_with([gone]), {'a#1': unknown_worktree})
check('unconfirmed-gone-revalidate',
      unknown_plan['actions'][0]['action'] == 'revalidate')
check('unconfirmed-gone-missing-evidence',
      'thread_absence_confirmed' in unknown_plan['actions'][0]['missingEvidence'])

dirty_gone = recovery_observation(
    gone, thread_exists=False, thread_status=None, status_porcelain=' M notes.md',
    worktree={'unmerged_commits': False},
    thread_absence_confirmed=True,
    thread_absence_evidence='list_threads exact lookup returned not found',
    branch_preserved=True, report_present=False, notes_present=False,
    dirty_patch_archived=False,
)
dirty_plan = build_next_actions(state_with([gone]), {'a#1': dirty_gone})
check('dirty-worktree-preserve-work', dirty_plan['actions'][0]['action'] == 'preserve_work')
check('dirty-worktree-reason', 'preserve' in dirty_plan['actions'][0]['reason'])

limited_gone = make_worker(threadId=None)
limited_obs = recovery_observation(
    limited_gone, thread_exists=False, thread_status=None, worktree_exists=False,
    thread_absence_confirmed=True,
    thread_absence_evidence='list_threads exact lookup returned not found',
    branch_preserved=True, report_present=False, notes_present=False,
)
limited_obs.pop('status_porcelain')
limited_plan = build_next_actions(
    state_with([limited_gone], limits={'replacementPerRole': 0}),
    {'a#1': limited_obs},
)
check('replacement-limit-block', limited_plan['actions'][0]['action'] == 'block')

# 陈旧观察不能作为直接动作依据。
stale_worker = make_worker()
stale_obs = recovery_observation(
    stale_worker, observedAt='2000-01-01T00:00:00+00:00', maxAgeSeconds=60,
)
stale_plan = build_next_actions(state_with([stale_worker]), {'a#1': stale_obs})
check('stale-observation-revalidate', stale_plan['actions'][0]['action'] == 'revalidate')
check('stale-observation-flag', stale_plan['actions'][0]['stale'] is True)

invalid_time_obs = recovery_observation(healthy, observedAt='not-a-time')
invalid_time_plan = build_next_actions(state_with([healthy]), {'a#1': invalid_time_obs})
check('invalid-observation-time-revalidate',
      invalid_time_plan['actions'][0]['action'] == 'revalidate')

missing_time_freshness = recovery_observation(healthy)
missing_time_freshness.pop('observedAt')
missing_time_freshness['maxAgeSeconds'] = 60
missing_time_plan = build_next_actions(
    state_with([healthy]), {'a#1': missing_time_freshness})
check('max-age-requires-observation-time',
      missing_time_plan['actions'][0]['action'] == 'revalidate')

# complete 且 cleanup complete 必须无动作。
complete = state_with(
    [make_worker(state='archived')], status='complete', cleanup_status='complete',
)
complete_plan = build_next_actions(complete, {})
check('complete-cleanup-no-actions', complete_plan['actions'] == [])

# 清理债务即使 worker 已归档也要给出只读清理建议。
archived = make_worker(state='archived')
cleanup_state = state_with([archived], status='aborted', cleanup_status='pending')
cleanup_plan = build_next_actions(cleanup_state, {'a#1': cleanup_observation(archived)})
cleanup_actions = {a['action'] for a in cleanup_plan['actions']}
check('cleanup-debt-worktree-action', 'remove_worktree' in cleanup_actions)
check('cleanup-debt-branch-retention-review',
      'review_branch_retention' in cleanup_actions)
check('cleanup-debt-no-delete-action',
      'delete_branch' not in cleanup_actions)
check('cleanup-debt-no-recovery-action',
      all(a['action'] not in ('recreate', 'reuse') for a in cleanup_plan['actions']))
branch_review = next(
    a for a in cleanup_plan['actions'] if a['action'] == 'review_branch_retention')
check('cleanup-branch-classification-preserved',
      branch_review.get('cleanupAction') == 'delete')
check('cleanup-branch-no-delete-authorization',
      branch_review.get('authorizationRequired') is True
      and 'does not authorize deletion' in branch_review['reason'])
check('cleanup-branch-no-safety-claim', 'safe' not in branch_review['reason'].lower())
worktree_review = next(
    a for a in cleanup_plan['actions'] if a['action'] == 'remove_worktree')
check('cleanup-worktree-requires-confirmation',
      worktree_review.get('authorizationRequired') is True)

# 清理证据缺失、停止证据不足、脏工作树都必须保护或重验。
cleanup_missing = build_next_actions(cleanup_state, {})
check('cleanup-missing-revalidate', cleanup_missing['actions'][0]['action'] == 'revalidate')
check('cleanup-missing-evidence',
      'worktree' in cleanup_missing['actions'][0]['missingEvidence'])

stop_obs = cleanup_observation(archived)
stop_obs['stop'] = {
    'thread_idle': False, 'final_answer_present': True, 'no_active_turn': True,
}
stop_plan = build_next_actions(cleanup_state, {'a#1': stop_obs})
check('cleanup-stop-unverified-revalidate', stop_plan['actions'][0]['action'] == 'revalidate')
check('cleanup-stop-unverified-no-delete',
      all(a['action'] not in ('remove_worktree', 'delete_branch') for a in stop_plan['actions']))

dirty_cleanup_obs = cleanup_observation(archived)
dirty_cleanup_obs['worktree']['status_porcelain'] = ' M x'
dirty_cleanup_plan = build_next_actions(cleanup_state, {'a#1': dirty_cleanup_obs})
check('cleanup-dirty-blocks-removal',
      all(a['action'] != 'remove_worktree' for a in dirty_cleanup_plan['actions']))
check('cleanup-dirty-has-block',
      any(a['action'] == 'block' for a in dirty_cleanup_plan['actions']))

run_only_cleanup = state_with([], status='aborted', cleanup_status='partial')
run_only_plan = build_next_actions(run_only_cleanup, {})
check('cleanup-run-level-debt',
      any(a['worker'] == 'run#1' and a['action'] == 'revalidate'
          for a in run_only_plan['actions']))

state_before = json.loads(json.dumps(cleanup_state, ensure_ascii=False))
observation_before = json.loads(json.dumps(
    {'a#1': cleanup_observation(archived)}, ensure_ascii=False))
build_next_actions(state_before, observation_before)
check('pure-state-not-mutated', state_before == cleanup_state)
check('pure-observations-not-mutated',
      observation_before['a#1']['branch']['name'] == archived['branch'])

# CLI：自动定位最近未完成 run、显式 run、错误输入与严格只读。
with tempfile.TemporaryDirectory(prefix='threadweave-next-') as d:
    d = Path(d)
    repo = d / 'repo'
    repo.mkdir()

    write_state_atomic(run_state_path(repo, 'run-incomplete'), {
        'runId': 'run-incomplete', 'status': 'observing', 'workers': [],
        'cleanup': {'status': 'pending'},
    })
    time.sleep(0.01)
    write_state_atomic(run_state_path(repo, 'run-complete-newer'), {
        'runId': 'run-complete-newer', 'status': 'complete', 'workers': [],
        'cleanup': {'status': 'complete'},
    })

    latest = next_plan(repo)
    check('cli-next-latest-incomplete', latest['runId'] == 'run-incomplete')

    output = io.StringIO()
    with redirect_stdout(output):
        rc = main(['next', '--repo', str(repo)])
    parsed = json.loads(output.getvalue())
    check('cli-next-exit-zero', rc == 0)
    check('cli-next-json-run', parsed['runId'] == 'run-incomplete')

    complete_state_path = run_state_path(repo, 'run-complete-newer')
    before_bytes = complete_state_path.read_bytes()
    before_mtime = complete_state_path.stat().st_mtime_ns
    output = io.StringIO()
    with redirect_stdout(output):
        rc = main(['next', '--repo', str(repo), '--run', 'run-complete-newer'])
    check('cli-explicit-complete-exit-zero', rc == 0)
    check('cli-explicit-complete-no-actions', json.loads(output.getvalue())['actions'] == [])
    check('cli-state-content-unchanged', complete_state_path.read_bytes() == before_bytes)
    check('cli-state-mtime-unchanged',
          complete_state_path.stat().st_mtime_ns == before_mtime)

    bad_obs = d / 'bad-observations.json'
    bad_obs.write_text('{not json', encoding='utf-8')
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = main(['next', '--repo', str(repo), '--run', 'run-incomplete',
                   '--observations', str(bad_obs)])
    check('cli-invalid-json-nonzero', rc == 2)
    check('cli-invalid-json-clear-error', 'invalid observations JSON' in stderr.getvalue())

    wrong_obs = d / 'wrong-observations.json'
    wrong_obs.write_text('[]', encoding='utf-8')
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = main(['next', '--repo', str(repo), '--run', 'run-incomplete',
                   '--observations', str(wrong_obs)])
    check('cli-observations-object-required', rc == 2)
    check('cli-observations-object-error', 'must be an object' in stderr.getvalue())

    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = main(['next', '--repo', str(repo), '--run', 'missing-run'])
    check('cli-missing-run-nonzero', rc == 2)
    check('cli-missing-run-error', 'run not found' in stderr.getvalue())

    only_complete = d / 'only-complete'
    only_complete.mkdir()
    write_state_atomic(run_state_path(only_complete, 'done'), {
        'runId': 'done', 'status': 'complete', 'workers': [],
        'cleanup': {'status': 'complete'},
    })
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = main(['next', '--repo', str(only_complete)])
    check('cli-no-incomplete-nonzero', rc == 2)
    check('cli-no-incomplete-error', 'no incomplete run' in stderr.getvalue())

    array_state = run_state_path(repo, 'run-array')
    array_state.parent.mkdir(parents=True, exist_ok=True)
    array_state.write_text('[]', encoding='utf-8')
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = main(['next', '--repo', str(repo), '--run', 'run-array'])
    check('cli-array-state-nonzero', rc == 2)
    check('cli-array-state-error', 'must be a JSON object' in stderr.getvalue())

for marker in ({'resolved': True}, {'status': 'passed'}, {'blocked': False}):
    actions = build_next_actions({'runId':'blocked', 'status':'observing',
                                 'workers':[], 'integrationBlocked':marker})['actions']
    check('truthy-block-cannot-self-clear-' + str(marker),
          any(a['action']=='recover_integration' for a in actions))
try:
    build_next_actions({'status':'complete', 'cleanup':{'status':'complete'},
                        'workers':[], 'integrationBlocked':{'role':'a'}})
except ValueError:
    check('contradictory-complete-block-rejected', True)
else:
    raise AssertionError('complete concealed unresolved block')
with tempfile.TemporaryDirectory() as td:
    write_state_atomic(run_state_path(td,'expected'), {'runId':'different','workers':[]})
    try: next_plan(td,'expected')
    except ValueError as exc: check('cli-rejects-run-identity-mismatch', 'identity mismatch' in str(exc))
    else: raise AssertionError('run identity mismatch accepted')

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Read-only next recovery suggestions; no host API calls or cleanup execution',
}, ensure_ascii=False, indent=2))

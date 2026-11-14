"""验证中断恢复分类，不调用宿主 API，也不修改真实仓库。"""
import json
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from recovery import (  # noqa: E402
    build_preservation_record,
    build_recovery_plan,
    classify_worker_recovery,
    classify_thread_presence,
)

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


base_worker = {
    'role': 'a', 'attempt': 1, 'state': 'ready',
    'threadId': 't-a', 'branch': 'tw/a-1', 'baseSha': 'a' * 40,
}
base_observed = {
    'thread_exists': True, 'thread_status': 'idle',
    'worktree_exists': True, 'branch': 'tw/a-1',
    'head': 'a' * 40, 'status_porcelain': '',
}

check('terminal-skip',
      classify_worker_recovery(dict(base_worker, state='archived'), base_observed) == 'skip')
check('missing-thread-unconfirmed-revalidate',
      classify_worker_recovery(dict(base_worker, threadId=None), {'thread_exists': False}) == 'revalidate')
check('thread-presence-classification',
      classify_thread_presence({'thread_exists': False}) == 'missing_unconfirmed')
confirmed_missing = dict(
    base_observed, thread_exists=False, thread_status=None,
    thread_absence_confirmed=True,
    thread_absence_evidence='list_threads exact lookup returned not found',
    worktree_exists=False, branch_preserved=True,
    report_present=False, notes_present=False,
)
check('confirmed-missing-preserve-first',
      classify_worker_recovery(dict(base_worker, threadId=None), confirmed_missing)
      == 'preserve_then_recreate')
preserved = build_preservation_record(dict(base_worker, threadId=None), confirmed_missing)
check('preservation-record-hash', len(preserved['preservationSha']) == 64)
check('confirmed-missing-after-preservation-recreate',
      classify_worker_recovery(
          dict(base_worker, threadId=None, recoveryPreservation=preserved),
          confirmed_missing) == 'recreate')
check('replacement-limit-block',
      classify_worker_recovery(dict(base_worker, attempt=2, threadId=None),
                               confirmed_missing, replacement_limit=1) == 'block')
dirty_missing = dict(
    confirmed_missing, worktree_exists=True, status_porcelain=' M notes.md',
    unmerged_commits=False, dirty_patch_archived=False,
    report_present=False, notes_present=False,
)
check('dirty-missing-preserve-work',
      classify_worker_recovery(dict(base_worker, threadId=None), dirty_missing)
      == 'preserve_work')
check('unknown-thread-revalidate',
      classify_worker_recovery(base_worker, dict(base_observed, thread_exists=None)) == 'revalidate')
check('dispatch-intent-pending-revalidate',
      classify_worker_recovery(
          dict(base_worker, dispatchIntent={'result': 'pending'}),
          base_observed) == 'revalidate')
check('pending-client-revalidate',
      classify_worker_recovery(
          dict(base_worker, state='pending', threadId=None),
          {'thread_exists': False}) == 'revalidate')
check('running-committed-reuse',
      classify_worker_recovery(
          dict(base_worker, state='running'),
          dict(base_observed, head='b' * 40, base_is_ancestor=True)) == 'reuse')
check('blocked-committed-reuse',
      classify_worker_recovery(
          dict(base_worker, state='blocked'),
          dict(base_observed, head='b' * 40, base_is_ancestor=True)) == 'reuse')
check('reported-missing-report-block',
      classify_worker_recovery(
          dict(base_worker, state='reported'),
          dict(base_observed, head='b' * 40, base_is_ancestor=True)) == 'block')
check('reported-committed-reuse',
      classify_worker_recovery(
          dict(base_worker, state='reported'),
          dict(base_observed, head='b' * 40,
               base_is_ancestor=True, report_sha='b' * 40)) == 'reuse')
check('reported-wrong-report-block',
      classify_worker_recovery(
          dict(base_worker, state='reported'),
          dict(base_observed, head='b' * 40,
               base_is_ancestor=True, report_sha='c' * 40)) == 'block')
check('reported-not-ancestor-block',
      classify_worker_recovery(
          dict(base_worker, state='reported'),
          dict(base_observed, head='b' * 40, base_is_ancestor=False)) == 'block')
check('healthy-reuse',
      classify_worker_recovery(base_worker, base_observed) == 'reuse')
check('active-revalidate',
      classify_worker_recovery(base_worker, dict(base_observed, thread_status='active')) == 'revalidate')
check('missing-worktree-revalidate',
      classify_worker_recovery(base_worker, dict(base_observed, worktree_exists=False)) == 'revalidate')
check('branch-mismatch-block',
      classify_worker_recovery(base_worker, dict(base_observed, branch='tw/other')) == 'block')
check('head-drift-block',
      classify_worker_recovery(base_worker, dict(base_observed, head='b' * 40)) == 'block')
check('dirty-block',
      classify_worker_recovery(base_worker, dict(base_observed, status_porcelain='?? x')) == 'block')

state = {
    'workers': [
        dict(base_worker, state='dispatched'),
        dict(base_worker, role='b', threadId='t-b', branch='tw/b-1'),
    ]
}
observations = {
    'a#1': base_observed,
    'b#1': dict(base_observed, branch='tw/b-1'),
}
plan = build_recovery_plan(state, observations)
check('plan-has-two-workers', set(plan) == {'a#1', 'b#1'})
check('dispatched-reuse-flags-risk',
      plan['a#1']['action'] == 'reuse' and plan['a#1']['duplicate_dispatch_risk'])
check('ready-reuse-no-risk',
      plan['b#1']['action'] == 'reuse' and not plan['b#1']['duplicate_dispatch_risk'])

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Recovery classification only; no host API calls',
}, ensure_ascii=False, indent=2))

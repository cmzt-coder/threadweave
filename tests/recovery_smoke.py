"""验证中断恢复分类，不调用宿主 API，也不修改真实仓库。"""
import json
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from recovery import build_recovery_plan, classify_worker_recovery  # noqa: E402

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
check('missing-thread-recreate',
      classify_worker_recovery(dict(base_worker, threadId=None), {'thread_exists': False}) == 'recreate')
check('replacement-limit-block',
      classify_worker_recovery(dict(base_worker, attempt=2, threadId=None),
                               {'thread_exists': False}, replacement_limit=1) == 'block')
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

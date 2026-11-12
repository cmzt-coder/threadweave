"""验证清理分类、停止证据与未合并成果保护，不执行真实删除。"""
import json
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from cleanup import (  # noqa: E402
    build_cleanup_plan, classify_automation_cleanup, classify_branch_cleanup,
    classify_report_cleanup, classify_worktree_cleanup, verify_stop_evidence,
)

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


clean = {
    'worktree_exists': True, 'status_porcelain': '',
    'unmerged_commits': False, 'worker_status': 'idle',
}
check('missing-worktree-idempotent',
      classify_worktree_cleanup({'worktree_exists': False}, True) == 'already_removed')
check('dirty-worktree-protected',
      classify_worktree_cleanup(dict(clean, status_porcelain='?? x'), True) == 'protect_dirty')
check('unmerged-worktree-protected',
      classify_worktree_cleanup(dict(clean, unmerged_commits=True), True) == 'protect_unmerged')
check('report-missing-protected',
      classify_worktree_cleanup(clean, False) == 'protect_report')
check('active-worker-protected',
      classify_worktree_cleanup(dict(clean, worker_status='active'), True) == 'protect_active')
check('clean-worktree-removable',
      classify_worktree_cleanup(clean, True) == 'remove')

check('missing-branch-idempotent',
      classify_branch_cleanup({'branch_exists': False}) == 'already_removed')
check('unmerged-branch-protected',
      classify_branch_cleanup({'branch_exists': True, 'merged_into_integration': False}) == 'protect_unmerged')
check('merged-branch-deletable',
      classify_branch_cleanup({'branch_exists': True, 'merged_into_integration': True}) == 'delete')

check('unverified-report-protected',
      classify_report_cleanup({'archived_and_verified': False}) == 'protect_unverified')
check('verified-report-deletable',
      classify_report_cleanup({'archived_and_verified': True}) == 'delete_source')

check('foreign-automation-protected',
      classify_automation_cleanup({'automation_exists': True, 'run_owned': False}) == 'protect_not_owned')
check('run-automation-deletable',
      classify_automation_cleanup({'automation_exists': True, 'run_owned': True}) == 'delete')

check('stop-evidence-complete',
      verify_stop_evidence({'thread_idle': True, 'final_answer_present': True, 'no_active_turn': True}) == 'stopped')
check('stop-evidence-incomplete',
      verify_stop_evidence({'thread_idle': True, 'final_answer_present': False, 'no_active_turn': True}) == 'unverified')

state = {'workers': [{'role': 'a', 'attempt': 1}]}
observations = {
    'a#1': {
        'worktree': clean,
        'report_archived': True,
        'branch': {'branch_exists': True, 'merged_into_integration': True},
        'stop': {'thread_idle': True, 'final_answer_present': True, 'no_active_turn': True},
    }
}
plan = build_cleanup_plan(state, observations)
check('plan-actions',
      plan['a#1'] == {'worktree': 'remove', 'branch': 'delete', 'stop_evidence': 'stopped'})

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Cleanup classification only; no real deletion',
}, ensure_ascii=False, indent=2))

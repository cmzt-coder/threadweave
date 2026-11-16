"""验证清理分类、停止证据与未合并成果保护，不执行真实删除。"""
import json
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from cleanup import (  # noqa: E402
    cleanup_plan_fingerprint,
    recheck_cleanup_plan,
    build_cleanup_plan, classify_automation_cleanup, classify_branch_cleanup,
    classify_report_cleanup, classify_worktree_cleanup, verify_stop_evidence,
)

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


clean = {
    'worktree_exists': True, 'acceptance_verified': True, 'status_porcelain': '',
    'unmerged_commits': False, 'worker_status': 'idle',
    'owned_by_run': True, 'is_primary': False,
    'stop': {
        'thread_idle': True, 'final_answer_present': True, 'no_active_turn': True,
    },
}
check('missing-worktree-idempotent',
      classify_worktree_cleanup({'worktree_exists': False}, True) == 'already_removed')
check('dirty-worktree-protected',
      classify_worktree_cleanup(dict(clean, status_porcelain='?? x'), True) == 'protect_dirty')
check('unmerged-worktree-protected',
      classify_worktree_cleanup(dict(clean, unmerged_commits=True), True) == 'protect_unmerged')
check('acceptance-missing-review',
      classify_worktree_cleanup({**clean, 'acceptance_verified': None}, True)
      == 'protect_acceptance_unknown')
check('acceptance-failed-blocked',
      classify_worktree_cleanup({**clean, 'acceptance_verified': False}, True)
      == 'protect_acceptance_failed')
check('report-missing-protected',
      classify_worktree_cleanup(clean, False) == 'protect_report')
check('active-worker-protected',
      classify_worktree_cleanup(dict(clean, worker_status='active'), True) == 'protect_active')
check('unknown-git-protected',
      classify_worktree_cleanup(dict(clean, status_porcelain=None), True) == 'protect_unknown')
check('not-owned-protected',
      classify_worktree_cleanup(dict(clean, owned_by_run=False), True) == 'protect_not_owned')
check('primary-protected',
      classify_worktree_cleanup(dict(clean, is_primary=True), True) == 'protect_not_owned')
check('unverified-stop-protected',
      classify_worktree_cleanup(
          dict(clean, stop={'thread_idle': True, 'final_answer_present': False,
                            'no_active_turn': True}), True) == 'protect_unverified')
check('clean-worktree-removable',
      classify_worktree_cleanup(clean, True) == 'remove')
check('shared-worktree-protected',
      classify_worktree_cleanup(dict(clean, is_shared=True), True) == 'protect_shared')
check('unknown-worktree-protected',
      classify_worktree_cleanup({'worktree_exists': None}, True) == 'protect_unknown')
check('foreign-branch-protected',
      classify_branch_cleanup({
          'branch_exists': True, 'merged_into_integration': True,
          'owned_by_run': False, 'checked_out': False,
      }) == 'protect_not_owned')
check('checked-out-branch-protected',
      classify_branch_cleanup({
          'branch_exists': True, 'merged_into_integration': True,
          'owned_by_run': True, 'checked_out': True,
      }) == 'protect_checked_out')

check('missing-branch-idempotent',
      classify_branch_cleanup({'branch_exists': False}) == 'already_removed')
check('unmerged-branch-protected',
      classify_branch_cleanup({
          'branch_exists': True, 'merged_into_integration': False,
          'owned_by_run': True, 'checked_out': False,
      }) == 'protect_unmerged')
check('merged-branch-deletable',
      classify_branch_cleanup({
          'branch_exists': True, 'merged_into_integration': True,
          'owned_by_run': True, 'checked_out': False,
      }) == 'delete')

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
        'acceptance_verified': True,
        'report_archived': True,
        'branch': {
            'branch_exists': True, 'merged_into_integration': True,
            'owned_by_run': True, 'checked_out': False,
        },
        'stop': {'thread_idle': True, 'final_answer_present': True, 'no_active_turn': True},
    }
}
plan = build_cleanup_plan(state, observations)
stale_state = {'workers': [{'role': 'a', 'attempt': 1}]}
stale_observations = {
    'a#1': {
        'observedAt': '2026-11-12T10:00:00+08:00',
        'worktree': clean,
        'acceptance_verified': True,
        'report_archived': True,
        'branch': {
            'branch_exists': True, 'merged_into_integration': True,
            'owned_by_run': True, 'checked_out': False,
        },
        'stop': {
            'thread_idle': True, 'final_answer_present': True, 'no_active_turn': True,
        },
    }
}
stale_plan = build_cleanup_plan(
    stale_state, stale_observations, max_evidence_age_seconds=300,
    now='2026-11-12T10:10:00+08:00')
check('stale-cleanup-protected', stale_plan['a#1']['worktree'] == 'protect_stale')

check('plan-actions', plan['a#1'] == {
    'worktree': 'remove', 'branch': 'delete', 'stop_evidence': 'stopped',
    'classification': 'SAFE', 'reasons': [],
    'resourceLevels': {'worktree': 'SAFE', 'branch': 'SAFE', 'stop_evidence': 'SAFE'},
})
review_observations = {
    'a#1': {
        'worktree': {**clean, 'acceptance_verified': None},
        'report_archived': True,
        'branch': {
            'branch_exists': True, 'merged_into_integration': True,
            'owned_by_run': True, 'checked_out': False,
        },
        'stop': {'thread_idle': True, 'final_answer_present': True, 'no_active_turn': True},
    }
}
review_plan = build_cleanup_plan(state, review_observations)
check('plan-review-without-acceptance',
      review_plan['a#1']['classification'] == 'REVIEW'
      and review_plan['a#1']['worktree'] == 'protect_acceptance_unknown')
blocked_observations = {
    'a#1': {
        'worktree': {**clean, 'status_porcelain': ' M a.txt'},
        'acceptance_verified': True,
        'report_archived': True,
        'branch': {
            'branch_exists': True, 'merged_into_integration': True,
            'owned_by_run': True, 'checked_out': False,
        },
        'stop': {'thread_idle': True, 'final_answer_present': True, 'no_active_turn': True},
    }
}
blocked_plan = build_cleanup_plan(state, blocked_observations)
check('plan-blocked-dirty', blocked_plan['a#1']['classification'] == 'BLOCKED')
fingerprint = cleanup_plan_fingerprint(plan)
recheck = recheck_cleanup_plan(
    state, plan, observations, expected_fingerprint=fingerprint,
    max_evidence_age_seconds=None)
check('recheck-safe-eligible', recheck['status'] == 'eligible')
check('recheck-fingerprint', recheck['fingerprints']['initial'] == fingerprint)
changed_observations = {
    'a#1': {
        'worktree': {**clean, 'status_porcelain': ' M a.txt'},
        'acceptance_verified': True,
        'report_archived': True,
        'branch': {
            'branch_exists': True, 'merged_into_integration': True,
            'owned_by_run': True, 'checked_out': False,
        },
        'stop': {'thread_idle': True, 'final_answer_present': True, 'no_active_turn': True},
    }
}
drifted = recheck_cleanup_plan(
    state, plan, changed_observations, expected_fingerprint=fingerprint,
    max_evidence_age_seconds=None)
check('recheck-drift-blocked', drifted['status'] == 'blocked' and drifted['planChanged'])
wrong_fingerprint = recheck_cleanup_plan(
    state, plan, observations, expected_fingerprint='0' * 64,
    max_evidence_age_seconds=None)
check('recheck-wrong-fingerprint-blocked',
      wrong_fingerprint['status'] == 'blocked' and not wrong_fingerprint['readOnly'] is False)

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Cleanup classification only; no real deletion',
}, ensure_ascii=False, indent=2))

"""Threadweave 清理分类参考实现。

O16a 只生成清理计划和三级分类，不执行删除，也不调用宿主 API。

SAFE: 已满足验收、干净、无未合并成果、报告归档、停止证据和归属检查。
REVIEW: 证据缺失/陈旧，需要重新采集或人工确认。
BLOCKED: 脏工作树、未合并成果、活动线程、primary/shared 或非本 run 资源。
"""

CLEANUP_LEVELS = ('SAFE', 'REVIEW', 'BLOCKED')

SAFE_ACTIONS = {'remove', 'delete', 'delete_source', 'already_removed'}
REVIEW_ACTIONS = {'protect_unknown', 'protect_stale', 'protect_report',
                  'protect_unverified', 'protect_acceptance_unknown'}
BLOCKED_ACTIONS = {'protect_dirty', 'protect_unmerged', 'protect_active',
                   'protect_shared', 'protect_not_owned', 'protect_checked_out',
                   'protect_acceptance_failed'}


def action_level(action):
    if action in SAFE_ACTIONS:
        return 'SAFE'
    if action in BLOCKED_ACTIONS:
        return 'BLOCKED'
    return 'REVIEW'


def classify_worktree_cleanup(observed, report_archived, acceptance_verified=None):
    """返回 worktree 清理动作；缺失验收证据时只进入 REVIEW。"""
    if acceptance_verified is None:
        acceptance_verified = observed.get('acceptance_verified')
    if observed.get('worktree_exists') is None:
        return 'protect_unknown'
    if not observed.get('worktree_exists'):
        return 'already_removed'
    if observed.get('is_shared'):
        return 'protect_shared'
    if observed.get('is_primary') or not observed.get('owned_by_run'):
        return 'protect_not_owned'
    if observed.get('status_porcelain') is None or observed.get('unmerged_commits') is None:
        return 'protect_unknown'
    if observed.get('status_porcelain'):
        return 'protect_dirty'
    if observed.get('unmerged_commits'):
        return 'protect_unmerged'
    if acceptance_verified is None:
        return 'protect_acceptance_unknown'
    if not acceptance_verified:
        return 'protect_acceptance_failed'
    if not report_archived:
        return 'protect_report'
    stop = observed.get('stop', {})
    required = ('thread_idle', 'final_answer_present', 'no_active_turn')
    if not all(stop.get(key) for key in required):
        return 'protect_unverified'
    if observed.get('worker_status') not in ('idle', 'archived'):
        return 'protect_active'
    return 'remove'


def classify_branch_cleanup(observed):
    """返回分支清理动作；未合并分支保留。"""
    if observed.get('branch_exists') is None:
        return 'protect_unknown'
    if not observed.get('branch_exists'):
        return 'already_removed'
    if not observed.get('owned_by_run'):
        return 'protect_not_owned'
    if observed.get('checked_out'):
        return 'protect_checked_out'
    if observed.get('merged_into_integration'):
        return 'delete'
    return 'protect_unmerged'


def classify_report_cleanup(observed):
    """返回源报告清理动作；归档校验未通过则保留。"""
    if observed.get('archived_and_verified'):
        return 'delete_source'
    return 'protect_unverified'


def classify_automation_cleanup(observed):
    """只允许删除本 run 创建的 automation。"""
    if not observed.get('automation_exists'):
        return 'already_removed'
    if observed.get('run_owned'):
        return 'delete'
    return 'protect_not_owned'


def verify_stop_evidence(observed):
    """检查停止证据；三者缺一即视为未验证。"""
    required = ('thread_idle', 'final_answer_present', 'no_active_turn')
    return 'stopped' if all(observed.get(key) for key in required) else 'unverified'


def _overall_level(resource_levels):
    values = set(resource_levels.values())
    if 'BLOCKED' in values:
        return 'BLOCKED'
    if 'REVIEW' in values:
        return 'REVIEW'
    return 'SAFE'


def build_cleanup_plan(state, observations, max_evidence_age_seconds=None, now=None):
    """为每个 worker 生成 worktree / branch / stop 清理计划与三级分类。"""
    plan = {}
    for worker in state.get('workers', []):
        key = f"{worker.get('role')}#{worker.get('attempt', 1)}"
        observed = observations.get(key, {})
        if max_evidence_age_seconds is not None:
            from datetime import datetime
            observed_at = observed.get('observedAt')
            stale = True
            if observed_at:
                try:
                    observed_dt = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
                    now_dt = (datetime.fromisoformat(now.replace('Z', '+00:00'))
                              if now else datetime.now(observed_dt.tzinfo))
                    stale = (now_dt - observed_dt).total_seconds() > max_evidence_age_seconds
                except ValueError:
                    stale = True
            if stale:
                plan[key] = {
                    'worktree': 'protect_stale',
                    'branch': 'protect_stale',
                    'stop_evidence': 'unverified',
                    'classification': 'REVIEW',
                    'reasons': ['cleanup evidence is stale'],
                    'resourceLevels': {
                        'worktree': 'REVIEW', 'branch': 'REVIEW',
                        'stop_evidence': 'REVIEW',
                    },
                }
                continue
        worktree_observed = dict(observed.get('worktree', {}))
        worktree_observed.setdefault('stop', observed.get('stop', {}))
        worktree_observed['acceptance_verified'] = observed.get('acceptance_verified')
        worktree_action = classify_worktree_cleanup(
            worktree_observed, observed.get('report_archived', False),
            worktree_observed.get('acceptance_verified'))
        branch_action = classify_branch_cleanup(observed.get('branch', {}))
        stop_action = verify_stop_evidence(observed.get('stop', {}))
        resource_levels = {
            'worktree': action_level(worktree_action),
            'branch': action_level(branch_action),
            'stop_evidence': 'SAFE' if stop_action == 'stopped' else 'REVIEW',
        }
        reasons = []
        for resource, action in (
                ('worktree', worktree_action), ('branch', branch_action)):
            if action not in SAFE_ACTIONS:
                reasons.append(f'{resource}: {action}')
        if stop_action != 'stopped':
            reasons.append('stop_evidence: unverified')
        plan[key] = {
            'worktree': worktree_action,
            'branch': branch_action,
            'stop_evidence': stop_action,
            'classification': _overall_level(resource_levels),
            'reasons': reasons,
            'resourceLevels': resource_levels,
        }
    return plan

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


def cleanup_plan_fingerprint(plan):
    """生成清理计划 canonical fingerprint，用于执行前漂移比较。"""
    import hashlib
    import json
    text = json.dumps(plan, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def recheck_cleanup_plan(state, initial_plan, observations,
                         expected_fingerprint=None, max_evidence_age_seconds=300,
                         now=None):
    """执行前只读重验；不删除资源，也不把旧 fingerprint 当成持续安全保证。"""
    if not isinstance(initial_plan, dict):
        raise ValueError('initial_plan must be an object')
    initial_fp = cleanup_plan_fingerprint(initial_plan)
    fingerprints = {
        'initial': initial_fp,
        'expected': expected_fingerprint,
    }
    if expected_fingerprint is not None and expected_fingerprint != initial_fp:
        return {
            'status': 'blocked',
            'readOnly': True,
            'deletionPerformed': False,
            'reason': 'expected fingerprint does not match the approved plan',
            'fingerprints': fingerprints,
            'planChanged': None,
            'currentPlan': None,
        }
    current_plan = build_cleanup_plan(
        state, observations, max_evidence_age_seconds=max_evidence_age_seconds,
        now=now)
    current_fp = cleanup_plan_fingerprint(current_plan)
    classifications = {
        key: value.get('classification')
        for key, value in current_plan.items()
        if isinstance(value, dict)
    }
    levels = set(classifications.values())
    plan_changed = current_fp != initial_fp
    if plan_changed:
        status = 'blocked'
        reason = 'cleanup plan changed since approval; re-plan and obtain authorization again'
    elif 'BLOCKED' in levels:
        status = 'blocked'
        reason = 'current cleanup observation contains blocked resources'
    elif 'REVIEW' in levels:
        status = 'review'
        reason = 'current cleanup observation requires fresh evidence or manual review'
    else:
        status = 'eligible'
        reason = 'current plan is SAFE; explicit execution authorization is still required'
    return {
        'status': status,
        'readOnly': True,
        'deletionPerformed': False,
        'executionApproved': status == 'eligible',
        'requiresExplicitExecutionAuthorization': status == 'eligible',
        'reason': reason,
        'fingerprints': {
            'initial': initial_fp,
            'current': current_fp,
            'expected': expected_fingerprint,
        },
        'planChanged': plan_changed,
        'classifications': classifications,
        'currentPlan': current_plan,
    }

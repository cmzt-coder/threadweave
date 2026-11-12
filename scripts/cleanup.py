"""Threadweave 清理分类参考实现。

对 worktree / branch / report / automation 给出幂等清理动作，并检查停止证据。
纯函数，不执行删除，也不调用宿主 API；调用方必须依据动作再执行安全清理。
"""


def classify_worktree_cleanup(observed, report_archived):
    """返回 worktree 清理动作；缺失证据、非本 run、primary 一律保护。"""
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


def build_cleanup_plan(state, observations):
    """为每个 worker 生成 worktree / branch / 停止证据动作。"""
    plan = {}
    for worker in state.get('workers', []):
        key = f"{worker.get('role')}#{worker.get('attempt')}"
        observed = observations.get(key, {})
        worktree_observed = dict(observed.get('worktree', {}))
        worktree_observed.setdefault('stop', observed.get('stop', {}))
        plan[key] = {
            'worktree': classify_worktree_cleanup(
                worktree_observed, observed.get('report_archived', False)),
            'branch': classify_branch_cleanup(observed.get('branch', {})),
            'stop_evidence': verify_stop_evidence(observed.get('stop', {})),
        }
    return plan

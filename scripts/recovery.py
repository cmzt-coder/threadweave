"""Threadweave 中断恢复分类参考实现。

根据持久状态与外部观察（thread / worktree / branch / HEAD / 干净状态）给出恢复动作，
用于避免重复创建和重复派发。纯函数，不调用宿主 API，也不修改仓库。
"""
TERMINAL_STATES = {'integrated', 'archived', 'failed', 'cancelled'}


def classify_worker_recovery(worker, observed, replacement_limit=1):
    """返回单个 worker 的恢复动作：skip/recreate/block/revalidate/reuse。"""
    state = worker.get('state')
    if state in TERMINAL_STATES:
        return 'skip'

    # unknown 先重验，不 recreation
    if observed.get('thread_exists') is None:
        return 'revalidate'

    # 已记录派发意图但结果未确认：先核对，不盲目重发或重建
    intent = worker.get('dispatchIntent', {})
    if intent.get('result') in ('pending', 'unknown'):
        return 'revalidate'

    # pending 有 clientThreadId 但无真实 threadId：先解析，不 recreation
    if state == 'pending' and not worker.get('threadId'):
        return 'revalidate'

    if observed.get('thread_exists') is False or not worker.get('threadId'):
        if worker.get('attempt', 1) > replacement_limit:
            return 'block'
        return 'recreate'

    if observed.get('thread_status') not in (None, 'idle'):
        return 'revalidate'
    if not observed.get('worktree_exists'):
        return 'revalidate'
    if observed.get('branch') != worker.get('branch'):
        return 'block'

    head = observed.get('head')
    base = worker.get('baseSha')
    if state == 'reported':
        # 执行后：base 必须是 HEAD 的祖先，报告 SHA 必须匹配 branch tip
        if not observed.get('base_is_ancestor', False):
            return 'block'
        report_sha = observed.get('report_sha')
        if report_sha and report_sha != head:
            return 'block'
    else:
        # 初始化阶段：HEAD 必须精确等于 base
        if head != base:
            return 'block'

    if observed.get('status_porcelain'):
        return 'block'
    return 'reuse'


def build_recovery_plan(state, observations, replacement_limit=1):
    """为每个 worker 生成恢复动作，并标记重复派发风险。"""
    plan = {}
    for worker in state.get('workers', []):
        key = f"{worker.get('role')}#{worker.get('attempt')}"
        action = classify_worker_recovery(
            worker, observations.get(key, {}), replacement_limit)
        plan[key] = {
            'action': action,
            'duplicate_dispatch_risk': action == 'reuse' and worker.get('state') == 'dispatched',
        }
    return plan

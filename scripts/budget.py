"""Threadweave 运行时预算、停滞与重试参考实现。

成本只在调用方提供实际观察值时判断；未提供的成本或耗时标记 unknown，不做估算。
纯函数，不调用宿主 API。
"""
from datetime import datetime

TERMINAL_STATES = {'archived', 'failed', 'cancelled', 'integrated'}


def _as_datetime(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def check_initialization(created_at, now, limit_seconds=300):
    """检查 worker 初始化是否超过默认五分钟。"""
    elapsed = (_as_datetime(now) - _as_datetime(created_at)).total_seconds()
    status = 'deadline_exceeded' if elapsed > limit_seconds else 'ok'
    return {'status': status, 'elapsedSeconds': elapsed}


def check_stall(last_progress_at, now, inactivity_seconds=360, grace_seconds=120,
                status_requested_at=None, worker_state=None):
    """区分无活动请求、宽限期与停滞；终态不适用。"""
    if worker_state in TERMINAL_STATES:
        return {'status': 'not_applicable', 'elapsedSeconds': 0}
    if status_requested_at is not None:
        grace_elapsed = (_as_datetime(now) - _as_datetime(status_requested_at)).total_seconds()
        status = 'stalled' if grace_elapsed >= grace_seconds else 'grace'
        return {'status': status, 'graceElapsedSeconds': grace_elapsed}
    elapsed = (_as_datetime(now) - _as_datetime(last_progress_at)).total_seconds()
    if elapsed >= inactivity_seconds:
        return {'status': 'request_status', 'elapsedSeconds': elapsed}
    return {'status': 'ok', 'elapsedSeconds': elapsed}


def check_retry(attempt, replacement_limit=1):
    """每个角色默认最多一次替换；超出上限阻塞。"""
    status = 'blocked' if attempt > replacement_limit else 'ok'
    return {'status': status, 'attempt': attempt, 'limit': replacement_limit}


def check_run_budget(observed, budget):
    """比较可观察的耗时/成本与预算；缺失值返回 unknown。"""
    result = {}
    elapsed = observed.get('elapsedSeconds')
    limit_elapsed = budget.get('maxElapsedSeconds')
    result['elapsed'] = 'unknown' if elapsed is None or limit_elapsed is None else (
        'exceeded' if elapsed > limit_elapsed else 'ok')
    cost = observed.get('costUsd')
    limit_cost = budget.get('maxCostUsd')
    result['cost'] = 'unknown' if cost is None or limit_cost is None else (
        'exceeded' if cost > limit_cost else 'ok')
    return result


def summarize_budget(state, observations, now,
                     initialization_limit=300, inactivity=360,
                     grace=120, replacement_limit=1):
    """汇总每个 worker 的初始化、停滞与重试状态。"""
    summary = {}
    for worker in state.get('workers', []):
        key = f"{worker.get('role')}#{worker.get('attempt')}"
        observed = observations.get(key, {})
        created_at = worker.get('createdAt')
        last_progress = worker.get('lastProgressAt') or worker.get('lastObservation')
        status_requested = worker.get('statusRequestedAt')
        summary[key] = {
            'initialization': (
                check_initialization(created_at, now, initialization_limit)
                if created_at else {'status': 'unknown'}),
            'stall': (
                check_stall(last_progress, now, inactivity, grace,
                            status_requested, worker.get('state'))
                if last_progress else {'status': 'unknown'}),
            'retry': check_retry(worker.get('attempt', 1), replacement_limit),
        }
    return summary

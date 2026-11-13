"""失败归因、确定性与恢复建议的纯逻辑实现。

错误来源必须由调用方依据实际证据传入；本模块不根据超时文本推断进程退出，
也不把 retryable=true 当作重试授权。
"""
from datetime import datetime, timezone


FAILURE_SOURCES = (
    'provider',
    'host_api',
    'supervisor_process',
    'workspace_setup',
    'model_output',
    'acceptance',
    'worker_process',
    'unknown',
)
RESULT_CERTAINTIES = ('confirmed_failure', 'unknown')
PROCESS_SOURCES = {'supervisor_process', 'worker_process'}
UNKNOWN_CODES = {
    'create_timeout',
    'dispatch_result_unknown',
    'send_result_unknown',
    'host_result_unknown',
}


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')
    return value.strip()


def default_recommendation(source, code, result_certainty):
    """按来源、错误码与确定性给出保守建议，不直接授权外部动作。"""
    if result_certainty == 'unknown' or code in UNKNOWN_CODES:
        return 'reconcile_result'
    if code in ('authentication_failed', 'unauthorized', 'invalid_credentials'):
        return 'verify_credentials'
    if code in ('rate_limited', 'rate_limit'):
        return 'retry_after_backoff_after_leader_decision'
    if code in ('model_validation_failed', 'reasoning_validation_failed',
                'branch_validation_failed', 'project_validation_failed',
                'unknown_project'):
        return 'correct_request_before_retry'
    if code == 'handoff_model_not_preserved':
        return 'reconcile_identity_and_model_before_continuing'
    if source == 'workspace_setup':
        return 'verify_idempotency_then_rerun_setup'
    if source == 'model_output':
        return 'rerequest_or_rework_existing_worker'
    if source == 'acceptance':
        return 'repair_and_reverify'
    if source == 'worker_process':
        return 'preserve_work_and_verify_worker_process'
    if source == 'supervisor_process':
        return 'read_only_resume_after_process_check'
    return 'inspect_and_decide'


def build_failure_record(source, code, stage, message, scope='run',
                         result_certainty='confirmed_failure', retryable=False,
                         recommendation=None, evidence=None, retry_evidence=None,
                         at=None):
    """构造可持久化错误记录；未知结果强制 retryable=false。"""
    if source not in FAILURE_SOURCES:
        raise ValueError('invalid failure source')
    if result_certainty not in RESULT_CERTAINTIES:
        raise ValueError('invalid result certainty')
    code = _text(code, 'code')
    stage = _text(stage, 'stage')
    message = _text(message, 'message')
    scope = _text(scope, 'scope')
    if type(retryable) is not bool:
        raise ValueError('retryable must be boolean')
    if source in PROCESS_SOURCES and (not isinstance(evidence, str) or not evidence.strip()):
        raise ValueError('process source requires explicit evidence')
    if evidence is not None:
        evidence = _text(evidence, 'evidence')
    if retry_evidence is not None:
        retry_evidence = _text(retry_evidence, 'retry_evidence')
    if result_certainty == 'unknown':
        retryable = False
    recommendation = recommendation or default_recommendation(
        source, code, result_certainty)
    recommendation = _text(recommendation, 'recommendation')
    record = {
        'at': at or _now(),
        'scope': scope,
        'code': code,
        'message': message,
        'resolved': False,
        'source': source,
        'stage': stage,
        'resultCertainty': result_certainty,
        'retryable': retryable,
        'recommendation': recommendation,
    }
    if evidence is not None:
        record['evidence'] = evidence
    if retry_evidence is not None:
        record['retryEvidence'] = retry_evidence
    return record


def is_unresolved(value):
    return isinstance(value, dict) and value.get('resolved') is False


def latest_unresolved_failure(state, role=None, attempt=None):
    """返回最近一条匹配 worker 或 run 的未解决错误。"""
    if not isinstance(state, dict) or not isinstance(state.get('errors'), list):
        return None
    expected_scope = None
    if role is not None:
        expected_scope = f'{role}#{attempt or 1}'
    for item in reversed(state['errors']):
        if not is_unresolved(item):
            continue
        scope = item.get('scope')
        if expected_scope is None or scope in (expected_scope, 'run'):
            return dict(item)
    return None


def unresolved_failures(state):
    """返回全部未解决错误，保持持久化顺序。"""
    if not isinstance(state, dict) or not isinstance(state.get('errors'), list):
        return []
    return [dict(item) for item in state['errors'] if is_unresolved(item)]


def failure_summary(record):
    """输出给 next/leader 审阅的最小失败摘要。"""
    if not isinstance(record, dict):
        return None
    keys = ('source', 'code', 'stage', 'resultCertainty', 'retryable',
            'recommendation', 'scope', 'message', 'at')
    return {key: record.get(key) for key in keys if key in record}

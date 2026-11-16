"""O13 分层 preflight 契约与失效判断。

contract inputs 只包含稳定身份/范围字段；evidence time 单独传入，避免每次观测
时间变化导致 contractHash 漂移。本模块不调用宿主 API，也不修改状态。
"""
import hashlib
import json
from datetime import datetime, timezone


VERSION = 1


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _worker_key(role, attempt):
    if not isinstance(role, str) or not role:
        raise ValueError('worker role required')
    if type(attempt) is not int or attempt < 1:
        raise ValueError('worker attempt must be a positive integer')
    return f'{role}#{attempt}'


def build_preflight_contract(run_id, run_inputs, worker_inputs, created_at=None):
    """构造 run 级 + worker 级契约；同一内容得到相同 contractHash。"""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError('run_id required')
    if not isinstance(run_inputs, dict):
        raise ValueError('run_inputs must be an object')
    if not isinstance(worker_inputs, dict) or not worker_inputs:
        raise ValueError('worker_inputs must be a non-empty object')
    workers = {}
    for key, inputs in sorted(worker_inputs.items()):
        if not isinstance(key, str) or '#' not in key or not isinstance(inputs, dict):
            raise ValueError('worker_inputs must map role#attempt to objects')
        role, attempt_text = key.rsplit('#', 1)
        attempt = int(attempt_text)
        normalized_key = _worker_key(role, attempt)
        if normalized_key != key:
            raise ValueError('worker key must be role#attempt')
        workers[key] = {
            'role': role,
            'attempt': attempt,
            'inputHash': _hash(inputs),
            'inputs': inputs,
        }
    payload = {
        'version': VERSION,
        'runId': run_id,
        'run': {'inputHash': _hash(run_inputs), 'inputs': run_inputs},
        'workers': workers,
    }
    contract_hash = _hash(payload)
    return {
        **payload,
        'preflightId': 'pf_' + contract_hash[:16],
        'contractHash': contract_hash,
        'createdAt': created_at or _now(),
    }


def _time_invalid(value, max_age_seconds, now):
    if max_age_seconds is None:
        return None
    if not isinstance(value, str):
        return 'evidence_missing'
    try:
        observed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return 'evidence_invalid'
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    now_value = (datetime.fromisoformat(now.replace('Z', '+00:00'))
                 if isinstance(now, str) else datetime.now(timezone.utc))
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=timezone.utc)
    age = (now_value - observed.astimezone(timezone.utc)).total_seconds()
    if age < -60:
        return 'evidence_from_future'
    if age > max_age_seconds:
        return 'evidence_stale'
    return None


def evaluate_preflight(contract, current_run_inputs, current_worker_inputs,
                       run_evidence_at=None, worker_evidence_at=None,
                       max_age_seconds=None, now=None):
    """比较当前输入与证据；run 变化全局失效，worker 变化局部失效。"""
    if (not isinstance(contract, dict) or contract.get('version') != VERSION
            or not isinstance(contract.get('run'), dict)
            or not isinstance(contract.get('workers'), dict)):
        return {
            'status': 'unsupported_legacy',
            'runValid': False,
            'valid': False,
            'reason': 'legacy preflight contract is not verified',
            'workers': {},
        }
    current_run_inputs = current_run_inputs if isinstance(current_run_inputs, dict) else {}
    current_worker_inputs = current_worker_inputs if isinstance(current_worker_inputs, dict) else {}
    run_changed = _hash(current_run_inputs) != contract['run'].get('inputHash')
    run_time_issue = _time_invalid(run_evidence_at, max_age_seconds, now)
    run_valid = not run_changed and run_time_issue is None
    workers = {}
    for key, saved in contract['workers'].items():
        current = current_worker_inputs.get(key)
        current_hash = _hash(current) if isinstance(current, dict) else None
        worker_changed = current_hash != saved.get('inputHash')
        observed_at = (worker_evidence_at or {}).get(key) if isinstance(worker_evidence_at, dict) else None
        time_issue = _time_invalid(observed_at, max_age_seconds, now)
        reasons = []
        if run_changed:
            reasons.append('run_contract_changed')
        if worker_changed:
            reasons.append('worker_contract_changed')
        if run_time_issue:
            reasons.append('run_' + run_time_issue)
        if time_issue:
            reasons.append(time_issue)
        workers[key] = {
            'valid': not reasons,
            'workerChanged': worker_changed,
            'reasons': reasons,
        }
    for key in sorted(set(current_worker_inputs) - set(contract['workers'])):
        workers[key] = {
            'valid': False,
            'workerChanged': True,
            'reasons': ['worker_not_in_contract'],
        }
    return {
        'status': 'valid' if run_valid and all(x['valid'] for x in workers.values())
        else 'invalidated',
        'runValid': run_valid,
        'valid': run_valid and all(x['valid'] for x in workers.values()),
        'runChanged': run_changed,
        'runEvidenceIssue': run_time_issue,
        'workers': workers,
    }

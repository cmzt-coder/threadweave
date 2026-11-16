"""H01 宿主迁移门禁，与 O09 制品交接无关；不调用宿主或派发消息。"""
from datetime import datetime, timezone
from pathlib import Path
import copy
import re


def begin_gate(source_thread_id, operation_id, expected):
    if not source_thread_id or not operation_id:
        raise ValueError('source thread and real handoff operationId required')
    required = ('threadId', 'root', 'branch', 'head', 'model', 'thinking', 'attachments')
    if not isinstance(expected, dict) or any(k not in expected for k in required):
        raise ValueError('complete destination contract required')
    if any(not isinstance(expected[k], str) or not expected[k].strip()
           for k in required if k != 'attachments'):
        raise ValueError('destination identity fields must be non-empty strings')
    if expected['model'] == 'unknown' or expected['thinking'] == 'unknown':
        raise ValueError('explicit requested model and thinking required')
    if not Path(expected['root']).is_absolute() or not re.fullmatch('[0-9a-f]{40}', expected['head']):
        raise ValueError('absolute root and exact HEAD required')
    attachments = expected['attachments']
    if (not isinstance(attachments, list) or any(not isinstance(a, str) or not a for a in attachments)
            or len(set(attachments)) != len(attachments)):
        raise ValueError('attachments must be unique explicit identities')
    return {'status': 'pending', 'sourceThreadId': source_thread_id,
            'operationId': operation_id, 'expected': copy.deepcopy(expected),
            'receipt': None, 'observation': None, 'reasons': ['handoff reconciliation pending']}


def evaluate_gate(gate, receipt, observation, now=None):
    result = copy.deepcopy(gate)
    result.update(receipt=copy.deepcopy(receipt), observation=copy.deepcopy(observation))
    reasons = []
    if gate.get('operationBound', True) is not True:
        reasons.append('handoff_operation_not_bound')
    expected = gate['expected']
    if not isinstance(receipt, dict) or (receipt.get('operationId') != gate['operationId']
            or receipt.get('status') != 'success'
            or receipt.get('sourceThreadId') != gate['sourceThreadId']
            or receipt.get('destinationThreadId') != expected['threadId']):
        reasons.append('handoff_receipt_mismatch')
    if not isinstance(observation, dict):
        observation = {}
    try:
        observed = datetime.fromisoformat(observation['observedAt'].replace('Z', '+00:00'))
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
        if not 0 <= age <= 300:
            reasons.append('stale_or_future_observation')
    except (KeyError, TypeError, ValueError, AttributeError):
        reasons.append('missing_observation_time')
    for key in ('threadId', 'root', 'branch', 'head'):
        if observation.get(key) != expected[key]:
            reasons.append(key + '_mismatch')
    if observation.get('cwd') != expected['root']:
        reasons.append('cwd_mismatch')
    if observation.get('gitSource') != 'leader_git' or observation.get('clean') is not True:
        reasons.append('git_observation_unverified')
    actual = observation.get('attachments')
    if not isinstance(actual, list) or sorted(actual) != sorted(expected['attachments']):
        reasons.append('attachments_mismatch')
    if observation.get('attachmentSource') != 'host_artifacts':
        reasons.append('attachments_unverified')
    runtime = observation.get('runtime', {})
    if not isinstance(runtime, dict):
        runtime = {}
    if (runtime.get('source') not in ('destination_host_metadata', 'destination_turn_context')
            or runtime.get('threadId') != expected['threadId']
            or runtime.get('operationId') != gate['operationId']):
        reasons.append('runtime_unverified')
    for key in ('model', 'thinking'):
        if runtime.get(key) != expected[key]:
            reasons.append(key + '_unknown_or_mismatch')
    result['status'] = 'blocked' if reasons else 'passed'
    result['reasons'] = reasons
    return result


def require_gate(worker):
    gate = worker.get('handoffGate')
    if gate is None:
        return
    if not isinstance(gate, dict) or gate.get('status') != 'passed':
        raise RuntimeError('handoff unresolved; task dispatch/review blocked')
    current = evaluate_gate(gate, gate.get('receipt'), gate.get('observation'))
    if current['status'] != 'passed':
        raise RuntimeError('handoff evidence stale or invalid; re-observe before continuing')
    expected = gate['expected']
    if any(worker.get(k) != expected[v] for k,v in (
            ('threadId','threadId'),('worktree','root'),('branch','branch'))):
        raise RuntimeError('handoff worker identity drift')

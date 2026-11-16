"""O15 compact status 与基于 JSONL 游标的只读增量摘要。"""
import copy
import json
from pathlib import Path

from delivery import delivery_status
from next_actions import build_next_actions
from run_log import LogCursorError, read_since
from state_store import latest_incomplete_run, locate_runs, read_state, run_state_path


DEFAULT_MAX_BYTES = 7000
MIN_MAX_BYTES = 512


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _action_summary(action):
    if not isinstance(action, dict):
        return None
    result = {
        'action': action.get('action'),
        'worker': action.get('worker'),
        'priority': action.get('priority'),
        'reason': action.get('reason'),
    }
    if action.get('threadPresence') is not None:
        result['threadPresence'] = action.get('threadPresence')
    if action.get('preservationGaps'):
        result['preservationGaps'] = action.get('preservationGaps')
    return result


def _state_summary(state, run_id):
    workers = state.get('workers', [])
    if not isinstance(workers, list):
        workers = []
    delivery = delivery_status(state)
    actions = []
    try:
        actions = build_next_actions(state, {}).get('actions', [])
    except (ValueError, TypeError, KeyError):
        actions = []
    return {
        'runId': run_id,
        'runStatus': state.get('status'),
        'revision': state.get('revision', 0),
        'currentSha': delivery.get('currentSha'),
        'deliveryStage': delivery.get('stage'),
        'workerCount': len(workers),
        'workers': [
            {'role': w.get('role'), 'attempt': w.get('attempt', 1),
             'state': w.get('state')}
            for w in workers if isinstance(w, dict)
        ],
        'nextActions': [_action_summary(a) for a in actions],
        'nextActionCount': len(actions),
        'unresolvedFailureCount': len([
            e for e in state.get('errors', [])
            if isinstance(e, dict) and e.get('resolved') is False
        ]) if isinstance(state.get('errors'), list) else 0,
    }


def build_compact_status(repo, run_id=None):
    repo = Path(repo)
    runs = locate_runs(repo)
    selected = None
    if run_id is not None:
        matches = [r for r in runs if r.get('runId') == run_id]
        if not matches:
            raise ValueError(f'run not found: {run_id}')
        selected = matches[-1]
    else:
        selected = latest_incomplete_run(repo)
        if selected is None and runs:
            selected = runs[-1]

    summary = {
        'schemaVersion': 1,
        'compact': True,
        'runCount': len(runs),
        'runs': [
            {'runId': r.get('runId'), 'status': r.get('status'),
             'mtime': r.get('mtime')}
            for r in runs
        ],
        'latestIncomplete': (
            selected.get('runId') if selected else None
        ),
    }
    if selected:
        try:
            state = read_state(selected['path'])
        except (OSError, ValueError):
            summary['selected'] = {
                'runId': selected.get('runId'), 'status': 'unreadable'
            }
        else:
            if isinstance(state, dict):
                summary['selected'] = _state_summary(state, selected.get('runId'))
            else:
                summary['selected'] = {
                    'runId': selected.get('runId'), 'status': 'invalid'
                }
    return summary


def compact_json(payload, max_bytes=DEFAULT_MAX_BYTES):
    """在字节上限内生成可解析 JSON；先保留下一步动作，再压缩可选明细。"""
    if type(max_bytes) is not int or max_bytes < MIN_MAX_BYTES:
        raise ValueError(f'max_bytes must be an integer >= {MIN_MAX_BYTES}')
    original = copy.deepcopy(payload)
    candidate = copy.deepcopy(original)
    candidate['truncated'] = False
    text = _dump(candidate)
    if len(text.encode('utf-8')) <= max_bytes:
        return text

    candidate['truncated'] = True
    selected = candidate.get('selected') or {}
    actions = selected.get('nextActions') or []
    if actions:
        selected['nextActions'] = actions[:1]
        selected['nextActionsOmitted'] = len(actions) - 1
    candidate['runs'] = candidate.get('runs', [])[:10]
    if candidate.get('runs'):
        total_runs = candidate.get('runCount', len(candidate['runs']))
        candidate['runsOmitted'] = max(0, total_runs - len(candidate['runs']))
    if isinstance(selected.get('workers'), list):
        selected['workerCount'] = len(selected['workers'])
        selected.pop('workers', None)
    text = _dump(candidate)
    if len(text.encode('utf-8')) <= max_bytes:
        return text

    selected = candidate.get('selected') or {}
    action = (selected.get('nextActions') or [None])[0]
    if action and isinstance(action.get('reason'), str) and len(action['reason']) > 240:
        action['reason'] = action['reason'][:237] + '...'
        action['reasonTruncated'] = True
    text = _dump(candidate)
    if len(text.encode('utf-8')) <= max_bytes:
        return text

    minimal = {
        'schemaVersion': 1,
        'compact': True,
        'truncated': True,
        'runCount': original.get('runCount'),
        'latestIncomplete': original.get('latestIncomplete'),
        'selected': {
            'runId': selected.get('runId'),
            'runStatus': selected.get('runStatus'),
            'currentSha': selected.get('currentSha'),
            'deliveryStage': selected.get('deliveryStage'),
            'nextAction': action,
            'nextActionCount': selected.get('nextActionCount', 0),
            'nextActionsOmitted': selected.get('nextActionsOmitted', 0),
            'unresolvedFailureCount': selected.get('unresolvedFailureCount', 0),
        },
    }
    text = _dump(minimal)
    if len(text.encode('utf-8')) > max_bytes:
        raise ValueError('compact status cannot fit max_bytes while preserving next action')
    return text


def build_delta_status(repo, run_id, cursor=None, max_bytes=DEFAULT_MAX_BYTES):
    repo = Path(repo)
    state_path = run_state_path(repo, run_id)
    if not state_path.is_file():
        raise ValueError(f'run not found: {run_id}')
    log_path = state_path.parent / 'audit.jsonl'
    result = read_since(log_path, cursor=cursor, scope=run_id)
    payload = {
        'schemaVersion': 1,
        'compact': True,
        'delta': True,
        'runId': run_id,
        'sinceCursor': cursor,
        'nextCursor': result['nextCursor'],
        'eventCount': len(result['events']),
        'events': result['events'],
        'truncatedTail': result['truncatedTail'],
    }
    text = _dump(payload)
    if len(text.encode('utf-8')) > max_bytes:
        raise ValueError(
            'delta output exceeds max_bytes; increase --max-bytes; no events were omitted')
    return text

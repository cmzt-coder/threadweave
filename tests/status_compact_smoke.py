"""验证 O15 compact status、字节上限与 JSONL delta 游标。"""
import copy
import hashlib
import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from run_log import log_event, make_cursor  # noqa: E402
from state_store import run_state_path, write_state_atomic  # noqa: E402
from status_view import (  # noqa: E402
    build_compact_status,
    build_delta_status,
    compact_json,
)
from threadweave import main  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


with tempfile.TemporaryDirectory(prefix='threadweave-compact-') as d:
    repo = Path(d)
    base_state = json.loads((project / 'references/state.example.json').read_text())
    state = copy.deepcopy(base_state)
    state.update(runId='run-a', status='observing', revision=3)
    state['workers'] = [dict(state['workers'][0], state='running', threadId='t-a')]
    state['workers'][0]['worktree'] = str(repo / 'worker-a')
    state['errors'] = [{
        'at': '2026-11-16T00:00:00+08:00', 'scope': 'a#1',
        'code': 'example_failure', 'message': 'example unresolved failure',
        'resolved': False, 'source': 'unknown',
        'stage': 'execute', 'resultCertainty': 'unknown',
        'retryable': False, 'recommendation': 'reconcile_result',
    }]
    write_state_atomic(run_state_path(repo, 'run-a'), state)
    report = repo / '.threadweave' / 'run-a' / 'report.json'
    report.write_text(json.dumps({'large': 'x' * 12000}), encoding='utf-8')
    before_report = (hashlib.sha256(report.read_bytes()).hexdigest(), report.stat().st_mtime_ns)
    before_state = (hashlib.sha256(run_state_path(repo, 'run-a').read_bytes()).hexdigest(),
                    run_state_path(repo, 'run-a').stat().st_mtime_ns)

    for index in range(12):
        write_state_atomic(run_state_path(repo, f'run-z{index:02d}'), {
            'schemaVersion': '1.0', 'runId': f'run-z{index:02d}',
            'status': 'complete', 'workers': [],
        })

    payload = build_compact_status(repo, 'run-a')
    check('compact-has-selected', payload['selected']['runId'] == 'run-a')
    check('compact-has-next-actions', payload['selected']['nextActionCount'] >= 1)
    compact = compact_json(payload, max_bytes=1400)
    parsed = json.loads(compact)
    check('compact-valid-json', parsed['compact'] is True)
    check('compact-byte-limit', len(compact.encode('utf-8')) <= 1400)
    check('compact-next-action-preserved',
          parsed.get('selected', {}).get('nextAction') is not None
          or parsed.get('selected', {}).get('nextActions'))

    synthetic = copy.deepcopy(payload)
    synthetic['selected']['nextActions'] = [
        {'action': 'first', 'worker': 'a#1', 'priority': 1, 'reason': 'preserve next action'},
        {'action': 'second', 'worker': 'b#1', 'priority': 2, 'reason': 'second action'},
        {'action': 'third', 'worker': 'c#1', 'priority': 3, 'reason': 'third action'},
    ]
    synthetic['selected']['nextActionCount'] = 3
    synthetic_compact = json.loads(compact_json(synthetic, max_bytes=900))
    check('compact-omission-explicit',
          synthetic_compact['selected'].get('nextActionsOmitted', 0) >= 1
          or synthetic_compact.get('nextActionsOmitted', 0) >= 1)

    log_path = repo / '.threadweave' / 'run-a' / 'audit.jsonl'
    log_event(log_path, 'first', value=1)
    cursor = make_cursor(log_path, scope='run-a')
    log_event(log_path, 'second', value=2)
    delta_text = build_delta_status(repo, 'run-a', cursor=cursor, max_bytes=4000)
    delta = json.loads(delta_text)
    check('delta-one-event', delta['eventCount'] == 1 and delta['events'][0]['event'] == 'second')
    check('delta-next-cursor', delta['nextCursor'] != cursor)
    check('delta-readonly', not delta['truncatedTail'])

    after_report = (hashlib.sha256(report.read_bytes()).hexdigest(), report.stat().st_mtime_ns)
    after_state = (hashlib.sha256(run_state_path(repo, 'run-a').read_bytes()).hexdigest(),
                   run_state_path(repo, 'run-a').stat().st_mtime_ns)
    check('compact-report-unchanged', before_report == after_report)
    check('compact-state-unchanged', before_state == after_state)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(['status', '--repo', str(repo), '--compact',
                     '--run', 'run-a', '--max-bytes', '1400'])
    cli_payload = json.loads(stdout.getvalue())
    check('compact-cli-exit', code == 0)
    check('compact-cli-json', cli_payload['compact'] is True)
    check('compact-cli-byte-limit', len(stdout.getvalue().strip().encode('utf-8')) <= 1400)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(['status', '--repo', str(repo), '--run', 'run-a',
                     '--since', cursor, '--max-bytes', '4000'])
    delta_cli = json.loads(stdout.getvalue())
    check('delta-cli-exit', code == 0)
    check('delta-cli-event', delta_cli['events'][0]['event'] == 'second')

    try:
        compact_json(payload, max_bytes=128)
        check('compact-small-budget-rejected', False)
    except ValueError:
        check('compact-small-budget-rejected', True)

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O15 local compact output and JSONL delta only; no host API',
}, ensure_ascii=False, indent=2))

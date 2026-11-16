"""验证结构化 JSONL 运行日志，不调用宿主 API。"""
import json
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from run_log import (  # noqa: E402
    LogCursorError,
    log_event,
    make_cursor,
    read_log,
    read_since,
    summarize_log,
)
from state_store import run_state_dir  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


with tempfile.TemporaryDirectory(prefix='threadweave-log-') as d:
    d = Path(d)
    repo = d / 'leader'
    log_path = run_state_dir(repo, 'run-1') / 'events.jsonl'

    first = log_event(log_path, 'worker.created', level='info', role='a', threadId='t-a')
    second = log_event(log_path, 'worker.dispatched', level='info', role='a', threadId='t-a')
    check('log-parent-created', log_path.is_file())
    check('log-returned-record', first['event'] == 'worker.created' and 'timestamp' in first)
    check('log-has-level', second['level'] == 'info')

    records = read_log(log_path)
    check('log-read-two', len(records) == 2)
    check('log-append-not-overwrite', [r['event'] for r in records] == ['worker.created', 'worker.dispatched'])
    check('log-in-run-dir', str(log_path).startswith(str(run_state_dir(repo, 'run-1'))))

    with log_path.open('a', encoding='utf-8') as f:
        f.write('{broken json\n')
    records = read_log(log_path)
    check('log-corrupt-marked', any(r['event'] == 'log.corrupt' and r['level'] == 'error' for r in records))

    counts = summarize_log(log_path)
    check('log-summary', counts.get('worker.created') == 1 and counts.get('log.corrupt') == 1)

    cursor_path = run_state_dir(repo, 'run-cursor') / 'events.jsonl'
    log_event(cursor_path, 'one', value=1)
    first_cursor = make_cursor(cursor_path, scope='run-cursor')
    log_event(cursor_path, 'two', value=2)
    delta = read_since(cursor_path, first_cursor, scope='run-cursor')
    check('cursor-delta-one', len(delta['events']) == 1 and delta['events'][0]['event'] == 'two')
    check('cursor-advanced', delta['nextCursor'] != first_cursor)
    empty = read_since(cursor_path, delta['nextCursor'], scope='run-cursor')
    check('cursor-repeat-empty', empty['events'] == [])
    try:
        read_since(cursor_path, first_cursor, scope='other-run')
        check('cursor-scope-mismatch', False)
    except LogCursorError as exc:
        check('cursor-scope-mismatch', exc.code == 'scope_mismatch')
    try:
        read_since(cursor_path, 'bad-cursor', scope='run-cursor')
        check('cursor-invalid-format', False)
    except LogCursorError as exc:
        check('cursor-invalid-format', exc.code == 'invalid_cursor')

    partial_path = run_state_dir(repo, 'run-partial') / 'events.jsonl'
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_bytes(b'{"event":"partial"')
    partial_result = read_since(partial_path, make_cursor(partial_path, 0, 'run-partial'), scope='run-partial')
    check('cursor-partial-tail-not-consumed',
          partial_result['events'] == [] and partial_result['truncatedTail'])
    partial_path.open('ab').write(b'}\n')
    partial_result = read_since(partial_path, partial_result['nextCursor'], scope='run-partial')
    check('cursor-partial-completes', len(partial_result['events']) == 1)

    rotated_path = run_state_dir(repo, 'run-rotated') / 'events.jsonl'
    log_event(rotated_path, 'first')
    rotated_cursor = make_cursor(rotated_path, scope='run-rotated')
    rotated_path.write_bytes(b'{"event":"rotated"}\n')
    try:
        read_since(rotated_path, rotated_cursor, scope='run-rotated')
        check('cursor-stale-after-rotation', False)
    except LogCursorError as exc:
        check('cursor-stale-after-rotation', exc.code == 'stale_cursor')

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Local JSONL log only; no host API calls',
}, ensure_ascii=False, indent=2))

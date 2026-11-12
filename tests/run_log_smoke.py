"""验证结构化 JSONL 运行日志，不调用宿主 API。"""
import json
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from run_log import log_event, read_log, summarize_log  # noqa: E402
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

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Local JSONL log only; no host API calls',
}, ensure_ascii=False, indent=2))

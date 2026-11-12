"""验证运行时预算、停滞与重试逻辑，不调用宿主 API。"""
import json
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from budget import (  # noqa: E402
    check_initialization, check_retry, check_run_budget, check_stall,
    summarize_budget,
)

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


check('initialization-ok',
      check_initialization('2026-11-12T10:00:00+08:00', '2026-11-12T10:04:00+08:00')['status'] == 'ok')
check('initialization-exceeded',
      check_initialization('2026-11-12T10:00:00+08:00', '2026-11-12T10:06:00+08:00')['status'] == 'deadline_exceeded')

check('stall-ok',
      check_stall('2026-11-12T10:00:00+08:00', '2026-11-12T10:05:00+08:00')['status'] == 'ok')
check('stall-request-status',
      check_stall('2026-11-12T10:00:00+08:00', '2026-11-12T10:07:00+08:00')['status'] == 'request_status')
check('stall-stalled',
      check_stall('2026-11-12T10:00:00+08:00', '2026-11-12T10:09:00+08:00',
                  status_requested_at='2026-11-12T10:07:00+08:00')['status'] == 'stalled')
check('stall-grace',
      check_stall('2026-11-12T10:00:00+08:00', '2026-11-12T10:08:00+08:00',
                  status_requested_at='2026-11-12T10:07:00+08:00')['status'] == 'grace')
check('stall-terminal-not-applicable',
      check_stall('2026-11-12T10:00:00+08:00', '2026-11-12T11:00:00+08:00',
                  worker_state='archived')['status'] == 'not_applicable')
check('stall-new-progress-ok',
      check_stall('2026-11-12T10:08:00+08:00', '2026-11-12T10:09:00+08:00')['status'] == 'ok')

check('retry-first-ok', check_retry(1, 1)['status'] == 'ok')
check('retry-over-limit-blocked', check_retry(2, 1)['status'] == 'blocked')

check('budget-unknown-elapsed', check_run_budget({}, {})['elapsed'] == 'unknown')
check('budget-unknown-cost', check_run_budget({}, {})['cost'] == 'unknown')
check('budget-elapsed-exceeded',
      check_run_budget({'elapsedSeconds': 100}, {'maxElapsedSeconds': 50})['elapsed'] == 'exceeded')
check('budget-cost-ok',
      check_run_budget({'costUsd': 0.5}, {'maxCostUsd': 1.0})['cost'] == 'ok')

state = {'workers': [{
    'role': 'a', 'attempt': 1,
    'createdAt': '2026-11-12T10:00:00+08:00',
    'lastObservation': '2026-11-12T10:05:00+08:00',
}]}
summary = summarize_budget(state, {}, '2026-11-12T10:12:00+08:00')
check('summarize-worker', set(summary) == {'a#1'})
check('summarize-stall-status', summary['a#1']['stall']['status'] == 'request_status')
check('summarize-retry-ok', summary['a#1']['retry']['status'] == 'ok')

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Budget/stall logic only; no host API calls',
}, ensure_ascii=False, indent=2))

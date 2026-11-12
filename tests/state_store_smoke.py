"""验证状态存储参考模块：转换校验、原子写入、锁互斥与并发隔离。

边界：测试的是纯逻辑模块，不驱动真实 Threadweave 流程，也不调用宿主 API。
"""
import json
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from state_store import (  # noqa: E402
    InvalidTransition, LockHeld, StateLock, read_state, validate_transition,
    write_state_atomic,
)

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def expect_invalid(kind, from_state, to_state):
    try:
        validate_transition(kind, from_state, to_state)
        return False
    except InvalidTransition:
        return True


worker_chain = [
    'planned', 'creating', 'validating', 'ready', 'dispatched',
    'running', 'reported', 'integrated', 'archived',
]
for a, b in zip(worker_chain, worker_chain[1:]):
    check(f'worker-{a}-to-{b}', validate_transition('worker', a, b))
check('worker-idempotent', validate_transition('worker', 'planned', 'planned'))
check('worker-invalid-planned-ready', expect_invalid('worker', 'planned', 'ready'))
check('worker-invalid-archived-planned', expect_invalid('worker', 'archived', 'planned'))

run_chain = ['planned', 'preflight', 'initializing', 'dispatching',
             'observing', 'integrating', 'final-review', 'complete']
for a, b in zip(run_chain, run_chain[1:]):
    check(f'run-{a}-to-{b}', validate_transition('run', a, b))
check('run-invalid-planned-complete', expect_invalid('run', 'planned', 'complete'))
check('run-abort-allowed', validate_transition('run', 'observing', 'aborted'))

with tempfile.TemporaryDirectory(prefix='threadweave-state-') as d:
    d = Path(d)
    state_path = d / 'state.json'
    original = {'runId': 'r1', 'workers': []}
    write_state_atomic(state_path, original)
    check('atomic-read-back', read_state(state_path) == original)
    check('atomic-no-temp-left', not list(d.glob('.state-*.tmp')))
    try:
        write_state_atomic(state_path, {'bad': object()})
        check('bad-write-rejected', False)
    except TypeError:
        check('bad-write-rejected', True)
    check('original-preserved-after-failure', read_state(state_path) == original)
    check('no-temp-after-failure', not list(d.glob('.state-*.tmp')))

    lock_path = d / 'run.lock'
    first = StateLock(lock_path).acquire()
    try:
        StateLock(lock_path).acquire()
        check('lock-mutual-exclusion', False)
    except LockHeld:
        check('lock-mutual-exclusion', True)
    first.release()
    second = StateLock(lock_path).acquire()
    check('lock-reacquire-after-release', True)
    second.release()

    lock_a = StateLock(d / 'a.lock').acquire()
    lock_b = StateLock(d / 'b.lock').acquire()
    check('distinct-run-locks-independent', True)
    lock_a.release()
    lock_b.release()

    with StateLock(d / 'ctx.lock'):
        pass
    check('lock-context-manager-releases', True)

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'State store logic only; no real runtime writes',
}, ensure_ascii=False, indent=2))

"""验证 StateLock 的跨进程互斥与进程退出自动释放。"""
import json
import multiprocessing
import sys
import tempfile
import time
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from state_store import LockHeld, StateLock  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def hold_lock(path, ready):
    with StateLock(path):
        ready.set()
        time.sleep(0.5)


def hold_and_exit(path):
    with StateLock(path):
        pass


def main():
    with tempfile.TemporaryDirectory(prefix='threadweave-mp-') as d:
        lock_path = Path(d) / 'run.lock'
        ready = multiprocessing.Event()
        worker = multiprocessing.Process(
            target=hold_lock, args=(str(lock_path), ready))
        worker.start()
        ready.wait(2)
        try:
            StateLock(lock_path).acquire()
            check('cross-process-lock-rejected', False)
        except LockHeld:
            check('cross-process-lock-rejected', True)
        worker.join(2)

        exit_worker = multiprocessing.Process(
            target=hold_and_exit, args=(str(lock_path),))
        exit_worker.start()
        exit_worker.join(2)
        lock = StateLock(lock_path)
        lock.acquire()
        lock.release()
        check('lock-released-after-process-exit', True)

    print(json.dumps({
        'passed': len(results),
        'checks': results,
        'scope': 'StateLock cross-process only; no host API',
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())

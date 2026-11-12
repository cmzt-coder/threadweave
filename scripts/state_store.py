"""Threadweave 状态存储参考实现：状态转换校验、原子写入与文件锁。

边界：这是可复用的纯逻辑模块，不创建线程、不调用宿主 API，也不自动驱动
Threadweave 流程；它把第二轮要求的状态写入安全能力固化为可测试代码。
真实运行时是否调用本模块仍由 leader/worker 协议决定。
"""
import fcntl
import json
import os
import tempfile
from pathlib import Path

# worker 状态转换表；终端态（archived/failed/cancelled）没有出边。
WORKER_TRANSITIONS = {
    'planned': {'creating'},
    'creating': {'pending', 'validating', 'failed', 'cancelled'},
    'pending': {'validating', 'failed', 'cancelled'},
    'validating': {'ready', 'failed', 'cancelled'},
    'ready': {'dispatched', 'failed', 'cancelled'},
    'dispatched': {'running', 'blocked', 'failed', 'cancelled'},
    'running': {'blocked', 'reported', 'failed', 'cancelled'},
    'blocked': {'running', 'reported', 'failed', 'cancelled'},
    'reported': {'integrated', 'failed', 'cancelled'},
    'integrated': {'archived'},
    'archived': set(),
    'failed': set(),
    'cancelled': set(),
}

# run 状态转换表；failed/aborted 可从任意非终端态进入。
RUN_TRANSITIONS = {
    'planned': {'preflight', 'failed', 'aborted'},
    'preflight': {'initializing', 'failed', 'aborted'},
    'initializing': {'dispatching', 'failed', 'aborted'},
    'dispatching': {'observing', 'failed', 'aborted'},
    'observing': {'integrating', 'failed', 'aborted'},
    'integrating': {'final-review', 'failed', 'aborted'},
    'final-review': {'complete', 'cleanup_pending', 'failed', 'aborted'},
    'cleanup_pending': {'complete', 'failed', 'aborted'},
    'complete': set(),
    'failed': set(),
    'aborted': set(),
}


class StateError(Exception):
    """状态存储相关错误基类。"""


class InvalidTransition(StateError):
    """请求的状态转换不在允许表内。"""


class LockHeld(StateError):
    """目标 run 的状态锁已被其他持有者占用。"""


def validate_transition(kind, from_state, to_state):
    """校验状态转换；同态写入视为幂等并通过。"""
    table = WORKER_TRANSITIONS if kind == 'worker' else RUN_TRANSITIONS
    if from_state == to_state:
        return True
    if to_state not in table.get(from_state, set()):
        raise InvalidTransition(f'{kind}: {from_state} -> {to_state} not allowed')
    return True


def write_state_atomic(path, state):
    """先写同目录临时文件并 fsync，再用 os.replace 原子替换目标。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.state-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_state(path):
    """读取并解析状态 JSON。"""
    return json.loads(Path(path).read_text(encoding='utf-8'))


def run_state_dir(repo_root, run_id):
    """返回 leader checkout 内的 durable run 目录，不在任何 worker worktree 内。"""
    return Path(repo_root) / '.threadweave' / run_id


def run_state_path(repo_root, run_id):
    """返回结构化状态文件的固定位置。"""
    return run_state_dir(repo_root, run_id) / 'state.json'


def locate_runs(repo_root):
    """扫描 durable 位置，返回可按 mtime 排序的 run 记录。"""
    root = Path(repo_root) / '.threadweave'
    if not root.is_dir():
        return []
    runs = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        state_path = entry / 'state.json'
        if not state_path.is_file():
            continue
        try:
            state = read_state(state_path)
        except (json.JSONDecodeError, OSError):
            runs.append({
                'runId': entry.name,
                'path': str(state_path),
                'status': 'unreadable',
                'mtime': state_path.stat().st_mtime,
            })
            continue
        runs.append({
            'runId': state.get('runId', entry.name),
            'path': str(state_path),
            'status': state.get('status', 'unknown'),
            'mtime': state_path.stat().st_mtime,
        })
    return sorted(runs, key=lambda r: r['mtime'])


def latest_incomplete_run(repo_root):
    """返回最近一个非终端、可读的 run；没有则返回 None。"""
    terminal = {'complete', 'failed', 'aborted', 'unreadable'}
    candidates = [r for r in locate_runs(repo_root) if r['status'] not in terminal]
    return candidates[-1] if candidates else None


class StateLock:
    """基于 flock 的 run 级排他锁；进程退出时由内核自动释放。"""

    def __init__(self, path):
        self.path = Path(path)
        self._file = None

    def acquire(self, blocking=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, 'a+', encoding='utf-8')
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(f.fileno(), flags)
        except BlockingIOError:
            f.close()
            raise LockHeld(f'lock already held: {self.path}')
        self._file = f
        return self

    def release(self):
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

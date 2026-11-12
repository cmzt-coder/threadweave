"""最小控制器骨架：依赖 HostAdapter，不直接调用宿主 API。

边界：这不是完整 Threadweave 控制器；它只演示纯逻辑状态转换与宿主调用如何隔离。
真实运行前必须提供具体宿主适配器，并用真实线程验证。
"""
from datetime import datetime, timezone

from budget import check_retry
from host_adapter import HostAdapter
from startup_gate import validate_startup
from state_store import run_state_path, validate_transition, write_state_atomic


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


class Controller:
    def __init__(self, adapter, repo_root, run_id, state):
        if not isinstance(adapter, HostAdapter):
            raise TypeError('adapter must implement HostAdapter')
        self.adapter = adapter
        self.repo_root = repo_root
        self.run_id = run_id
        self.state = state
        self.state_path = run_state_path(repo_root, run_id)

    def save(self):
        write_state_atomic(self.state_path, self.state)

    def transition_run(self, new_status):
        validate_transition('run', self.state['status'], new_status)
        self.state['status'] = new_status
        self.save()

    def _worker(self, role, attempt):
        for worker in self.state.get('workers', []):
            if worker.get('role') == role and worker.get('attempt') == attempt:
                return worker
        raise KeyError(f'{role}#{attempt}')

    def transition_worker(self, role, attempt, new_status):
        worker = self._worker(role, attempt)
        validate_transition('worker', worker['state'], new_status)
        worker['state'] = new_status
        self.save()

    def dispatch(self, role, attempt, message, startup_report=None, expected=None):
        """仅在 worker ready、启动报告通过且已有真实 threadId 时派发。"""
        worker = self._worker(role, attempt)
        if worker['state'] != 'ready':
            raise RuntimeError(f'worker {role}#{attempt} is {worker["state"]}, expected ready')
        if not worker.get('threadId'):
            raise RuntimeError(f'worker {role}#{attempt} has no real threadId')
        if startup_report is not None:
            if expected is None:
                raise ValueError('expected startup metadata is required')
            ok, reasons = validate_startup(startup_report, expected)
            if not ok:
                raise RuntimeError(f'startup validation failed: {reasons}')
        self.adapter.send_message(worker['threadId'], message)
        worker['dispatchAt'] = _now_iso()
        self.transition_worker(role, attempt, 'dispatched')
        return worker['threadId']

    def replace_worker(self, role, attempt, replacement_limit=1):
        """检查替换上限；超限抛错，未超限只返回替换计划，不创建线程。"""
        worker = self._worker(role, attempt)
        result = check_retry(worker['attempt'], replacement_limit)
        if result['status'] == 'blocked':
            raise RuntimeError(f'replacement limit reached for {role}#{attempt}')
        return {
            'role': role,
            'nextAttempt': worker['attempt'] + 1,
            'limit': replacement_limit,
        }

    def write_plan(self, plan, update_plan_available=True):
        """可选工具降级：有 update_plan 时返回工具名，否则写入 state 文件。"""
        if update_plan_available:
            return 'update_plan'
        self.state['plan'] = plan
        self.save()
        return 'state-file'

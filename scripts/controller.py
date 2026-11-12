"""最小控制器骨架：依赖 HostAdapter，不直接调用宿主 API。

边界：这不是完整 Threadweave 控制器；它只演示纯逻辑状态转换与宿主调用如何隔离。
真实运行前必须提供具体宿主适配器，并用真实线程验证。
"""
from datetime import datetime, timezone

from budget import check_retry
from host_adapter import HostAdapter
from startup_gate import validate_startup
from state_schema import validate_state
from state_store import StateLock, read_state, run_state_path, validate_transition, write_state_atomic


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


class StaleStateError(Exception):
    """内存快照落后于磁盘 revision，拒绝覆盖。"""


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
        """加锁、校验 revision 与 schema 后原子写入；拒绝旧快照覆盖。"""
        self.state.setdefault('revision', 0)
        self.state.setdefault('updatedAt', _now_iso())
        lock = StateLock(self.state_path.with_suffix('.lock'))
        lock.acquire()
        try:
            if self.state_path.is_file():
                disk = read_state(self.state_path)
                disk_revision = disk.get('revision', 0)
                if disk_revision != self.state['revision']:
                    raise StaleStateError(
                        f'stale state: disk revision {disk_revision}, '
                        f'memory revision {self.state["revision"]}')
            self.state['updatedAt'] = _now_iso()
            validate_state(self.state)
            self.state['revision'] += 1
            write_state_atomic(self.state_path, self.state)
        finally:
            lock.release()

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

    def dispatch(self, role, attempt, message, startup_report, expected):
        """派发前校验团队门禁、启动证据与集成 HEAD；发送前持久化 intent。"""
        worker = self._worker(role, attempt)
        if worker['state'] != 'ready':
            raise RuntimeError(f'worker {role}#{attempt} is {worker["state"]}, expected ready')
        if not worker.get('threadId'):
            raise RuntimeError(f'worker {role}#{attempt} has no real threadId')
        if startup_report is None or expected is None:
            raise ValueError('startup_report and expected are required')
        ok, reasons = validate_startup(startup_report, expected)
        if not ok:
            raise RuntimeError(f'startup validation failed: {reasons}')
        integration_head = expected.get('integration_head')
        if integration_head != self.state['integration']['baseSha']:
            raise RuntimeError('integration HEAD drifted from recorded base')
        report_role = startup_report.get('role')
        report_attempt = startup_report.get('attempt')
        if report_role is not None and report_role != role:
            raise RuntimeError('startup evidence belongs to another role')
        if report_attempt is not None and report_attempt != attempt:
            raise RuntimeError('startup evidence belongs to another attempt')
        observed_at = startup_report.get('observedAt')
        if observed_at is not None:
            from datetime import datetime
            observed = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
            age = (datetime.now(observed.tzinfo) - observed).total_seconds()
            max_age = expected.get('max_evidence_age_seconds', 300)
            if age > max_age:
                raise RuntimeError('startup evidence is stale')
        allowed = {'ready', 'dispatched', 'reported', 'integrated', 'archived'}
        blocked = [
            f'{w.get("role")}#{w.get("attempt")}:{w.get("state")}'
            for w in self.state.get('workers', [])
            if w.get('state') not in allowed
        ]
        if blocked:
            raise RuntimeError('team gate not ready: ' + ', '.join(blocked))

        dispatch_id = f"{role}#{attempt}@{_now_iso()}"
        worker['dispatchIntent'] = {
            'dispatchId': dispatch_id,
            'message': message,
            'at': _now_iso(),
            'result': 'pending',
        }
        self.save()
        try:
            self.adapter.send_message(worker['threadId'], message)
        except Exception:
            worker['dispatchIntent']['result'] = 'unknown'
            self.save()
            raise
        worker['startupEvidence'] = {
            'role': report_role if report_role is not None else role,
            'attempt': report_attempt if report_attempt is not None else attempt,
            'cwd': startup_report['cwd'],
            'worktree_root': startup_report['worktree_root'],
            'git_common_dir': startup_report['git_common_dir'],
            'branch': startup_report['branch'],
            'head': startup_report['head'],
            'status_porcelain': startup_report['status_porcelain'],
            'validatedAt': _now_iso(),
        }
        worker['dispatchIntent']['result'] = 'sent'
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

    def create_worker(self, prompt, project_id, timeout_seconds=300):
        """创建 worker；超时记录证据后抛出，不自动重试或发送消息。"""
        try:
            return self.adapter.create_worker(prompt, project_id)
        except TimeoutError as exc:
            self.state['lastCreateError'] = {
                'code': 'create_timeout',
                'message': str(exc),
                'at': _now_iso(),
            }
            self.save()
            raise

    def wait_for_workers(self, thread_ids, timeout_seconds=60):
        """有 wait_threads 时等待事件；缺失时返回有界状态检查模式。"""
        available = bool(getattr(self.adapter, 'capabilities', {}).get('wait_threads', True))
        if available:
            return self.adapter.wait_threads(thread_ids, timeout_seconds)
        return {
            'mode': 'bounded-status',
            'threadIds': list(thread_ids),
            'timeoutSeconds': timeout_seconds,
        }

    def write_plan(self, plan, update_plan_available=None):
        """可选工具降级：有 update_plan 时返回工具名，否则写入 state 文件。"""
        if update_plan_available is None:
            update_plan_available = bool(
                getattr(self.adapter, 'capabilities', {}).get('update_plan', True))
        if update_plan_available:
            return 'update_plan'
        self.state['plan'] = plan
        self.save()
        return 'state-file'

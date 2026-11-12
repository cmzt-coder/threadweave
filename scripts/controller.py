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
        """加锁、校验 revision 与 schema，成功写入后才替换内存。"""
        self.state.setdefault('revision', 0)
        self.state.setdefault('updatedAt', _now_iso())
        if self.state.get('runId') != self.run_id:
            raise ValueError('state runId does not match controller run_id')
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
            import copy
            candidate = copy.deepcopy(self.state)
            candidate['updatedAt'] = _now_iso()
            candidate['revision'] = self.state['revision'] + 1
            validate_state(candidate)
            write_state_atomic(self.state_path, candidate)
            self.state['revision'] = candidate['revision']
            self.state['updatedAt'] = candidate['updatedAt']
        finally:
            lock.release()

    def transition_run(self, new_status):
        validate_transition('run', self.state['status'], new_status)
        self.state['status'] = new_status
        self.save()

    def _ensure_external_action_allowed(self, worker, action):
        run_status = self.state.get('status')
        if run_status in ('aborted', 'failed', 'complete'):
            raise RuntimeError(f'run is {run_status}; {action} blocked')
        if worker.get('stopStatus') in ('requested', 'confirmed'):
            raise RuntimeError(
                f'worker {worker.get("role")}#{worker.get("attempt")} is stopping; {action} blocked')
        intent = worker.get('dispatchIntent', {})
        if action == 'dispatch' and intent.get('result') in ('pending', 'unknown'):
            raise RuntimeError('unresolved dispatch intent; reconcile before retrying')

    def _active_workers(self):
        """每个角色只保留 attempt 最大的有效 worker；历史记录继续审计。"""
        by_role = {}
        for worker in self.state.get('workers', []):
            role = worker.get('role')
            attempt = worker.get('attempt', 1)
            current = by_role.get(role)
            if current is None or attempt > current.get('attempt', 0):
                by_role[role] = worker
        return by_role

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
        self._ensure_external_action_allowed(worker, 'dispatch')
        if self._active_workers().get(role) is not worker:
            raise RuntimeError(f'{role}#{attempt} is not the active attempt')
        if worker['state'] != 'ready':
            raise RuntimeError(f'worker {role}#{attempt} is {worker["state"]}, expected ready')
        if not worker.get('threadId'):
            raise RuntimeError(f'worker {role}#{attempt} has no real threadId')
        if startup_report is None or expected is None:
            raise ValueError('startup_report and expected are required')
        for field in ('role', 'attempt', 'observedAt'):
            if field not in startup_report:
                raise ValueError(f'startup_report missing {field}')
        expected = dict(expected)
        if expected.get('branch') not in (None, worker['branch']):
            raise RuntimeError('expected branch does not match persisted worker')
        if expected.get('base') not in (None, worker['baseSha']):
            raise RuntimeError('expected base does not match persisted worker')
        expected['branch'] = worker['branch']
        expected['base'] = worker['baseSha']
        expected['role'] = role
        expected['attempt'] = attempt
        expected.setdefault('common_dir', self.state['project']['commonDirectory'])
        worktree = worker.get('worktree')
        if not worktree:
            raise RuntimeError('worker has no registered worktree')
        if expected.get('worker_root') not in (None, worktree):
            raise RuntimeError('expected worker_root does not match persisted worker')
        expected['worker_root'] = worktree
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
            if age < -60:
                raise RuntimeError('startup evidence is from the future')
            if age > max_age:
                raise RuntimeError('startup evidence is stale')
        allowed = {'ready', 'dispatched', 'running', 'reported', 'integrated', 'archived'}
        blocked = [
            f'{role_name}#{w.get("attempt")}:{w.get("state")}'
            for role_name, w in self._active_workers().items()
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
        worker['activityRevision'] = worker.get('activityRevision', 0) + 1
        worker.pop('stopStatus', None)
        self.transition_worker(role, attempt, 'dispatched')
        return worker['threadId']

    def request_stop(self, role, attempt):
        """记录停止请求；不代表线程已停止。"""
        worker = self._worker(role, attempt)
        worker['stopRequestedAt'] = _now_iso()
        worker['stopStatus'] = 'requested'
        self.save()
        return worker['stopStatus']

    def confirm_stop(self, role, attempt, idle, final_answer_present, no_active_turn):
        """只有完整停止证据才确认停止。"""
        worker = self._worker(role, attempt)
        if not (idle and final_answer_present and no_active_turn):
            raise RuntimeError('stop evidence incomplete')
        worker['stopConfirmedAt'] = _now_iso()
        worker['stopStatus'] = 'confirmed'
        worker['stopActivityRevision'] = worker.get('activityRevision', 0)
        self.save()
        return worker['stopStatus']

    def rework(self, role, attempt, notes=''):
        """把 reported worker 退回 running，记录返工修订。"""
        worker = self._worker(role, attempt)
        worker['reworkCount'] = worker.get('reworkCount', 0) + 1
        worker['reworkNotes'] = notes
        worker['activityRevision'] = worker.get('activityRevision', 0) + 1
        worker.pop('stopStatus', None)
        worker.pop('stopActivityRevision', None)
        self.transition_worker(role, attempt, 'running')
        return worker['reworkCount']

    def resolve_dispatch(self, role, attempt, result):
        """收敛 pending/unknown 的派发意图，并同步 worker 执行状态。"""
        if result not in ('sent', 'not_sent', 'unknown'):
            raise ValueError('result must be sent/not_sent/unknown')
        worker = self._worker(role, attempt)
        intent = worker.get('dispatchIntent')
        if not intent:
            raise RuntimeError('no dispatch intent to resolve')
        if result == 'sent' and worker['state'] != 'ready':
            raise RuntimeError(
                f'cannot mark {worker["state"]} as dispatched; expected ready')
        intent['result'] = result
        intent['resolvedAt'] = _now_iso()
        if result == 'sent':
            worker['state'] = 'dispatched'
            worker['dispatchAt'] = _now_iso()
            worker['activityRevision'] = worker.get('activityRevision', 0) + 1
            worker.pop('stopStatus', None)
        self.save()
        return intent['result']

    def replace_worker(self, role, attempt, replacement_limit=None):
        """替换前要求停止证据、无后续 attempt，并从持久状态读取上限。"""
        worker = self._worker(role, attempt)
        if replacement_limit is None:
            replacement_limit = self.state.get('limits', {}).get('replacementPerRole', 1)
        if worker.get('stopStatus') != 'confirmed':
            raise RuntimeError('stop evidence not confirmed')
        if worker.get('stopActivityRevision') != worker.get('activityRevision', 0):
            raise RuntimeError('stop evidence is for an older activity revision')
        if any(
            w.get('role') == role and w.get('attempt', 0) > attempt
            for w in self.state.get('workers', [])
        ):
            raise RuntimeError('replacement attempt already exists')
        result = check_retry(worker['attempt'], replacement_limit)
        if result['status'] == 'blocked':
            raise RuntimeError(f'replacement limit reached for {role}#{attempt}')
        return {
            'role': role,
            'nextAttempt': worker['attempt'] + 1,
            'limit': replacement_limit,
        }

    def capability_snapshot(self):
        return {
            'hostId': self.state.get('project', {}).get('hostId'),
            'capabilities': dict(getattr(self.adapter, 'capabilities', {})),
            'recordedAt': _now_iso(),
        }

    def record_capabilities(self):
        self.state['hostCapabilities'] = self.capability_snapshot()
        self.save()
        return self.state['hostCapabilities']

    def create_worker(self, prompt, project_id, timeout_seconds=300):
        """创建 worker；超时记录证据后抛出，不自动重试或发送消息。"""
        if self.state.get('status') in ('aborted', 'failed', 'complete'):
            raise RuntimeError(
                f'run is {self.state.get("status")}; create_worker blocked')
        try:
            if getattr(self.adapter, 'capabilities', {}).get('create_timeout'):
                return self.adapter.create_worker(
                    prompt, project_id, timeout_seconds=timeout_seconds)
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

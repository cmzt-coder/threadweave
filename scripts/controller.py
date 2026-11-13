"""最小控制器骨架：依赖 HostAdapter，不直接调用宿主 API。

边界：这不是完整 Threadweave 控制器；它只演示纯逻辑状态转换与宿主调用如何隔离。
真实运行前必须提供具体宿主适配器，并用真实线程验证。
"""
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import subprocess

from budget import check_retry
from host_adapter import HostAdapter
from startup_gate import validate_startup
from run_log import log_event
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
        if run_status in ('aborted', 'failed', 'complete', 'cleanup_pending'):
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
        if new_status == 'integrated':
            worker = self._review_worker(role, attempt)
            if self.state.get('integrationBlocked'):
                raise RuntimeError('integration blocked; explicit recovery required')
            self._require_stopped(worker)
            if not worker.get('report'):
                raise RuntimeError('cannot integrate without accepted report')
            _, current_head = self._integration_observation()
            if not any(
                entry.get('mergeCommit') == current_head
                and entry.get('acceptance', {}).get('context') == self._context(worker, 'integration', current_head)
                and entry.get('branch') == worker.get('branch') and entry.get('result') == 'merged'
                and entry.get('acceptance', {}).get('status') == 'passed'
                and entry.get('acceptance', {}).get('context', {}).get('role') == role
                and entry.get('acceptance', {}).get('context', {}).get('attempt') == attempt
                and entry.get('acceptance', {}).get('context', {}).get('taskRevision') == worker.get('activityRevision', 0)
                for entry in self.state.get('integrationLog', [])
            ):
                raise RuntimeError('cannot integrate without integration log')
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

        self._scope_reservations()
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

    def _review_worker(self, role, attempt):
        """只审查当前有效任务；停止请求与终端 run 不允许继续推进。"""
        worker = self._worker(role, attempt)
        if self._active_workers().get(role) is not worker:
            raise RuntimeError('not the active attempt')
        if self.state.get('status') in ('aborted', 'failed', 'complete', 'cleanup_pending'):
            raise RuntimeError('run cannot accept or integrate work')
        if worker.get('stopStatus') == 'requested':
            raise RuntimeError('worker stop is not yet confirmed')
        return worker

    @staticmethod
    def _git(root, *args):
        result = subprocess.run(['git', '-C', str(root), *args],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('git observation failed: ' + ' '.join(args[:2]))
        return result.stdout.rstrip('\n')

    def _observe_worker(self, worker):
        """独立读取 Git，报告自报不能代替身份、SHA 与文件清单观察。"""
        root = Path(worker.get('worktree') or '').resolve()
        if not worker.get('worktree') or not root.is_dir():
            raise RuntimeError('registered worker worktree is unavailable')
        if Path(self._git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
            raise RuntimeError('worker root identity mismatch')
        common = Path(self._git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
        if common != Path(self.state['project']['commonDirectory']).resolve():
            raise RuntimeError('worker git common directory mismatch')
        branch = self._git(root, 'symbolic-ref', '--short', 'HEAD')
        head = self._git(root, 'rev-parse', 'HEAD')
        if branch != worker['branch']:
            raise RuntimeError('worker branch mismatch')
        if self._git(root, 'rev-parse', '--verify', 'refs/heads/' + branch) != head:
            raise RuntimeError('worker branch tip mismatch')
        if worker.get('branchTip') and worker['branchTip'] != head:
            raise RuntimeError('worker head changed from registered branch tip')
        if self._git(root, 'status', '--porcelain'):
            raise RuntimeError('worker worktree is dirty')
        self._git(root, 'merge-base', '--is-ancestor', worker['baseSha'], head)
        # 禁用 rename 折叠，两个端点都得到检查；NUL 分隔保留特殊文件名。
        paths = self._git(root, 'diff', '--name-only', '--no-renames', '-z',
                          worker['baseSha'], head).split('\0')
        return {'head': head, 'branch': branch, 'baseIsAncestor': True,
                'filesChanged': sorted(x for x in paths if x)}

    def _scope_reservations(self):
        workers = list(self._active_workers().values())
        if not any('ownedPaths' in w for w in workers):
            return
        if any('ownedPaths' not in w for w in workers):
            raise RuntimeError('all active workers must declare ownedPaths')
        from scope import check_reservations
        resources = []
        config_path = Path(self.repo_root) / 'threadweave.config.json'
        if config_path.is_file():
            if config_path.resolve().parent != Path(self.repo_root).resolve():
                raise RuntimeError('project configuration escapes repository')
            config = json.loads(config_path.read_text(encoding='utf-8'))
            resources = config.get('serializedResources', [])
        errors = check_reservations(workers, self.repo_root, resources)
        if errors:
            raise RuntimeError('scope reservation conflict: ' + '; '.join(errors))

    @staticmethod
    def _scope_changes(worker, observed):
        if 'ownedPaths' not in worker:
            return
        from scope import check_changes, check_reservations
        errors = check_reservations([worker], worker.get("worktree"))
        errors += check_changes(observed['filesChanged'], worker['ownedPaths'],
                               worker.get('scopeExceptions', []))
        if errors:
            raise RuntimeError('scope_drift: ' + '; '.join(errors))

    def accept_report(self, role, attempt, report):
        """接收完整 JSON 报告；旧格式需重报，不能绕过独立 Git 核验。"""
        from report import validate_report
        worker = self._review_worker(role, attempt)
        if worker['state'] not in ('running', 'blocked'):
            raise RuntimeError('report requires running/blocked worker')
        expected = {'runId': self.run_id, 'role': role, 'attempt': attempt,
                    'threadId': worker.get('threadId'), 'branch': worker['branch'],
                    'taskRevision': worker.get('activityRevision', 0)}
        observed = self._observe_worker(worker)
        validated = validate_report(report, expected, observed)
        self._scope_changes(worker, observed)
        worker['report'] = dict(validated, acceptedAt=_now_iso())
        worker.pop('acceptance', None)
        worker['state'] = 'reported'
        self.save()
        return worker['report']

    def _require_stopped(self, worker):
        if (worker.get('stopStatus') != 'confirmed' or
                worker.get('stopActivityRevision') != worker.get('activityRevision', 0)):
            raise RuntimeError('fresh confirmed stop evidence required')

    def _context(self, worker, stage, head):
        return {'runId': self.run_id, 'role': worker['role'],
                'attempt': worker['attempt'],
                'taskRevision': worker.get('activityRevision', 0),
                'stage': stage, 'head': head}

    def _block_integration(self, worker, result, before=None, head=None):
        self.state['integrationBlocked'] = {
            'role': worker['role'], 'attempt': worker['attempt'],
            'head': head or result['context']['head'], 'at': _now_iso(),
            'reason': 'independent acceptance failed',
        }
        self.state.setdefault('integrationLog', []).append({
            'branch': worker['branch'], 'mergeCommit': None,
            'result': 'failed', 'verification': 'independent acceptance failed',
            'beforeSha': before, 'afterSha': head,
            'acceptance': result,
        })
        self.save()

    def verify_worker(self, role, attempt, plan):
        from verify import execute_plan
        worker = self._review_worker(role, attempt)
        if worker['state'] != 'reported' or not worker.get('report'):
            raise RuntimeError('accepted report required')
        self._require_stopped(worker)
        observed = self._observe_worker(worker)
        report = worker['report']
        from report import validate_report
        validate_report(report, {**self._context(worker, 'worker', observed['head']),
                                'threadId': worker.get('threadId'), 'branch': worker['branch']}, observed)
        if (report['head'] != observed['head'] or
                report['taskRevision'] != worker.get('activityRevision', 0)):
            raise RuntimeError('report evidence is stale')
        self._scope_changes(worker, observed)
        result = execute_plan(plan, worker['worktree'], self._context(worker, 'worker', observed['head']))
        worker['verificationPlan'] = copy.deepcopy(plan)
        worker['acceptance'] = {'result': result, 'reportSha': report['reportSha'],
                                'worktree': str(Path(worker['worktree']).resolve())}
        if result['status'] != 'passed':
            self._block_integration(worker, result)
            raise RuntimeError('worker acceptance failed')
        self.save()
        return result

    def _integration_observation(self):
        root = Path(self.state['integration']['repoPath']).resolve()
        if root != Path(self.repo_root).resolve():
            raise RuntimeError('integration checkout identity mismatch')
        if Path(self._git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
            raise RuntimeError('integration root mismatch')
        if self._git(root, 'symbolic-ref', '--short', 'HEAD') != self.state['integration']['branch']:
            raise RuntimeError('integration branch mismatch')
        if Path(self._git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != Path(self.state['project']['commonDirectory']).resolve():
            raise RuntimeError('integration common directory mismatch')
        if self._git(root, 'status', '--porcelain'):
            raise RuntimeError('integration worktree is dirty')
        return root, self._git(root, 'rev-parse', 'HEAD')

    def integrate(self, role, attempt, merge_commit, verification=None, plan=None):
        """核验外部串行 merge 的实际结果，并独立验收；不执行 Git merge。"""
        from verify import execute_plan, plan_hash
        worker = self._review_worker(role, attempt)
        if self.state.get('integrationBlocked'):
            raise RuntimeError('integration blocked; explicit recovery required')
        if verification is not None:
            raise RuntimeError('legacy verification strings are not acceptance evidence')
        if not plan:
            raise RuntimeError('integration verification plan required')
        if worker['state'] != 'reported' or not worker.get('report'):
            raise RuntimeError('accepted report required')
        self._require_stopped(worker)
        observed = self._observe_worker(worker)
        self._scope_changes(worker, observed)
        acceptance = worker.get('acceptance', {})
        result = acceptance.get('result', {})
        report = worker['report']
        from report import validate_report
        validate_report(report, {**self._context(worker, 'worker', observed['head']),
                                'threadId': worker.get('threadId'), 'branch': worker['branch']}, observed)
        if (result.get('status') != 'passed' or
                result.get('context') != self._context(worker, 'worker', observed['head']) or
                result.get('planHash') != plan_hash(worker.get('verificationPlan', {})) or
                acceptance.get('reportSha') != report['reportSha'] or
                acceptance.get('worktree') != str(Path(worker['worktree']).resolve()) or
                report['head'] != observed['head'] or
                report['taskRevision'] != worker.get('activityRevision', 0)):
            raise RuntimeError('worker acceptance is missing or stale')
        root, head = self._integration_observation()
        if merge_commit != head:
            raise RuntimeError('merge commit is not actual integration HEAD')
        self._git(root, 'merge-base', '--is-ancestor', observed['head'], head)
        before = self.state['integration'].get('currentSha', self.state['integration']['baseSha'])
        self._git(root, 'merge-base', '--is-ancestor', before, head)
        integrated = execute_plan(plan, root, self._context(worker, 'integration', head))
        if integrated['status'] != 'passed':
            self._block_integration(worker, integrated, before, head)
            raise RuntimeError('integration acceptance failed')
        self.state.setdefault('integrationLog', []).append({
            'branch': worker['branch'], 'mergeCommit': head, 'result': 'merged',
            'verification': 'independent acceptance passed',
            'beforeSha': before, 'afterSha': head, 'acceptance': integrated,
        })
        self.state['integration']['currentSha'] = head
        self.transition_worker(role, attempt, 'integrated')
        return self.state['integrationLog'][-1]

    def recover_integration(self, plan):
        """leader 显式重验当前集成 HEAD，成功后解除阻断，保留失败记录。"""
        from verify import execute_plan
        blocked = self.state.get('integrationBlocked')
        if not blocked:
            raise RuntimeError('integration is not blocked')
        worker = self._review_worker(blocked['role'], blocked['attempt'])
        self._require_stopped(worker)
        root, head = self._integration_observation()
        self._git(root, 'merge-base', '--is-ancestor', self.state['integration']['baseSha'], head)
        result = execute_plan(plan, root, self._context(worker, 'integration', head))
        self.state.setdefault('integrationRecovery', []).append(result)
        if result['status'] != 'passed':
            self.save()
            raise RuntimeError('integration recovery verification failed')
        self.state.pop('integrationBlocked')
        self.save()
        return result

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
        """返工有界；超限独立审计，任务状态与计数不变。"""
        worker = self._review_worker(role, attempt)
        if worker['state'] != 'reported':
            raise RuntimeError('rework requires reported worker')
        count = worker.get('reworkCount', 0)
        limit = self.state.get('limits', {}).get('maxReworkPerWorker', 2)
        if type(limit) is not int or limit < 0:
            raise ValueError('invalid maxReworkPerWorker')
        if count >= limit:
            log_event(self.state_path.parent / 'audit.jsonl', 'rework.limit_reached',
                      role=role, attempt=attempt, count=count, limit=limit,
                      decision='blocked; leader decision required')
            raise RuntimeError('rework limit reached; leader decision required')
        snapshot = copy.deepcopy(self.state)
        try:
            worker['reworkCount'] = count + 1
            worker['reworkNotes'] = notes
            worker['activityRevision'] = worker.get('activityRevision', 0) + 1
            for key in ('stopStatus', 'stopActivityRevision', 'report', 'acceptance', 'verificationPlan', 'branchTip'):
                worker.pop(key, None)
            self.transition_worker(role, attempt, 'running')
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise
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
        if self.state.get('status') in ('aborted', 'failed', 'complete', 'cleanup_pending'):
            raise RuntimeError(
                f'run is {self.state.get("status")}; create_worker blocked')
        if not self.state.get('authorization', {}).get('provenance'):
            raise RuntimeError('authorization provenance missing; create_worker blocked')
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

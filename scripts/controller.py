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
from contracts import ContractError, check_contracts, hash_contracts, load_stable_contracts
from delivery import add_landed, add_push_verified, add_verified, delivery_status
from preflight_contract import build_preflight_contract, evaluate_preflight
from setup import SetupConfigError, load_setup_config, run_setup_command
from failure import FAILURE_SOURCES, build_failure_record
from recovery import build_preservation_record, verify_preservation_record
from host_adapter import HostActionPending, HostAdapter
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
        bridge_binding = None
        if getattr(self.adapter, 'capabilities', {}).get('queued_actions'):
            bridge_binding = self.adapter.dispatch_binding()
            if bridge_binding != {'runId': self.run_id, 'role': role, 'attempt': attempt,
                                  'taskRevision': worker.get('activityRevision', 0),
                                  'operationId': bridge_binding.get('operationId')} or not bridge_binding.get('operationId'):
                raise ValueError('bridge dispatch binding mismatch')
        dispatch_id = f"{role}#{attempt}@{_now_iso()}"
        worker['dispatchIntent'] = {
            'dispatchId': dispatch_id,
            'message': message,
            'at': _now_iso(),
            'result': 'pending',
        }
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
        if bridge_binding is not None:
            worker['dispatchIntent']['bridgeOperationId'] = bridge_binding['operationId']
        self.save()
        try:
            self.adapter.send_message(worker['threadId'], message)
        except HostActionPending:
            # 请求账本负责区分 pending/in_flight/unknown，控制器不猜测调用结果。
            raise
        except Exception as exc:
            worker['dispatchIntent']['result'] = 'unknown'
            source = getattr(exc, 'failure_source', 'unknown')
            if source not in FAILURE_SOURCES:
                source = 'unknown'
            failure = build_failure_record(
                source=source,
                code=getattr(exc, 'failure_code', 'dispatch_result_unknown'),
                stage='dispatch',
                message=f'{type(exc).__name__}: {exc}',
                scope=f'{role}#{attempt}',
                result_certainty=getattr(exc, 'result_certainty', 'unknown'),
                retryable=False,
                evidence=getattr(
                    exc, 'evidence',
                    'adapter raised without a definitive send result; reconcile before retry'),
            )
            self.state.setdefault('errors', []).append(failure)
            self.save()
            raise
        return self._finish_dispatch(role, attempt)

    def _finish_dispatch(self, role, attempt):
        snapshot = copy.deepcopy(self.state)
        worker = self._worker(role, attempt)
        worker['dispatchIntent']['result'] = 'sent'
        worker['dispatchAt'] = _now_iso()
        worker['activityRevision'] = worker.get('activityRevision', 0) + 1
        worker.pop('stopStatus', None)
        try:
            self.transition_worker(role, attempt, 'dispatched')
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise
        return worker['threadId']

    def resume_bridge_dispatch(self, role, attempt):
        """同一账本发送回执落盘后恢复状态；不发起新的宿主消息。"""
        worker = self._worker(role, attempt)
        self._ensure_external_action_allowed(worker, 'resume_dispatch')
        if self._active_workers().get(role) is not worker or worker['state'] != 'ready':
            raise RuntimeError('only active ready worker can resume dispatch')
        intent = worker.get('dispatchIntent', {})
        if intent.get('result') not in ('pending', 'unknown') or not intent.get('bridgeOperationId'):
            raise RuntimeError('unresolved bridge dispatch intent required')
        if not getattr(self.adapter, 'capabilities', {}).get('queued_actions'):
            raise ValueError('queued bridge adapter required')
        if self.adapter.dispatch_binding() != {
                'runId': self.run_id, 'role': role, 'attempt': attempt,
                'taskRevision': worker.get('activityRevision', 0),
                'operationId': intent['bridgeOperationId']}:
            raise ValueError('bridge dispatch receipt binding mismatch')
        self._scope_reservations()
        self.adapter.completed_message(worker['threadId'], intent['message'])
        return self._finish_dispatch(role, attempt)

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

    def _contract_drift(self, worker):
        freeze = worker.get('contractFreeze')
        if not freeze:
            return None
        try:
            return check_contracts(worker['worktree'], freeze)
        except ContractError as exc:
            raise RuntimeError('contract_drift: ' + str(exc)) from exc

    def freeze_worker_contracts(self, role, attempt, contract_paths=None):
        """从显式配置冻结稳定契约；不修改工作树或批准变更。"""
        worker = self._worker(role, attempt)
        if worker.get('state') not in ('pending', 'validating', 'ready'):
            raise RuntimeError('contract freeze requires pending/validating/ready worker')
        paths = load_stable_contracts(self.repo_root) if contract_paths is None else contract_paths
        if not isinstance(paths, list) or not paths:
            return None
        observed = self._observe_worker(worker)
        if observed['head'] != worker['baseSha']:
            raise RuntimeError('contract freeze requires worker HEAD at base')
        freeze = hash_contracts(worker['worktree'], paths)
        worker['contractFreeze'] = freeze
        self.save()
        return freeze

    def approve_contract_changes(self, role, attempt, reason):
        """记录 leader 批准的契约变更，并失效旧报告/验收证据。"""
        worker = self._worker(role, attempt)
        reason = str(reason or '').strip()
        if not reason:
            raise ValueError('contract change approval reason required')
        freeze = worker.get('contractFreeze')
        if not freeze:
            raise RuntimeError('no contract freeze recorded')
        drift = self._contract_drift(worker)
        if not drift or not drift['changed']:
            raise RuntimeError('no contract drift to approve')
        if worker.get('state') in ('integrated', 'archived', 'cancelled', 'failed'):
            raise RuntimeError('terminal worker cannot approve contract changes')
        previous = copy.deepcopy(freeze)
        history = worker.setdefault('contractHistory', [])
        history.append({
            'previousHash': previous.get('contractHash'),
            'newHash': drift['current']['contractHash'],
            'changedPaths': drift['changed'],
            'reason': reason,
            'approvedAt': _now_iso(),
        })
        worker['contractFreeze'] = drift['current']
        for key in ('report', 'acceptance', 'verificationPlan', 'branchTip',
                    'stopStatus', 'stopActivityRevision'):
            worker.pop(key, None)
        if worker.get('state') in ('reported', 'blocked'):
            worker['state'] = 'running'
        worker['activityRevision'] = worker.get('activityRevision', 0) + 1
        self.save()
        return {'freeze': copy.deepcopy(worker['contractFreeze']),
                'history': copy.deepcopy(history[-1])}

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
        drift = self._contract_drift(worker)
        if drift and drift['changed']:
            raise RuntimeError('contract_drift: ' + ', '.join(drift['changed']))
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
        add_verified(
            self.state, stage='worker', role=role, attempt=attempt,
            task_revision=worker.get('activityRevision', 0),
            sha=observed['head'], report_sha=report['reportSha'],
            plan_hash=result.get('planHash'))
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
        drift = self._contract_drift(worker)
        if drift and drift['changed']:
            raise RuntimeError('contract_drift: ' + ', '.join(drift['changed']))
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
        add_verified(
            self.state, stage='integration', role=role, attempt=attempt,
            task_revision=worker.get('activityRevision', 0),
            sha=head, report_sha=report['reportSha'],
            plan_hash=integrated.get('planHash'))
        add_landed(
            self.state, branch=worker['branch'], sha=head, before_sha=before,
            task_revision=worker.get('activityRevision', 0),
            plan_hash=integrated.get('planHash'))
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

    def record_recovery_preservation(self, role, attempt, observed):
        """持久化确认失联后的旧 attempt 成果保全记录；不创建 replacement。"""
        worker = self._worker(role, attempt)
        if worker.get('state') in ('integrated', 'archived', 'cancelled'):
            raise RuntimeError('terminal worker cannot record recovery preservation')
        record = build_preservation_record(worker, observed)
        actual = self._git(self.repo_root, 'rev-parse', '--verify',
                           record['branch'] + '^{commit}')
        if actual != record['head']:
            raise RuntimeError('preserved branch head does not match observation')
        snapshot = copy.deepcopy(self.state)
        try:
            worker['recoveryPreservation'] = record
            self.save()
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise
        return copy.deepcopy(record)

    def authorize_recovery_replacement(self, role, attempt, replacement_limit=None):
        """旧成果保全通过后授权下一 attempt；不创建线程或 worktree。"""
        worker = self._worker(role, attempt)
        record = worker.get('recoveryPreservation')
        if not verify_preservation_record(record):
            raise RuntimeError('valid recovery preservation record required')
        if (record.get('oldAttempt') != attempt
                or record.get('branch') != worker.get('branch')):
            raise RuntimeError('recovery preservation identity mismatch')
        if replacement_limit is None:
            replacement_limit = self.state.get('limits', {}).get('replacementPerRole', 1)
        if any(w.get('role') == role and w.get('attempt', 0) > attempt
               for w in self.state.get('workers', [])):
            raise RuntimeError('replacement attempt already exists')
        result = check_retry(attempt, replacement_limit)
        if result['status'] == 'blocked':
            raise RuntimeError(f'replacement limit reached for {role}#{attempt}')
        authorization = {
            'preservationSha': record['preservationSha'],
            'nextAttempt': attempt + 1,
            'limit': replacement_limit,
            'authorizedAt': _now_iso(),
        }
        snapshot = copy.deepcopy(self.state)
        try:
            worker['recoveryReplacementAuthorization'] = authorization
            self.save()
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise
        return copy.deepcopy(authorization)

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

    def observe_worker_host(self, role, attempt):
        """保存桥接只读观察；不推进 worker 状态，不生成停止或验收证据。"""
        if self.state.get('runId') != self.run_id:
            raise ValueError('state runId does not match controller run_id')
        worker = self._worker(role, attempt)
        if not worker.get('threadId'):
            raise ValueError('registered threadId required')
        observation = self.adapter.read_thread(worker['threadId'])
        expected = {'runId': self.run_id, 'role': role, 'attempt': attempt,
                    'taskRevision': worker.get('activityRevision', 0)}
        if (not isinstance(observation, dict)
                or any(observation.get(k) != v for k, v in expected.items())
                or not isinstance(observation.get('metadata'), dict)
                or observation['metadata'].get('threadId') != worker['threadId']
                or observation.get('source') != 'desktop_tool_bridge'):
            raise ValueError('host observation binding mismatch')
        if worker.get('hostObservation') == observation:
            return copy.deepcopy(observation)
        before = copy.deepcopy(self.state)
        worker['hostObservation'] = copy.deepcopy(observation)
        try:
            self.save()
        except Exception:
            self.state.clear()
            self.state.update(before)
            raise
        return copy.deepcopy(observation)

    def record_push_verification(self, sha, remote, ref, remote_sha,
                                 authorization_evidence, observed_at=None):
        """记录已观察到的远端 SHA；不执行 push，也不改变本地 HEAD。"""
        current = self.state.get('integration', {}).get(
            'currentSha', self.state.get('integration', {}).get('baseSha'))
        if sha != current:
            raise RuntimeError('push verification must bind current integration SHA')
        try:
            self._git(self.repo_root, 'remote', 'get-url', remote)
        except RuntimeError as exc:
            raise ValueError('remote is not configured') from exc
        record = add_push_verified(
            self.state, sha=sha, remote=remote, ref=ref, remote_sha=remote_sha,
            authorization_evidence=authorization_evidence, observed_at=observed_at)
        self.save()
        return record

    def _setup_git_observation(self, worker):
        root = Path(worker.get('worktree') or '').resolve()
        if not root.is_dir():
            raise RuntimeError('worker worktree unavailable for setup')
        common = Path(self._git(root, 'rev-parse', '--path-format=absolute',
                                '--git-common-dir')).resolve()
        return {
            'workerRoot': str(root),
            'gitCommonDir': str(common),
            'branch': self._git(root, 'symbolic-ref', '--short', 'HEAD'),
            'head': self._git(root, 'rev-parse', 'HEAD'),
            'statusPorcelain': self._git(root, 'status', '--porcelain=v1',
                                         '--untracked-files=all'),
            'observedAt': _now_iso(),
        }

    def run_worker_setup(self, role, attempt, config_path=None):
        """执行显式 setup；失败只记录并阻断，不自动重试或重置文件。"""
        worker = self._worker(role, attempt)
        if worker.get('state') not in ('pending', 'validating'):
            raise RuntimeError('setup requires pending/validating worker')
        config = load_setup_config(self.repo_root, config_path)
        if config is None:
            return {'status': 'not_configured', 'ran': False}
        existing = worker.get('setupEvidence')
        if (isinstance(existing, dict) and existing.get('status') == 'passed'
                and existing.get('configHash') == config['configHash']):
            return dict(existing, reused=True)
        try:
            result = run_setup_command(worker['worktree'], config)
            observation = self._setup_git_observation(worker)
        except (SetupConfigError, OSError, RuntimeError) as exc:
            code = 'setup_observation_failed'
            failure = build_failure_record(
                source='workspace_setup', code=code, stage='setup',
                message=str(exc), scope=f'{role}#{attempt}',
                result_certainty='confirmed_failure', retryable=False,
                evidence='setup command/observation did not complete with a verifiable worker identity')
            snapshot = copy.deepcopy(self.state)
            try:
                worker['setupStatus'] = 'failed'
                worker.setdefault('errors', []).append({
                    'code': code, 'message': str(exc), 'at': _now_iso()})
                self.state.setdefault('errors', []).append(failure)
                self.save()
            except Exception:
                self.state.clear()
                self.state.update(snapshot)
                raise
            return {'status': 'failed', 'ran': True, 'code': code, 'message': str(exc)}
        worker_root = observation['workerRoot']
        expected_status = {
            'workerRoot': str(Path(worker['worktree']).resolve()),
            'gitCommonDir': self.state['project']['commonDirectory'],
            'branch': worker['branch'],
            'head': worker['baseSha'],
        }
        mismatches = []
        if worker_root != expected_status['workerRoot']:
            mismatches.append('worktree_root_mismatch')
        if Path(observation['gitCommonDir']).resolve() != Path(expected_status['gitCommonDir']).resolve():
            mismatches.append('git_common_dir_mismatch')
        if observation['branch'] != expected_status['branch']:
            mismatches.append('branch_mismatch')
        if observation['head'] != expected_status['head']:
            mismatches.append('head_not_base')
        if observation['statusPorcelain']:
            mismatches.append('dirty_worktree')
        passed = result['status'] == 'passed' and not mismatches
        evidence = {
            'status': 'passed' if passed else 'failed',
            'configHash': config['configHash'],
            'configPath': config['configPath'],
            'argv': config['argv'],
            'cwd': result['cwd'],
            'timeoutSeconds': config['timeoutSeconds'],
            'failurePolicy': config['failurePolicy'],
            'idempotent': config['idempotent'],
            'exitCode': result['exitCode'],
            'timedOut': result['timedOut'],
            'outputTruncated': result['outputTruncated'],
            'stdoutSha256': result['stdoutSha256'],
            'stderrSha256': result['stderrSha256'],
            'stdoutBytes': result['stdoutBytes'],
            'stderrBytes': result['stderrBytes'],
            'startedAt': result['startedAt'],
            'finishedAt': result['finishedAt'],
            'workerRoot': worker_root,
            'branch': observation['branch'],
            'head': observation['head'],
            'statusPorcelain': observation['statusPorcelain'],
            'observedAt': observation['observedAt'],
            'checks': mismatches,
        }
        snapshot = copy.deepcopy(self.state)
        try:
            worker['setupEvidence'] = evidence
            worker['setupStatus'] = evidence['status']
            if not passed:
                code = ('setup_timeout' if result['timedOut']
                        else ('setup_dirty_worktree' if observation['statusPorcelain']
                              else 'setup_failed'))
                failure = build_failure_record(
                    source='workspace_setup', code=code, stage='setup',
                    message='setup failed: ' + (', '.join(mismatches) or result['status']),
                    scope=f'{role}#{attempt}', result_certainty='confirmed_failure',
                    retryable=bool(config['idempotent']),
                    evidence='explicit setup configuration; no automatic retry performed')
                worker.setdefault('errors', []).append({
                    'code': code, 'message': failure['message'], 'at': _now_iso()})
                self.state.setdefault('errors', []).append(failure)
            self.save()
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise
        return evidence

    def record_preflight_contract(self, run_inputs, worker_inputs):
        """持久化分层 preflight 契约；不执行环境探测或宿主调用。"""
        contract = build_preflight_contract(
            self.run_id, run_inputs, worker_inputs)
        self.state['preflight'] = contract
        self.save()
        return contract

    def evaluate_preflight_contract(self, current_run_inputs, current_worker_inputs,
                                    run_evidence_at=None, worker_evidence_at=None,
                                    max_age_seconds=None, now=None):
        return evaluate_preflight(
            self.state.get('preflight'), current_run_inputs, current_worker_inputs,
            run_evidence_at=run_evidence_at,
            worker_evidence_at=worker_evidence_at,
            max_age_seconds=max_age_seconds, now=now)

    def delivery_status(self, current_sha=None):
        return delivery_status(self.state, current_sha)

    def record_capabilities(self):
        self.state['hostCapabilities'] = self.capability_snapshot()
        self.save()
        return self.state['hostCapabilities']

    def record_failure(self, source, code, stage, message, scope='run',
                       result_certainty='confirmed_failure', retryable=False,
                       recommendation=None, evidence=None, retry_evidence=None):
        """持久化规范化失败记录，不执行任何恢复或重试动作。"""
        failure = build_failure_record(
            source=source, code=code, stage=stage, message=message, scope=scope,
            result_certainty=result_certainty, retryable=retryable,
            recommendation=recommendation, evidence=evidence,
            retry_evidence=retry_evidence)
        self.state.setdefault('errors', []).append(failure)
        self.save()
        return failure

    def create_worker(self, prompt, project_id, timeout_seconds=300):
        """创建 worker；超时记录证据后抛出，不自动重试或发送消息。"""
        if self.state.get('status') in ('aborted', 'failed', 'complete', 'cleanup_pending'):
            raise RuntimeError(
                f'run is {self.state.get("status")}; create_worker blocked')
        if not self.state.get('authorization', {}).get('provenance'):
            raise RuntimeError('authorization provenance missing; create_worker blocked')
        if getattr(self.adapter, 'capabilities', {}).get('queued_actions'):
            binding = self.adapter.dispatch_binding()
            if (self.state.get('runId') != self.run_id or binding.get('runId') != self.run_id
                    or project_id != self.state['project']['projectId']):
                raise ValueError('bridge creation run/project mismatch')
            worker = self._worker(binding['role'], binding['attempt'])
            self._ensure_external_action_allowed(worker, 'create_worker')
            if (self._active_workers().get(binding['role']) is not worker
                    or worker['state'] not in ('creating', 'pending')
                    or worker.get('threadId')
                    or binding['taskRevision'] != worker.get('activityRevision', 0)):
                raise ValueError('bridge creation worker binding/state mismatch')
        try:
            if getattr(self.adapter, 'capabilities', {}).get('create_timeout'):
                return self.adapter.create_worker(
                    prompt, project_id, timeout_seconds=timeout_seconds)
            return self.adapter.create_worker(prompt, project_id)
        except TimeoutError as exc:
            failure = build_failure_record(
                source='host_api', code='create_timeout', stage='create_worker',
                message=f'{type(exc).__name__}: {exc}', scope='run',
                result_certainty='unknown', retryable=False,
                evidence='create call timed out; whether a thread or worktree was created is unknown')
            self.state['lastCreateError'] = dict(failure)
            self.state.setdefault('errors', []).append(failure)
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

    def confirm_stop_from_wait(self, role, attempt, observation, max_age_seconds=60):
        """只从新鲜、同任务的真实等待回执确认停止，不从 timeout 推导停止。"""
        worker = self._review_worker(role, attempt)
        expected = {'runId': self.run_id, 'role': role, 'attempt': attempt,
                    'taskRevision': worker.get('activityRevision', 0)}
        if (not isinstance(observation, dict)
                or any(observation.get(k) != v for k, v in expected.items())
                or observation.get('source') != 'desktop_tool_bridge'):
            raise ValueError('wait observation binding mismatch')
        from desktop_lifecycle_bridge import validate_lifecycle_metadata, WAIT
        metadata = observation.get('metadata', {})
        validate_lifecycle_metadata(WAIT, {'targets': [{'threadId': worker['threadId']}]}, metadata)
        if type(max_age_seconds) is not int or not 0 <= max_age_seconds <= 60:
            raise ValueError('invalid wait evidence age limit')
        try:
            stamp = datetime.fromisoformat(observation['observedAt'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError('invalid wait observation time') from exc
        if not 0 <= age <= max_age_seconds:
            raise ValueError('wait observation is stale or in the future')
        polls = metadata['result']['polls']
        poll = next((p for p in polls if p['thread']['id'] == worker['threadId']), None)
        turn = (poll or {}).get('latestTurn') or {}
        message = (poll or {}).get('latestAssistantMessage') or {}
        report = worker.get('report', {})
        # 宿主 wait 不携带 Threadweave taskRevision。仅给新回执贴版本标签，
        # 无法排除其返回上一轮的 idle/final；最终消息必须回显当前已接收报告哈希。
        report_sha = report.get('reportSha')
        if (not poll or poll['thread']['status']['type'] != 'idle'
                or turn.get('status') != 'completed' or message.get('phase') != 'final_answer'
                or not isinstance(message.get('text'), str) or message.get('turnId') != turn.get('id')
                or not turn.get('id') or worker['state'] != 'reported'
                or report.get('taskRevision') != worker.get('activityRevision', 0)
                or not isinstance(report_sha, str) or len(report_sha) != 64
                or report_sha not in message['text']):
            raise RuntimeError('wait receipt does not prove completed/idle final answer for current report')
        snapshot = copy.deepcopy(self.state)
        try:
            return self.confirm_stop(role, attempt, idle=True, final_answer_present=True, no_active_turn=True)
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise

    def archive_worker_host(self, role, attempt):
        """先确认已集成且停止，再经桥接归档聊天；queued 不推进本地生命周期。"""
        worker = self._review_worker(role, attempt)
        if worker['state'] not in ('integrated', 'archived'):
            raise RuntimeError('host archive requires integrated worker')
        self._require_stopped(worker)
        if not self.state.get('authorization', {}).get('provenance'):
            raise RuntimeError('authorization provenance missing; archive blocked')
        expected = {'runId': self.run_id, 'role': role, 'attempt': attempt,
                    'taskRevision': worker.get('activityRevision', 0)}
        binding = self.adapter.dispatch_binding()
        if any(binding.get(k) != v for k, v in expected.items()):
            raise ValueError('archive adapter binding mismatch')
        observation = self.adapter.archive_thread(worker['threadId'])
        if (not isinstance(observation, dict)
                or any(observation.get(k) != v for k, v in expected.items())
                or observation.get('source') != 'desktop_tool_bridge'):
            raise ValueError('archive observation binding mismatch')
        from desktop_lifecycle_bridge import validate_lifecycle_metadata, ARCHIVE_THREAD
        validate_lifecycle_metadata(ARCHIVE_THREAD, {'threadId': worker['threadId']}, observation.get('metadata', {}))
        if worker['state'] == 'archived':
            return observation
        snapshot = copy.deepcopy(self.state)
        try:
            self.transition_worker(role, attempt, 'archived')
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise
        return observation

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

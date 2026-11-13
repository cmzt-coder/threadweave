"""桌面 leader 工具桥接：持久请求、显式领取、真实结果回填。

支持 read_thread 与显式配置的创建/消息请求。Python 不连接桌面私有 socket，也不直接调用
Codex 工具；leader 执行领取的请求，再提供带关联信息的结果。哈希与领取令牌
防止误关联，不证明回填者身份或服务端签名。所有外部结果仍需 leader 审核。
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from host_adapter import HostActionPending, UnsupportedHostAdapter
from state_store import StateError, StateLock, read_state, run_state_dir, run_state_path, write_state_atomic


METHOD = 'mcp__codex_app__read_thread'
CREATE_METHOD = 'mcp__codex_app__create_thread'
SEND_METHOD = 'mcp__codex_app__send_message_to_thread'
SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _segment(value):
    if not isinstance(value, str) or not SEGMENT.fullmatch(value):
        raise ValueError('invalid bridge identity segment')
    return value


class BridgePending(HostActionPending):
    """请求已持久化，等待 leader 领取/回填；不能当作工具调用已成功。"""


class BridgeJournal:
    def __init__(self, repo, run_id):
        self.repo = Path(repo).resolve()
        self.run_id = _segment(run_id)
        self.root = run_state_dir(self.repo, self.run_id) / 'bridge'

    def _path(self, operation_id):
        path = self.root / (_segment(operation_id) + '.json')
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError('bridge paths must not be symlinks')
        # 每次访问都重新检查 run 目录，避免初始化后的路径变化。
        run_state_dir(self.repo, self.run_id)
        return path

    @staticmethod
    def _lock(path):
        lock = path.with_suffix('.lock')
        if lock.is_symlink():
            raise ValueError('bridge lock must not be a symlink')
        return StateLock(lock)

    def _read(self, path):
        value = read_state(path)
        if (not isinstance(value, dict) or value.get('version') != 1
                or value.get('runId') != self.run_id
                or value.get('operationId') != path.stem):
            raise ValueError('bridge record identity mismatch')
        request = value.get('request', {})
        if not isinstance(request, dict):
            raise ValueError('bridge request must be an object')
        binding = request.get('binding', {})
        if (not isinstance(binding, dict) or binding.get('runId') != self.run_id
                or set(binding) != {'runId', 'role', 'attempt', 'taskRevision'}
                or type(binding.get('attempt')) is not int or binding['attempt'] < 1
                or type(binding.get('taskRevision')) is not int or binding['taskRevision'] < 0):
            raise ValueError('invalid request binding')
        _segment(binding.get('role'))
        if (set(request) != {'method', 'binding', 'params'}
                or value.get('requestSha') != digest(request)):
            raise ValueError('bridge request hash/method mismatch')
        self._validate_params(request['method'], request['params'])
        if value.get('status') not in ('pending', 'in_flight', 'unknown', 'succeeded', 'failed'):
            raise ValueError('invalid bridge status')
        if value['status'] in ('succeeded', 'failed'):
            response = value.get('response', {})
            if value.get('responseSha') != digest(response):
                raise ValueError('bridge response hash mismatch')
            self._validate_response(value, response)
            if value['status'] != response['status']:
                raise ValueError('bridge status/result mismatch')
        if 'creationResolution' in value:
            self._validate_creation_resolution(value, value['creationResolution'])
            if value.get('creationResolutionSha') != digest(value['creationResolution']):
                raise ValueError('creation resolution hash mismatch')
        return value

    @staticmethod
    def _validate_params(method, params):
        if not isinstance(params, dict):
            raise ValueError('bridge parameters must be an object')
        def text_field(name):
            if not isinstance(params.get(name), str) or not params[name].strip():
                raise ValueError(name + ' required')
        if method == METHOD:
            text_field('threadId')
            if params != {'threadId': params['threadId'], 'hostId': 'local',
                          'turnLimit': 1, 'includeOutputs': False, 'maxOutputCharsPerItem': 400}:
                raise ValueError('invalid read-only bridge parameters')
        elif method == SEND_METHOD:
            if set(params) != {'threadId', 'hostId', 'prompt'} or params.get('hostId') != 'local':
                raise ValueError('invalid send parameters; no model override allowed')
            text_field('threadId'); text_field('prompt')
        elif method == CREATE_METHOD:
            if set(params) != {'prompt', 'model', 'thinking', 'title', 'target'}:
                raise ValueError('explicit creation configuration required')
            for field in ('prompt', 'model', 'thinking', 'title'):
                text_field(field)
            if params['thinking'] not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
                raise ValueError('invalid thinking')
            target = params['target']
            if (not isinstance(target, dict) or set(target) != {'type', 'projectId', 'environment'}
                    or target['type'] != 'project' or not isinstance(target['projectId'], str)
                    or not target['projectId'].strip()):
                raise ValueError('project target required')
            env = target['environment']
            if (not isinstance(env, dict) or set(env) != {'type', 'startingState'}
                    or env['type'] != 'worktree'):
                raise ValueError('isolated worktree required')
            start = env['startingState']
            if (not isinstance(start, dict) or set(start) != {'type', 'branchName'}
                    or start['type'] != 'branch' or not isinstance(start['branchName'], str)
                    or not start['branchName'].strip()):
                raise ValueError('existing baseline ref required; no implicit branch creation')
        else:
            from desktop_lifecycle_bridge import validate_lifecycle_params
            validate_lifecycle_params(method, params)

    def _prepare(self, operation_id, role, attempt, task_revision, method, params):
        _segment(role)
        for name, number, minimum in [('attempt', attempt, 1), ('taskRevision', task_revision, 0)]:
            if type(number) is not int or number < minimum:
                raise ValueError('invalid ' + name)
        self._validate_params(method, params)
        request = {'method': method, 'binding': {
            'runId': self.run_id, 'role': role, 'attempt': attempt,
            'taskRevision': task_revision}, 'params': copy.deepcopy(params)}
        path = self._path(operation_id)
        with self._lock(path):
            if path.exists():
                record = self._read(path)
                if record['request'] != request:
                    raise ValueError('operationId already bound to different request')
                return record
            record = {'version': 1, 'runId': self.run_id, 'operationId': operation_id,
                      'status': 'pending', 'createdAt': now(), 'request': request,
                      'requestSha': digest(request)}
            write_state_atomic(path, record)
            return copy.deepcopy(record)

    def prepare_read(self, operation_id, role, attempt, task_revision, thread_id):
        return self._prepare(operation_id, role, attempt, task_revision, METHOD, {
            'threadId': thread_id, 'hostId': 'local', 'turnLimit': 1,
            'includeOutputs': False, 'maxOutputCharsPerItem': 400})

    def prepare_create(self, operation_id, role, attempt, task_revision, params):
        return self._prepare(operation_id, role, attempt, task_revision, CREATE_METHOD, params)

    def prepare_send(self, operation_id, role, attempt, task_revision, thread_id, message):
        return self._prepare(operation_id, role, attempt, task_revision, SEND_METHOD, {
            'threadId': thread_id, 'hostId': 'local', 'prompt': message})

    def prepare_lifecycle(self, operation_id, role, attempt, task_revision, method, params):
        """准备等待/资源生命周期请求；实际执行仍由桌面 leader 完成。"""
        from desktop_lifecycle_bridge import validate_lifecycle_params
        validate_lifecycle_params(method, params)
        return self._prepare(operation_id, role, attempt, task_revision, method, params)

    def inspect(self, operation_id):
        return self._read(self._path(operation_id))

    def claim(self, operation_id):
        path = self._path(operation_id)
        with self._lock(path):
            record = self._read(path)
            if record['status'] != 'pending':
                raise RuntimeError('operation is not pending; reconcile, do not resend')
            record.update(status='in_flight', claimedAt=now(), claimToken=str(uuid.uuid4()))
            write_state_atomic(path, record)
            return copy.deepcopy(record)

    def mark_unknown(self, operation_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('reconciliation reason required')
        path = self._path(operation_id)
        with self._lock(path):
            record = self._read(path)
            if record['status'] not in ('in_flight', 'unknown'):
                raise RuntimeError('only in_flight operations can become unknown')
            record.update(status='unknown', unknownAt=now(), unknownReason=reason)
            write_state_atomic(path, record)
            return copy.deepcopy(record)

    @staticmethod
    def _validate_response(record, response):
        if not isinstance(response, dict):
            raise ValueError('response must be an object')
        fields = {'operationId', 'runId', 'requestSha', 'claimToken', 'method',
                  'source', 'observedAt', 'status', 'toolResultSha'}
        fields.add('metadata' if response.get('status') == 'succeeded' else 'error')
        if set(response) != fields:
            raise ValueError('unexpected or missing response fields')
        for field in ('operationId', 'runId', 'requestSha', 'claimToken'):
            if response.get(field) != record.get(field):
                raise ValueError('response binding mismatch: ' + field)
        if response.get('method') != record['request']['method'] or response.get('source') != 'desktop_tool_bridge':
            raise ValueError('response source/method mismatch')
        try:
            stamp = datetime.fromisoformat(response['observedAt'].replace('Z', '+00:00'))
            claimed = datetime.fromisoformat(record['claimedAt'])
            if stamp.tzinfo is None or not claimed <= stamp <= datetime.now(timezone.utc):
                raise ValueError('response observation time out of range')
        except (KeyError, TypeError, AttributeError):
            raise ValueError('invalid response observation time')
        if response.get('status') == 'succeeded':
            metadata = response.get('metadata')
            if not isinstance(metadata, dict) or metadata.get('hostId') != 'local':
                raise ValueError('response metadata/hostId required')
            method = record['request']['method']
            if method == CREATE_METHOD:
                keys = set(metadata)
                if keys not in ({'threadId', 'hostId'}, {'clientThreadId', 'hostId'}):
                    raise ValueError('creation requires exactly one actual or asynchronous ID')
                identity = metadata.get('threadId', metadata.get('clientThreadId'))
                if not isinstance(identity, str) or not identity.strip():
                    raise ValueError('creation identity required')
            elif method in (METHOD, SEND_METHOD):
                if (set(metadata) != {'threadId', 'status', 'hostId'}
                        or metadata.get('threadId') != record['request']['params']['threadId']):
                    raise ValueError('response thread identity mismatch')
                if not isinstance(metadata.get('status'), str) or not metadata['status']:
                    raise ValueError('thread status required')
                if method == SEND_METHOD and metadata['status'] != 'accepted':
                    raise ValueError('send receipt only proves accepted, not task completion')
            else:
                from desktop_lifecycle_bridge import validate_lifecycle_metadata
                validate_lifecycle_metadata(method, record['request']['params'], metadata)
        elif response.get('status') == 'failed':
            if not isinstance(response.get('error'), str) or not response['error']:
                raise ValueError('failed response requires error')
        else:
            raise ValueError('invalid terminal response status')
        sha = response.get('toolResultSha')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError('tool result hash required')

    @staticmethod
    def _validate_creation_resolution(record, resolution):
        if (record['request']['method'] != CREATE_METHOD or record['status'] != 'succeeded'
                or 'clientThreadId' not in record['response'].get('metadata', {})):
            raise ValueError('only successful asynchronous creation can be resolved')
        fields = {'clientThreadId', 'threadId', 'hostId', 'projectId', 'title',
                  'model', 'thinking', 'branchName', 'cwd', 'observedAt', 'toolResultSha', 'evidence'}
        if not isinstance(resolution, dict) or set(resolution) != fields:
            raise ValueError('exact creation reconciliation metadata required')
        for field in fields:
            if not isinstance(resolution[field], str) or not resolution[field].strip():
                raise ValueError('invalid resolution ' + field)
        params = record['request']['params']
        expected = {k: params[k] for k in ('title', 'model', 'thinking')}
        expected.update(hostId='local', projectId=params['target']['projectId'],
                        branchName=params['target']['environment']['startingState']['branchName'],
                        clientThreadId=record['response']['metadata']['clientThreadId'])
        if any(resolution[k] != v for k, v in expected.items()):
            raise ValueError('asynchronous creation identity mismatch')
        if (resolution['threadId'] == resolution['clientThreadId']
                or not Path(resolution['cwd']).is_absolute()
                or not re.fullmatch(r'[0-9a-f]{64}', resolution['toolResultSha'])):
            raise ValueError('actual threadId, absolute cwd and evidence hash required')
        stamp = datetime.fromisoformat(resolution['observedAt'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or not datetime.fromisoformat(record['claimedAt']) <= stamp <= datetime.now(timezone.utc):
            raise ValueError('resolution observation time out of range')

    def resolve_creation(self, operation_id, resolution):
        """leader 独立核对精确元数据后回填；不靠标题猜测线程，不重复创建。"""
        path = self._path(operation_id)
        with self._lock(path):
            record = self._read(path)
            self._validate_creation_resolution(record, resolution)
            if 'creationResolution' in record:
                if record['creationResolution'] != resolution:
                    raise ValueError('creation resolution cannot be overwritten')
                return copy.deepcopy(record)
            record.update(creationResolution=copy.deepcopy(resolution), creationResolutionSha=digest(resolution))
            write_state_atomic(path, record)
            return copy.deepcopy(record)

    def complete(self, operation_id, response, reconciliation=None):
        path = self._path(operation_id)
        with self._lock(path):
            record = self._read(path)
            self._validate_response(record, response)
            if record['status'] in ('succeeded', 'failed'):
                if record['response'] != response:
                    raise ValueError('terminal result cannot be overwritten')
                return copy.deepcopy(record)
            if record['status'] == 'pending':
                raise RuntimeError('operation must be claimed before result submission')
            if record['status'] == 'unknown':
                if not isinstance(reconciliation, str) or not reconciliation.strip():
                    raise RuntimeError('unknown operation requires explicit reconciliation evidence')
                record['reconciliation'] = {'at': now(), 'evidence': reconciliation}
            record.update(status=response['status'], response=copy.deepcopy(response),
                          responseSha=digest(response), completedAt=now())
            write_state_atomic(path, record)
            return copy.deepcopy(record)


class DesktopBridgeAdapter(UnsupportedHostAdapter):
    """HostAdapter 的只读实现；其他外部动作仍默认拒绝。"""
    capabilities = {'update_plan': False, 'wait_threads': False,
                    'create_timeout': False, 'read_thread': True,
                    'create_worker': False, 'send_message': False,
                    'create_worktree': False, 'archive_worktree': False}

    def __init__(self, repo, run_id, operation_id, role, attempt, task_revision=0):
        self.journal = BridgeJournal(repo, run_id)
        self.operation_id = operation_id
        self.role, self.attempt, self.task_revision = role, attempt, task_revision

    def read_thread(self, thread_id):
        record = self.journal.prepare_read(self.operation_id, self.role, self.attempt,
                                           self.task_revision, thread_id)
        if record['status'] == 'failed':
            raise RuntimeError('host read failed: ' + record['response']['error'])
        if record['status'] != 'succeeded':
            raise BridgePending('bridge operation ' + self.operation_id + ': ' + record['status'])
        response = record['response']
        return {**record['request']['binding'], 'operationId': self.operation_id,
                'source': response['source'], 'observedAt': response['observedAt'],
                'requestSha': record['requestSha'], 'responseSha': record['responseSha'],
                'toolResultSha': response['toolResultSha'], 'metadata': copy.deepcopy(response['metadata'])}


class DesktopWriteBridgeAdapter(DesktopBridgeAdapter):
    """显式启用创建和消息账本；实际工具仍由桌面 leader 执行。"""

    def __init__(self, *args, create_config=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.create_config = copy.deepcopy(create_config)
        self.capabilities = dict(super().capabilities, create_worker=create_config is not None,
                                 send_message=True, queued_actions=True)

    def dispatch_binding(self):
        return {'runId': self.journal.run_id, 'role': self.role, 'attempt': self.attempt,
                'taskRevision': self.task_revision, 'operationId': self.operation_id}

    @staticmethod
    def _result(record):
        if record['status'] == 'failed':
            raise RuntimeError('host action failed: ' + record['response']['error'])
        if record['status'] != 'succeeded':
            raise BridgePending('bridge operation ' + record['operationId'] + ': ' + record['status'])
        return copy.deepcopy(record['response']['metadata'])

    def create_worker(self, prompt, project_id):
        if self.create_config is None:
            raise NotImplementedError('explicit create_config required')
        if set(self.create_config) != {'model', 'thinking', 'title', 'branchName'}:
            raise ValueError('invalid create_config')
        params = {k: self.create_config[k] for k in ('model', 'thinking', 'title')}
        params.update(prompt=prompt, target={'type': 'project', 'projectId': project_id,
                      'environment': {'type': 'worktree', 'startingState': {
                          'type': 'branch', 'branchName': self.create_config['branchName']}}})
        record = self.journal.prepare_create(self.operation_id, self.role, self.attempt,
                                             self.task_revision, params)
        metadata = self._result(record)
        if 'clientThreadId' in metadata:
            if 'creationResolution' not in record:
                raise BridgePending('asynchronous creation requires exact metadata reconciliation; do not recreate')
            metadata = {k: record['creationResolution'][k] for k in ('threadId', 'hostId')}
        return metadata

    def send_message(self, thread_id, message):
        self.journal.prepare_send(self.operation_id, self.role, self.attempt,
                                  self.task_revision, thread_id, message)
        return self.completed_message(thread_id, message)

    def completed_message(self, thread_id, message):
        """仅回读既存发送；恢复派发时禁止生成新的请求。"""
        record = self.journal.inspect(self.operation_id)
        expected = {'method': SEND_METHOD, 'binding': {
            k: v for k, v in self.dispatch_binding().items() if k != 'operationId'},
            'params': {'threadId': thread_id, 'hostId': 'local', 'prompt': message}}
        if record['request'] != expected:
            raise ValueError('message receipt binding mismatch')
        return self._result(record)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Desktop bridge request journal; never calls host tools')
    parser.add_argument('action', choices=['observe', 'resume-dispatch', 'prepare-read', 'prepare-create', 'prepare-send',
                                         'resolve-creation', 'inspect', 'claim', 'unknown', 'complete'])
    parser.add_argument('--repo', default='.')
    parser.add_argument('--run', required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--role')
    parser.add_argument('--attempt', type=int, default=1)
    parser.add_argument('--task-revision', type=int, default=0)
    parser.add_argument('--thread')
    parser.add_argument('--reason')
    parser.add_argument('--response')
    parser.add_argument('--reconciliation')
    parser.add_argument('--params', help='creation tool parameters JSON file')
    parser.add_argument('--message-file', help='UTF-8 message text; never execute as shell code')
    parser.add_argument('--resolution', help='exact async identity reconciliation JSON file')
    args = parser.parse_args(argv)
    try:
        journal = BridgeJournal(args.repo, args.run)
        if args.action in ('observe', 'resume-dispatch'):
            from controller import Controller
            from state_schema import validate_state
            from state_view import write_state_view
            state = read_state(run_state_path(args.repo, args.run))
            validate_state(state)
            worker = next((w for w in state['workers'] if w['role'] == args.role and w['attempt'] == args.attempt), None)
            if worker is None:
                raise ValueError('registered worker role/attempt not found')
            adapter_class = DesktopBridgeAdapter if args.action == 'observe' else DesktopWriteBridgeAdapter
            adapter = adapter_class(args.repo, args.run, args.operation,
                                    args.role, args.attempt, worker.get('activityRevision', 0))
            controller = Controller(adapter, args.repo, args.run, state)
            if args.action == 'observe':
                result = controller.observe_worker_host(args.role, args.attempt)
            else:
                result = controller.resume_bridge_dispatch(args.role, args.attempt)
            write_state_view(args.repo, args.run)
        elif args.action == 'prepare-read':
            result = journal.prepare_read(args.operation, args.role, args.attempt, args.task_revision, args.thread)
        elif args.action == 'prepare-create':
            result = journal.prepare_create(args.operation, args.role, args.attempt,
                                             args.task_revision, read_state(args.params))
        elif args.action == 'prepare-send':
            result = journal.prepare_send(args.operation, args.role, args.attempt,
                                           args.task_revision, args.thread,
                                           Path(args.message_file).read_text(encoding='utf-8'))
        elif args.action == 'resolve-creation':
            result = journal.resolve_creation(args.operation, read_state(args.resolution))
        elif args.action == 'claim':
            result = journal.claim(args.operation)
        elif args.action == 'unknown':
            result = journal.mark_unknown(args.operation, args.reason)
        elif args.action == 'complete':
            result = journal.complete(args.operation, read_state(args.response), args.reconciliation)
        else:
            result = journal.inspect(args.operation)
    except BridgePending as exc:
        parser.exit(3, 'pending: ' + str(exc) + '\n')
    except (ValueError, RuntimeError, OSError, TypeError, StateError) as exc:
        parser.exit(2, 'error: ' + str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

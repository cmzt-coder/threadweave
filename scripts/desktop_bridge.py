"""桌面 leader 工具桥接：持久请求、显式领取、真实结果回填。

第一阶段仅支持 read_thread。Python 不连接桌面私有 socket，也不直接调用
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

from host_adapter import UnsupportedHostAdapter
from state_store import StateError, StateLock, read_state, run_state_dir, run_state_path, write_state_atomic


METHOD = 'mcp__codex_app__read_thread'
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


class BridgePending(RuntimeError):
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
        if (request.get('method') != METHOD
                or value.get('requestSha') != digest(request)):
            raise ValueError('bridge request hash/method mismatch')
        params = request.get('params', {})
        if not isinstance(params, dict):
            raise ValueError('bridge parameters must be an object')
        expected = {'threadId': params.get('threadId'), 'hostId': 'local',
                    'turnLimit': 1, 'includeOutputs': False,
                    'maxOutputCharsPerItem': 400}
        if not isinstance(params.get('threadId'), str) or not params['threadId'] or params != expected:
            raise ValueError('invalid read-only bridge parameters')
        if value.get('status') not in ('pending', 'in_flight', 'unknown', 'succeeded', 'failed'):
            raise ValueError('invalid bridge status')
        if value['status'] in ('succeeded', 'failed'):
            response = value.get('response', {})
            if value.get('responseSha') != digest(response):
                raise ValueError('bridge response hash mismatch')
            self._validate_response(value, response)
            if value['status'] != response['status']:
                raise ValueError('bridge status/result mismatch')
        return value

    def prepare_read(self, operation_id, role, attempt, task_revision, thread_id):
        _segment(role)
        for name, number, minimum in [('attempt', attempt, 1), ('taskRevision', task_revision, 0)]:
            if type(number) is not int or number < minimum:
                raise ValueError('invalid ' + name)
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError('threadId required')
        request = {'method': METHOD, 'binding': {
            'runId': self.run_id, 'role': role, 'attempt': attempt,
            'taskRevision': task_revision}, 'params': {
                'threadId': thread_id, 'hostId': 'local', 'turnLimit': 1,
                'includeOutputs': False, 'maxOutputCharsPerItem': 400}}
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
        if response.get('method') != METHOD or response.get('source') != 'desktop_tool_bridge':
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
            if not isinstance(metadata, dict) or metadata.get('threadId') != record['request']['params']['threadId']:
                raise ValueError('response thread identity mismatch')
            if set(metadata) - {'threadId', 'status', 'hostId'}:
                raise ValueError('unexpected thread metadata fields')
            if metadata.get('hostId') != 'local' or not isinstance(metadata.get('status'), str) or not metadata['status']:
                raise ValueError('thread status/hostId required')
        elif response.get('status') == 'failed':
            if not isinstance(response.get('error'), str) or not response['error']:
                raise ValueError('failed response requires error')
        else:
            raise ValueError('invalid terminal response status')
        sha = response.get('toolResultSha')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError('tool result hash required')

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


def main(argv=None):
    parser = argparse.ArgumentParser(description='Desktop bridge request journal; never calls host tools')
    parser.add_argument('action', choices=['observe', 'prepare-read', 'inspect', 'claim', 'unknown', 'complete'])
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
    args = parser.parse_args(argv)
    try:
        journal = BridgeJournal(args.repo, args.run)
        if args.action == 'observe':
            from controller import Controller
            from state_schema import validate_state
            from state_view import write_state_view
            state = read_state(run_state_path(args.repo, args.run))
            validate_state(state)
            worker = next((w for w in state['workers'] if w['role'] == args.role and w['attempt'] == args.attempt), None)
            if worker is None:
                raise ValueError('registered worker role/attempt not found')
            adapter = DesktopBridgeAdapter(args.repo, args.run, args.operation,
                                           args.role, args.attempt, worker.get('activityRevision', 0))
            controller = Controller(adapter, args.repo, args.run, state)
            result = controller.observe_worker_host(args.role, args.attempt)
            write_state_view(args.repo, args.run)
        elif args.action == 'prepare-read':
            result = journal.prepare_read(args.operation, args.role, args.attempt, args.task_revision, args.thread)
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

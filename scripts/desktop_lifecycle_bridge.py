"""等待与 managed worktree/聊天归档的桌面账本适配。

不自行调用宿主、不删除目录。原始结果完整保留；queued/timeout 不能当作完成。
"""
import argparse
import copy
import json
from pathlib import Path

from desktop_bridge import (BridgeJournal, BridgePending, DesktopWriteBridgeAdapter,
                            digest, now)
from state_store import StateError, read_state

LIST_ARTIFACTS = 'mcp__codex_app__list_artifacts'
WAIT = 'mcp__codex_app__wait_threads'
CREATE_WORKTREE = 'mcp__codex_app__create_worktree'
ATTACH_WORKTREE = 'mcp__codex_app__attach_worktree'
WORKTREE_STATUS = 'mcp__codex_app__get_worktree_creation_status'
ARCHIVE_WORKTREE = 'mcp__codex_app__archive_worktree'
ARCHIVE_THREAD = 'mcp__codex_app__set_thread_archived'
METHODS = {'list-artifacts': LIST_ARTIFACTS, 'wait': WAIT, 'create-worktree': CREATE_WORKTREE,
           'attach-worktree': ATTACH_WORKTREE, 'worktree-status': WORKTREE_STATUS,
           'archive-worktree': ARCHIVE_WORKTREE, 'archive-thread': ARCHIVE_THREAD}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _absolute(value):
    return _text(value) and Path(value).is_absolute() and '..' not in Path(value).parts


def validate_lifecycle_params(method, params):
    if not isinstance(params, dict):
        raise ValueError('lifecycle parameters must be an object')
    if method == LIST_ARTIFACTS:
        if params != {}:
            raise ValueError('list_artifacts only supports current caller attachments')
    elif method == WAIT:
        if (set(params) != {'targets', 'timeoutMs'} or type(params['timeoutMs']) is not int
                or not 0 <= params['timeoutMs'] <= 60000):
            raise ValueError('bounded wait requires integer timeoutMs in 0..60000')
        targets = params['targets']
        if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
            raise ValueError('wait requires 1..8 targets')
        seen = set()
        for target in targets:
            if (not isinstance(target, dict)
                    or set(target) not in ({'threadId', 'hostId'}, {'threadId', 'hostId', 'afterCursor'})
                    or target.get('hostId') != 'local' or not _text(target.get('threadId'))
                    or target['threadId'].startswith('client-new-thread:')
                    or ('afterCursor' in target and not _text(target['afterCursor']))
                    or target['threadId'] in seen):
                raise ValueError('unique actual local thread identities/cursors required')
            seen.add(target['threadId'])
    elif method == CREATE_WORKTREE:
        if (set(params) != {'name', 'ref', 'sourceRoot', 'allowAsync'}
                or params['allowAsync'] is not True or not _absolute(params['sourceRoot'])
                or not _text(params['ref']) or not _text(params['name'])):
            raise ValueError('explicit worktree name, ref and sourceRoot required')
        import re
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', params['name']):
            raise ValueError('invalid worktree name')
    elif method in (ATTACH_WORKTREE, ARCHIVE_WORKTREE):
        key = 'path' if method == ATTACH_WORKTREE else 'root'
        if set(params) != {key} or not _absolute(params[key]):
            raise ValueError('explicit absolute worktree identity required')
    elif method == WORKTREE_STATUS:
        if set(params) != {'operationId'} or not _text(params['operationId']):
            raise ValueError('actual worktree operationId required')
    elif method == ARCHIVE_THREAD:
        if (set(params) != {'threadId', 'hostId', 'archived'} or params['hostId'] != 'local'
                or params['archived'] is not True or not _text(params['threadId'])
                or params['threadId'].startswith('client-new-thread:')):
            raise ValueError('explicit actual threadId and archived=true required')
    else:
        raise ValueError('unsupported bridge method')


def validate_lifecycle_metadata(method, params, metadata):
    if (set(metadata) != {'hostId', 'result'} or metadata['hostId'] != 'local'
            or not isinstance(metadata['result'], dict) or not metadata['result']):
        raise ValueError('lifecycle raw result and local host required')
    result = metadata['result']
    if method == LIST_ARTIFACTS:
        if not isinstance(result.get('artifacts'), list):
            raise ValueError('artifact inventory requires artifacts array')
    elif method == WAIT:
        if type(result.get('timedOut')) is not bool or not isinstance(result.get('polls'), list):
            raise ValueError('wait result requires timedOut and polls')
        targets = {t['threadId'] for t in params['targets']}
        seen = set()
        for poll in result['polls']:
            if not isinstance(poll, dict):
                raise ValueError('invalid wait poll')
            thread = poll.get('thread', {})
            if (not isinstance(thread, dict) or thread.get('id') not in targets
                    or thread.get('id') in seen or thread.get('hostId') != 'local'
                    or not isinstance(thread.get('status'), dict)
                    or not _text(thread['status'].get('type')) or not _text(poll.get('cursor'))):
                raise ValueError('wait poll thread/cursor mismatch')
            seen.add(thread['id'])
        wake = result.get('wake')
        if wake is not None and (not isinstance(wake, dict)
                                 or wake.get('threadId') not in targets or wake.get('hostId') != 'local'):
            raise ValueError('wait wake identity mismatch')
        # 宿主可能只返回首先完成的目标；没有 poll 的目标不推导任何状态。
    elif method == ARCHIVE_THREAD:
        if result != {'threadId': params['threadId'], 'archived': True}:
            raise ValueError('archive thread receipt mismatch')
    # worktree 结果保持原始对象，不猜测宿主版本特有字段；调用方须另行核验
    # 实际目录/attachment。成功回执不代表创建完成或资源已经删除。


class DesktopLifecycleBridgeAdapter(DesktopWriteBridgeAdapter):
    """扩展读/写桥接；资源写动作需要显式配置或精确 allowlist。"""

    def __init__(self, *args, worktree_config=None, attach_paths=(), archive_roots=(),
                 archive_threads=(), wait_cursors=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.worktree_config = copy.deepcopy(worktree_config)
        self.attach_paths = tuple(attach_paths)
        self.archive_roots = tuple(archive_roots)
        self.archive_threads = tuple(archive_threads)
        self.wait_cursors = copy.deepcopy(wait_cursors or {})
        self.capabilities.update(list_artifacts=True, wait_threads=True, create_worktree=worktree_config is not None,
                                 attach_worktree=bool(attach_paths), archive_worktree=bool(archive_roots),
                                 archive_thread=bool(archive_threads), worktree_creation_status=True)

    def _lifecycle(self, method, params):
        record = self.journal.prepare_lifecycle(self.operation_id, self.role, self.attempt,
                                                self.task_revision, method, params)
        metadata = self._result(record)
        response = record['response']
        return {**record['request']['binding'], 'operationId': self.operation_id,
                'source': response['source'], 'observedAt': response['observedAt'],
                'requestSha': record['requestSha'], 'responseSha': record['responseSha'],
                'toolResultSha': response['toolResultSha'], 'metadata': metadata}

    def list_artifacts(self):
        return self._lifecycle(LIST_ARTIFACTS, {})

    def wait_threads(self, thread_ids, timeout_seconds):
        if type(timeout_seconds) is not int or not 0 <= timeout_seconds <= 60:
            raise ValueError('timeout_seconds must be integer in 0..60')
        if not isinstance(thread_ids, (list, tuple)):
            raise ValueError('thread_ids must be a list or tuple')
        if not isinstance(self.wait_cursors, dict) or set(self.wait_cursors) - set(thread_ids):
            raise ValueError('wait cursors must belong to requested targets')
        targets = []
        for thread_id in thread_ids:
            target = {'threadId': thread_id, 'hostId': 'local'}
            if thread_id in self.wait_cursors:
                target['afterCursor'] = self.wait_cursors[thread_id]
            targets.append(target)
        return self._lifecycle(WAIT, {'targets': targets, 'timeoutMs': timeout_seconds * 1000})

    def create_worktree(self, name):
        if (not isinstance(self.worktree_config, dict)
                or set(self.worktree_config) != {'ref', 'sourceRoot'}):
            raise ValueError('explicit worktree_config ref/sourceRoot required')
        return self._lifecycle(CREATE_WORKTREE, dict(self.worktree_config, name=name, allowAsync=True))

    def attach_worktree(self, path):
        if path not in self.attach_paths:
            raise ValueError('worktree path is not explicitly allowed')
        return self._lifecycle(ATTACH_WORKTREE, {'path': path})

    def worktree_creation_status(self, operation_id):
        return self._lifecycle(WORKTREE_STATUS, {'operationId': operation_id})

    def archive_worktree(self, root):
        if root not in self.archive_roots:
            raise ValueError('worktree identity is not explicitly allowed')
        return self._lifecycle(ARCHIVE_WORKTREE, {'root': root})

    def archive_thread(self, thread_id):
        if thread_id not in self.archive_threads:
            raise ValueError('thread is not explicitly allowed for archive')
        return self._lifecycle(ARCHIVE_THREAD, {'threadId': thread_id, 'hostId': 'local', 'archived': True})


def complete_tool_result(journal, operation_id, raw, reconciliation=None):
    """从实际 MCP 原始返回生成回执；保留原始文件由调用方负责。"""
    record = journal.inspect(operation_id)
    method = record['request']['method']
    if method not in METHODS.values():
        raise ValueError('not a lifecycle operation')
    if record['status'] in ('succeeded', 'failed'):
        if record['response']['toolResultSha'] != digest(raw):
            raise ValueError('terminal tool result cannot be overwritten')
        return record
    if not isinstance(raw, dict) or type(raw.get('isError', False)) is not bool:
        raise ValueError('invalid tool result envelope')
    blocks = raw.get('content', [])
    texts = [b['text'] for b in blocks if isinstance(b, dict) and b.get('type') == 'text']
    if not texts or not all(isinstance(text, str) and text.strip() for text in texts):
        raise ValueError('tool result text required')
    response = {k: record[k] for k in ('operationId', 'runId', 'requestSha', 'claimToken')}
    response.update(method=method, source='desktop_tool_bridge', observedAt=now(),
                    toolResultSha=digest(raw), status='failed' if raw.get('isError') else 'succeeded')
    if raw.get('isError'):
        response['error'] = '\n'.join(texts)[:4000]
    else:
        if len(texts) != 1:
            raise ValueError('one unambiguous JSON tool result required')
        response['metadata'] = {'hostId': 'local', 'result': json.loads(texts[0])}
    return journal.complete(operation_id, response, reconciliation)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Lifecycle bridge journal; never executes host tools')
    parser.add_argument('command', choices=['prepare', 'claim', 'inspect', 'unknown', 'complete'])
    parser.add_argument('--repo', default='.')
    parser.add_argument('--run', required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--role')
    parser.add_argument('--attempt', type=int, default=1)
    parser.add_argument('--task-revision', type=int, default=0)
    parser.add_argument('--action', choices=sorted(METHODS))
    parser.add_argument('--params')
    parser.add_argument('--tool-result')
    parser.add_argument('--reason')
    parser.add_argument('--reconciliation')
    args = parser.parse_args(argv)
    try:
        journal = BridgeJournal(args.repo, args.run)
        if args.command == 'prepare':
            if not args.action or not args.params:
                raise ValueError('explicit action and params file required')
            result = journal.prepare_lifecycle(args.operation, args.role, args.attempt,
                                               args.task_revision, METHODS[args.action], read_state(args.params))
        elif args.command == 'claim':
            result = journal.claim(args.operation)
        elif args.command == 'unknown':
            result = journal.mark_unknown(args.operation, args.reason)
        elif args.command == 'complete':
            if not args.tool_result:
                raise ValueError('raw tool-result file required')
            result = complete_tool_result(journal, args.operation, read_state(args.tool_result), args.reconciliation)
        else:
            result = journal.inspect(args.operation)
    except (ValueError, RuntimeError, OSError, TypeError, KeyError, StateError) as exc:
        parser.exit(2, 'error: ' + str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

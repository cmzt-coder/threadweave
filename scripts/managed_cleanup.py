"""O16 managed worktree 清理门禁；只允许宿主归档，不降级本地删除。"""
from datetime import datetime, timezone
from cleanup import cleanup_plan_fingerprint
from cleanup_apply import preview


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + ' must be an object')
    return value


def _objects(value, label):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(label + ' must be an array of objects')
    return value


def _receipt(value, run_id):
    if (not isinstance(value, dict) or value.get('source') != 'desktop_tool_bridge'
            or value.get('runId') != run_id or not value.get('operationId')):
        raise ValueError('host observation binding missing')
    if (not isinstance(value.get('role'), str) or not value['role']
            or type(value.get('attempt')) is not int or value['attempt'] < 1
            or type(value.get('taskRevision')) is not int or value['taskRevision'] < 0):
        raise ValueError('host observation worker binding missing')
    observed_at = value.get('observedAt')
    if not isinstance(observed_at, str):
        raise ValueError('host observation time missing')
    stamp = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
    if not 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 30:
        raise ValueError('host observation stale or future')
    metadata = _object(value['metadata'], 'host metadata')
    if metadata.get('hostId') != 'local':
        raise ValueError('local host required')
    return _object(metadata['result'], 'host result')


def recheck_managed_cleanup(declaration, approved, stop_receipt, inventory_receipt, owner_thread_id):
    """将即时宿主观察与精确 Git 计划合并；未知归属绝不进入执行。"""
    try:
        if not isinstance(owner_thread_id, str) or not owner_thread_id.strip():
            raise ValueError('explicit owner thread required')
        stop = _receipt(stop_receipt, declaration['runId'])
        inventory = _receipt(inventory_receipt, declaration['runId'])
        if any(stop_receipt.get(k) != inventory_receipt.get(k) for k in ('role','attempt','taskRevision')):
            raise ValueError('host observation worker binding mismatch')
        polls = [p for p in _objects(stop['polls'], 'stop polls')
                 if _object(p.get('thread'), 'poll thread').get('id') == owner_thread_id]
        if len(polls) != 1:
            raise ValueError('unique exact stop poll required')
        poll = polls[0]
        turn = _object(poll.get('latestTurn'), 'latest turn')
        final = _object(poll.get('latestAssistantMessage'), 'final message')
        status = _object(poll['thread'].get('status'), 'thread status')
        if (poll['thread'].get('hostId') != 'local' or status.get('type') != 'idle'
                or turn.get('status') != 'completed' or not turn.get('id')
                or final.get('turnId') != turn['id'] or final.get('phase') != 'final_answer'):
            raise ValueError('worker not freshly confirmed stopped')
        matches = [a for a in _objects(inventory['artifacts'], 'artifacts') if a.get('type') == 'worktree'
                   and a.get('identityKey') == declaration['worktree']]
        if len(matches) != 1:
            raise ValueError('exact attached worktree identity missing or ambiguous')
        payload = _object(matches[0].get('payload'), 'worktree ownership payload')
        # 只有附件路径不足以证明归属。宿主没有暴露字段时明确拒绝。
        if (payload.get('ownerThreadId') != owner_thread_id
                or payload.get('isPrimary') is not False or payload.get('isShared') is not False
                or payload.get('runId') != declaration['runId']):
            raise ValueError('host ownership/primary/shared observation unavailable or unsafe')
        current = preview(declaration)
        # 必须绑定 preview 实际读取并哈希的证据；本地进程或其他聊天的声明不能混用。
        if (current['plan'].get('executionKind') != 'host_thread'
                or current['plan'].get('threadId') != owner_thread_id):
            raise ValueError('cleanup evidence execution kind or thread identity mismatch')
        if (not isinstance(approved, dict) or not isinstance(approved.get('plan'), dict)
                or approved.get('fingerprint') != cleanup_plan_fingerprint(approved['plan'])
                or current['fingerprint'] != approved['fingerprint']):
            raise ValueError('Git cleanup plan drift')
        return {'status':'eligible','deletionPerformed':False,'readOnly':True,
                'fingerprint':current['fingerprint'],'worktree':declaration['worktree']}
    except (ValueError, KeyError, TypeError, OSError) as error:
        return {'status':'blocked','deletionPerformed':False,'readOnly':True,'reason':str(error)}


def execute_managed_cleanup(adapter, declaration, approved, stop_receipt, inventory_receipt,
                            owner_thread_id, *, authorized=False):
    if authorized is not True:
        raise ValueError('explicit managed cleanup authorization required')
    check = recheck_managed_cleanup(declaration, approved, stop_receipt, inventory_receipt, owner_thread_id)
    if check['status'] != 'eligible':
        return check
    # 宿主的最终所有权校验仍是权威；失败/未决不尝试 git worktree remove。
    result = adapter.archive_worktree(declaration['worktree'])
    return {'status':'host_result','deletionPerformed':None,'hostArchiveResult':result,
            'requiresHostCompletionVerification':True}

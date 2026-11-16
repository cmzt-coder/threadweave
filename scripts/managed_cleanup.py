"""O16 managed worktree 清理门禁；只允许宿主归档，不降级本地删除。"""
from datetime import datetime, timezone
from cleanup import cleanup_plan_fingerprint
from cleanup_apply import preview


def _receipt(value, run_id):
    if (not isinstance(value, dict) or value.get('source') != 'desktop_tool_bridge'
            or value.get('runId') != run_id or not value.get('operationId')):
        raise ValueError('host observation binding missing')
    if (not isinstance(value.get('role'), str) or not value['role']
            or type(value.get('attempt')) is not int or value['attempt'] < 1
            or type(value.get('taskRevision')) is not int or value['taskRevision'] < 0):
        raise ValueError('host observation worker binding missing')
    stamp = datetime.fromisoformat(value['observedAt'].replace('Z', '+00:00'))
    if not 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 30:
        raise ValueError('host observation stale or future')
    metadata = value['metadata']
    if metadata.get('hostId') != 'local':
        raise ValueError('local host required')
    return metadata['result']


def recheck_managed_cleanup(declaration, approved, stop_receipt, inventory_receipt, owner_thread_id):
    """将即时宿主观察与精确 Git 计划合并；未知归属绝不进入执行。"""
    try:
        if not owner_thread_id:
            raise ValueError('explicit owner thread required')
        stop = _receipt(stop_receipt, declaration['runId'])
        inventory = _receipt(inventory_receipt, declaration['runId'])
        if any(stop_receipt.get(k) != inventory_receipt.get(k) for k in ('role','attempt','taskRevision')):
            raise ValueError('host observation worker binding mismatch')
        polls = [p for p in stop['polls'] if p['thread']['id'] == owner_thread_id]
        if len(polls) != 1:
            raise ValueError('unique exact stop poll required')
        poll = polls[0]; turn = poll.get('latestTurn', {}); final = poll.get('latestAssistantMessage', {})
        if (poll['thread'].get('hostId') != 'local' or poll['thread']['status'].get('type') != 'idle'
                or turn.get('status') != 'completed' or not turn.get('id')
                or final.get('turnId') != turn['id'] or final.get('phase') != 'final_answer'):
            raise ValueError('worker not freshly confirmed stopped')
        matches = [a for a in inventory['artifacts'] if a.get('type') == 'worktree'
                   and a.get('identityKey') == declaration['worktree']]
        if len(matches) != 1:
            raise ValueError('exact attached worktree identity missing or ambiguous')
        payload = matches[0].get('payload', {})
        # 只有附件路径不足以证明归属。宿主没有暴露字段时明确拒绝。
        if (payload.get('ownerThreadId') != owner_thread_id
                or payload.get('isPrimary') is not False or payload.get('isShared') is not False
                or payload.get('runId') != declaration['runId']):
            raise ValueError('host ownership/primary/shared observation unavailable or unsafe')
        current = preview(declaration)
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

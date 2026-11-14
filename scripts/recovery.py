"""Threadweave 中断恢复分类参考实现。

根据持久状态与外部观察（thread / worktree / branch / HEAD / 干净状态）给出恢复动作，
用于避免重复创建和重复派发。纯函数，不调用宿主 API，也不修改仓库。
"""
import hashlib
import json
import re
from datetime import datetime, timezone

from failure import latest_unresolved_failure


TERMINAL_STATES = {'integrated', 'archived', 'failed', 'cancelled'}
THREAD_PRESENT = 'present'
THREAD_UNKNOWN = 'unknown'
THREAD_MISSING_UNCONFIRMED = 'missing_unconfirmed'
THREAD_MISSING_CONFIRMED = 'missing_confirmed'
SHA_RE = re.compile(r'^[0-9a-f]{40}$')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def classify_thread_presence(observed):
    """区分存在、未知、未确认缺失与有证据的确认缺失。"""
    exists = observed.get('thread_exists') if isinstance(observed, dict) else None
    if exists is None:
        return THREAD_UNKNOWN
    if exists is True:
        return THREAD_PRESENT
    if exists is False and observed.get('thread_absence_confirmed') is True:
        return THREAD_MISSING_CONFIRMED
    return THREAD_MISSING_UNCONFIRMED


def preservation_gaps(worker, observed):
    """列出确认失联后仍需保全的旧 attempt 成果。"""
    gaps = []
    presence = classify_thread_presence(observed)
    if presence != THREAD_MISSING_CONFIRMED:
        gaps.append('thread_absence_confirmed')
    if not isinstance(observed.get('thread_absence_evidence'), str) or not observed.get('thread_absence_evidence').strip():
        gaps.append('thread_absence_evidence')

    branch = observed.get('branch')
    head = observed.get('head')
    if not isinstance(branch, str) or not branch.strip():
        gaps.append('branch')
    if not isinstance(head, str) or not SHA_RE.fullmatch(head):
        gaps.append('head')
    if observed.get('branch_preserved') is not True:
        gaps.append('branch_preserved')

    worktree_exists = observed.get('worktree_exists')
    if worktree_exists is None:
        gaps.append('worktree_exists')
    elif worktree_exists:
        for field in ('status_porcelain', 'unmerged_commits'):
            if field not in observed or observed.get(field) is None:
                gaps.append(field)
        if observed.get('status_porcelain') and observed.get('dirty_patch_archived') is not True:
            gaps.append('dirty_patch_archived')
        if observed.get('dirty_patch_archived') is True and not (
                isinstance(observed.get('dirty_patch_sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', observed['dirty_patch_sha256'])):
            gaps.append('dirty_patch_sha256')

    for prefix in ('report', 'notes'):
        present = observed.get(f'{prefix}_present')
        if present is None:
            gaps.append(f'{prefix}_present')
        elif present and observed.get(f'{prefix}_archived') is not True:
            gaps.append(f'{prefix}_archived')
    return list(dict.fromkeys(gaps))


def _preservation_matches(worker, observed, record):
    if not isinstance(record, dict):
        return False
    return (
        record.get('oldAttempt') == worker.get('attempt', 1)
        and record.get('branch') == observed.get('branch')
        and record.get('head') == observed.get('head')
        and record.get('threadAbsenceConfirmed') is True
        and isinstance(record.get('preservationSha'), str)
    )


def build_preservation_record(worker, observed):
    """构造旧 attempt 成果保全记录；证据不足时拒绝。"""
    gaps = preservation_gaps(worker, observed)
    if gaps:
        raise ValueError('preservation evidence incomplete: ' + ', '.join(gaps))
    record = {
        'oldAttempt': worker.get('attempt', 1),
        'threadId': worker.get('threadId'),
        'threadAbsenceConfirmed': True,
        'threadAbsenceEvidence': observed['thread_absence_evidence'].strip(),
        'branch': observed['branch'],
        'head': observed['head'],
        'worktree': worker.get('worktree'),
        'worktreeExists': observed.get('worktree_exists'),
        'statusPorcelain': observed.get('status_porcelain', ''),
        'unmergedCommits': bool(observed.get('unmerged_commits')),
        'branchPreserved': True,
        'reportPresent': bool(observed.get('report_present')),
        'reportArchived': bool(observed.get('report_archived')),
        'notesPresent': bool(observed.get('notes_present')),
        'notesArchived': bool(observed.get('notes_archived')),
        'dirtyPatchArchived': bool(observed.get('dirty_patch_archived')),
        'dirtyPatchSha256': observed.get('dirty_patch_sha256'),
        'preservedAt': _now(),
    }
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    record['preservationSha'] = hashlib.sha256(canonical.encode()).hexdigest()
    return record


def verify_preservation_record(record):
    """校验保全记录哈希与最小身份字段。"""
    if not isinstance(record, dict):
        return False
    sha = record.get('preservationSha')
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
        return False
    payload = dict(record)
    payload.pop('preservationSha', None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest() == sha


def classify_worker_recovery(worker, observed, replacement_limit=1, failure=None):
    """返回 skip/recreate/preserve_then_recreate/preserve_work/block/revalidate/reuse。"""
    state = worker.get('state')
    if state in TERMINAL_STATES:
        return 'skip'

    # 未决或来源未确认的失败必须先核对，不能凭“线程不存在”直接重建。
    if (isinstance(failure, dict)
            and (failure.get('resultCertainty') == 'unknown'
                 or failure.get('recommendation') == 'reconcile_result')):
        return 'revalidate'

    presence = classify_thread_presence(observed)
    if presence in (THREAD_UNKNOWN, THREAD_MISSING_UNCONFIRMED):
        return 'revalidate'

    # 已记录派发意图但结果未确认：先核对，不盲目重发或重建
    intent = worker.get('dispatchIntent', {})
    if intent.get('result') in ('pending', 'unknown'):
        return 'revalidate'

    if presence == THREAD_MISSING_CONFIRMED:
        if worker.get('attempt', 1) > replacement_limit:
            return 'block'
        record = worker.get('recoveryPreservation')
        if _preservation_matches(worker, observed, record):
            return 'recreate'
        gaps = preservation_gaps(worker, observed)
        if not gaps:
            return 'preserve_then_recreate'
        if observed.get('worktree_exists') and (
                observed.get('status_porcelain')
                or observed.get('unmerged_commits')
                or any(g in gaps for g in ('dirty_patch_archived', 'report_archived', 'notes_archived'))):
            return 'preserve_work'
        return 'revalidate'

    # pending 有 clientThreadId 但无真实 threadId：先解析，不 recreation
    if state == 'pending' and not worker.get('threadId'):
        return 'revalidate'

    if not observed.get('thread_status'):
        return 'revalidate'
    if observed.get('thread_status') != 'idle':
        return 'revalidate'
    if not observed.get('worktree_exists'):
        return 'revalidate'
    if observed.get('branch') != worker.get('branch'):
        return 'block'

    head = observed.get('head')
    base = worker.get('baseSha')
    if state in ('running', 'blocked', 'reported'):
        if not observed.get('base_is_ancestor', False):
            return 'block'
        if state == 'reported':
            report_sha = observed.get('report_sha')
            if not report_sha or report_sha != head:
                return 'block'
    else:
        if head != base:
            return 'block'

    if observed.get('status_porcelain'):
        return 'block'
    return 'reuse'


def build_recovery_plan(state, observations, replacement_limit=1):
    """为每个 worker 生成恢复动作，并标记重复派发风险与成果保全证据。"""
    plan = {}
    for worker in state.get('workers', []):
        key = f"{worker.get('role')}#{worker.get('attempt')}"
        observed = observations.get(key, {})
        failure = latest_unresolved_failure(
            state, role=worker.get('role'), attempt=worker.get('attempt', 1))
        action = classify_worker_recovery(
            worker, observed, replacement_limit, failure=failure)
        plan[key] = {
            'action': action,
            'duplicate_dispatch_risk': action == 'reuse' and worker.get('state') == 'dispatched',
            'threadPresence': classify_thread_presence(observed),
            'preservationGaps': preservation_gaps(worker, observed),
        }
        if failure:
            plan[key]['failure'] = failure
    return plan

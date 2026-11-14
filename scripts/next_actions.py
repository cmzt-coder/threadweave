"""O04 只读恢复建议。

组合 recovery / cleanup 的既有分类结果，生成 leader 可直接审阅的有序动作建议。
本模块不调用宿主 API、不写状态、不创建线程，也不执行任何清理。
"""
from datetime import datetime, timezone

from cleanup import build_cleanup_plan
from failure import failure_summary, latest_unresolved_failure, unresolved_failures
from recovery import build_recovery_plan


UNRESOLVED_DISPATCH_RESULTS = {'pending', 'unknown'}
HALTED_RUN_STATUSES = {'complete', 'failed', 'aborted', 'cleanup_pending', 'cancelled'}
CLEANUP_DUE_RUN_STATUSES = {'complete', 'failed', 'aborted', 'cleanup_pending'}

PRIORITY_DISPATCH = 10
PRIORITY_INTEGRATION_BLOCKED = 15
PRIORITY_MISSING_EVIDENCE = 20
PRIORITY_PRESERVE = 25
PRIORITY_BLOCK = 30
PRIORITY_RECREATE = 40
PRIORITY_REUSE = 50
PRIORITY_RUN_CLEANUP = 60
PRIORITY_CLEANUP = 70


def _worker_key(worker):
    return f"{worker.get('role')}#{worker.get('attempt', 1)}"


def _recovery_branch_name(observed):
    """兼容 recovery 的字符串 branch 与 cleanup 的 branch 对象。"""
    branch = observed.get('branch')
    if isinstance(branch, str):
        return branch
    if isinstance(branch, dict):
        return branch.get('name') or branch.get('branch')
    return None


def _recovery_observation(observed):
    normalized = dict(observed)
    if isinstance(normalized.get('branch'), dict):
        normalized['branch'] = _recovery_branch_name(normalized)
    return normalized


def _cleanup_branch_observation(observed):
    branch = observed.get('branch')
    if isinstance(branch, dict):
        return branch
    for key in ('branchCleanup', 'branchObservation'):
        value = observed.get(key)
        if isinstance(value, dict):
            return value
    # 字符串 branch 只能证明分支名，不能证明归属、合并和 checkout 状态。
    return {'name': branch} if isinstance(branch, str) else {}


def _cleanup_observation(observed):
    normalized = dict(observed)
    normalized['branch'] = _cleanup_branch_observation(observed)
    worktree = observed.get('worktree')
    normalized['worktree'] = worktree if isinstance(worktree, dict) else {}
    stop = observed.get('stop')
    normalized['stop'] = stop if isinstance(stop, dict) else {}
    return normalized


def _unique(items):
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def _integration_block(state):
    """读取 O01 的 integrationBlocked；兼容布尔值与对象两种持久化形态。"""
    value = state.get('integrationBlocked')
    if not value:
        return None
    if isinstance(value, dict):
        return dict(value)
    return {'blocked': True, 'value': value}


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _freshness(observed, state):
    """返回缺失/无效时间证据、是否陈旧与观察年龄。"""
    issues = []
    stale = False
    observed_at = observed.get('observedAt')
    parsed = _parse_time(observed_at)
    if observed_at is not None and parsed is None:
        issues.append('observedAt(valid ISO8601)')
    if parsed is None:
        age = None
    else:
        age = int((datetime.now(timezone.utc) - parsed).total_seconds())
        if age < 0:
            stale = True

    max_age = observed.get('maxAgeSeconds')
    if max_age is None:
        limits = state.get('limits')
        if isinstance(limits, dict):
            max_age = limits.get('observationMaxAgeSeconds')
    if max_age is not None:
        if isinstance(max_age, bool) or not isinstance(max_age, (int, float)) or max_age < 0:
            issues.append('maxAgeSeconds(positive number)')
            stale = True
        elif age is None:
            issues.append('observedAt(required when maxAgeSeconds is set)')
        elif age > max_age:
            stale = True
    return issues, stale, age


def _recovery_missing(worker, observed, state):
    """列出判断 reuse/recreate 前必须具备的外部证据。"""
    issues, stale, _ = _freshness(observed, state)
    issues = list(issues)

    if observed.get('thread_exists') is None:
        issues.append('thread_exists')
    if observed.get('thread_exists') is False:
        if observed.get('thread_absence_confirmed') is not True:
            issues.append('thread_absence_confirmed')
        if not isinstance(observed.get('thread_absence_evidence'), str) or not observed.get('thread_absence_evidence').strip():
            issues.append('thread_absence_evidence')
    if observed.get('thread_exists') is True and observed.get('thread_status') is None:
        issues.append('thread_status')

    worktree_exists = observed.get('worktree_exists')
    if worktree_exists is None:
        issues.append('worktree_exists')
    elif worktree_exists:
        if not _recovery_branch_name(observed):
            issues.append('branch')

        for field in ('head', 'status_porcelain'):
            if field not in observed or observed.get(field) is None:
                issues.append(field)

    worker_state = worker.get('state')
    if worker_state in ('running', 'blocked', 'reported'):
        if observed.get('base_is_ancestor') is None:
            issues.append('base_is_ancestor')
    if worker_state == 'reported' and not observed.get('report_sha'):
        issues.append('report_sha')
    return _unique(issues), stale


def _recreate_missing(observed):
    """recreate 前额外要求有“是否存在未合并成果”的证据。"""
    if not observed.get('worktree_exists'):
        return []
    worktree = observed.get('worktree')
    if not isinstance(worktree, dict) or 'unmerged_commits' not in worktree:
        return ['worktree.unmerged_commits']
    return []


def _cleanup_missing(observed, state):
    """列出清理分类前的完整证据；缺一项都只建议重新核对。"""
    issues, stale, _ = _freshness(observed, state)
    issues = list(issues)

    worktree = observed.get('worktree')
    if not isinstance(worktree, dict):
        issues.append('worktree')
    else:
        if worktree.get('worktree_exists') is None:
            issues.append('worktree.worktree_exists')
        elif worktree.get('worktree_exists'):
            for field in (
                'is_shared', 'is_primary', 'owned_by_run', 'status_porcelain',
                'unmerged_commits', 'worker_status',
            ):
                if field not in worktree or worktree.get(field) is None:
                    issues.append(f'worktree.{field}')

    if 'report_archived' not in observed:
        issues.append('report_archived')

    branch = _cleanup_branch_observation(observed)
    if not branch:
        issues.append('branch')
    else:
        if branch.get('branch_exists') is None:
            issues.append('branch.branch_exists')
        elif branch.get('branch_exists'):
            for field in ('owned_by_run', 'checked_out', 'merged_into_integration'):
                if field not in branch or branch.get(field) is None:
                    issues.append(f'branch.{field}')

    stop = observed.get('stop')
    if not isinstance(stop, dict):
        issues.append('stop')
    else:
        for field in ('thread_idle', 'final_answer_present', 'no_active_turn'):
            if field not in stop or stop.get(field) is None:
                issues.append(f'stop.{field}')
    return _unique(issues), stale


def _make_action(action, worker, reason, observed, missing=None, prerequisites=None,
                 priority=PRIORITY_MISSING_EVIDENCE, **extra):
    result = {
        'action': action,
        'worker': worker,
        'reason': reason,
        'observedAt': observed.get('observedAt') if isinstance(observed, dict) else None,
        'missingEvidence': list(missing or []),
        'prerequisites': list(prerequisites or []),
        'priority': priority,
    }
    result.update(extra)
    return result


def _integration_blocked_action(block):
    details = {}
    for key in ('worker', 'role', 'attempt', 'reason', 'message', 'at', 'observedAt',
                'verification', 'planHash', 'head'):
        if key in block:
            details[key] = block[key]
    observed_at = block.get('at') or block.get('observedAt')
    reason = (
        'integrationBlocked is active; worker reuse/recreate/send is globally blocked. '
        'The leader must explicitly rerun recover_integration verification at the current integration HEAD.'
    )
    detail = block.get('reason') or block.get('message')
    if detail:
        reason += f' Blocking evidence: {detail}'
    return _make_action(
        'recover_integration', 'run#1', reason, {'observedAt': observed_at},
        prerequisites=[
            'leader explicitly authorizes recover_integration',
            'current integration HEAD is stable',
            'fresh integration verification plan passes',
        ],
        priority=PRIORITY_INTEGRATION_BLOCKED,
        requiredAction='recover_integration',
        recoveryExecutionBlocked=True,
        integrationBlocked=details or True,
    )


def _blocked_recovery_action(worker, observed, recovery_action, run_status,
                             integration_blocked):
    key = _worker_key(worker)
    if run_status in HALTED_RUN_STATUSES:
        reason = (
            f'run is {run_status}; recovery execution is globally blocked. '
            'Verify thread stop and cleanup evidence only.'
        )
        prerequisites = ['verify thread idle/stopped', 'complete cleanup evidence']
    else:
        reason = (
            'integrationBlocked is active; worker reuse/recreate/send is blocked '
            'until leader recover_integration verification passes.'
        )
        prerequisites = ['leader recover_integration decision', 'fresh integration verification']
    return _make_action(
        'revalidate', key, reason, observed,
        missing=recovery_action.get('missingEvidence', []),
        prerequisites=prerequisites,
        priority=PRIORITY_MISSING_EVIDENCE,
        recoveryExecutionBlocked=True,
        blockedAction=recovery_action.get('action'),
        runStatus=run_status,
        integrationBlocked=bool(integration_blocked),
    )


def _mismatch_reason(worker, observed):
    if observed.get('thread_status') not in (None, 'idle'):
        return f"thread status is {observed.get('thread_status')!r}; verify completion or stop it explicitly"
    if observed.get('worktree_exists') is False:
        return 'registered worktree is missing; verify ownership and preserved commits'
    if _recovery_branch_name(observed) != worker.get('branch'):
        return 'observed branch does not match the worker branch'
    if observed.get('status_porcelain'):
        return 'worktree has uncommitted changes; preserve and inspect them before any recovery'
    if worker.get('state') in ('running', 'blocked', 'reported') \
            and observed.get('base_is_ancestor') is False:
        return 'worker base is not an ancestor of the observed HEAD'
    if worker.get('state') == 'reported' \
            and observed.get('report_sha') != observed.get('head'):
        return 'reported SHA does not match the observed HEAD'
    if observed.get('head') is not None and observed.get('head') != worker.get('baseSha'):
        return 'observed HEAD differs from the expected base'
    return 'recovery classifier found an evidence mismatch; inspect before acting'


def _recovery_action(worker, observed, recovery, state):
    key = _worker_key(worker)
    if isinstance(recovery, dict):
        recovery_result = recovery
        recovery = recovery_result.get('action')
    else:
        recovery_result = {'action': recovery}

    # 终态 worker 不再参与执行恢复；清理债务由 cleanup 分支单独处理。
    if recovery == 'skip':
        return None

    missing, stale = _recovery_missing(worker, observed, state)
    intent = worker.get('dispatchIntent') or {}
    intent_result = intent.get('result') if isinstance(intent, dict) else None

    if intent_result in UNRESOLVED_DISPATCH_RESULTS:
        return _make_action(
            'revalidate', key,
            'dispatch intent is pending/unknown; reconcile the dispatch outcome before reuse, recreate, or send',
            observed,
            missing=missing,
            prerequisites=['resolve dispatchIntent.result from host evidence'],
            priority=PRIORITY_DISPATCH,
            dispatchResult=intent_result,
        )

    if recovery == 'preserve_work':
        gaps = recovery_result.get('preservationGaps', [])
        return _make_action(
            'preserve_work', key,
            'thread absence is confirmed but old attempt artifacts are not yet safely preserved',
            observed,
            missing=gaps,
            prerequisites=[
                'archive dirty worktree changes with SHA256 evidence',
                'preserve the worker branch/HEAD and existing reports/notes',
                'do not delete or recreate until preservation evidence is complete',
            ],
            priority=PRIORITY_PRESERVE,
            threadPresence=recovery_result.get('threadPresence'),
            preservationGaps=gaps,
        )

    if recovery == 'preserve_then_recreate':
        gaps = recovery_result.get('preservationGaps', [])
        if gaps:
            return _make_action(
                'revalidate', key,
                f"cannot record preservation; missing evidence: {', '.join(gaps)}",
                observed,
                missing=gaps,
                prerequisites=['collect preservation evidence'],
                priority=PRIORITY_MISSING_EVIDENCE,
                threadPresence=recovery_result.get('threadPresence'),
                preservationGaps=gaps,
            )
        return _make_action(
            'preserve_then_recreate', key,
            'thread absence is confirmed and old attempt artifacts have preservation evidence',
            observed,
            prerequisites=[
                'record recovery preservation through the controller',
                'confirm replacement budget and no later attempt',
                'create a new attempt without deleting old branch/report evidence',
            ],
            priority=PRIORITY_RECREATE,
            threadPresence=recovery_result.get('threadPresence'),
            preservationGaps=[],
        )

    # block 本身不会创建或发送资源；保留分类器的阻断结论，并附上缺失证据。
    if recovery == 'block':
        return _make_action(
            'block', key, _mismatch_reason(worker, observed), observed,
            missing=missing,
            prerequisites=['resolve the evidence mismatch; do not delete or replace automatically'],
            priority=PRIORITY_BLOCK,
            stale=stale,
        )

    if missing or stale:
        if stale:
            reason = 'external observations are stale or invalid; collect fresh host evidence first'
        else:
            reason = f"external observations are incomplete: {', '.join(missing)}"
        return _make_action(
            'revalidate', key, reason, observed,
            missing=missing,
            prerequisites=['collect fresh thread/worktree/branch evidence'],
            priority=PRIORITY_MISSING_EVIDENCE,
            stale=stale,
        )

    if recovery == 'revalidate':
        return _make_action(
            'revalidate', key,
            'recovery classifier requires revalidation before any follow-up action',
            observed,
            prerequisites=['confirm host observation and worker identity'],
            priority=PRIORITY_MISSING_EVIDENCE,
        )

    if recovery == 'recreate':
        if observed.get('worktree_exists'):
            worktree = observed.get('worktree') or {}
            if worktree.get('unmerged_commits'):
                return _make_action(
                    'block', key,
                    'thread is absent but the worktree has unmerged commits; preserve and inspect the existing work',
                    observed,
                    prerequisites=['preserve old attempt and branch before replacement'],
                    priority=PRIORITY_BLOCK,
                )
            if observed.get('status_porcelain'):
                return _make_action(
                    'block', key,
                    'thread is absent but the worktree is dirty; preserve uncommitted work before replacement',
                    observed,
                    prerequisites=['inspect and preserve uncommitted changes'],
                    priority=PRIORITY_BLOCK,
                )
        recreate_missing = _recreate_missing(observed)
        if recreate_missing:
            return _make_action(
                'revalidate', key,
                f"cannot safely propose recreate; missing evidence: {', '.join(recreate_missing)}",
                observed,
                missing=recreate_missing,
                prerequisites=['collect preserved-work and merge evidence'],
                priority=PRIORITY_MISSING_EVIDENCE,
            )
        return _make_action(
            'recreate', key,
            'thread is confirmed absent and observed worktree evidence is safe for a replacement attempt',
            observed,
            prerequisites=['preserve old attempt audit', 'confirm replacement budget'],
            priority=PRIORITY_RECREATE,
        )

    if recovery == 'reuse':
        return _make_action(
            'reuse', key,
            'thread and worktree evidence match; leader may reuse the existing thread '
            'without resending a duplicate dispatch',
            observed,
            prerequisites=['leader decision before any reuse', 'do not resend an already-sent dispatch'],
            priority=PRIORITY_REUSE,
            duplicateDispatchRisk=bool(recovery_result.get('duplicate_dispatch_risk')),
        )

    return _make_action(
        'block', key,
        f'unknown recovery action {recovery!r}; stop and inspect',
        observed,
        prerequisites=['leader decision required'],
        priority=PRIORITY_BLOCK,
    )


def _cleanup_reason(kind, cleanup_action):
    if kind == 'worktree':
        if cleanup_action == 'protect_dirty':
            return 'worktree is dirty; preserve uncommitted work before cleanup'
        if cleanup_action == 'protect_unmerged':
            return 'worktree has unmerged commits; preserve the branch before cleanup'
        if cleanup_action == 'protect_report':
            return 'report is not archived and verified; preserve it before cleanup'
        if cleanup_action == 'protect_active':
            return 'worker is still active; cleanup is blocked'
        if cleanup_action == 'protect_shared':
            return 'worktree is shared; cleanup is blocked'
        if cleanup_action == 'protect_not_owned':
            return 'worktree is not owned by this run; cleanup is blocked'
    if kind == 'branch':
        if cleanup_action == 'protect_unmerged':
            return 'branch is not merged into the integration branch; preserve it'
        if cleanup_action == 'protect_checked_out':
            return 'branch is checked out; cleanup is blocked'
        if cleanup_action == 'protect_not_owned':
            return 'branch is not owned by this run; cleanup is blocked'
    if cleanup_action in ('protect_unknown', 'protect_stale'):
        return f'{kind} cleanup evidence is missing or stale; re-collect it'
    return f'{kind} cleanup classification is {cleanup_action!r}; inspect before cleanup'


def _cleanup_actions(worker, observed, cleanup, state):
    key = _worker_key(worker)
    missing, stale = _cleanup_missing(observed, state)
    if missing or stale:
        reason = 'cleanup evidence is stale or incomplete'
        if missing:
            reason += f": {', '.join(missing)}"
        return [_make_action(
            'revalidate', key, reason, observed,
            missing=missing,
            prerequisites=['collect fresh cleanup evidence'],
            priority=PRIORITY_MISSING_EVIDENCE,
            stale=stale,
            cleanupKind='evidence',
        )]

    stop = cleanup.get('stop_evidence')
    if stop != 'stopped':
        return [_make_action(
            'revalidate', key,
            'stop evidence is not verified; confirm the thread is idle, has a final answer, and has no active turn',
            observed,
            missing=[],
            prerequisites=['thread_idle', 'final_answer_present', 'no_active_turn'],
            priority=PRIORITY_MISSING_EVIDENCE,
            cleanupKind='stop_evidence',
        )]

    actions = []
    worktree_action = cleanup.get('worktree')
    if worktree_action == 'remove':
        actions.append(_make_action(
            'remove_worktree', key,
            'cleanup classifier reports worktree removal is eligible, but this read-only '
            'suggestion requires leader confirmation before any cleanup action',
            observed,
            prerequisites=['preserve/verify report', 'confirm branch preservation or merge',
                           'leader confirms worktree cleanup authorization'],
            priority=PRIORITY_CLEANUP,
            cleanupKind='worktree',
            cleanupAction=worktree_action,
            authorizationRequired=True,
        ))
    elif worktree_action not in (None, 'already_removed'):
        if worktree_action in ('protect_unknown', 'protect_stale'):
            action = 'revalidate'
            priority = PRIORITY_MISSING_EVIDENCE
        else:
            action = 'block'
            priority = PRIORITY_BLOCK
        actions.append(_make_action(
            action, key, _cleanup_reason('worktree', worktree_action), observed,
            prerequisites=['resolve cleanup protection before deletion'],
            priority=priority,
            cleanupKind='worktree',
            cleanupAction=worktree_action,
        ))

    branch_action = cleanup.get('branch')
    if branch_action == 'delete':
        actions.append(_make_action(
            'review_branch_retention', key,
            'cleanup classifier reports branch delete-eligible, but branch audit retention '
            'requires manual review; this suggestion does not authorize deletion',
            observed,
            prerequisites=['manual branch retention review', 'leader explicit approval',
                           'confirm archive/audit policy'],
            priority=PRIORITY_CLEANUP,
            cleanupKind='branch',
            cleanupAction=branch_action,
            authorizationRequired=True,
        ))
    elif branch_action not in (None, 'already_removed'):
        if branch_action in ('protect_unknown', 'protect_stale'):
            action = 'revalidate'
            priority = PRIORITY_MISSING_EVIDENCE
        else:
            action = 'block'
            priority = PRIORITY_BLOCK
        actions.append(_make_action(
            action, key, _cleanup_reason('branch', branch_action), observed,
            prerequisites=['resolve cleanup protection before deletion'],
            priority=priority,
            cleanupKind='branch',
            cleanupAction=branch_action,
        ))
    return actions


def build_next_actions(state, observations=None):
    """返回只读、可审计的下一步建议；不执行建议中的动作。"""
    if not isinstance(state, dict):
        raise ValueError('state must be a JSON object')
    if observations is None:
        observations = {}
    if not isinstance(observations, dict):
        raise ValueError('observations must be a JSON object keyed by role#attempt')

    workers = state.get('workers', [])
    if not isinstance(workers, list):
        raise ValueError('state.workers must be a list')
    for worker in workers:
        if not isinstance(worker, dict):
            raise ValueError('state.workers entries must be objects')

    for key, observed in observations.items():
        if not isinstance(observed, dict):
            raise ValueError(f'observations[{key!r}] must be an object')

    cleanup = state.get('cleanup') if isinstance(state.get('cleanup'), dict) else {}
    run_status = state.get('status')
    cleanup_status = cleanup.get('status')
    plan_state = dict(state)
    plan_state['workers'] = []
    for worker in workers:
        normalized_worker = dict(worker)
        if normalized_worker.get('attempt') is None:
            normalized_worker['attempt'] = 1
        if (
            'dispatchIntent' in normalized_worker
            and not isinstance(normalized_worker['dispatchIntent'], dict)
        ):
            normalized_worker.pop('dispatchIntent')
        plan_state['workers'].append(normalized_worker)

    recovery_observations = {
        key: _recovery_observation(observed)
        for key, observed in observations.items()
    }
    cleanup_observations = {
        key: _cleanup_observation(observed)
        for key, observed in observations.items()
    }
    limits = state.get('limits')
    replacement_limit = (
        limits.get('replacementPerRole', 1)
        if isinstance(limits, dict) else 1
    )
    if (
        isinstance(replacement_limit, bool)
        or not isinstance(replacement_limit, int)
        or replacement_limit < 0
    ):
        replacement_limit = 1
    recovery_plan = build_recovery_plan(
        plan_state, recovery_observations, replacement_limit=replacement_limit)
    cleanup_plan = build_cleanup_plan(plan_state, cleanup_observations)

    integration_block = _integration_block(state)
    if run_status == 'complete' and integration_block:
        raise ValueError('complete run has unresolved integrationBlocked')
    if run_status == 'complete' and cleanup_status == 'complete':
        return {
            'runId': state.get('runId'),
            'runStatus': run_status,
            'cleanupStatus': cleanup_status,
            'readOnly': True,
            'unresolvedFailures': [
                failure_summary(item) for item in unresolved_failures(state)
            ],
            'actions': [],
        }

    cleanup_due = (
        run_status in CLEANUP_DUE_RUN_STATUSES
        or cleanup_status in ('partial', 'failed')
    ) and cleanup_status != 'complete'

    integration_block = _integration_block(state)
    run_recovery_blocked = run_status in HALTED_RUN_STATUSES
    execution_blocked = bool(integration_block) or run_recovery_blocked

    actions = []
    if integration_block and not run_recovery_blocked:
        actions.append(_integration_blocked_action(integration_block))

    for worker in workers:
        key = _worker_key(worker)
        observed = observations.get(key, {})
        recovery = recovery_plan.get(key, {'action': 'revalidate'})
        action = _recovery_action(worker, observed, recovery, state)
        failure = latest_unresolved_failure(
            state, role=worker.get('role'), attempt=worker.get('attempt', 1))
        if action is not None and failure:
            action['failure'] = failure_summary(failure)
        if action is not None:
            action.setdefault('threadPresence', recovery.get('threadPresence'))
            action.setdefault('preservationGaps', recovery.get('preservationGaps', []))
        if action is not None and execution_blocked:
            if action.get('action') in ('reuse', 'recreate'):
                action = _blocked_recovery_action(
                    worker, observed, action, run_status, integration_block)
            else:
                action['recoveryExecutionBlocked'] = True
                action['runStatus'] = run_status
                action['integrationBlocked'] = bool(integration_block)
        if action is not None:
            actions.append(action)

        if cleanup_due:
            worker_cleanup = cleanup_plan.get(key, {})
            actions.extend(_cleanup_actions(worker, observed, worker_cleanup, state))

    outstanding = cleanup.get('outstandingResources', [])
    if cleanup_due and isinstance(outstanding, list) and outstanding:
        actions.append(_make_action(
            'revalidate', 'run#1',
            'run-level cleanup.outstandingResources is non-empty; reconcile it before marking cleanup complete',
            {},
            prerequisites=['resolve outstanding resources', 'rerun read-only next evidence check'],
            priority=PRIORITY_RUN_CLEANUP,
            cleanupKind='outstanding_resources',
            outstandingResources=list(outstanding),
        ))
    elif cleanup_due and not any(item.get('cleanupKind') for item in actions):
        actions.append(_make_action(
            'revalidate', 'run#1',
            f"cleanup status is {cleanup_status!r} but no worker cleanup action was proven; "
            "reconcile run-level cleanup",
            {},
            prerequisites=['verify all run resources and cleanup evidence'],
            priority=PRIORITY_RUN_CLEANUP,
            cleanupKind='run_status',
        ))

    actions.sort(key=lambda item: item['priority'])
    return {
        'runId': state.get('runId'),
        'runStatus': run_status,
        'cleanupStatus': cleanup_status,
        'runExecutionBlocked': execution_blocked,
        'integrationBlocked': bool(integration_block),
        'readOnly': True,
        'unresolvedFailures': [
            failure_summary(item) for item in unresolved_failures(state)
        ],
        'actions': actions,
    }

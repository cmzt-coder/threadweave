"""桌面派发回执恢复的本地回归测试；不创建真实线程、不发送宿主消息。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from controller import Controller
from desktop_bridge import (BridgePending, DesktopWriteBridgeAdapter, SEND_METHOD,
                            digest, now)
from state_store import read_state, run_state_path

RUN_ID = 'send-recovery-probe'
ROLE = 'probe'
ATTEMPT = 1
THREAD_ID = 'fixture-thread'
BASE_SHA = 'a' * 40
WORKTREE = '/fixture/worktree'
COMMON_DIR = '/fixture/common/.git'

checks = []


def check(name, condition):
    assert condition, name
    checks.append(name)


def rejects(name, error, function):
    try:
        function()
    except error:
        checks.append(name)
    else:
        raise AssertionError(name)


def make_state():
    state = json.loads((project / 'references/state.example.json').read_text())
    state.update(runId=RUN_ID, status='dispatching', revision=0,
                 resources=[], mergeOrder=[], integrationLog=[])
    state['project']['commonDirectory'] = COMMON_DIR
    state['integration'].update(baseSha=BASE_SHA, repoPath='/fixture/repo')
    state['workers'] = [dict(
        state['workers'][0], role=ROLE, attempt=ATTEMPT, state='ready',
        branch='fixture-branch', baseSha=BASE_SHA, threadId=THREAD_ID,
        worktree=WORKTREE, activityRevision=0)]
    return state


def make_adapter(repo, operation_id, task_revision=0):
    return DesktopWriteBridgeAdapter(
        repo, RUN_ID, operation_id, ROLE, ATTEMPT,
        task_revision=task_revision)


def startup_report(state):
    worker = state['workers'][0]
    return dict(role=ROLE, attempt=ATTEMPT, observedAt=now(), cwd=WORKTREE,
                worktree_root=WORKTREE, git_common_dir=COMMON_DIR,
                branch=worker['branch'], head=worker['baseSha'],
                status_porcelain='')


def expected(state):
    return dict(common_dir=COMMON_DIR, other_roots=[],
                integration_head=state['integration']['baseSha'])


def receipt(record, metadata=None, error=None):
    value = {key: record[key] for key in
             ('operationId', 'runId', 'requestSha', 'claimToken')}
    value.update(method=record['request']['method'], source='desktop_tool_bridge',
                 observedAt=now(), toolResultSha=digest({'fixture': True}))
    if error is None:
        value.update(status='succeeded', metadata=metadata)
    else:
        value.update(status='failed', error=error)
    return value


def worker_from(state):
    return state['workers'][0]


with tempfile.TemporaryDirectory() as root:
    # 1. 失败回执不能推进 ready worker，且 pending intent 必须阻止再次派发。
    td = str(Path(root) / 'failed-case')
    failed_state = make_state()
    failed_adapter = make_adapter(td, 'failed-send')
    failed_controller = Controller(failed_adapter, td, RUN_ID, failed_state)
    failed_controller.save()
    failed_path = run_state_path(td, RUN_ID)

    report = startup_report(failed_state)
    dispatch_expected = expected(failed_state)
    rejects('failed-dispatch-queues', BridgePending, lambda: failed_controller.dispatch(
        ROLE, ATTEMPT, 'fixture nonce', report, dispatch_expected))

    queued_record = failed_adapter.journal.inspect('failed-send')
    queued_state = read_state(failed_path)
    check('failed-send-request-is-persisted-before-receipt',
          queued_record['status'] == 'pending'
          and queued_record['request']['method'] == SEND_METHOD)
    check('failed-send-intent-stays-pending',
          worker_from(queued_state)['dispatchIntent']['result'] == 'pending')
    check('failed-send-worker-not-advanced',
          worker_from(queued_state)['state'] == 'ready'
          and worker_from(queued_state)['activityRevision'] == 0)

    claim = failed_adapter.journal.claim('failed-send')
    failed_adapter.journal.complete(
        'failed-send', receipt(claim, error='fixture host rejected send'))
    failed_record = failed_adapter.journal.inspect('failed-send')
    check('failed-receipt-is-durable', failed_record['status'] == 'failed')

    memory_before_failed_resume = copy.deepcopy(failed_controller.state)
    bytes_before_failed_resume = failed_path.read_bytes()
    journal_bytes_before_failed_resume = failed_adapter.journal._path(
        'failed-send').read_bytes()
    rejects('failed-receipt-resume-raises', RuntimeError,
            lambda: failed_controller.resume_bridge_dispatch(ROLE, ATTEMPT))

    failed_durable = read_state(failed_path)
    failed_worker = worker_from(failed_durable)
    check('failed-receipt-memory-unchanged',
          failed_controller.state == memory_before_failed_resume)
    check('failed-receipt-durable-bytes-unchanged',
          failed_path.read_bytes() == bytes_before_failed_resume)
    check('failed-receipt-worker-remains-ready',
          failed_worker['state'] == 'ready'
          and failed_worker['activityRevision'] == 0
          and failed_worker['dispatchIntent']['result'] == 'pending')
    check('failed-receipt-is-not-completion-stop-or-acceptance',
          failed_worker['state'] not in ('reported', 'integrated', 'archived')
          and 'report' not in failed_worker
          and 'stopStatus' not in failed_worker
          and 'acceptance' not in failed_worker)
    check('failed-receipt-does-not-rewrite-journal',
          failed_adapter.journal._path('failed-send').read_bytes()
          == journal_bytes_before_failed_resume)

    with patch.object(failed_adapter, 'send_message',
                      side_effect=AssertionError('must not resend after failed receipt')):
        rejects('pending-intent-blocks-second-dispatch', RuntimeError,
                lambda: failed_controller.dispatch(
                    ROLE, ATTEMPT, 'second nonce', report, dispatch_expected))
    check('second-dispatch-does-not-change-journal',
          failed_adapter.journal._path('failed-send').read_bytes()
          == journal_bytes_before_failed_resume)

    # 2. accepted 回执落盘后，最终状态保存失败必须回滚内存并保留未决状态；
    #    新控制器复用同一回执只能成功恢复一次，且绝不能再次发送消息。
    td = str(Path(root) / 'accepted-case')
    accepted_state = make_state()
    accepted_adapter = make_adapter(td, 'accepted-send')
    accepted_controller = Controller(accepted_adapter, td, RUN_ID, accepted_state)
    accepted_controller.save()
    accepted_path = run_state_path(td, RUN_ID)

    report = startup_report(accepted_state)
    dispatch_expected = expected(accepted_state)
    rejects('accepted-dispatch-queues', BridgePending,
            lambda: accepted_controller.dispatch(
                ROLE, ATTEMPT, 'fixture nonce', report, dispatch_expected))
    queued_state = read_state(accepted_path)
    check('accepted-dispatch-worker-ready-with-pending-intent',
          worker_from(queued_state)['state'] == 'ready'
          and worker_from(queued_state)['activityRevision'] == 0
          and worker_from(queued_state)['dispatchIntent']['result'] == 'pending')

    claim = accepted_adapter.journal.claim('accepted-send')
    accepted_adapter.journal.complete('accepted-send', receipt(claim, {
        'threadId': THREAD_ID, 'hostId': 'local', 'status': 'accepted'}))
    accepted_record = accepted_adapter.journal.inspect('accepted-send')
    accepted_journal_bytes = accepted_adapter.journal._path(
        'accepted-send').read_bytes()
    check('accepted-receipt-is-exactly-accepted',
          accepted_record['status'] == 'succeeded'
          and accepted_record['response']['metadata']['status'] == 'accepted')
    check('accepted-receipt-alone-does-not-advance-worker',
          worker_from(read_state(accepted_path))['state'] == 'ready'
          and worker_from(read_state(accepted_path))['activityRevision'] == 0)

    memory_before_save_failure = copy.deepcopy(accepted_controller.state)
    bytes_before_save_failure = accepted_path.read_bytes()
    queued_revision = queued_state['revision']
    with patch('controller.write_state_atomic',
               side_effect=OSError('fixture final state save failed')):
        rejects('accepted-final-save-failure-raises', OSError,
                lambda: accepted_controller.resume_bridge_dispatch(ROLE, ATTEMPT))

    failed_save_durable = read_state(accepted_path)
    failed_save_worker = worker_from(failed_save_durable)
    check('accepted-save-failure-rolls-back-memory',
          accepted_controller.state == memory_before_save_failure)
    check('accepted-save-failure-preserves-durable-bytes',
          accepted_path.read_bytes() == bytes_before_save_failure)
    check('accepted-save-failure-preserves-revision',
          failed_save_durable['revision'] == queued_revision
          and accepted_controller.state['revision'] == queued_revision)
    check('accepted-save-failure-preserves-run-status',
          failed_save_durable['status'] == 'dispatching')
    check('accepted-save-failure-leaves-intent-unresolved',
          failed_save_worker['state'] == 'ready'
          and failed_save_worker['activityRevision'] == 0
          and failed_save_worker['dispatchIntent']['result'] == 'pending')
    check('accepted-save-failure-does-not-rewrite-receipt',
          accepted_adapter.journal._path('accepted-send').read_bytes()
          == accepted_journal_bytes)

    restarted = Controller(
        make_adapter(td, 'accepted-send'), td, RUN_ID,
        read_state(accepted_path))
    with patch.object(restarted.adapter, 'send_message',
                      side_effect=AssertionError('must not resend accepted receipt')):
        recovered_thread = restarted.resume_bridge_dispatch(ROLE, ATTEMPT)

    recovered = read_state(accepted_path)
    recovered_worker = worker_from(recovered)
    check('new-controller-resumes-same-receipt-without-send',
          recovered_thread == THREAD_ID)
    check('receipt-recovery-dispatches-worker',
          recovered_worker['state'] == 'dispatched')
    check('receipt-recovery-advances-activity-once',
          recovered_worker['activityRevision'] == 1)
    check('receipt-recovery-resolves-intent',
          recovered_worker['dispatchIntent']['result'] == 'sent')
    check('receipt-recovery-saves-exactly-one-new-revision',
          recovered['revision'] == queued_revision + 1)
    check('receipt-recovery-does-not-complete-run',
          recovered['status'] == 'dispatching')
    check('accepted-receipt-is-not-task-completion',
          recovered_worker['state'] not in ('reported', 'integrated', 'archived')
          and 'report' not in recovered_worker)
    check('accepted-receipt-is-not-stop',
          'stopStatus' not in recovered_worker
          and 'stopActivityRevision' not in recovered_worker)
    check('accepted-receipt-is-not-acceptance',
          'acceptance' not in recovered_worker)
    check('receipt-recovery-reuses-immutable-journal',
          restarted.adapter.journal._path('accepted-send').read_bytes()
          == accepted_journal_bytes)

    recovered_bytes = accepted_path.read_bytes()
    rejects('second-resume-is-blocked', RuntimeError,
            lambda: restarted.resume_bridge_dispatch(ROLE, ATTEMPT))
    check('second-resume-does-not-rewrite-state',
          accepted_path.read_bytes() == recovered_bytes)
    check('second-resume-does-not-rewrite-receipt',
          restarted.adapter.journal._path('accepted-send').read_bytes()
          == accepted_journal_bytes)

    # 3. ledger 状态为 unknown 时，重启后的 resume 必须阻塞；只有显式
    #    reconciliation 才能让同一原始 claim 的 accepted receipt 恢复一次。
    td = str(Path(root) / 'unknown-case')
    unknown_state = make_state()
    unknown_adapter = make_adapter(td, 'unknown-send')
    unknown_controller = Controller(unknown_adapter, td, RUN_ID, unknown_state)
    unknown_controller.save()
    unknown_path = run_state_path(td, RUN_ID)

    report = startup_report(unknown_state)
    dispatch_expected = expected(unknown_state)
    rejects('unknown-dispatch-queues', BridgePending,
            lambda: unknown_controller.dispatch(
                ROLE, ATTEMPT, 'unknown nonce', report, dispatch_expected))

    unknown_claim = unknown_adapter.journal.claim('unknown-send')
    original_claim_token = unknown_claim['claimToken']
    unknown_adapter.journal.mark_unknown(
        'unknown-send', 'fixture host outcome was lost')
    unknown_journal_bytes = unknown_adapter.journal._path(
        'unknown-send').read_bytes()
    unknown_state_before_resume = read_state(unknown_path)
    unknown_state_bytes = unknown_path.read_bytes()
    unknown_revision = unknown_state_before_resume['revision']

    unknown_restarted = Controller(
        make_adapter(td, 'unknown-send'), td, RUN_ID,
        unknown_state_before_resume)
    unknown_memory_before_resume = copy.deepcopy(unknown_restarted.state)
    with patch.object(unknown_restarted.adapter, 'send_message',
                      side_effect=AssertionError('must not resend unknown send')):
        rejects('unknown-ledger-resume-blocks', BridgePending,
                lambda: unknown_restarted.resume_bridge_dispatch(ROLE, ATTEMPT))
    check('unknown-ledger-resume-does-not-send',
          unknown_restarted.state == unknown_memory_before_resume)
    check('unknown-ledger-resume-preserves-state',
          read_state(unknown_path) == unknown_state_before_resume
          and unknown_path.read_bytes() == unknown_state_bytes)
    check('unknown-ledger-resume-preserves-journal',
          unknown_adapter.journal._path('unknown-send').read_bytes()
          == unknown_journal_bytes)

    accepted_unknown_receipt = receipt(unknown_claim, {
        'threadId': THREAD_ID, 'hostId': 'local', 'status': 'accepted'})
    rejects('unknown-receipt-without-reconciliation-rejected', RuntimeError,
            lambda: unknown_adapter.journal.complete(
                'unknown-send', accepted_unknown_receipt))
    unreconciled = unknown_adapter.journal.inspect('unknown-send')
    check('unreconciled-receipt-leaves-ledger-unknown',
          unreconciled['status'] == 'unknown'
          and unreconciled['claimToken'] == original_claim_token
          and unknown_adapter.journal._path('unknown-send').read_bytes()
          == unknown_journal_bytes)

    unknown_adapter.journal.complete(
        'unknown-send', accepted_unknown_receipt,
        'fixture explicit reconciliation: original claimed send was accepted')
    reconciled = unknown_adapter.journal.inspect('unknown-send')
    reconciled_journal_bytes = unknown_adapter.journal._path(
        'unknown-send').read_bytes()
    check('unknown-receipt-with-reconciliation-accepted',
          reconciled['status'] == 'succeeded'
          and reconciled['response']['metadata']['status'] == 'accepted'
          and reconciled['claimToken'] == original_claim_token)
    check('unknown-reconciliation-retains-original-claim-binding',
          reconciled['claimToken'] == unknown_claim['claimToken']
          and reconciled['requestSha'] == unknown_claim['requestSha'])
    check('unknown-reconciliation-evidence-is-durable',
          reconciled['reconciliation']['evidence'].startswith('fixture explicit'))

    unknown_recovered = Controller(
        make_adapter(td, 'unknown-send'), td, RUN_ID,
        read_state(unknown_path))
    with patch.object(unknown_recovered.adapter, 'send_message',
                      side_effect=AssertionError('must not resend reconciled send')):
        recovered_unknown_thread = unknown_recovered.resume_bridge_dispatch(
            ROLE, ATTEMPT)
    recovered_unknown = read_state(unknown_path)
    recovered_unknown_worker = worker_from(recovered_unknown)
    check('unknown-receipt-resumes-once-without-send',
          recovered_unknown_thread == THREAD_ID)
    check('unknown-receipt-dispatches-worker',
          recovered_unknown_worker['state'] == 'dispatched')
    check('unknown-receipt-advances-activity-once',
          recovered_unknown_worker['activityRevision'] == 1)
    check('unknown-receipt-resolves-intent',
          recovered_unknown_worker['dispatchIntent']['result'] == 'sent')
    check('unknown-receipt-saves-exactly-one-new-revision',
          recovered_unknown['revision'] == unknown_revision + 1)
    check('unknown-receipt-preserves-journal-bytes',
          unknown_adapter.journal._path('unknown-send').read_bytes()
          == reconciled_journal_bytes)
    check('unknown-receipt-preserves-original-claim-token',
          unknown_adapter.journal.inspect('unknown-send')['claimToken']
          == original_claim_token)

    recovered_unknown_bytes = unknown_path.read_bytes()
    rejects('unknown-receipt-second-resume-blocked', RuntimeError,
            lambda: unknown_recovered.resume_bridge_dispatch(ROLE, ATTEMPT))
    check('unknown-receipt-second-resume-does-not-rewrite-state',
          unknown_path.read_bytes() == recovered_unknown_bytes)
    check('unknown-receipt-second-resume-does-not-rewrite-journal',
          unknown_adapter.journal._path('unknown-send').read_bytes()
          == reconciled_journal_bytes)

print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False))

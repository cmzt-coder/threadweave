"""跨模块集成验收回归：真实临时 Git 仓库 + FakeAdapter。

覆盖第四轮契约 O01/O02/O05 的交叉行为：
- worker 停止证据与任务修订绑定，报告必须完整且 reportSha 正确；
- A 集成验收失败后阻断 B，磁盘恢复后仍阻断，显式重验后才恢复；
- 旧报告 taskRevision、旧 worker 验收 SHA、返工后的旧 acceptance 失效；
- 返工上限默认 2，超限不改状态/计数/修订且留下可审计记录。

FakeAdapter 只记录控制器发出的调用，不代表真实宿主；本测试证明的是控制器
本地状态机与真实 Git 观察/执行器的组合，不证明真实线程闭环。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))

from controller import Controller  # noqa: E402
from host_adapter import HostAdapter  # noqa: E402
import report as report_module  # noqa: E402
from run_log import read_log  # noqa: E402
from state_store import read_state, run_state_path  # noqa: E402

results = []
NOW = '2026-11-13T10:00:00+08:00'


def check(name, condition, detail=''):
    if not condition:
        raise AssertionError(f'{name}{": " + detail if detail else ""}')
    results.append(name)


def expect_failure(name, func, needles=None):
    """断言调用失败；needles 用于确认失败原因落在契约语义内。"""
    try:
        func()
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - 需要覆盖 ValueError/RuntimeError 等
        text = str(exc).lower()
        if needles and not any(n.lower() in text for n in needles):
            raise AssertionError(
                f'{name}: 失败信息未包含 {needles}，实际为 {exc!r}')
        results.append(name)
        return exc
    raise AssertionError(f'{name}: 预期失败但调用成功')


class FakeAdapter(HostAdapter):
    """只记录调用的适配器；不创建线程、不代表真实宿主。"""

    def __init__(self):
        self.calls = []

    def create_worker(self, prompt, project_id):
        self.calls.append(('create_worker', prompt, project_id))
        return {'threadId': 'fake-thread'}

    def send_message(self, thread_id, message):
        self.calls.append(('send_message', thread_id, message))

    def read_thread(self, thread_id):
        self.calls.append(('read_thread', thread_id))
        return {'status': 'idle'}

    def wait_threads(self, thread_ids, timeout_seconds):
        self.calls.append(('wait_threads', thread_ids, timeout_seconds))
        return {'timedOut': False}

    def create_worktree(self, name):
        self.calls.append(('create_worktree', name))
        return {'root': name}

    def archive_worktree(self, root):
        self.calls.append(('archive_worktree', root))


def git(cwd, *args):
    result = subprocess.run(
        ['git', '-C', str(cwd)] + list(args),
        capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f'git {" ".join(args)} 在 {cwd} 失败: {result.stderr.strip()}')
    return result.stdout.strip()


def head(cwd):
    return git(cwd, 'rev-parse', 'HEAD')


def init_repo(root):
    repo = Path(root) / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.email', 'regression@example.invalid')
    git(repo, 'config', 'user.name', 'threadweave regression')
    (repo / 'base.txt').write_text('base\n', encoding='utf-8')
    git(repo, 'add', 'base.txt')
    git(repo, 'commit', '-m', 'base')
    # 与真实运行前置一致：run 状态目录不属于被测仓库成果。
    exclude = repo / '.git' / 'info' / 'exclude'
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open('a', encoding='utf-8') as handle:
        handle.write('.threadweave/\n')
    return repo


def add_worktree(repo, root, name, branch):
    path = Path(root) / name
    git(repo, 'worktree', 'add', '-b', branch, str(path), 'main')
    return path


def commit_file(worktree, name, content, message):
    (Path(worktree) / name).write_text(content, encoding='utf-8')
    git(worktree, 'add', name)
    git(worktree, 'commit', '-m', message)
    return head(worktree)


def merge_branch(repo, branch, message):
    git(repo, 'merge', '--no-ff', branch, '-m', message)
    return head(repo)


def common_dir(repo):
    value = git(repo, 'rev-parse', '--git-common-dir')
    path = Path(value)
    if not path.is_absolute():
        path = Path(repo) / path
    return str(path.resolve())


def make_worker(role, worktree, base):
    return {
        'role': role,
        'attempt': 1,
        'state': 'running',
        'threadId': f'fake-{role}-1',
        'branch': f'tw/{role}-1',
        'baseSha': base,
        'worktree': str(worktree),
        'createdAt': NOW,
        'deadline': NOW,
        'lastObservation': NOW,
        'activityRevision': 0,
        'errors': [],
    }


def make_state(repo, base, run_id, workers):
    return {
        'schemaVersion': '1.0',
        'runId': run_id,
        'createdAt': NOW,
        'updatedAt': NOW,
        'status': 'observing',
        'project': {
            'projectId': 'regression',
            'hostId': 'fake-local',
            'canonicalRepositoryRoot': str(repo),
            'commonDirectory': common_dir(repo),
        },
        'integration': {
            'baseSha': base,
            'branch': 'main',
            'repoPath': str(repo),
        },
        'authorization': {'provenance': 'test', 'messagingScope': 'test'},
        'limits': {
            'workers': 2,
            'initializationTimeoutSeconds': 300,
            'replacementPerRole': 1,
        },
        'leader': {'threadId': 'fake-leader'},
        'resources': [],
        'mergeOrder': [],
        'integrationLog': [],
        'finalReview': {'status': 'pending', 'notes': ''},
        'cleanup': {
            'status': 'pending',
            'preservedReports': [],
            'outstandingResources': [],
            'notes': '',
        },
        'errors': [],
        'workers': workers,
    }


def report_for(run_id, worker, files_changed, task_revision):
    """构造 O02 要求的完整报告 JSON，reportSha 由 report_hash 计算。"""
    tip = head(worker['worktree'])
    payload = {
        'runId': run_id,
        'role': worker['role'],
        'attempt': worker['attempt'],
        'threadId': worker['threadId'],
        'branch': worker['branch'],
        'head': tip,
        'taskRevision': task_revision,
        'baseIsAncestor': True,
        'filesChanged': sorted(files_changed),
        'testsRun': ['worker-plan'],
        'selfReportedVerification': ['worker self-check only'],
        'contractChanges': [],
        'knownRisks': [],
    }
    payload['reportSha'] = report_module.report_hash(payload)
    return payload


def pass_plan():
    return {
        'version': 1,
        'steps': [{
            'id': 'head',
            'argv': ['git', 'rev-parse', 'HEAD'],
            'cwd': '.',
            'timeoutSeconds': 30,
            'expectedExitCode': 0,
            'dependsOn': [],
            'continueOnFailure': False,
        }],
    }


def fail_plan():
    return {
        'version': 1,
        'steps': [{
            'id': 'forced-failure',
            'argv': [sys.executable, '-c', 'raise SystemExit(3)'],
            'cwd': '.',
            'timeoutSeconds': 30,
            'expectedExitCode': 0,
            'dependsOn': [],
            'continueOnFailure': False,
        }],
    }


def worker_by_role(state, role, attempt=1):
    for worker in state.get('workers', []):
        if worker.get('role') == role and worker.get('attempt') == attempt:
            return worker
    raise AssertionError(f'worker {role}#{attempt} 不存在')


def worker_state(path, role, attempt=1):
    return worker_by_role(read_state(path), role, attempt)['state']


def stop_and_report(controller, worker, files_changed, task_revision):
    controller.request_stop(worker['role'], worker['attempt'])
    controller.confirm_stop(
        worker['role'], worker['attempt'],
        idle=True, final_answer_present=True, no_active_turn=True)
    payload = report_for(
        controller.run_id, worker, files_changed, task_revision)
    controller.accept_report(worker['role'], worker['attempt'], payload)
    return payload


# ---------------------------------------------------------------------------
# 场景 1：A 集成验收失败阻断 B；磁盘恢复仍阻断；显式重验后继续
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory(prefix='tw-reg-block-') as root:
    repo = init_repo(root)
    base = head(repo)
    wt_a = add_worktree(repo, root, 'wt-a', 'tw/a-1')
    wt_b = add_worktree(repo, root, 'wt-b', 'tw/b-1')
    commit_file(wt_a, 'a.txt', 'a\n', 'a: add a.txt')
    commit_file(wt_b, 'b.txt', 'b\n', 'b: add b.txt')

    run_id = 'run-block'
    adapter = FakeAdapter()
    worker_a = make_worker('a', wt_a, base)
    worker_b = make_worker('b', wt_b, base)
    controller = Controller(
        adapter, repo, run_id, make_state(repo, base, run_id, [worker_a, worker_b]))
    controller.save()
    state_path = run_state_path(repo, run_id)

    payload_a = stop_and_report(controller, worker_a, ['a.txt'], 0)
    check('a-running-to-reported', worker_state(state_path, 'a') == 'reported')
    controller.verify_worker('a', 1, pass_plan())
    saved = read_state(state_path)
    acc_a = worker_by_role(saved, 'a').get('acceptance', {})
    check('a-acceptance-bound-to-report',
          acc_a.get('reportSha') == payload_a['reportSha'])
    check('a-acceptance-result-passed',
          acc_a.get('result', {}).get('status') == 'passed')
    context_a = acc_a.get('result', {}).get('context', {})
    check('a-acceptance-context-binding',
          context_a.get('role') == 'a'
          and context_a.get('attempt') == 1
          and context_a.get('stage') == 'worker'
          and context_a.get('head') == payload_a['head']
          and context_a.get('taskRevision') == 0)
    check('a-verification-plan-persisted',
          worker_by_role(saved, 'a').get('verificationPlan') == pass_plan())

    # B 也必须先独立完成停止证据、报告与分支验收。
    stop_and_report(controller, worker_b, ['b.txt'], 0)
    controller.verify_worker('b', 1, pass_plan())
    check('b-worker-verification-passed',
          worker_state(state_path, 'b') == 'reported')

    merge_a = merge_branch(repo, 'tw/a-1', 'merge a')
    controller.transition_run('integrating')
    # 旧的自报字符串不能冒充验收证据。
    expect_failure(
        'self-reported-string-cannot-integrate',
        lambda: controller.integrate(
            'a', 1, merge_a, verification='tests passed'),
        needles=['plan', 'verification'])
    expect_failure(
        'a-integration-verification-failure-raised',
        lambda: controller.integrate('a', 1, merge_a, plan=fail_plan()))
    saved = read_state(state_path)
    check('a-integration-blocked-persisted', bool(saved.get('integrationBlocked')))
    check('a-stays-reported-after-failure',
          worker_by_role(saved, 'a')['state'] == 'reported')
    check('a-failed-integration-log-kept',
          any(entry.get('result') == 'failed'
              for entry in saved.get('integrationLog', [])))

    expect_failure('direct-transition-cannot-bypass-block',
                   lambda: controller.transition_worker('a', 1, 'integrated'),
                   needles=['blocked'])

    head_before_b = head(repo)
    expect_failure(
        'b-integration-blocked-after-a-failure',
        lambda: controller.integrate('b', 1, head_before_b, plan=pass_plan()),
        needles=['block', '阻断'])
    check('blocked-b-not-integrated',
          worker_state(state_path, 'b') == 'reported')
    check('blocked-integration-head-unchanged', head(repo) == head_before_b)

    # 模拟进程重启：从磁盘恢复状态，阻断必须仍然有效。
    recovered = Controller(FakeAdapter(), repo, run_id, read_state(state_path))
    check('disk-recovery-block-visible', bool(recovered.state.get('integrationBlocked')))
    expect_failure(
        'disk-recovery-still-blocks-b',
        lambda: recovered.integrate('b', 1, head_before_b, plan=pass_plan()),
        needles=['block', '阻断'])

    # 恢复不能只是删除失败标记：重验失败必须保持阻断。
    expect_failure(
        'recovery-with-failing-plan-rejected',
        lambda: recovered.recover_integration(fail_plan()))
    check('failing-recovery-keeps-block',
          bool(read_state(state_path).get('integrationBlocked')))

    recovered.recover_integration(pass_plan())
    saved = read_state(state_path)
    check('explicit-reverify-clears-block', not saved.get('integrationBlocked'))
    check('recovery-keeps-old-failure-log',
          any(entry.get('result') == 'failed'
              for entry in saved.get('integrationLog', [])))

    merge_b = merge_branch(repo, 'tw/b-1', 'merge b')
    recovered.integrate('b', 1, merge_b, plan=pass_plan())
    check('b-integrates-after-explicit-reverify',
          worker_state(state_path, 'b') == 'integrated')
    # 历史通过日志不能代表当前 HEAD，跨 run 的记录不能充当本轮证据。
    worker_by_role(recovered.state, 'b')['state'] = 'reported'
    last = recovered.state['integrationLog'][-1]
    last['acceptance']['context']['runId'] = 'another-run'
    expect_failure('direct-transition-rejects-wrong-run-log',
                   lambda: recovered.transition_worker('b', 1, 'integrated'))
    last['acceptance']['context']['runId'] = run_id
    commit_file(repo, 'later.txt', 'later\n', 'advance integration')
    expect_failure('direct-transition-rejects-stale-head-log',
                   lambda: recovered.transition_worker('b', 1, 'integrated'))



# ---------------------------------------------------------------------------
# 场景 2：报告真实性、旧 SHA/taskRevision 失效、返工使 acceptance 失效
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory(prefix='tw-reg-stale-') as root:
    repo = init_repo(root)
    base = head(repo)
    wt_a = add_worktree(repo, root, 'wt-a', 'tw/a-1')
    commit_file(wt_a, 'a.txt', 'a\n', 'a: add a.txt')

    run_id = 'run-stale'
    adapter = FakeAdapter()
    worker_a = make_worker('a', wt_a, base)
    controller = Controller(
        adapter, repo, run_id, make_state(repo, base, run_id, [worker_a]))
    controller.save()
    state_path = run_state_path(repo, run_id)

    good_report = report_for(run_id, worker_a, ['a.txt'], 0)
    incomplete = dict(good_report)
    incomplete.pop('filesChanged')
    expect_failure(
        'incomplete-report-rejected',
        lambda: controller.accept_report('a', 1, incomplete),
        needles=['filesChanged'])
    forged = dict(good_report, reportSha='0' * 64)
    expect_failure(
        'forged-report-hash-rejected',
        lambda: controller.accept_report('a', 1, forged),
        needles=['reportsha', 'hash'])
    check('rejected-reports-keep-worker-running',
          worker_state(state_path, 'a') == 'running')

    stop_and_report(controller, worker_a, ['a.txt'], 0)
    controller.verify_worker('a', 1, pass_plan())

    # 验收绑定 tip1；随后分支前进到 tip2，旧 acceptance 不得代表新 SHA。
    commit_file(wt_a, 'a2.txt', 'a2\n', 'a: add a2.txt')
    merge_tip2 = merge_branch(repo, 'tw/a-1', 'merge a tip2')
    controller.transition_run('integrating')
    expect_failure(
        'stale-worker-sha-acceptance-rejected',
        lambda: controller.integrate('a', 1, merge_tip2, plan=pass_plan()),
        needles=['sha', 'tip', 'head', 'stale', 'moved'])
    check('stale-sha-worker-not-integrated',
          worker_state(state_path, 'a') == 'reported')

    # 返工递增 taskRevision，旧报告与旧 acceptance 均失效。
    controller.rework('a', 1, 'second revision')
    saved = read_state(state_path)
    check('rework-increments-activity-revision',
          worker_by_role(saved, 'a').get('activityRevision') == 1)
    # 只保留 taskRevision 失效这一个变量：head 与文件清单取当前真实状态。
    stale_revision_report = report_for(
        run_id,
        worker_by_role(read_state(state_path), 'a'),
        ['a.txt', 'a2.txt'],
        0)
    expect_failure(
        'old-task-revision-report-rejected',
        lambda: controller.accept_report('a', 1, stale_revision_report),
        needles=['taskrevision', 'task revision', 'revision'])

    commit_file(wt_a, 'a3.txt', 'a3\n', 'a: add a3.txt')
    new_report = report_for(
        run_id,
        worker_by_role(read_state(state_path), 'a'),
        ['a.txt', 'a2.txt', 'a3.txt'],
        1)
    controller.accept_report('a', 1, new_report)
    check('new-task-revision-report-accepted',
          worker_state(state_path, 'a') == 'reported')

    merge_tip3 = merge_branch(repo, 'tw/a-1', 'merge a tip3')

    # 先隔离停止证据：返工清除了旧确认，verify 必须拒绝。
    expect_failure(
        'verify-requires-current-stop-evidence',
        lambda: controller.verify_worker('a', 1, pass_plan()),
        needles=['stop', '停止'])

    controller.confirm_stop(
        'a', 1, idle=True, final_answer_present=True, no_active_turn=True)
    # 停止证据已刷新，此时唯一缺口是返工后失效的旧 acceptance。
    expect_failure(
        'rework-invalidates-old-acceptance',
        lambda: controller.integrate('a', 1, merge_tip3, plan=pass_plan()),
        needles=['acceptance', 'verification', 'stale', 'revision'])

    controller.verify_worker('a', 1, pass_plan())
    controller.integrate('a', 1, merge_tip3, plan=pass_plan())
    check('reverified-worker-integrates',
          worker_state(state_path, 'a') == 'integrated')


# ---------------------------------------------------------------------------
# 场景 3：worker.verificationPlan 被替换后，旧 acceptance 的计划哈希失效
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory(prefix='tw-reg-plan-') as root:
    repo = init_repo(root)
    base = head(repo)
    wt_a = add_worktree(repo, root, 'wt-a', 'tw/a-1')
    commit_file(wt_a, 'a.txt', 'a\n', 'a: add a.txt')

    run_id = 'run-plan'
    adapter = FakeAdapter()
    worker_a = make_worker('a', wt_a, base)
    controller = Controller(
        adapter, repo, run_id, make_state(repo, base, run_id, [worker_a]))
    controller.save()
    state_path = run_state_path(repo, run_id)

    stop_and_report(controller, worker_a, ['a.txt'], 0)
    controller.verify_worker('a', 1, pass_plan())

    # 模拟验证契约被替换：旧 acceptance 绑定的 planHash 不再代表当前计划。
    controller.state['workers'][0]['verificationPlan'] = fail_plan()
    controller.save()
    merge_tip = merge_branch(repo, 'tw/a-1', 'merge a')
    controller.transition_run('integrating')
    expect_failure(
        'stale-verification-plan-rejected',
        lambda: controller.integrate('a', 1, merge_tip, plan=pass_plan()),
        needles=['plan', 'acceptance', 'verification'])
    check('stale-plan-worker-not-integrated',
          worker_state(state_path, 'a') == 'reported')


# ---------------------------------------------------------------------------
# 场景 4：返工上限默认 2，超限不改领域状态且可审计
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory(prefix='tw-reg-rework-') as root:
    repo = init_repo(root)
    base = head(repo)
    wt_a = add_worktree(repo, root, 'wt-a', 'tw/a-1')
    commit_file(wt_a, 'a.txt', 'a\n', 'a: add a.txt')

    run_id = 'run-rework'
    adapter = FakeAdapter()
    worker_a = make_worker('a', wt_a, base)
    controller = Controller(
        adapter, repo, run_id, make_state(repo, base, run_id, [worker_a]))
    controller.save()
    state_path = run_state_path(repo, run_id)

    expect_failure(
        'rework-requires-reported-state',
        lambda: controller.rework('a', 1, 'too early'),
        needles=['reported', 'running', 'state', '状态'])

    stop_and_report(controller, worker_a, ['a.txt'], 0)
    for expected_count in (1, 2):
        controller.rework('a', 1, f'rework {expected_count}')
        current = worker_by_role(read_state(state_path), 'a')
        check(
            f'rework-{expected_count}-accepted',
            current.get('reworkCount') == expected_count
            and current.get('activityRevision') == expected_count)
        payload = report_for(
            run_id, current, ['a.txt'], expected_count)
        controller.accept_report('a', 1, payload)

    # 停止中的 worker 不允许返工；确认前先撤掉停止标记以便继续验证上限。
    controller.request_stop('a', 1)
    expect_failure(
        'rework-blocked-while-stopping',
        lambda: controller.rework('a', 1, 'stopping'),
        needles=['stop', 'stopping', '停止'])
    controller.state['workers'][0].pop('stopStatus', None)
    controller.state['workers'][0].pop('stopActivityRevision', None)
    controller.save()

    before = worker_by_role(read_state(state_path), 'a')
    expect_failure(
        'third-rework-rejected-by-default-limit',
        lambda: controller.rework('a', 1, 'rework 3'),
        needles=['rework', '返工', 'limit', '上限'])
    after = worker_by_role(read_state(state_path), 'a')
    check('rework-limit-count-unchanged', after.get('reworkCount') == 2)
    check('rework-limit-state-unchanged', after.get('state') == 'reported')
    check('rework-limit-revision-unchanged',
          after.get('activityRevision') == before.get('activityRevision'))
    check('rework-limit-no-message-sent',
          not any(call[0] == 'send_message' for call in adapter.calls))
    audit_records = [
        record for record in read_log(state_path.parent / 'audit.jsonl')
        if record.get('event') == 'rework.limit_reached'
        and record.get('role') == 'a'
        and record.get('attempt') == 1
    ]
    check('rework-limit-audit-recorded', bool(audit_records))
    check('rework-limit-audit-fields',
          audit_records[-1].get('limit') == 2
          and audit_records[-1].get('count') == 2)

    # 旧 attempt 不再是有效 attempt 时，也不得返工。
    controller.state['workers'].append({
        'role': 'a',
        'attempt': 2,
        'state': 'ready',
        'threadId': 'fake-a-2',
        'branch': 'tw/a-2',
        'baseSha': base,
        'worktree': str(wt_a),
        'createdAt': NOW,
        'deadline': NOW,
        'lastObservation': NOW,
        'activityRevision': 0,
        'errors': [],
    })
    controller.save()
    expect_failure(
        'old-attempt-rework-rejected',
        lambda: controller.rework('a', 1, 'old attempt'),
        needles=['active', 'attempt', '旧'])

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'real temporary git repo + FakeAdapter controller logic only; '
             'not a real host run',
}, ensure_ascii=False, indent=2))

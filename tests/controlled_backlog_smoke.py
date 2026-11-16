"""剩余待办的受控集成验证：真实临时 Git/账本，所有宿主响应均为测试端模拟。"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from cleanup_apply import preview
from controller import Controller
from desktop_bridge import BridgePending, now
from desktop_lifecycle_bridge import DesktopLifecycleBridgeAdapter, complete_tool_result
from host_adapter import UnsupportedHostAdapter
from managed_cleanup import execute_managed_cleanup
from project_resolver import ProjectResolutionError, resolve_project
from state_store import read_state

checks = []
evidence = {}


def check(name, condition):
    assert condition, name
    checks.append(name)


def rejected(name, fn, error=RuntimeError):
    try:
        fn()
    except error as exc:
        checks.append(name)
        return str(exc)
    raise AssertionError(name)


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


def raw(result, failed=False):
    return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': failed}


def state(repo, run, head):
    value = json.loads((project / 'references/state.example.json').read_text())
    value.update(runId=run, status='observing', revision=0, resources=[], mergeOrder=[], integrationLog=[])
    value['project'].update(canonicalRepositoryRoot=str(repo), commonDirectory=str(repo / '.git'))
    value['integration'].update(baseSha=head, branch='main', repoPath=str(repo))
    worker = copy.deepcopy(value['workers'][0])
    worker.update(threadId='controlled-source', state='ready', branch='main',
                  baseSha=head, worktree=str(repo), activityRevision=0)
    value['workers'] = [worker]
    return value


with tempfile.TemporaryDirectory(prefix='threadweave-controlled-backlog-') as td:
    base = Path(td).resolve()
    repo = base / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Validation')
    git(repo, 'config', 'user.email', 'validation@example.invalid')
    (repo / '.gitignore').write_text('.threadweave/\n')
    (repo / 'tracked').write_text('base')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'controlled baseline')
    head = git(repo, 'rev-parse', 'HEAD')
    destination = base / 'destination'
    git(repo, 'worktree', 'add', '-b', 'codex/controlled-destination', str(destination))

    # O16：通过真实 prepare/claim/原始结果导入取得观察，不手工拼接桥接回执。
    run = 'controlled-cleanup'

    def adapter(operation, run_id=run):
        return DesktopLifecycleBridgeAdapter(
            repo, run_id, operation, 'a', 1, 0, archive_roots=[str(destination)])

    def observe(operation, method, result):
        item = adapter(operation)
        rejected(operation + '-prepared', lambda: method(item), BridgePending)
        item.journal.claim(operation)
        complete_tool_result(item.journal, operation, raw(result))
        return method(item)

    def poll(active=False):
        return {'cursor': 'controlled:1',
                'thread': {'id': 'controlled-worker', 'hostId': 'local',
                           'status': {'type': 'active' if active else 'idle'}},
                'latestTurn': {'id': 'controlled-turn', 'status': 'inProgress' if active else 'completed'},
                'latestAssistantMessage': {'turnId': 'controlled-turn', 'phase': 'final_answer'}}

    stop = observe('wait', lambda a: a.wait_threads(['controlled-worker'], 0),
                   {'timedOut': False, 'polls': [poll()]})
    inventory = {'artifacts': [{'type': 'worktree', 'identityKey': str(destination), 'payload': {
        'ownerThreadId': 'controlled-worker', 'runId': run, 'isPrimary': False, 'isShared': False}}]}
    artifacts = observe('inventory', lambda a: a.list_artifacts(), inventory)
    report = base / 'report.md'
    report.write_text('controlled integration report')
    ep = base / 'cleanup-evidence.json'
    declaration = dict(runId=run, repository=str(repo), worktree=str(destination),
                       branch='codex/controlled-destination', head=head, integrationRef='main',
                       report=str(report), reportSha256=hashlib.sha256(report.read_bytes()).hexdigest(),
                       evidence=str(ep))
    local = {k: declaration[k] for k in ('runId', 'worktree', 'branch', 'head', 'reportSha256')}
    local.update(observedAt=now(), ownedByRun=True, shared=False, acceptanceVerified=True,
                 executionKind='host_thread', threadId='controlled-worker',
                 stop=dict(threadIdle=True, finalAnswerPresent=True, noActiveTurn=True))
    ep.write_text(json.dumps(local))
    approved = preview(declaration)

    def cleanup(item, wait=stop, inv=artifacts):
        return execute_managed_cleanup(
            item, declaration, approved, wait, inv, 'controlled-worker', authorized=True)

    for field, value in [('ownerThreadId', 'other'), ('isPrimary', True), ('isShared', True)]:
        bad = copy.deepcopy(inventory)
        bad['artifacts'][0]['payload'][field] = value
        observed = observe('inventory-' + field, lambda a: a.list_artifacts(), bad)
        target = adapter('blocked-' + field)
        result = cleanup(target, inv=observed)
        check(field + '-no-archive-request', result['status'] == 'blocked'
              and not target.journal._path(target.operation_id).exists())
    active = observe('wait-active', lambda a: a.wait_threads(['controlled-worker'], 0),
                     {'timedOut': True, 'polls': [poll(True)]})
    target = adapter('blocked-active')
    check('active-timeout-no-archive-request', cleanup(target, wait=active)['status'] == 'blocked'
          and not target.journal._path(target.operation_id).exists())

    queued = adapter('archive-queued')
    rejected('valid-cleanup-prepares-archive', lambda: cleanup(queued), BridgePending)
    queued.journal.claim('archive-queued')
    complete_tool_result(queued.journal, 'archive-queued', raw({'status': 'queued'}))
    before = queued.journal._path('archive-queued').read_bytes()
    recovered = cleanup(adapter('archive-queued'))
    check('queued-is-not-deletion', recovered['deletionPerformed'] is None
          and recovered['requiresHostCompletionVerification'] and destination.exists())
    check('queued-replay-keeps-original-request', queued.journal._path('archive-queued').read_bytes() == before)

    unknown = adapter('archive-unknown')
    rejected('unknown-archive-prepares-once', lambda: cleanup(unknown), BridgePending)
    unknown.journal.claim('archive-unknown')
    unknown.journal.mark_unknown('archive-unknown', 'controlled host response lost')
    before = unknown.journal._path('archive-unknown').read_bytes()
    rejected('unknown-archive-no-resend', lambda: unknown.journal.claim('archive-unknown'))
    rejected('unknown-archive-remains-pending', lambda: cleanup(adapter('archive-unknown')), BridgePending)
    cli = subprocess.run([sys.executable, str(project / 'scripts/desktop_lifecycle_bridge.py'),
                          'inspect', '--repo', str(repo), '--run', run, '--operation', 'archive-unknown'],
                         text=True, capture_output=True, timeout=30)
    check('fresh-process-keeps-archive-unknown', cli.returncode == 0
          and json.loads(cli.stdout)['status'] == 'unknown'
          and unknown.journal._path('archive-unknown').read_bytes() == before)

    failure = adapter('archive-refused')
    rejected('refused-archive-prepares', lambda: cleanup(failure), BridgePending)
    failure.journal.claim('archive-refused')
    complete_tool_result(failure.journal, 'archive-refused', raw('controlled protected resource refusal', True))
    rejected('host-refusal-propagates', lambda: cleanup(adapter('archive-refused')))
    check('host-refusal-never-deletes-locally', destination.exists()
          and git(destination, 'rev-parse', 'HEAD') == head and not git(destination, 'status', '--porcelain'))
    succeeded = adapter('archive-success')
    rejected('successful-response-prepares', lambda: cleanup(succeeded), BridgePending)
    succeeded.journal.claim('archive-success')
    complete_tool_result(succeeded.journal, 'archive-success', raw({'status': 'archived'}))
    check('success-response-still-requires-observation', cleanup(succeeded)['deletionPerformed'] is None
          and destination.exists())
    evidence['O16'] = {'queued': queued.journal.inspect('archive-queued'),
                       'unknown': unknown.journal.inspect('archive-unknown'),
                       'refused': failure.journal.inspect('archive-refused')}

    # H01：只模拟宿主迁移回执；目标 Git 必须由 Controller 真实读取。
    run = 'controlled-handoff'
    c = Controller(UnsupportedHostAdapter(), repo, run, state(repo, run, head))
    c.save()
    expected = dict(threadId='controlled-destination', root=str(destination),
                    branch='codex/controlled-destination', head=head,
                    model='deepseek-flash', thinking='high', attachments=['controlled-worktree'])
    c.prepare_handoff_intent('a', 1, expected)
    c.bind_handoff_operation('a', 1, 'controlled-handoff-op', expected)
    receipt = dict(operationId='controlled-handoff-op', status='success',
                   sourceThreadId='controlled-source', destinationThreadId='controlled-destination')
    observation = {k: expected[k] for k in ('threadId', 'root', 'branch', 'head', 'attachments')}
    observation.update(cwd=str(destination), clean=True, gitSource='leader_git',
                       attachmentSource='host_artifacts', observedAt=now(),
                       runtime=dict(source='destination_host_metadata', threadId=expected['threadId'],
                                    operationId=receipt['operationId'], model=expected['model'], thinking='high'))
    cases = {}
    bad = copy.deepcopy(observation); bad['runtime']['model'] = 'wrong'; cases['model-change'] = bad
    bad = copy.deepcopy(observation); bad['runtime'] = {}; cases['runtime-unknown'] = bad
    bad = copy.deepcopy(observation); bad['attachments'] = []; cases['attachments-missing'] = bad
    bad = copy.deepcopy(observation); bad['attachments'] = ['controlled-worktree', None]; cases['malformed-attachments'] = bad
    bad = copy.deepcopy(observation); bad['observedAt'] = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat(); cases['stale'] = bad
    for name, value in cases.items():
        result = c.reconcile_handoff('a', 1, receipt, value)
        disk = read_state(c.state_path)
        check('handoff-' + name + '-persisted-blocked', result['status'] == 'blocked'
              and disk['workers'][0]['threadId'] == 'controlled-source')
    code = '''import sys
sys.path.insert(0, sys.argv[1])
from controller import Controller
from host_adapter import UnsupportedHostAdapter
from state_store import read_state
c=Controller(UnsupportedHostAdapter(),sys.argv[2],sys.argv[3],read_state(sys.argv[4]))
try:c.dispatch('a',1,'must not dispatch',None,None)
except RuntimeError as exc:
 print(str(exc))
 sys.exit(0 if 'handoff unresolved' in str(exc) else 2)
sys.exit(1)
'''
    child = subprocess.run([sys.executable, '-c', code, str(project / 'scripts'), str(repo), run,
                            str(c.state_path)], text=True, capture_output=True, timeout=30)
    check('handoff-fresh-process-dispatch-blocked', child.returncode == 0)
    (destination / 'tracked').write_text('controlled uncommitted change')
    result = c.reconcile_handoff('a', 1, receipt, observation)
    check('handoff-direct-git-overrides-clean-claim', result['status'] == 'blocked'
          and 'direct_git_mismatch' in result['reasons'])
    (destination / 'tracked').write_text('base')
    result = c.reconcile_handoff('a', 1, receipt, observation)
    check('handoff-real-git-reconciles', result['status'] == 'passed'
          and c.state['workers'][0]['threadId'] == expected['threadId'])
    check('handoff-requires-startup-revalidation', c.state['workers'][0]['state'] == 'validating'
          and c.state['workers'][0]['activityRevision'] == 1)
    evidence['H01'] = {'gate': result, 'state': read_state(c.state_path), 'restart': child.stdout.strip()}

    # 受控测试端执行真正的非强制 Git 移除，再独立核验；不冒充桌面宿主归档。
    final_archive = adapter('archive-controlled-final')
    rejected('controlled-final-archive-prepared',
             lambda: final_archive.archive_worktree(str(destination)), BridgePending)
    final_archive.journal.claim('archive-controlled-final')
    git(repo, 'worktree', 'remove', str(destination))
    complete_tool_result(final_archive.journal, 'archive-controlled-final',
                         raw({'status': 'archived', 'scope': 'controlled-local-git'}))
    final_result = adapter('archive-controlled-final').archive_worktree(str(destination))
    check('controlled-final-resource-removal-observed', not destination.exists()
          and str(destination) not in git(repo, 'worktree', 'list', '--porcelain'))
    check('controlled-final-branch-report-preserved', report.exists() and ep.exists()
          and git(repo, 'rev-parse', 'codex/controlled-destination') == head)
    check('controlled-final-receipt-matches-scope',
          final_result['metadata']['result']['scope'] == 'controlled-local-git')
    evidence['O16']['controlledFinal'] = {'receipt': final_result, 'pathAbsent': True,
                                         'branchPreserved': True, 'reportPreserved': True}

    # P0：可控多匹配、真实本地副作用后模拟 API 超时、能力缺失降级。
    projects = [dict(projectId=p, path=str(repo), hostId='local', isGitRepository=True) for p in ('p1', 'p2')]
    rejected('multiple-projects-refused', lambda: resolve_project(projects, repo, 'local'), ProjectResolutionError)
    marker = base / 'controlled-created-resources.json'

    class TimeoutHost(UnsupportedHostAdapter):
        capabilities = dict(create_timeout=True, wait_threads=False, update_plan=False)

        def create_worker(self, prompt, project_id, timeout_seconds=None):
            resources = json.loads(marker.read_text()) if marker.exists() else []
            resources.append({'id': 'controlled-' + str(len(resources) + 1), 'projectId': project_id})
            marker.write_text(json.dumps(resources))
            raise TimeoutError('controlled API timeout after local resource creation')

    run = 'controlled-create-timeout'
    c = Controller(TimeoutHost(), repo, run, state(repo, run, head))
    c.save()
    rejected('create-timeout-after-side-effect', lambda: c.create_worker('controlled', 'p1'), TimeoutError)
    disk = read_state(c.state_path)
    check('create-timeout-persisted-unknown', disk['lastCreateError']['resultCertainty'] == 'unknown')
    before = c.state_path.read_bytes()
    rejected('create-timeout-same-process-no-blind-retry', lambda: c.create_worker('controlled', 'p1'))
    restarted = Controller(TimeoutHost(), repo, run, disk)
    rejected('create-timeout-restart-no-blind-retry', lambda: restarted.create_worker('controlled', 'p1'))
    rejected('create-timeout-changed-request-not-a-resolution', lambda: restarted.create_worker('different', 'p2'))
    check('create-timeout-only-one-resource', len(json.loads(marker.read_text())) == 1)
    check('create-timeout-rejections-keep-state-bytes', c.state_path.read_bytes() == before)
    wait = restarted.wait_for_workers(['controlled-1'], 0)
    check('missing-wait-bounded-fallback', wait['mode'] == 'bounded-status')
    check('missing-wait-does-not-confirm-stop', not restarted.state['workers'][0].get('stopStatus'))
    check('missing-wait-does-not-mutate-state', c.state_path.read_bytes() == before)
    check('missing-plan-state-file-fallback', restarted.write_plan({'phases': ['controlled-review']}) == 'state-file')
    evidence['P0'] = {'timeout': disk['lastCreateError'], 'createdResources': json.loads(marker.read_text()),
                      'fallback': wait}

print(json.dumps({'scope': 'controlled_integration', 'realHostValidated': False,
                  'passed': len(checks), 'checks': checks, 'evidence': evidence}, ensure_ascii=False))

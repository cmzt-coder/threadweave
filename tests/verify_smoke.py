"""用真实临时 Git checkout 和进程验证独立执行器。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from verify import execute_plan, plan_hash, OUTPUT_LIMIT  # noqa: E402

checks = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)


def step(ident='one', code='print("ok")', **extra):
    return dict(id=ident, argv=[sys.executable, '-c', code], cwd='.',
                timeoutSeconds=5, expectedExitCode=0, dependsOn=[],
                continueOnFailure=False, **extra)


def plan(*steps):
    return dict(version=1, steps=list(steps))


def rejected(name, payload, root, context):
    try:
        execute_plan(payload, root, context)
    except ValueError:
        check(name, True)
    else:
        check(name, False)


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory) / 'repo'
    root.mkdir()

    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

    git('init', '-q')
    git('config', 'user.name', 'Verification Test')
    git('config', 'user.email', 'verification@users.noreply.github.com')
    (root / 'tracked').write_text('base\n')
    git('add', 'tracked')
    git('commit', '-qm', 'base')
    head = git('rev-parse', 'HEAD')
    context = dict(runId='run', role='test', attempt=1, taskRevision=1,
                   stage='worker', head=head)
    good = plan(step())
    result = execute_plan(good, root, context)
    check('success', result['status'] == 'passed')
    check('zero-task-revision', execute_plan(good, root, dict(context, taskRevision=0))['status'] == 'passed')
    check('context-bound', result['context'] == context and result['context'] is not context)
    check('hash-bound', result['planHash'] == plan_hash(good))
    check('timestamps', result['startedAt'] <= result['finishedAt'])
    check('exit-and-output', result['steps'][0]['exitCode'] == 0 and
          result['steps'][0]['stdout'].strip() == 'ok')
    check('canonical-hash', plan_hash({'a': 1, 'b': 2}) == plan_hash({'b': 2, 'a': 1}))
    check('hash-changes', plan_hash(good) != plan_hash(plan(step(code='print(2)'))))

    rejected('empty-plan', plan(), root, context)
    rejected('wrong-version', dict(good, version=True), root, context)
    rejected('duplicate-id', plan(step(), step()), root, context)
    bad = step(); bad['dependsOn'] = ['absent']
    rejected('unknown-dependency', plan(bad), root, context)
    first, second = step('a'), step('b')
    first['dependsOn'], second['dependsOn'] = ['b'], ['a']
    rejected('cycle', plan(first, second), root, context)
    for key, value in [('argv', 'echo ok'), ('argv', []), ('argv', ['x\0']),
                       ('cwd', '..'), ('timeoutSeconds', 0), ('timeoutSeconds', True),
                       ('timeoutSeconds', float('inf')), ('expectedExitCode', False),
                       ('continueOnFailure', 1), ('dependsOn', 'one')]:
        bad = step(); bad[key] = value
        rejected('invalid-' + key + '-' + repr(value), plan(bad), root, context)
    rejected('invalid-context', good, root, dict(context, stage='unknown'))
    rejected('wrong-head', good, root, dict(context, head='0' * 40))
    outside = Path(directory) / 'outside'; outside.mkdir()
    (root / 'link').symlink_to(outside, target_is_directory=True)
    bad = step(); bad['cwd'] = 'link'
    rejected('symlink-escape', plan(bad), root, context)
    (root / 'link').unlink()
    (root / 'sub').mkdir()
    retarget = step('retarget', 'import os;os.rmdir("sub");os.symlink(' + repr(str(outside)) + ',"sub")')
    escaped = step('escaped', 'open("escape-marker","w").write("bad")'); escaped['cwd'] = 'sub'
    result = execute_plan(plan(retarget, escaped), root, context)
    check('cwd-rechecked-at-execution', result['steps'][1]['status'] == 'failed' and not (outside / 'escape-marker').exists())
    (root / 'sub').unlink()
    marker = root / 'marker'
    bad = step('bad'); bad['argv'] = 'invalid'
    rejected('whole-plan-validated-before-execution',
             plan(step(code='open("marker", "w").write("oops")'), bad), root, context)
    check('no-validation-side-effects', not marker.exists())
    (root / 'dirty').write_text('x')
    rejected('dirty-checkout', good, root, context)
    (root / 'dirty').unlink()
    rejected('not-git-checkout', good, outside, context)

    failing = step('fail', 'raise SystemExit(3)')
    result = execute_plan(plan(failing, step('later')), root, context)
    check('failure-stops', result['status'] == 'failed' and
          result['steps'][1]['status'] == 'skipped')
    failing['continueOnFailure'] = True
    dependent = step('dependent'); dependent['dependsOn'] = ['fail']
    result = execute_plan(plan(failing, dependent, step('independent')), root, context)
    check('continue-independent', [s['status'] for s in result['steps']] ==
          ['failed', 'skipped', 'passed'])
    dependent = step('dependent'); dependent['dependsOn'] = ['parent']
    result = execute_plan(plan(dependent, step('parent')), root, context)
    check('forward-dependency', [s['id'] for s in result['steps']] == ['parent', 'dependent'])
    expected = step(code='raise SystemExit(7)'); expected['expectedExitCode'] = 7
    check('expected-nonzero', execute_plan(plan(expected), root, context)['status'] == 'passed')
    missing = step(); missing['argv'] = ['/no/such/executable']
    check('spawn-failure', execute_plan(plan(missing), root, context)['status'] == 'failed')
    literal = step(); literal['argv'] = [sys.executable, '-c', 'import sys; print(sys.argv[1])', '$(touch marker);*']
    result = execute_plan(plan(literal), root, context)
    check('argv-no-shell', not marker.exists() and '$(touch marker);*' in result['steps'][0]['stdout'])
    loud = step(code='import os; os.write(1,b"x"*2000000); os.write(2,b"y"*2000000)')
    result = execute_plan(plan(loud), root, context)['steps'][0]
    check('bounded-stdout', len(result['stdout']) <= OUTPUT_LIMIT)
    check('bounded-stderr', len(result['stderr']) <= OUTPUT_LIMIT)
    check('drained-output', result['status'] == 'passed' and result['outputTruncated'])
    timeout = step(code='import time; time.sleep(30)'); timeout['timeoutSeconds'] = 0.15
    started = time.monotonic()
    result = execute_plan(plan(timeout), root, context)
    check('timeout-status', result['status'] == 'failed' and result['steps'][0]['status'] == 'timeout')
    check('timeout-prompt', time.monotonic() - started < 3)

    # 子进程在父进程退出后仍持有管道；超时必须杀掉整个进程组。
    late_marker = Path(directory) / 'late-marker'
    child = 'import time,pathlib;time.sleep(0.8);pathlib.Path(' + repr(str(late_marker)) + ').write_text("bad")'
    parent = 'import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(child) + '])'
    timeout = step(code=parent); timeout['timeoutSeconds'] = 0.15
    result = execute_plan(plan(timeout), root, context)
    time.sleep(0.9)
    check('descendant-timeout', result['steps'][0]['status'] == 'timeout')
    check('descendant-killed', not late_marker.exists())
    result = execute_plan(plan(step(code='open("dirty","w").write("x")')), root, context)
    check('post-dirty-rejected', result['status'] == 'failed' and 'error' in result)
    (root / 'dirty').unlink()
    result = execute_plan(plan(step(code='import subprocess;subprocess.run(["git","commit","--allow-empty","-qm","drift"],check=True)')), root, context)
    check('post-head-drift-rejected', result['status'] == 'failed')
    git('reset', '--hard', head)
    for name, payload in [('plan.json', good), ('context.json', context)]:
        (Path(directory) / name).write_text(json.dumps(payload))
    command = [sys.executable, str(project / 'scripts/verify.py'), '--plan',
               str(Path(directory) / 'plan.json'), '--context',
               str(Path(directory) / 'context.json'), '--repo', str(root)]
    cli = subprocess.run(command, capture_output=True, text=True)
    check('cli-success', cli.returncode == 0 and json.loads(cli.stdout)['status'] == 'passed')
    (Path(directory) / 'plan.json').write_text('{')
    cli = subprocess.run(command, capture_output=True, text=True)
    check('cli-invalid-json', cli.returncode != 0 and json.loads(cli.stdout)['status'] == 'failed')

print(json.dumps(dict(passed=len(checks), checks=checks), ensure_ascii=False, indent=2))

"""验证 worker 启动门禁的拒绝条件，使用本地临时 Git 仓库做故障注入。

边界说明：本脚本不创建真实 worker，也不解析 SKILL.md；validate_startup 是协议
条件的参考实现，用于把「错误启动报告必须被拒绝」固化为可回归的检查，不能代表
真实线程已经执行同一逻辑。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from startup_gate import validate_startup  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def git(root, *args, ok=True):
    r = subprocess.run(
        ['git', '-c', 'user.name=Threadweave Test',
         '-c', 'user.email=threadweave-test@example.invalid', *args],
        cwd=root, text=True, capture_output=True,
    )
    if ok and r.returncode:
        raise RuntimeError(r.stderr)
    return r


def good_report(repo, wt, branch, base):
    return {
        'cwd': str(wt),
        'worktree_root': str(wt),
        'git_common_dir': str(repo / git(repo, 'rev-parse', '--git-common-dir').stdout.strip()),
        'branch': branch,
        'head': base,
        'status_porcelain': '',
        '_wt': wt,
        '_repo': repo,
    }


with tempfile.TemporaryDirectory(prefix='threadweave-startup-') as d:
    root = Path(d)
    repo = root / 'leader'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    (repo / 'baseline.txt').write_text('baseline\n')
    git(repo, 'add', 'baseline.txt')
    git(repo, 'commit', '-m', 'baseline')
    base = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    common = repo / git(repo, 'rev-parse', '--git-common-dir').stdout.strip()

    workers = {}
    for role in ('a', 'b'):
        wt = root / role
        git(repo, 'worktree', 'add', '--detach', str(wt), base)
        branch = 'threadweave/test/' + role + '-1'
        git(wt, 'switch', '-c', branch)
        workers[role] = (wt, branch)

    expected_a = {
        'common_dir': common,
        'branch': workers['a'][1],
        'base': base,
        'other_roots': [repo, workers['b'][0]],
    }
    report = good_report(repo, workers['a'][0], workers['a'][1], base)
    ok, reasons = validate_startup(report, expected_a)
    check('valid-report-accepted', ok and not reasons)

    wrong_root_expected = dict(expected_a)
    wrong_root_expected['worker_root'] = str(root / 'other')
    ok, reasons = validate_startup(report, wrong_root_expected)
    check('wrong-worker-root-rejected',
          not ok and 'worktree_root_mismatch' in reasons)

    bad = dict(report, cwd=str(root))
    ok, reasons = validate_startup(bad, expected_a)
    check('wrong-cwd-rejected', not ok and 'cwd_outside_worktree_root' in reasons)

    foreign = root / 'foreign'
    foreign.mkdir()
    git(foreign, 'init', '-b', 'main')
    bad = dict(report, git_common_dir=str(foreign / git(foreign, 'rev-parse', '--git-common-dir').stdout.strip()))
    ok, reasons = validate_startup(bad, expected_a)
    check('foreign-repository-rejected', not ok and 'git_common_dir_mismatch' in reasons)

    bad = dict(report, head='0' * 40)
    ok, reasons = validate_startup(bad, expected_a)
    check('wrong-base-sha-rejected', not ok and 'head_not_base' in reasons)

    bad = dict(report, status_porcelain='?? unexpected.txt\n')
    ok, reasons = validate_startup(bad, expected_a)
    check('dirty-worktree-rejected', not ok and 'dirty_worktree' in reasons)

    bad = dict(report, branch='threadweave/test/other-1')
    ok, reasons = validate_startup(bad, expected_a)
    check('wrong-branch-rejected', not ok and 'branch_mismatch' in reasons)

    expected_overlap = dict(expected_a, other_roots=[root])
    bad = dict(report)
    ok, reasons = validate_startup(bad, expected_overlap)
    check('root-overlap-rejected', not ok and 'worktree_root_overlap' in reasons)

    # 收尾：确认测试没有污染真实仓库
    check('temp-worktrees-isolated',
          len({repo.resolve(), *(w[0].resolve() for w in workers.values())}) == 3)
    for wt, _ in workers.values():
        git(repo, 'worktree', 'remove', str(wt))

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Local startup-gate rejection scenarios; no real worker chats',
}, ensure_ascii=False, indent=2))

"""Threadweave 本地辅助 CLI。

只做本地 Git/文件检查、状态读取与清理计划输出；不创建线程、不调用宿主 API，
也不自动执行删除。所有清理相关命令只输出计划（dry-run）。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from cleanup import build_cleanup_plan
from state_store import latest_incomplete_run, locate_runs, read_state, run_state_path


def git(repo, *args):
    result = subprocess.run(['git', *args], cwd=str(repo), text=True, capture_output=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def preflight(repo):
    """检查本地 Git 前置条件；不替代 leader 的完整 preflight。"""
    repo = Path(repo)
    checks = {}
    rc, out, _ = git(repo, 'rev-parse', '--is-inside-work-tree')
    checks['inside_work_tree'] = rc == 0 and out == 'true'
    rc, out, _ = git(repo, 'rev-parse', '--verify', 'HEAD')
    checks['has_head'] = rc == 0 and bool(out)
    rc, out, _ = git(repo, 'symbolic-ref', '--short', 'HEAD')
    checks['attached_branch'] = rc == 0 and bool(out)
    rc, out, _ = git(repo, 'status', '--porcelain')
    checks['clean'] = rc == 0 and not out
    return checks


def status(repo):
    """列出 durable run，并返回最近一个非终端 run。"""
    repo = Path(repo)
    return {'runs': locate_runs(repo), 'latestIncomplete': latest_incomplete_run(repo)}


def inspect(repo):
    """只读列出每个 run 的关键状态；异常结构标记 invalid。"""
    details = []
    for run in locate_runs(repo):
        try:
            state = read_state(run['path'])
        except (OSError, ValueError):
            details.append({'runId': run['runId'], 'status': 'unreadable'})
            continue
        if not isinstance(state, dict):
            details.append({'runId': run['runId'], 'status': 'unreadable'})
            continue
        workers = state.get('workers')
        if not isinstance(workers, list):
            details.append({'runId': run['runId'], 'status': 'invalid', 'workers': []})
            continue
        details.append({
            'runId': run['runId'],
            'status': state.get('status'),
            'revision': state.get('revision', 0),
            'workers': [
                {'role': w.get('role'), 'attempt': w.get('attempt'), 'state': w.get('state')}
                for w in workers
            ],
        })
    return {'runs': details}


def doctor(repo):
    """只读诊断状态目录：schema、runId、不变量与损坏状态。"""
    from state_schema import validate_state
    problems = []
    for run in locate_runs(repo):
        if run['status'] == 'unreadable':
            problems.append(f"{run['runId']}: unreadable state")
            continue
        try:
            state = read_state(run['path'])
            if not isinstance(state, dict):
                problems.append(f"{run['runId']}: state is not an object")
                continue
            if state.get('runId') != Path(run['path']).parent.name:
                problems.append(f"{run['runId']}: runId does not match directory")
            validate_state(state)
        except (ValueError, OSError) as exc:
            problems.append(f"{run['runId']}: {exc}")
    return {'ok': not problems, 'problems': problems}


def cleanup_plan(repo, run_id, observations_path):
    """读取状态与观察，输出清理计划；不执行删除。"""
    state = read_state(run_state_path(repo, run_id))
    observations = json.loads(Path(observations_path).read_text(encoding='utf-8'))
    return build_cleanup_plan(state, observations)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Threadweave local helper (no host API calls, cleanup is dry-run)')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('preflight')
    p.add_argument('--repo', default='.')
    p = sub.add_parser('status')
    p.add_argument('--repo', default='.')
    p = sub.add_parser('inspect')
    p.add_argument('--repo', default='.')
    p = sub.add_parser('doctor')
    p.add_argument('--repo', default='.')
    p = sub.add_parser('cleanup-plan')
    p.add_argument('--repo', default='.')
    p.add_argument('--run', required=True)
    p.add_argument('--observations', required=True)
    args = parser.parse_args(argv)

    if args.command == 'preflight':
        result = preflight(args.repo)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(result.values()) else 1
    if args.command == 'status':
        result = status(args.repo)
    elif args.command == 'inspect':
        result = inspect(args.repo)
    elif args.command == 'doctor':
        result = doctor(args.repo)
    else:
        result = cleanup_plan(args.repo, args.run, args.observations)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())

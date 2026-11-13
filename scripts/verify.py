"""独立执行冻结验证计划，使用真实 Git 身份与有界子进程输出。"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from datetime import datetime, timezone

OUTPUT_LIMIT = 16384


def plan_hash(plan):
    return hashlib.sha256(json.dumps(
        plan, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode('utf-8')).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _run(argv, cwd, timeout):
    """持续排空管道，只保留前 OUTPUT_LIMIT 字节；超时杀整个进程组。"""
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    truncated = False
    with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as proc:
        with selectors.DefaultSelector() as selector:
            for name, stream in [('stdout', proc.stdout), ('stderr', proc.stderr)]:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            deadline = time.monotonic() + timeout
            timed_out = False
            while selector.get_map() or proc.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                for key, _ in selector.select(min(remaining, 0.05)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = output[key.data]
                    room = OUTPUT_LIMIT - len(buffer)
                    buffer.extend(chunk[:room])
                    truncated |= len(chunk) > room
            proc.wait()
        return dict(exitCode=proc.returncode, timedOut=timed_out,
                    outputTruncated=truncated,
                    **{k: bytes(v).decode('utf-8', errors='replace')
                       for k, v in output.items()})


def _git(root, *args):
    result = _run(['git', '-C', str(root), *args], root, 10)
    if result['exitCode'] or result['timedOut']:
        raise ValueError('Git 检查失败: ' + result['stderr'][:1000])
    return result['stdout'].strip()


def _snapshot(root):
    if Path(_git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('repo_root 必须是 Git checkout 根目录')
    return (_git(root, 'rev-parse', 'HEAD'),
            _git(root, 'status', '--porcelain', '--untracked-files=all'))


def _validate(plan, root, context):
    if not isinstance(plan, dict) or type(plan.get('version')) is not int or plan['version'] != 1:
        raise ValueError('plan.version 必须为 1')
    steps = plan.get('steps')
    if not isinstance(steps, list) or not steps:
        raise ValueError('plan.steps 不能为空')
    if not isinstance(context, dict):
        raise ValueError('context 必须为字典')
    for name in ('runId', 'role', 'head'):
        if not isinstance(context.get(name), str) or not context[name]:
            raise ValueError('context.' + name + ' 必须为非空字符串')
    for name, minimum in (('attempt', 1), ('taskRevision', 0)):
        if type(context.get(name)) is not int or context[name] < minimum:
            raise ValueError('context.' + name + ' 整数范围无效')
    if context.get('stage') not in ('worker', 'integration'):
        raise ValueError('context.stage 无效')
    by_id = {}
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError('step 必须为字典')
        ident = step.get('id')
        if not isinstance(ident, str) or not ident or ident in by_id:
            raise ValueError('step.id 为空或重复')
        argv = step.get('argv')
        if not isinstance(argv, list) or not argv or any(
                not isinstance(arg, str) or '\0' in arg for arg in argv) or not argv[0]:
            raise ValueError('step.argv 必须为非空字符串数组')
        cwd = step.get('cwd', '.')
        if not isinstance(cwd, str) or '\0' in cwd:
            raise ValueError('step.cwd 无效')
        resolved = (root / cwd).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_dir():
            raise ValueError('step.cwd 必须位于 repo_root 内且存在')
        timeout = step.get('timeoutSeconds')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('step.timeoutSeconds 必须为有限正数')
        if type(step.get('expectedExitCode', 0)) is not int:
            raise ValueError('step.expectedExitCode 必须为整数')
        if type(step.get('continueOnFailure', False)) is not bool:
            raise ValueError('step.continueOnFailure 必须为布尔值')
        deps = step.get('dependsOn', [])
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
            raise ValueError('step.dependsOn 必须为字符串数组')
        by_id[ident] = step
    ordered, visiting, visited = [], set(), set()

    def visit(ident):
        if ident not in by_id:
            raise ValueError('未知依赖: ' + ident)
        if ident in visiting:
            raise ValueError('计划存在循环依赖')
        if ident in visited:
            return
        visiting.add(ident)
        for dep in by_id[ident].get('dependsOn', []):
            visit(dep)
        visiting.remove(ident)
        visited.add(ident)
        ordered.append(by_id[ident])

    for ident in by_id:
        visit(ident)
    return ordered


def execute_plan(plan, repo_root, context):
    root = Path(repo_root).resolve()
    plan, context = copy.deepcopy(plan), copy.deepcopy(context)
    ordered = _validate(plan, root, context)
    digest = plan_hash(plan)
    before = _snapshot(root)
    if before[0] != context['head'] or before[1]:
        raise ValueError('执行前 HEAD 不匹配或工作区不干净')
    result = dict(status='passed', context=context, planHash=digest,
                  steps=[], startedAt=_now(), finishedAt=None)
    statuses = {}
    stopped = False
    for step in ordered:
        ident = step['id']
        if stopped or any(statuses[dep] != 'passed' for dep in step.get('dependsOn', [])):
            entry = dict(id=ident, status='skipped', exitCode=None)
        else:
            try:
                cwd = (root / step.get('cwd', '.')).resolve()
                if not cwd.is_relative_to(root) or not cwd.is_dir():
                    raise ValueError('执行时 step.cwd 已越界或不存在')
                entry = _run(step['argv'], cwd, step['timeoutSeconds'])
                entry.update(id=ident, status='timeout' if entry['timedOut'] else (
                    'passed' if entry['exitCode'] == step.get('expectedExitCode', 0) else 'failed'))
            except (OSError, ValueError) as exc:
                entry = dict(id=ident, status='failed', exitCode=None,
                             stderr=str(exc)[:OUTPUT_LIMIT])
            if entry['status'] != 'passed':
                result['status'] = 'failed'
                stopped = not step.get('continueOnFailure', False)
        statuses[ident] = entry['status']
        result['steps'].append(entry)
    try:
        after = _snapshot(root)
        if after != before or after[1]:
            result.update(status='failed', error='执行后 HEAD 或工作区状态变化')
    except ValueError as exc:
        result.update(status='failed', error=str(exc))
    result['finishedAt'] = _now()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--context', required=True)
    args = parser.parse_args()
    try:
        with open(args.plan, encoding='utf-8') as stream:
            plan = json.load(stream)
        with open(args.context, encoding='utf-8') as stream:
            context = json.load(stream)
        result = execute_plan(plan, args.repo, context)
    except (ValueError, OSError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

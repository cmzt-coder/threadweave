"""O16b 有界本地 worktree 清理：默认预览，显式授权后即时重验并非强制移除。

输入证据由 leader 提供，本模块不将静态停止声明冒充实时宿主查询。
仅清理单个专用 worktree，保留分支、报告、聊天及 automation。
"""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from cleanup import cleanup_plan_fingerprint


class CleanupBlocked(ValueError):
    pass


def _git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
    if result.returncode:
        raise CleanupBlocked(result.stderr.strip() or 'Git observation failed')
    return result.stdout.strip()


def _outside(path, target):
    return path != target and target not in path.parents


def preview(declaration):
    """直接观察 Git；核对新鲜、绑定 HEAD 的 leader 验收/停止与归档证据。"""
    d = declaration
    if not isinstance(d, dict):
        raise CleanupBlocked('declaration must be an object')
    required = ('runId', 'repository', 'worktree', 'branch', 'head',
                'integrationRef', 'report', 'reportSha256', 'evidence')
    if any(not isinstance(d.get(k), str) or not d[k] for k in required):
        raise CleanupBlocked('missing cleanup identity')
    if (not re.fullmatch(r'[0-9a-f]{40}', d['head'])
            or d['integrationRef'].startswith('-') or d['branch'].startswith('-')):
        raise CleanupBlocked('invalid SHA or ref')
    raw = Path(d['worktree'])
    target = raw.resolve()
    if not raw.is_absolute() or str(raw) != str(target):
        raise CleanupBlocked('worktree must be canonical and not a symlink')
    repo = Path(d['repository']).resolve()
    if repo == target or not _outside(Path.cwd().resolve(), target):
        raise CleanupBlocked('primary or active cwd is protected')
    listing = _git(repo, 'worktree', 'list', '--porcelain')
    blocks = [dict(line.split(' ', 1) if ' ' in line else (line, '')
                   for line in block.splitlines()) for block in listing.split('\n\n')]
    found = [b for b in blocks if b.get('worktree') == str(target)]
    if not found or blocks[0].get('worktree') == str(target):
        raise CleanupBlocked('unregistered or primary worktree')
    if found[0].get('locked') is not None or found[0].get('prunable') is not None:
        raise CleanupBlocked('locked/shared or prunable worktree')
    if _git(target, 'rev-parse', '--show-toplevel') != str(target):
        raise CleanupBlocked('root mismatch')
    common = Path(_git(target, 'rev-parse', '--git-common-dir'))
    if not common.is_absolute():
        common = target / common
    repo_common = Path(_git(repo, 'rev-parse', '--git-common-dir'))
    if not repo_common.is_absolute():
        repo_common = repo / repo_common
    if common.resolve() != repo_common.resolve():
        raise CleanupBlocked('repository mismatch')
    if _git(target, 'branch', '--show-current') != d['branch']:
        raise CleanupBlocked('branch mismatch')
    head = _git(target, 'rev-parse', 'HEAD')
    if head != d['head']:
        raise CleanupBlocked('HEAD drift')
    # --ignored 防止非强制 Git 清理时连同被忽略的本地成果一起移除。
    if _git(target, 'status', '--porcelain', '--untracked-files=all', '--ignored'):
        raise CleanupBlocked('dirty or ignored files protected')
    integration = _git(repo, 'rev-parse', '--verify', d['integrationRef'] + '^{commit}')
    _git(repo, 'merge-base', '--is-ancestor', head, integration)
    report, evidence_path = (Path(d[k]).resolve() for k in ('report', 'evidence'))
    if not all(_outside(x, target) for x in (report, evidence_path)):
        raise CleanupBlocked('evidence must be externalized')
    report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
    if report_hash != d['reportSha256']:
        raise CleanupBlocked('report hash mismatch')
    evidence_bytes = evidence_path.read_bytes()
    e = json.loads(evidence_bytes)
    observed = datetime.fromisoformat(e['observedAt'].replace('Z', '+00:00'))
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if not 0 <= age <= 300:
        raise CleanupBlocked('stale or future evidence')
    for key in ('runId', 'worktree', 'branch', 'head'):
        if e.get(key) != d[key]:
            raise CleanupBlocked('evidence identity mismatch: ' + key)
    if (e.get('ownedByRun') is not True or e.get('shared') is not False
            or e.get('acceptanceVerified') is not True
            or e.get('reportSha256') != report_hash):
        raise CleanupBlocked('ownership, acceptance or report evidence missing')
    stop = e.get('stop', {})
    if e.get('executionKind') == 'local_git':
        if e.get('threadId') is not None or stop.get('localProcessStopped') is not True:
            raise CleanupBlocked('local process stop evidence incomplete')
    elif e.get('executionKind') == 'host_thread':
        if not e.get('threadId') or not all(stop.get(k) is True for k in (
                'threadIdle', 'finalAnswerPresent', 'noActiveTurn')):
            raise CleanupBlocked('thread stop evidence incomplete')
    else:
        raise CleanupBlocked('explicit execution kind required')
    plan = {'version': 1, 'declaration': d, 'integrationHead': integration,
            'evidenceSha256': hashlib.sha256(evidence_bytes).hexdigest(),
            'classification': 'SAFE', 'action': 'git worktree remove',
            'preserveBranch': True}
    return {'plan': plan, 'fingerprint': cleanup_plan_fingerprint(plan),
            'readOnly': True, 'deletionPerformed': False}


def apply(declaration, approved, *, authorized=False):
    """先重新完整预览并比较精确计划；仅调用不带 force 的 Git worktree remove。"""
    if authorized is not True:
        raise CleanupBlocked('explicit execution authorization required')
    if not isinstance(approved, dict) or not isinstance(approved.get('plan'), dict):
        raise CleanupBlocked('approved plan must be an object')
    current = preview(declaration)
    if (approved.get('fingerprint') != cleanup_plan_fingerprint(approved.get('plan'))
            or current['fingerprint'] != approved['fingerprint']):
        raise CleanupBlocked('approved plan drift; preview again')
    _git(declaration['repository'], 'worktree', 'remove', declaration['worktree'])
    return {'status': 'removed', 'worktree': declaration['worktree'],
            'fingerprint': current['fingerprint'], 'deletionPerformed': True,
            'branchPreserved': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('declaration', type=Path)
    parser.add_argument('--approved-plan', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        declaration = json.loads(args.declaration.read_text())
        target = Path(declaration['worktree']).resolve()
        if not _outside(args.declaration.resolve(), target) or (
                args.approved_plan and not _outside(args.approved_plan.resolve(), target)):
            raise CleanupBlocked('declaration and approved plan must be externalized')
        if args.execute:
            if not args.approved_plan:
                raise CleanupBlocked('--execute requires --approved-plan')
            result = apply(declaration, json.loads(args.approved_plan.read_text()), authorized=True)
        else:
            result = preview(declaration)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'blocked', 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Worker 启动门禁校验参考实现。

纯函数，不调用宿主 API；供测试与控制器复用，避免门禁逻辑散落。
"""
from pathlib import Path


def canonical(path):
    return Path(path).resolve()


def validate_startup(report, expected):
    """按 SKILL.md 启动门禁校验 worker 报告，返回 (ok, reasons)。

    检查项：cwd 位于 worker root 内、Git common dir 与 leader 一致、分支匹配、
    HEAD 精确等于 base、工作区干净、worker root 不与 leader/其他 worker 重叠。
    """
    reasons = []
    cwd = canonical(report['cwd'])
    root = canonical(report['worktree_root'])
    try:
        cwd.relative_to(root)
    except ValueError:
        reasons.append('cwd_outside_worktree_root')
    if canonical(report['git_common_dir']) != canonical(expected['common_dir']):
        reasons.append('git_common_dir_mismatch')
    if report['branch'] != expected['branch']:
        reasons.append('branch_mismatch')
    if report['head'].strip() != expected['base'].strip():
        reasons.append('head_not_base')
    expected_root = expected.get('worker_root')
    if expected_root is not None and root != canonical(expected_root):
        reasons.append('worktree_root_mismatch')
    if report['status_porcelain'].strip():
        reasons.append('dirty_worktree')
    for other in expected['other_roots']:
        o = canonical(other)
        if root == o or root in o.parents or o in root.parents:
            reasons.append('worktree_root_overlap')
            break
    return (not reasons, reasons)

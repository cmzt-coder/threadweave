"""项目发现解析参考实现：要求仓库根目录唯一匹配一个 Git 项目。"""
from pathlib import Path


class ProjectResolutionError(Exception):
    """无匹配、多匹配或元数据不足。"""


def _canonical(value):
    return str(Path(value).resolve()) if value else ''


def resolve_project(projects, repo_root, host_id=None):
    """按 canonical 仓库根、host 与 isGitRepository 过滤，要求唯一匹配。"""
    canonical_root = _canonical(repo_root)
    matches = []
    for project in projects:
        if not project.get('isGitRepository'):
            continue
        if not project.get('projectId'):
            continue
        if host_id is not None and project.get('hostId') != host_id:
            continue
        if _canonical(project.get('path')) == canonical_root:
            matches.append(project)
    if not matches:
        raise ProjectResolutionError('no matching git project')
    if len(matches) > 1:
        raise ProjectResolutionError(f'multiple matching projects: {len(matches)}')
    return matches[0]

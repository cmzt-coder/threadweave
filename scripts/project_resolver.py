"""项目发现解析参考实现：要求仓库根目录唯一匹配一个 Git 项目。"""
from pathlib import Path


class ProjectResolutionError(Exception):
    """无匹配、多匹配或元数据不足。"""


def _canonical(value):
    return str(Path(value).resolve()) if value else ''


def resolve_project(projects, repo_root):
    """按 canonical 仓库根与 isGitRepository 过滤，要求唯一匹配。"""
    canonical_root = _canonical(repo_root)
    matches = [
        project for project in projects
        if project.get('isGitRepository')
        and _canonical(project.get('path')) == canonical_root
    ]
    if not matches:
        raise ProjectResolutionError('no matching git project')
    if len(matches) > 1:
        raise ProjectResolutionError(f'multiple matching projects: {len(matches)}')
    return matches[0]

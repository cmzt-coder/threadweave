"""O08 稳定契约冻结与漂移检测。

契约路径来自显式项目配置；对每个精确文件记录 SHA256。不使用 verify plan
推导契约授权，不修改文件，也不自动批准变更。
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


class ContractError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _path(value):
    if not isinstance(value, str) or not value or value.startswith('/'):
        raise ContractError('contract path must be repository-relative')
    if '\x00' in value or '\\' in value:
        raise ContractError('contract path contains invalid characters')
    path = Path(value)
    if '..' in path.parts or any(ch in value for ch in '*?[]{}'):
        raise ContractError('contract path must be an exact repository-relative file')
    return path.as_posix()


def load_stable_contracts(repo_root, config_path=None):
    """读取 threadweave.config.json 的 stableContracts；缺省为空。"""
    repo_root = Path(repo_root).resolve()
    path = Path(config_path) if config_path is not None else repo_root / 'threadweave.config.json'
    path = path.resolve()
    if path.parent != repo_root:
        raise ContractError('contracts config must stay in repository root')
    if not path.is_file():
        return []
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ContractError(f'invalid project config JSON: {exc}') from exc
    values = config.get('stableContracts', [])
    if not isinstance(values, list):
        raise ContractError('stableContracts must be a list')
    result = []
    for value in values:
        normalized = _path(value)
        if normalized not in result:
            result.append(normalized)
    return result


def _safe_file(worktree, relative):
    root = Path(worktree).resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ContractError(f'contract path escapes worktree: {relative}')
    raw = root / relative
    if raw.is_symlink():
        raise ContractError(f'contract path must not be a symlink: {relative}')
    if not target.is_file():
        raise ContractError(f'contract file missing: {relative}')
    return target


def hash_contracts(worktree, paths):
    """计算精确契约文件哈希；路径缺失或越界时拒绝。"""
    hashes = {}
    for relative in paths:
        relative = _path(relative)
        target = _safe_file(worktree, relative)
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    canonical = json.dumps(hashes, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    return {
        'paths': list(paths),
        'hashes': hashes,
        'contractHash': hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
        'frozenAt': _now(),
    }


def check_contracts(worktree, freeze):
    """返回未批准契约漂移；drift 为空表示当前文件仍匹配冻结值。"""
    if not isinstance(freeze, dict) or not isinstance(freeze.get('paths'), list):
        raise ContractError('invalid contract freeze')
    current = hash_contracts(worktree, freeze['paths'])
    changed = [
        path for path in sorted(set(freeze.get('hashes', {})) | set(current['hashes']))
        if freeze.get('hashes', {}).get(path) != current['hashes'].get(path)
    ]
    return {
        'changed': changed,
        'current': current,
        'expectedHash': freeze.get('contractHash'),
        'currentHash': current['contractHash'],
    }

"""Threadweave 报告与 notes 归档参考实现。

把 worker 的 report/notes 复制到 leader checkout 内的 durable run 目录，并生成
带 SHA256 的 manifest。纯文件操作，不删除源文件，不调用宿主 API。
"""
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from state_store import write_state_atomic


class ArchiveConflict(Exception):
    """目标已存在且内容不同，拒绝覆盖证据。"""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _load_manifest(manifest_path):
    if not manifest_path.is_file():
        return []
    return json.loads(manifest_path.read_text(encoding='utf-8'))


def _safe_segment(value, name):
    if not isinstance(value, str) or not value or value in ('.', '..'):
        raise ValueError(f'invalid {name}')
    if '/' in value or '\\' in value or Path(value).is_absolute():
        raise ValueError(f'invalid {name}')
    return value


def archive_artifacts(run_dir, role, files):
    """归档一组 artifact；返回本次写入的 manifest 条目。"""
    run_dir = Path(run_dir)
    _safe_segment(role, 'role')
    target_dir = run_dir / 'reports' / role
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / 'archive-manifest.json'
    entries = []
    for name, source in files.items():
        _safe_segment(name, 'artifact name')
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(source)
        target = target_dir / name
        source_sha = _sha256(source)
        if target.exists():
            if _sha256(target) != source_sha:
                raise ArchiveConflict(f'{target} exists with different content')
        else:
            shutil.copy2(source, target)
        entries.append({
            'role': role,
            'name': name,
            'source': str(source),
            'target': str(target),
            'sha256': source_sha,
            'size': source.stat().st_size,
            'archivedAt': _now_iso(),
        })
    merged = {(e['role'], e['name']): e for e in _load_manifest(manifest_path)}
    for entry in entries:
        merged[(entry['role'], entry['name'])] = entry
    write_state_atomic(manifest_path, sorted(merged.values(), key=lambda e: (e['role'], e['name'])))
    return entries


def verify_archive(run_dir, expected):
    """检查期望的 role/name 是否已归档且哈希一致；返回问题列表。"""
    manifest_path = Path(run_dir) / 'archive-manifest.json'
    if not manifest_path.is_file():
        return [f'missing manifest: {manifest_path}']
    indexed = {(e['role'], e['name']): e for e in _load_manifest(manifest_path)}
    problems = []
    for role, name in expected:
        entry = indexed.get((role, name))
        if entry is None:
            problems.append(f'missing entry: {role}/{name}')
            continue
        target = Path(entry['target'])
        if not target.is_file():
            problems.append(f'missing file: {target}')
        elif _sha256(target) != entry['sha256']:
            problems.append(f'hash mismatch: {target}')
    return problems

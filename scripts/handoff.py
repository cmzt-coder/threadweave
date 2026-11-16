"""O09 不可变制品交接。

制品先复制到 run 外的 durable archive，再写入带生产者、接收者、源 SHA、
任务修订、路径和 SHA256 的 manifest。消费者使用前必须校验 manifest 和归档字节。
本模块不调度消费者、不删除生产文件，也不调用宿主 API。
"""
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from state_store import write_state_atomic


SHA_RE = re.compile(r'^[0-9a-f]{40}$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')


class HandoffError(ValueError):
    pass


class HandoffConflict(HandoffError):
    pass


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise HandoffError(f'{name} must be a non-empty string')
    return value.strip()


def _sha(value, name, pattern=SHA_RE):
    value = _text(value, name)
    if not pattern.fullmatch(value):
        raise HandoffError(f'{name} has invalid SHA format')
    return value


def _relative(value):
    if not isinstance(value, str) or not value or value.startswith('/'):
        raise HandoffError('artifact path must be repository-relative')
    if '\x00' in value or '\\' in value:
        raise HandoffError('artifact path contains invalid characters')
    path = Path(value)
    if '..' in path.parts:
        raise HandoffError('artifact path escapes repository')
    return path.as_posix()


def _file_sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _safe_target(archive_root, relative):
    root = Path(archive_root).resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise HandoffError('archive path escapes archive root')
    return target


def archive_handoff(source_root, archive_root, run_id, producer, attempt,
                    source_sha, task_revision, receiver, purpose, artifact_paths,
                    created_at=None):
    """归档精确文件并生成不可变 handoff manifest。"""
    source_root = Path(source_root).resolve()
    archive_root = Path(archive_root).resolve()
    run_id = _text(run_id, 'run_id')
    producer = _text(producer, 'producer')
    receiver = _text(receiver, 'receiver')
    purpose = _text(purpose, 'purpose')
    source_sha = _sha(source_sha, 'source_sha')
    if type(attempt) is not int or attempt < 1:
        raise HandoffError('attempt must be a positive integer')
    if type(task_revision) is not int or task_revision < 0:
        raise HandoffError('task_revision must be non-negative')
    if not isinstance(artifact_paths, list) or not artifact_paths:
        raise HandoffError('artifact_paths must be a non-empty list')
    paths = []
    for value in artifact_paths:
        value = _relative(value)
        if value not in paths:
            paths.append(value)

    artifacts = []
    for relative in paths:
        source = (source_root / relative).resolve()
        if source != source_root and source_root not in source.parents:
            raise HandoffError('artifact path escapes source root')
        raw = source_root / relative
        if raw.is_symlink() or not source.is_file():
            raise HandoffError(f'artifact is not a regular file: {relative}')
        sha = _file_sha(source)
        size = source.stat().st_size
        artifact_id = hashlib.sha256(
            f'{run_id}\0{relative}\0{sha}'.encode('utf-8')).hexdigest()[:32]
        archive_relative = str(Path('artifacts') / run_id / artifact_id / Path(relative).name)
        target = _safe_target(archive_root, archive_relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _file_sha(target) != sha:
                raise HandoffConflict(f'archive target already has different content: {target}')
        else:
            temp = target.with_suffix(target.suffix + '.tmp')
            try:
                shutil.copy2(source, temp)
                if _file_sha(temp) != sha:
                    raise HandoffError('archive copy hash mismatch')
                temp.replace(target)
            finally:
                if temp.exists():
                    temp.unlink()
        artifacts.append({
            'sourcePath': relative,
            'archivePath': archive_relative,
            'sha256': sha,
            'size': size,
        })

    manifest = {
        'schemaVersion': 1,
        'runId': run_id,
        'producer': {'role': producer, 'attempt': attempt,
                     'sourceSha': source_sha, 'taskRevision': task_revision},
        'receiver': {'role': receiver},
        'purpose': purpose,
        'artifacts': artifacts,
        'createdAt': created_at or _now(),
    }
    manifest['handoffId'] = 'handoff_' + hashlib.sha256(
        _canonical(manifest).encode('utf-8')).hexdigest()[:24]
    manifest_path = archive_root / 'handoffs' / manifest['handoffId'] / 'manifest.json'
    write_state_atomic(manifest_path, manifest)
    return {'manifest': manifest, 'manifestPath': str(manifest_path)}


def load_manifest(manifest_path):
    path = Path(manifest_path)
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or value.get('schemaVersion') != 1
            or not isinstance(value.get('handoffId'), str)
            or not isinstance(value.get('artifacts'), list)):
        raise HandoffError('invalid handoff manifest')
    payload = dict(value)
    handoff_id = payload.pop('handoffId', None)
    expected = 'handoff_' + hashlib.sha256(
        _canonical(payload).encode('utf-8')).hexdigest()[:24]
    if handoff_id != expected:
        raise HandoffError('handoff manifest hash mismatch')
    return value


def verify_handoff(manifest_path, archive_root, run_id=None,
                   producer=None, receiver=None, purpose=None):
    """消费者使用前校验身份、归档路径、大小和 SHA。"""
    manifest = load_manifest(manifest_path)
    archive_root = Path(archive_root).resolve()
    if run_id is not None and manifest.get('runId') != run_id:
        raise HandoffError('handoff run identity mismatch')
    if producer is not None and manifest.get('producer', {}).get('role') != producer:
        raise HandoffError('handoff producer mismatch')
    if receiver is not None and manifest.get('receiver', {}).get('role') != receiver:
        raise HandoffError('handoff receiver mismatch')
    if purpose is not None and manifest.get('purpose') != purpose:
        raise HandoffError('handoff purpose mismatch')
    for item in manifest['artifacts']:
        if not isinstance(item, dict):
            raise HandoffError('invalid artifact entry')
        relative = _relative(item.get('archivePath'))
        expected = _sha(item.get('sha256'), 'artifact sha256', SHA256_RE)
        target = _safe_target(archive_root, relative)
        if not target.is_file():
            raise HandoffError(f'archived artifact missing: {relative}')
        if target.stat().st_size != item.get('size'):
            raise HandoffError(f'archived artifact size mismatch: {relative}')
        if _file_sha(target) != expected:
            raise HandoffError(f'archived artifact hash mismatch: {relative}')
    return manifest

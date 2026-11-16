"""O14 绑定 SHA 的交付状态。

记录 verified、landed、pushVerified 三段证据。纯逻辑模块不执行 Git push，
也不从无 SHA 的字符串推断交付成功。
"""
import re
from datetime import datetime, timezone


SHA_RE = re.compile(r'^[0-9a-f]{40}$')
GIT_REF_RE = re.compile(r'^refs/(heads|tags)/[^\s~^:?*\\\[]+$')
DELIVERY_STAGES = ('verified', 'landed', 'pushVerified')


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')
    return value.strip()


def _sha(value, name):
    value = _text(value, name)
    if not SHA_RE.fullmatch(value):
        raise ValueError(f'{name} must be a 40-character lowercase SHA')
    return value


def _remote(value):
    value = _text(value, 'remote')
    if any(char.isspace() or char in '~^:?*\\[]' for char in value):
        raise ValueError('remote contains invalid characters')
    return value


def _ref(value):
    value = _text(value, 'ref')
    if (not GIT_REF_RE.fullmatch(value) or '..' in value or '//' in value
            or '@{' in value or value.endswith(('.', '.lock'))):
        raise ValueError('ref must be a valid refs/heads or refs/tags name')
    return value


def ensure_delivery(state):
    delivery = state.setdefault('delivery', {})
    if not isinstance(delivery, dict):
        raise ValueError('delivery must be an object')
    for stage in DELIVERY_STAGES:
        delivery.setdefault(stage, [])
        if not isinstance(delivery[stage], list):
            raise ValueError(f'delivery.{stage} must be a list')
    return delivery


def _append_unique(records, record, key_fields):
    key = tuple(record.get(field) for field in key_fields)
    for existing in records:
        if tuple(existing.get(field) for field in key_fields) == key:
            return existing
    records.append(record)
    return record


def add_verified(state, stage, role, attempt, task_revision, sha,
                 report_sha=None, plan_hash=None, verified_at=None):
    """记录 worker 或 integration 阶段的独立验收 SHA。"""
    if stage not in ('worker', 'integration'):
        raise ValueError('verified stage must be worker or integration')
    role = _text(role, 'role')
    if type(attempt) is not int or attempt < 1:
        raise ValueError('attempt must be a positive integer')
    if type(task_revision) is not int or task_revision < 0:
        raise ValueError('task_revision must be non-negative')
    sha = _sha(sha, 'sha')
    if report_sha is not None:
        report_sha = _text(report_sha, 'report_sha')
        if not re.fullmatch(r'[0-9a-f]{64}', report_sha):
            raise ValueError('report_sha must be a SHA256')
    if plan_hash is not None:
        plan_hash = _text(plan_hash, 'plan_hash')
        if not re.fullmatch(r'[0-9a-f]{64}', plan_hash):
            raise ValueError('plan_hash must be a SHA256')
    record = {
        'stage': stage,
        'role': role,
        'attempt': attempt,
        'taskRevision': task_revision,
        'sha': sha,
        'verifiedAt': verified_at or _now(),
    }
    if report_sha is not None:
        record['reportSha'] = report_sha
    if plan_hash is not None:
        record['planHash'] = plan_hash
    delivery = ensure_delivery(state)
    return _append_unique(
        delivery['verified'], record,
        ('stage', 'role', 'attempt', 'taskRevision', 'sha', 'reportSha', 'planHash'))


def add_landed(state, branch, sha, before_sha, task_revision, plan_hash,
               verification='independent acceptance passed', landed_at=None):
    """记录本地集成成功；不代表远端已推送。"""
    branch = _text(branch, 'branch')
    sha = _sha(sha, 'sha')
    before_sha = _sha(before_sha, 'before_sha')
    if type(task_revision) is not int or task_revision < 0:
        raise ValueError('task_revision must be non-negative')
    plan_hash = _text(plan_hash, 'plan_hash')
    if not re.fullmatch(r'[0-9a-f]{64}', plan_hash):
        raise ValueError('plan_hash must be a SHA256')
    record = {
        'branch': branch,
        'sha': sha,
        'beforeSha': before_sha,
        'taskRevision': task_revision,
        'planHash': plan_hash,
        'verification': _text(verification, 'verification'),
        'landedAt': landed_at or _now(),
    }
    delivery = ensure_delivery(state)
    return _append_unique(
        delivery['landed'], record,
        ('branch', 'sha', 'beforeSha', 'taskRevision', 'planHash'))


def add_push_verified(state, sha, remote, ref, remote_sha, authorization_evidence,
                      observed_at=None):
    """记录已经实际观察到的远端 ref；本函数本身绝不执行 push。"""
    sha = _sha(sha, 'sha')
    remote_sha = _sha(remote_sha, 'remote_sha')
    if sha != remote_sha:
        raise ValueError('remote SHA must equal landed SHA')
    remote = _remote(remote)
    ref = _ref(ref)
    authorization_evidence = _text(
        authorization_evidence, 'authorization_evidence')
    delivery = ensure_delivery(state)
    if not any(item.get('sha') == sha for item in delivery['landed']):
        raise ValueError('pushVerified requires a matching landed record')
    record = {
        'sha': sha,
        'remote': remote,
        'ref': ref,
        'remoteSha': remote_sha,
        'observedAt': observed_at or _now(),
        'authorizationEvidence': authorization_evidence,
        'recordedAt': _now(),
    }
    return _append_unique(
        delivery['pushVerified'], record,
        ('sha', 'remote', 'ref', 'remoteSha'))


def delivery_status(state, current_sha=None):
    """返回当前 HEAD 可见的交付阶段；旧 SHA 记录保留但不冒充当前状态。"""
    delivery = state.get('delivery') if isinstance(state.get('delivery'), dict) else {}
    integration = state.get('integration') if isinstance(state.get('integration'), dict) else {}
    current_sha = current_sha or integration.get('currentSha') or integration.get('baseSha')
    verified = [x for x in delivery.get('verified', []) if x.get('sha') == current_sha]
    landed = [x for x in delivery.get('landed', []) if x.get('sha') == current_sha]
    pushed = [x for x in delivery.get('pushVerified', []) if x.get('sha') == current_sha]
    latest_verified = verified[-1] if verified else None
    latest_landed = landed[-1] if landed else None
    latest_pushed = pushed[-1] if pushed else None
    stage = 'unverified'
    if latest_pushed:
        stage = 'pushVerified'
    elif latest_landed:
        stage = 'landed'
    elif latest_verified:
        stage = 'verified'
    stale = {
        'verified': [x for x in delivery.get('verified', []) if x.get('sha') != current_sha],
        'landed': [x for x in delivery.get('landed', []) if x.get('sha') != current_sha],
        'pushVerified': [x for x in delivery.get('pushVerified', []) if x.get('sha') != current_sha],
    }
    return {
        'currentSha': current_sha,
        'stage': stage,
        'verified': latest_verified,
        'landed': latest_landed,
        'pushVerified': latest_pushed,
        'stale': stale,
    }

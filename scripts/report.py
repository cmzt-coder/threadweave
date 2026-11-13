"""Worker JSON 报告校验。

本模块只校验报告结构和调用方提供的独立 Git 观察，不执行 Git，也不生成
acceptance。报告自报的测试结果只保留为 ``selfReportedVerification``，是否验收
通过必须由 leader 的独立验证流程决定。
"""
import copy
import hashlib
import json
import re
from collections import Counter


REQUIRED_REPORT_FIELDS = (
    'runId',
    'role',
    'attempt',
    'threadId',
    'branch',
    'head',
    'reportSha',
    'taskRevision',
    'baseIsAncestor',
    'filesChanged',
    'testsRun',
    'selfReportedVerification',
    'contractChanges',
    'knownRisks',
)
REQUIRED_EXPECTED_FIELDS = (
    'runId',
    'role',
    'attempt',
    'threadId',
    'branch',
    'taskRevision',
)
STRING_LIST_FIELDS = (
    'testsRun',
    'selfReportedVerification',
    'contractChanges',
    'knownRisks',
)
HEAD_RE = re.compile(r'^[0-9a-f]{40}$')
REPORT_SHA_RE = re.compile(r'^[0-9a-f]{64}$')


def _fail(path, message):
    raise ValueError(f'{path}: {message}')


def _mapping(value, path):
    if not isinstance(value, dict):
        _fail(path, 'expected object')
    return value


def _present(mapping, field, path, legacy_hint=False):
    if field not in mapping:
        suffix = '; legacy report format must be resubmitted' if legacy_hint else ''
        _fail(f'{path}.{field}', f'missing required field{suffix}')
    return mapping[field]


def _string(value, path):
    if not isinstance(value, str):
        _fail(path, 'expected string')
    if not value.strip():
        _fail(path, 'must not be empty')
    return value


def _integer(value, path, minimum):
    # bool 是 int 的子类，必须显式排除，避免 True == 1 绕过修订号校验。
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(path, 'expected integer')
    if value < minimum:
        _fail(path, f'must be >= {minimum}')
    return value


def _sha(value, path, pattern, label):
    _string(value, path)
    if not pattern.fullmatch(value):
        _fail(path, f'invalid {label}')
    return value


def _identity_field(mapping, field, path):
    value = mapping[field]
    if field in ('runId', 'role', 'threadId', 'branch'):
        return _string(value, f'{path}.{field}')
    if field == 'attempt':
        return _integer(value, f'{path}.{field}', 1)
    if field == 'taskRevision':
        return _integer(value, f'{path}.{field}', 0)
    if field == 'head':
        return _sha(value, f'{path}.{field}', HEAD_RE, 'Git SHA')
    _fail(f'{path}.{field}', 'unsupported identity field')


def _string_list(value, path, unique=False):
    if not isinstance(value, list):
        _fail(path, 'expected array')
    seen = set()
    for index, item in enumerate(value):
        item_path = f'{path}[{index}]'
        _string(item, item_path)
        if unique and item in seen:
            _fail(item_path, 'duplicate changed file path')
        seen.add(item)
    return value


def _validate_payload_shape(payload):
    _mapping(payload, 'payload')
    for field in REQUIRED_REPORT_FIELDS:
        _present(payload, field, 'payload', legacy_hint=True)

    for field in ('runId', 'role', 'attempt', 'threadId', 'branch', 'head',
                  'taskRevision'):
        _identity_field(payload, field, 'payload')

    _sha(payload['reportSha'], 'payload.reportSha', REPORT_SHA_RE, 'report SHA')
    if not isinstance(payload['baseIsAncestor'], bool):
        _fail('payload.baseIsAncestor', 'expected boolean')

    _string_list(payload['filesChanged'], 'payload.filesChanged', unique=True)
    for field in STRING_LIST_FIELDS:
        _string_list(payload[field], f'payload.{field}')

    if 'commitSha' in payload:
        commit_sha = _sha(
            payload['commitSha'], 'payload.commitSha', HEAD_RE, 'Git SHA')
        if commit_sha != payload['head']:
            _fail('payload.commitSha', 'must equal payload.head')

    if 'acceptedAt' in payload:
        _string(payload['acceptedAt'], 'payload.acceptedAt')


def _validate_expected(expected):
    _mapping(expected, 'expected')
    for field in REQUIRED_EXPECTED_FIELDS:
        _present(expected, field, 'expected')
    for field in REQUIRED_EXPECTED_FIELDS:
        _identity_field(expected, field, 'expected')
    if 'head' in expected:
        _identity_field(expected, 'head', 'expected')


def _validate_observed(observed):
    _mapping(observed, 'observed')
    for field in ('head', 'branch', 'baseIsAncestor', 'filesChanged'):
        _present(observed, field, 'observed')

    _identity_field(observed, 'head', 'observed')
    _identity_field(observed, 'branch', 'observed')
    if not isinstance(observed['baseIsAncestor'], bool):
        _fail('observed.baseIsAncestor', 'expected boolean')
    _string_list(observed['filesChanged'], 'observed.filesChanged', unique=True)


def report_hash(payload):
    """返回报告中除 ``reportSha`` 与 ``acceptedAt`` 外 canonical JSON 的 SHA256。"""
    _mapping(payload, 'payload')
    hashed = {
        key: value
        for key, value in payload.items()
        if key not in ('reportSha', 'acceptedAt')
    }
    try:
        canonical = json.dumps(
            hashed,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(',', ':'),
        )
    except (TypeError, ValueError) as exc:
        _fail('payload', f'must be JSON serializable: {exc}')
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def validate_report(payload, expected, observed):
    """校验报告结构、内容哈希、身份和独立 Git 观察。

    ``expected`` 至少包含派发时已知的身份字段；``observed`` 必须来自 leader
    对实际 Git 状态的独立采集。成功时返回深拷贝后的报告，不增加 acceptance
    字段，也不修改任何入参。
    """
    _validate_payload_shape(payload)
    _validate_expected(expected)
    _validate_observed(observed)

    for field in REQUIRED_EXPECTED_FIELDS:
        if payload[field] != expected[field]:
            _fail(
                f'payload.{field}',
                f'does not match expected.{field}',
            )
    if 'head' in expected and payload['head'] != expected['head']:
        _fail('payload.head', 'does not match expected.head')

    if payload['head'] != observed['head']:
        _fail('payload.head', 'does not match observed.head')
    if payload['branch'] != observed['branch']:
        _fail('payload.branch', 'does not match observed.branch')

    if observed['baseIsAncestor'] is not True:
        _fail('observed.baseIsAncestor', 'base is not an ancestor of head')
    if payload['baseIsAncestor'] != observed['baseIsAncestor']:
        _fail(
            'payload.baseIsAncestor',
            'does not match observed.baseIsAncestor',
        )

    if Counter(payload['filesChanged']) != Counter(observed['filesChanged']):
        _fail(
            'payload.filesChanged',
            'does not match observed.filesChanged (exact, order-insensitive)',
        )

    actual_hash = report_hash(payload)
    if payload['reportSha'] != actual_hash:
        _fail('payload.reportSha', f'must equal report_hash(payload) {actual_hash}')

    return copy.deepcopy(payload)

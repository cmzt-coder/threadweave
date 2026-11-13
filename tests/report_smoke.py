"""验证报告结构、canonical 哈希、身份门禁与独立 Git 观察。"""
import copy
import hashlib
import json
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from report import report_hash, validate_report  # noqa: E402


results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def expect_value_error(name, function, *args, path=None):
    try:
        function(*args)
    except ValueError as exc:
        check(name, path in str(exc) if path else True)
        return str(exc)
    raise AssertionError(name)


def make_payload():
    payload = {
        'runId': 'run-report',
        'role': 'report',
        'attempt': 1,
        'threadId': 'thread-report',
        'branch': 'codex/r4-report',
        'head': 'a' * 40,
        'reportSha': '0' * 64,
        'taskRevision': 3,
        'baseIsAncestor': True,
        'filesChanged': ['scripts/report.py', 'tests/report_smoke.py'],
        'testsRun': ['report-smoke', 'run-all'],
        'selfReportedVerification': ['report smoke passed'],
        'contractChanges': [],
        'knownRisks': ['host model identity unknown'],
    }
    payload['reportSha'] = report_hash(payload)
    return payload


def make_expected(payload):
    return {
        'runId': payload['runId'],
        'role': payload['role'],
        'attempt': payload['attempt'],
        'threadId': payload['threadId'],
        'branch': payload['branch'],
        'taskRevision': payload['taskRevision'],
    }


def make_observed(payload):
    return {
        'head': payload['head'],
        'branch': payload['branch'],
        'baseIsAncestor': True,
        'filesChanged': list(reversed(payload['filesChanged'])),
    }


payload = make_payload()
expected = make_expected(payload)
observed = make_observed(payload)

# canonical 哈希：对象键顺序无关，并排除自身哈希与接收时间。
reordered = {key: copy.deepcopy(payload[key]) for key in reversed(list(payload))}
check('hash-canonical-key-order', report_hash(reordered) == payload['reportSha'])
hashed = {key: value for key, value in payload.items() if key not in ('reportSha', 'acceptedAt')}
manual_canonical = json.dumps(hashed, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
manual_sha = hashlib.sha256(manual_canonical.encode('utf-8')).hexdigest()
check('hash-matches-canonical-json-sha256', payload['reportSha'] == manual_sha)
accepted = dict(reordered, acceptedAt='2026-11-13T12:00:00+08:00')
check('hash-excludes-accepted-at', report_hash(accepted) == payload['reportSha'])
check('hash-excludes-report-sha', report_hash(dict(payload, reportSha='f' * 64)) == payload['reportSha'])
check('hash-is-sha256', len(payload['reportSha']) == 64 and payload['reportSha'] == payload['reportSha'].lower())

before_payload = copy.deepcopy(payload)
before_expected = copy.deepcopy(expected)
before_observed = copy.deepcopy(observed)
validated = validate_report(payload, expected, observed)
check('valid-report-returned', validated == payload)
check('valid-report-is-copy', validated is not payload and validated['filesChanged'] is not payload['filesChanged'])
check('validation-does-not-mutate-inputs',
      payload == before_payload and expected == before_expected and observed == before_observed)
check('self-report-does-not-create-acceptance', 'acceptedAt' not in validated and 'acceptance' not in validated)
check('file-order-insensitive', validated['filesChanged'] == payload['filesChanged'])

# commitSha 可选；存在时必须与 head 完全一致。
with_alias = make_payload()
with_alias['commitSha'] = with_alias['head']
with_alias['reportSha'] = report_hash(with_alias)
check('commit-sha-alias-accepted',
      validate_report(with_alias, expected, observed)['commitSha'] == with_alias['head'])
bad_alias = dict(with_alias, commitSha='b' * 40)
bad_alias['reportSha'] = report_hash(bad_alias)
expect_value_error('commit-sha-alias-mismatch',
                   validate_report, bad_alias, expected, observed, path='payload.commitSha')
bad_alias_type = dict(with_alias, commitSha=True)
bad_alias_type['reportSha'] = report_hash(bad_alias_type)
expect_value_error('commit-sha-alias-type',
                   validate_report, bad_alias_type, expected, observed, path='payload.commitSha')

# 旧报告和每个新增必填字段都有明确 JSON 路径。
for field in (
    'runId', 'role', 'attempt', 'threadId', 'branch', 'head', 'reportSha',
    'taskRevision', 'baseIsAncestor', 'filesChanged', 'testsRun',
    'selfReportedVerification', 'contractChanges', 'knownRisks',
):
    legacy = make_payload()
    legacy.pop(field)
    expect_value_error(
        'missing-' + field,
        validate_report,
        legacy,
        expected,
        observed,
        path=f'payload.{field}',
    )

legacy_message = expect_value_error(
    'legacy-format-requires-resubmit',
    validate_report,
    {'threadId': 'thread-report', 'verification': 'passed'},
    expected,
    observed,
    path='payload.runId',
)
check('legacy-format-hint', 'resubmitted' in legacy_message)

# 身份字段必须精确匹配派发预期。
for field, wrong in (
    ('runId', 'other-run'),
    ('role', 'verifier'),
    ('attempt', 2),
    ('threadId', 'other-thread'),
    ('branch', 'codex/other'),
    ('taskRevision', 2),
):
    mismatched = make_payload()
    mismatched[field] = wrong
    mismatched['reportSha'] = report_hash(mismatched)
    expect_value_error(
        'identity-mismatch-' + field,
        validate_report,
        mismatched,
        expected,
        observed,
        path=f'payload.{field}',
    )

expected_head = dict(expected, head=payload['head'])
check('expected-optional-head-accepted',
      validate_report(payload, expected_head, observed)['head'] == payload['head'])
wrong_expected_head = dict(expected, head='b' * 40)
expect_value_error('expected-head-mismatch',
                   validate_report, payload, wrong_expected_head, observed,
                   path='payload.head')
for field in ('runId', 'role', 'attempt', 'threadId', 'branch', 'taskRevision'):
    missing_expected = dict(expected)
    missing_expected.pop(field)
    expect_value_error(
        'missing-expected-' + field,
        validate_report,
        payload,
        missing_expected,
        observed,
        path=f'expected.{field}',
    )

# bool 不得冒充 int。
bool_attempt = make_payload()
bool_attempt['attempt'] = True
bool_attempt['reportSha'] = report_hash(bool_attempt)
expect_value_error('bool-attempt-rejected',
                   validate_report, bool_attempt, expected, observed,
                   path='payload.attempt')
bool_revision = make_payload()
bool_revision['taskRevision'] = False
bool_revision['reportSha'] = report_hash(bool_revision)
expect_value_error('bool-task-revision-rejected',
                   validate_report, bool_revision, expected, observed,
                   path='payload.taskRevision')
bool_expected_attempt = dict(expected, attempt=True)
expect_value_error('bool-expected-attempt-rejected',
                   validate_report, payload, bool_expected_attempt, observed,
                   path='expected.attempt')

# SHA、字符串和固定数组字段类型严格。
invalid_shape = make_payload()
invalid_shape['head'] = 'A' * 40
invalid_shape['reportSha'] = report_hash(invalid_shape)
expect_value_error('uppercase-head-rejected',
                   validate_report, invalid_shape, expected, observed,
                   path='payload.head')
invalid_sha = make_payload()
invalid_sha['reportSha'] = 'f' * 63
expect_value_error('short-report-sha-rejected',
                   validate_report, invalid_sha, expected, observed,
                   path='payload.reportSha')
invalid_branch = make_payload()
invalid_branch['branch'] = ''
invalid_branch['reportSha'] = report_hash(invalid_branch)
expect_value_error('empty-branch-rejected',
                   validate_report, invalid_branch, expected, observed,
                   path='payload.branch')
for field in (
    'filesChanged', 'testsRun', 'selfReportedVerification',
    'contractChanges', 'knownRisks',
):
    wrong_list_type = make_payload()
    wrong_list_type[field] = 'not-an-array'
    wrong_list_type['reportSha'] = report_hash(wrong_list_type)
    expect_value_error(
        'string-list-required-' + field,
        validate_report,
        wrong_list_type,
        expected,
        observed,
        path=f'payload.{field}',
    )
    wrong_item = make_payload()
    wrong_item[field] = [1]
    wrong_item['reportSha'] = report_hash(wrong_item)
    expect_value_error(
        'string-list-item-' + field,
        validate_report,
        wrong_item,
        expected,
        observed,
        path=f'payload.{field}[0]',
    )

# 文件清单必须与 leader 的独立 Git 观察完全一致，包含删除/rename 两端。
rename_payload = make_payload()
rename_payload['filesChanged'] = ['old/report.py', 'new/report.py', 'deleted.py']
rename_payload['reportSha'] = report_hash(rename_payload)
rename_observed = make_observed(rename_payload)
rename_observed['filesChanged'] = ['deleted.py', 'new/report.py', 'old/report.py']
check('rename-and-delete-endpoints-are-order-insensitive',
      validate_report(rename_payload, expected, rename_observed)['filesChanged'] == rename_payload['filesChanged'])
fake_files = dict(rename_payload, filesChanged=['old/report.py', 'new/report.py'])
fake_files['reportSha'] = report_hash(fake_files)
fake_observed = dict(rename_observed, filesChanged=['old/report.py', 'new/report.py', 'extra.py'])
expect_value_error('forged-file-list-rejected',
                   validate_report, fake_files, expected, fake_observed,
                   path='payload.filesChanged')
duplicate_files = make_payload()
duplicate_files['filesChanged'] = ['a.py', 'a.py']
duplicate_files['reportSha'] = report_hash(duplicate_files)
expect_value_error('duplicate-files-rejected',
                   validate_report, duplicate_files, expected, observed,
                   path='payload.filesChanged[1]')

# observed 必须提供独立 Git 事实，且 head/branch/祖先关系不能只看 payload。
for field in ('head', 'branch', 'baseIsAncestor', 'filesChanged'):
    missing_observed = dict(observed)
    missing_observed.pop(field)
    expect_value_error(
        'missing-observed-' + field,
        validate_report,
        payload,
        expected,
        missing_observed,
        path=f'observed.{field}',
    )
expect_value_error('observed-head-mismatch',
                   validate_report,
                   payload,
                   expected,
                   dict(observed, head='b' * 40),
                   path='payload.head')
expect_value_error('observed-branch-mismatch',
                   validate_report,
                   payload,
                   expected,
                   dict(observed, branch='codex/other'),
                   path='payload.branch')
expect_value_error('observed-not-ancestor',
                   validate_report,
                   payload,
                   expected,
                   dict(observed, baseIsAncestor=False),
                   path='observed.baseIsAncestor')
false_self_claim = make_payload()
false_self_claim['baseIsAncestor'] = False
false_self_claim['reportSha'] = report_hash(false_self_claim)
expect_value_error('self-ancestor-mismatch',
                   validate_report,
                   false_self_claim,
                   expected,
                   observed,
                   path='payload.baseIsAncestor')

# 修改内容后旧 reportSha 必须失效；非 JSON 值不能参与哈希。
tampered = make_payload()
tampered['knownRisks'].append('tampered after hash')
expect_value_error('report-hash-tamper-rejected',
                   validate_report, tampered, expected, observed,
                   path='payload.reportSha')
expect_value_error('non-json-hash-value-rejected',
                   report_hash,
                   {'runId': object()},
                   path='payload')

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Report structure, canonical hash, identity and observed Git facts only',
}, ensure_ascii=False, indent=2))

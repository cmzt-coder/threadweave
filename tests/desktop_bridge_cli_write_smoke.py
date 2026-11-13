"""通过全新子进程验证桌面写桥接 CLI 的创建、核对与消息持久化契约。

所有回执均为本地伪宿主夹具，不调用 Codex 工具，也不发送真实线程消息。
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
bridge_cli = project / 'scripts' / 'desktop_bridge.py'
run_id = 'fixture-cli-write-run'
role = 'fixture-worker-a'
attempt = 1
task_revision = 1
create_method = 'mcp__codex_app__create_thread'
send_method = 'mcp__codex_app__send_message_to_thread'
response_source = 'desktop_tool_bridge'
fake_sha = hashlib.sha256(b'fixture: not a real host result').hexdigest()
checks = []


def check(name, condition):
    assert condition, name
    checks.append(name)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def run_cli(*arguments, expected_exit=0):
    env = dict(os.environ)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run(
        [sys.executable, str(bridge_cli), *map(str, arguments)],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=20,
    )
    if result.returncode != expected_exit:
        raise AssertionError(
            f'CLI exit {result.returncode}, expected {expected_exit}: {arguments}\n'
            f'stdout={result.stdout!r}\nstderr={result.stderr!r}'
        )
    if expected_exit != 0:
        assert result.stdout == '', f'rejected command wrote JSON stdout: {result.stdout!r}'
    return result


def run_json(*arguments):
    result = run_cli(*arguments)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f'CLI success must emit JSON only: {result.stdout!r}') from exc


def response(record, method, status, metadata=None, error=None):
    value = {key: record[key] for key in ('operationId', 'runId', 'requestSha', 'claimToken')}
    value.update(
        method=method,
        source=response_source,
        observedAt=utc_now(),
        status=status,
        toolResultSha=fake_sha,
    )
    if status == 'succeeded':
        value['metadata'] = copy.deepcopy(metadata)
    else:
        value['error'] = error
    return value


def create_params(prompt='fixture creation prompt'):
    return {
        'prompt': prompt,
        'model': 'deepseek-flash',
        'thinking': 'high',
        'title': 'fixture worker create',
        'target': {
            'type': 'project',
            'projectId': 'fixture-project',
            'environment': {
                'type': 'worktree',
                'startingState': {'type': 'branch', 'branchName': 'fixture-base'},
            },
        },
    }


def common_args(repo, operation):
    return (
        '--repo', repo,
        '--run', run_id,
        '--operation', operation,
        '--role', role,
        '--attempt', str(attempt),
        '--task-revision', str(task_revision),
    )


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    params = create_params()
    params_path = root / 'create-params.json'
    write_json(params_path, params)
    create_args = ('prepare-create', *common_args(root, 'create-1'), '--params', params_path)

    created = run_json(*create_args)
    check('new-process-prepare-create-returns-pending', created['status'] == 'pending')
    check('create-uses-explicit-tool-method', created['request']['method'] == create_method)
    check(
        'create-request-preserves-explicit-config',
        created['request']['params'] == params,
    )
    check(
        'create-request-binds-full-identity',
        created['request']['binding'] == {
            'runId': run_id,
            'role': role,
            'attempt': attempt,
            'taskRevision': task_revision,
        },
    )

    record_path = root / '.threadweave' / run_id / 'bridge' / 'create-1.json'
    create_bytes = record_path.read_bytes()
    replayed_create = run_json(*create_args)
    check('exact-prepare-create-replay-keeps-bytes', record_path.read_bytes() == create_bytes)
    check('exact-prepare-create-replay-returns-original', replayed_create['createdAt'] == created['createdAt'])

    for field, changed in (
        ('model', dict(params, model='fixture-other-model')),
        ('project', copy.deepcopy(params)),
        ('prompt', dict(params, prompt='fixture changed prompt')),
    ):
        if field == 'project':
            changed['target']['projectId'] = 'fixture-other-project'
        changed_path = root / f'changed-{field}.json'
        write_json(changed_path, changed)
        changed_args = ('prepare-create', *common_args(root, 'create-1'), '--params', changed_path)
        result = run_cli(*changed_args, expected_exit=2)
        check(f'changed-{field}-same-operation-rejected', 'already bound to different request' in result.stderr)
    check('rejected-create-conflicts-leave-bytes-unchanged', record_path.read_bytes() == create_bytes)

    missing_config = copy.deepcopy(params)
    del missing_config['model']
    missing_path = root / 'missing-explicit-config.json'
    write_json(missing_path, missing_config)
    missing_args = ('prepare-create', *common_args(root, 'invalid-config'), '--params', missing_path)
    result = run_cli(*missing_args, expected_exit=2)
    check('missing-explicit-create-config-rejected', 'explicit creation configuration required' in result.stderr)
    check(
        'missing-explicit-config-writes-no-request',
        not (root / '.threadweave' / run_id / 'bridge' / 'invalid-config.json').exists(),
    )

    local_params = copy.deepcopy(params)
    local_params['target']['environment'] = {'type': 'local'}
    local_path = root / 'local-environment.json'
    write_json(local_path, local_params)
    local_args = ('prepare-create', *common_args(root, 'invalid-local'), '--params', local_path)
    result = run_cli(*local_args, expected_exit=2)
    check('local-create-environment-rejected', 'isolated worktree required' in result.stderr)
    check(
        'local-environment-writes-no-request',
        not (root / '.threadweave' / run_id / 'bridge' / 'invalid-local.json').exists(),
    )

    inspected = run_json('inspect', *common_args(root, 'create-1'))
    check('inspect-in-fresh-process-sees-pending-request', inspected['status'] == 'pending')

    claimed = run_json('claim', *common_args(root, 'create-1'))
    check('create-claim-is-in-flight', claimed['status'] == 'in_flight' and bool(claimed['claimToken']))

    unknown = run_json(
        'unknown', *common_args(root, 'create-1'),
        '--reason', 'fixture: response was lost before durable capture',
    )
    check('unknown-state-is-persisted', unknown['status'] == 'unknown' and 'claimToken' in unknown)

    client_id = 'fixture-client-thread'
    create_response = response(
        unknown,
        create_method,
        'succeeded',
        metadata={'clientThreadId': client_id, 'hostId': 'local'},
    )
    response_path = root / 'create-response.json'
    write_json(response_path, create_response)
    complete_args = ('complete', *common_args(root, 'create-1'), '--response', response_path)
    result = run_cli(*complete_args, expected_exit=2)
    check('unknown-completion-requires-reconciliation', 'explicit reconciliation evidence' in result.stderr)

    completed = run_json(
        *complete_args,
        '--reconciliation', 'fixture: exact clientThreadId recovered from local journal',
    )
    check('reconciled-create-completes', completed['status'] == 'succeeded')
    check(
        'client-id-remains-asynchronous-until-resolved',
        completed['response']['metadata']['clientThreadId'] == client_id,
    )

    completed_bytes = record_path.read_bytes()
    run_json(*complete_args, '--reconciliation', 'fixture replay; no write expected')
    check('exact-complete-replay-keeps-bytes', record_path.read_bytes() == completed_bytes)

    resolution = {
        'clientThreadId': client_id,
        'threadId': 'fixture-real-thread',
        'hostId': 'local',
        'projectId': params['target']['projectId'],
        'title': params['title'],
        'model': params['model'],
        'thinking': params['thinking'],
        'branchName': params['target']['environment']['startingState']['branchName'],
        'cwd': str(root / 'fixture-worker-worktree'),
        'observedAt': utc_now(),
        'toolResultSha': fake_sha,
        'evidence': 'fixture: exact client/project/model/branch linkage and independent read',
    }
    bad_resolution_path = root / 'resolution-client-as-real.json'
    write_json(bad_resolution_path, dict(resolution, threadId=client_id))
    resolution_args = ('resolve-creation', *common_args(root, 'create-1'), '--resolution', bad_resolution_path)
    before_bad_resolution = record_path.read_bytes()
    result = run_cli(*resolution_args, expected_exit=2)
    check('client-id-cannot-become-real-thread-id', 'actual threadId' in result.stderr)
    check('rejected-client-id-resolution-leaves-bytes', record_path.read_bytes() == before_bad_resolution)

    resolution_path = root / 'resolution.json'
    write_json(resolution_path, resolution)
    resolution_args = ('resolve-creation', *common_args(root, 'create-1'), '--resolution', resolution_path)
    resolved = run_json(*resolution_args)
    check('real-client-linkage-resolves-creation', resolved['creationResolution'] == resolution)
    check(
        'creation-resolution-has-independent-hash',
        resolved['creationResolutionSha'] == hashlib.sha256(
            json.dumps(resolution, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest(),
    )

    resolved_bytes = record_path.read_bytes()
    run_json(*resolution_args)
    check('exact-resolution-replay-keeps-bytes', record_path.read_bytes() == resolved_bytes)
    final_inspect = run_json('inspect', *common_args(root, 'create-1'))
    check(
        'fresh-inspect-sees-completed-resolution',
        final_inspect['status'] == 'succeeded'
        and final_inspect['creationResolution']['threadId'] == 'fixture-real-thread',
    )

    sentinel_one = root / 'shell-substitution-must-not-run'
    sentinel_two = root / 'backtick-must-not-run'
    message = (
        '第一行：保留多行 UTF-8 内容。\n'
        f'第二行：$(touch {sentinel_one})\n'
        f'第三行：`touch {sentinel_two}` && echo should-not-run\n'
        '第四行：$HOME ${USER} ; * ? [x] "quoted"\n'
    )
    message_path = root / 'dispatch-message.txt'
    message_path.write_text(message, encoding='utf-8')
    send_args = (
        'prepare-send',
        *common_args(root, 'send-1'),
        '--thread', 'fixture-real-thread',
        '--message-file', message_path,
    )
    pending_send = run_json(*send_args)
    check('message-file-prepare-send-is-pending', pending_send['status'] == 'pending')
    check('message-file-prompt-round-trips-exactly', pending_send['request']['params']['prompt'] == message)
    check(
        'message-file-preserves-utf8-and-metacharacters',
        '第一行' in pending_send['request']['params']['prompt']
        and '$(touch ' in pending_send['request']['params']['prompt']
        and '`touch ' in pending_send['request']['params']['prompt'],
    )
    check(
        'send-request-has-no-model-override',
        set(pending_send['request']['params']) == {'threadId', 'hostId', 'prompt'}
        and pending_send['request']['params']['hostId'] == 'local',
    )
    check('message-content-was-not-executed', not sentinel_one.exists() and not sentinel_two.exists())

    claimed_send = run_json('claim', *common_args(root, 'send-1'))
    check('send-claim-is-in-flight', claimed_send['status'] == 'in_flight')
    failed_response = response(claimed_send, send_method, 'failed', error='fixture host rejected send')
    failed_response_path = root / 'send-failed-response.json'
    write_json(failed_response_path, failed_response)
    send_complete_args = ('complete', *common_args(root, 'send-1'), '--response', failed_response_path)
    failed_record = run_json(*send_complete_args)
    check('failed-send-terminal-state-is-persisted', failed_record['status'] == 'failed')

    send_record_path = root / '.threadweave' / run_id / 'bridge' / 'send-1.json'
    failed_bytes = send_record_path.read_bytes()
    overwrite_response = response(
        claimed_send,
        send_method,
        'succeeded',
        metadata={'threadId': 'fixture-real-thread', 'hostId': 'local', 'status': 'accepted'},
    )
    overwrite_path = root / 'send-overwrite-response.json'
    write_json(overwrite_path, overwrite_response)
    result = run_cli(
        'complete', *common_args(root, 'send-1'), '--response', overwrite_path,
        expected_exit=2,
    )
    check('terminal-failure-cannot-be-overwritten', 'terminal result cannot be overwritten' in result.stderr)
    check('terminal-failure-bytes-remain-unchanged', send_record_path.read_bytes() == failed_bytes)
    durable_send = run_json('inspect', *common_args(root, 'send-1'))
    check(
        'terminal-failure-remains-durable',
        durable_send['status'] == 'failed' and durable_send['response']['error'] == 'fixture host rejected send',
    )

print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False))

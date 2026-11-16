"""O16 资源来源只读映射；关联证明不等于所有权、停止或清理授权。"""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path

from desktop_bridge import BridgeJournal, digest
from desktop_lifecycle_bridge import CREATE_WORKTREE, LIST_ARTIFACTS
from state_store import read_state


def normalize_artifacts(result):
    """兼容已观察到的 attachmentType 及旧 type；保留 payload，不填充保护字段。"""
    if not isinstance(result, dict) or not isinstance(result.get('artifacts'), list):
        raise ValueError('artifact inventory requires artifacts array')
    normalized = []
    for artifact in result['artifacts']:
        if not isinstance(artifact, dict):
            raise ValueError('artifact must be an object')
        kind = artifact.get('attachmentType', artifact.get('type'))
        if ('attachmentType' in artifact and 'type' in artifact
                and artifact['attachmentType'] != artifact['type']):
            raise ValueError('conflicting artifact type fields')
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError('explicit artifact type required')
        item = copy.deepcopy(artifact)
        item['kind'] = kind
        normalized.append(item)
    return normalized


def parse_raw_result(raw):
    """只接收一个明确 JSON 文本结果，不将工具错误当成资源观察。"""
    if not isinstance(raw, dict) or raw.get('isError', False) is not False:
        raise ValueError('successful raw tool envelope required')
    blocks = raw.get('content')
    if not isinstance(blocks, list):
        raise ValueError('raw content array required')
    texts = [b.get('text') for b in blocks if isinstance(b, dict) and b.get('type') == 'text']
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise ValueError('one JSON text tool result required')
    result = json.loads(texts[0])
    if not isinstance(result, dict):
        raise ValueError('raw tool result must be an object')
    return result


def _canonical(value):
    if (not isinstance(value, str) or not Path(value).is_absolute()
            or str(Path(value).resolve()) != value):
        raise ValueError('canonical absolute resource path required')
    return value


def _verified(journal, operation, raw_path, method, expected_binding):
    record = journal.inspect(operation)
    if (record['status'] != 'succeeded' or record['request']['method'] != method
            or record['request']['binding'] != expected_binding):
        raise ValueError('terminal method/binding mismatch: ' + operation)
    path = Path(raw_path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('absolute non-symlink raw result file required')
    raw = read_state(path)
    result = parse_raw_result(raw)
    response = record['response']
    if (response['toolResultSha'] != digest(raw)
            or response['metadata'] != {'hostId': 'local', 'result': result}):
        raise ValueError('raw tool result does not match journal: ' + operation)
    return record, result


def map_resource_provenance(spec, *, now=None):
    """验证本 run 的创建来源和新鲜附件；始终 readOnly 且 cleanupEligible=false。"""
    result = {'version': 1, 'status': 'blocked', 'readOnly': True,
              'deletionPerformed': False, 'cleanupEligible': False,
              'runAssociationVerified': False, 'currentAttachmentObserved': False,
              'unknown': ['ownerThreadId', 'isPrimary', 'isShared', 'callingThreadId']}
    try:
        if not isinstance(spec, dict):
            raise ValueError('provenance specification must be an object')
        repository = _canonical(spec['repository'])
        worktree = _canonical(spec['worktree'])
        if any(type(spec.get(k)) is not int or spec[k] < minimum for k, minimum in (
                ('attempt', 1), ('createdTaskRevision', 0), ('taskRevision', 0))):
            raise ValueError('explicit attempt and task revisions required')
        if spec['createdTaskRevision'] > spec['taskRevision']:
            raise ValueError('inventory predates task revision')
        binding = {k: spec[k] for k in ('runId', 'role', 'attempt', 'taskRevision')}
        creation_binding = dict(binding, taskRevision=spec['createdTaskRevision'])
        journal = BridgeJournal(repository, spec['runId'])
        created, creation = _verified(journal, spec['createOperation'], spec['createRaw'],
                                      CREATE_WORKTREE, creation_binding)
        observed, inventory = _verified(journal, spec['inventoryOperation'], spec['inventoryRaw'],
                                        LIST_ARTIFACTS, binding)
        if _canonical(created['request']['params']['sourceRoot']) != repository:
            raise ValueError('creation source repository mismatch')
        # 仅支持真实历史回执已确认的同步 created 结构；异步结果不能猜测完成。
        if (creation.get('type') != 'created' or creation.get('registrationError') is not None
                or creation.get('worktreeGitRoot') != worktree
                or creation.get('worktreeWorkspaceRoot') != worktree):
            raise ValueError('unresolved or unsupported creation identity')
        created_at = datetime.fromisoformat(created['response']['observedAt'].replace('Z', '+00:00'))
        observed_at = datetime.fromisoformat(observed['response']['observedAt'].replace('Z', '+00:00'))
        age = ((now or datetime.now(timezone.utc)) - observed_at).total_seconds()
        if not 0 <= age <= 30 or observed_at < created_at:
            raise ValueError('inventory observation stale, future or before creation')
        matches = [a for a in normalize_artifacts(inventory) if a.get('identityKey') == worktree]
        if len(matches) != 1:
            raise ValueError('exact resource identity missing or ambiguous')
        artifact = matches[0]
        kind = artifact['kind']
        if kind not in ('worktree', 'archived_worktree'):
            raise ValueError('resource attachment is not a worktree')
        payload = artifact.get('payload')
        if not isinstance(payload, dict):
            raise ValueError('worktree payload required')
        resource = payload.get('worktree') if kind == 'archived_worktree' else payload
        if (not isinstance(resource, dict) or resource.get('hostId') != 'local'
                or resource.get('root') != worktree or resource.get('workspaceRoot') != worktree):
            raise ValueError('attachment payload identity mismatch')
        result.update(status='associated' if kind == 'worktree' else 'associated_archived',
                      runAssociationVerified=True, currentAttachmentObserved=kind == 'worktree',
                      binding=binding, worktree=worktree, attachmentKind=kind,
                      proof={'creationOperation': created['operationId'],
                             'inventoryOperation': observed['operationId'],
                             'creationRequestSha': created['requestSha'],
                             'creationToolResultSha': created['response']['toolResultSha'],
                             'inventoryRequestSha': observed['requestSha'],
                             'inventoryToolResultSha': observed['response']['toolResultSha'],
                             'inventoryObservedAt': observed['response']['observedAt']},
                      reason='association only; ownership, stop, Git safety and protection checks still required')
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result['reason'] = str(exc)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, help='UTF-8 JSON with journal IDs and absolute raw-result paths')
    args = parser.parse_args(argv)
    try:
        result = map_resource_provenance(read_state(args.manifest))
    except (ValueError, OSError) as exc:
        result = {'status': 'blocked', 'readOnly': True, 'cleanupEligible': False,
                  'deletionPerformed': False, 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('associated', 'associated_archived') else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""O16 宿主归档完成观察；保护被委托给宿主，本模块不调用宿主、不删除资源。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from desktop_bridge import BridgeJournal, digest
from desktop_lifecycle_bridge import ARCHIVE_WORKTREE, LIST_ARTIFACTS
from archive_authorization import verify_envelope_for_archive
from resource_provenance import map_resource_provenance, normalize_artifacts, parse_raw_result
from state_store import read_state


PROTECTION_POLICY = 'host_archive_worktree_execution'


def _canonical(value):
    if (not isinstance(value, str) or not Path(value).is_absolute()
            or str(Path(value).resolve()) != value):
        raise ValueError('canonical absolute repository/worktree path required')
    return value


def _stamp(value, label):
    if not isinstance(value, str):
        raise ValueError(label + ' observation time required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(label + ' observation time invalid') from exc
    if parsed.tzinfo is None:
        raise ValueError(label + ' observation time requires timezone')
    return parsed


def _operation(journal, operation, raw_path, method, binding):
    record = journal.inspect(operation)
    if (record.get('status') != 'succeeded'
            or record.get('request', {}).get('method') != method
            or record.get('request', {}).get('binding') != binding):
        raise ValueError('terminal method/binding mismatch: ' + operation)
    path = Path(raw_path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('absolute non-symlink raw result file required: ' + operation)
    raw = read_state(path)
    result = parse_raw_result(raw)
    response = record.get('response')
    if (not isinstance(response, dict)
            or response.get('source') != 'desktop_tool_bridge'
            or response.get('method') != method
            or response.get('toolResultSha') != digest(raw)
            or response.get('metadata') != {'hostId': 'local', 'result': result}):
        raise ValueError('raw host result does not match journal: ' + operation)
    return record, result


def _worktree_registered(repository, worktree):
    try:
        output = subprocess.run(
            ['git', '-C', repository, 'worktree', 'list', '--porcelain'],
            check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError('Git worktree registration cannot be observed') from exc
    registered = []
    for line in output.splitlines():
        if line.startswith('worktree '):
            value = line[len('worktree '):]
            registered.append(str(Path(value).resolve()))
    return worktree in registered


def verify_host_archive(spec, *, now=None):
    """审计一次外部已执行的 host archive；返回事实，不提供后续清理授权。"""
    result = {
        'version': 1,
        'status': 'blocked',
        'readOnly': True,
        'deletionPerformed': False,
        'cleanupEligible': False,
        'completionEvidenceVerified': False,
        'hostArchiveCompleted': False,
        'preObservableProtection': False,
        'protectionDelegation': None,
        'protectionEnforcementVerified': False,
        'callerContinuityVerified': False,
        'approvalBindingVerified': False,
        'authorizationBindingVerified': False,
        'authorizationFingerprint': None,
        'authorizationSource': None,
        'hostSignedIdentityVerified': False,
        'preInventoryShapeVerified': False,
        'postArchiveTransitionObserved': False,
        'unknown': ['ownerThreadId', 'isPrimary', 'isShared', 'callingThreadId'],
    }
    try:
        if not isinstance(spec, dict):
            raise ValueError('host archive evidence specification must be an object')
        if spec.get('protectionPolicy') != PROTECTION_POLICY:
            raise ValueError('explicit host archive protection policy required')
        repository = _canonical(spec['repository'])
        worktree = _canonical(spec['worktree'])
        if any(type(spec.get(k)) is not int or spec[k] < minimum for k, minimum in (
                ('attempt', 1), ('createdTaskRevision', 0), ('taskRevision', 0))):
            raise ValueError('explicit attempt and task revisions required')
        binding = {k: spec[k] for k in ('runId', 'role', 'attempt', 'taskRevision')}
        provenance = map_resource_provenance({
            'repository': repository,
            'worktree': worktree,
            'runId': spec['runId'],
            'role': spec['role'],
            'attempt': spec['attempt'],
            'createdTaskRevision': spec['createdTaskRevision'],
            'taskRevision': spec['taskRevision'],
            'createOperation': spec['createOperation'],
            'createRaw': spec['createRaw'],
            'inventoryOperation': spec['preInventoryOperation'],
            'inventoryRaw': spec['preInventoryRaw'],
        }, now=now)
        if (provenance.get('status') != 'associated'
                or provenance.get('runAssociationVerified') is not True
                or provenance.get('currentAttachmentObserved') is not True
                or provenance.get('binding') != binding
                or provenance.get('cleanupEligible') is not False):
            raise ValueError('pre-archive provenance/current-caller scope not verified')

        journal = BridgeJournal(repository, spec['runId'])
        archive_record, archive_result = _operation(
            journal, spec['archiveOperation'], spec['archiveRaw'], ARCHIVE_WORKTREE, binding)
        if spec.get('expectedArchiveRequestSha') != archive_record.get('requestSha'):
            raise ValueError('expected archive request hash mismatch')
        if archive_record.get('request', {}).get('params') != {'root': worktree}:
            raise ValueError('archive request does not target exact worktree')
        # 仅为历史实证的宿主签发格式；queued 是受理，不是删除完成。
        if archive_result != {'type': 'queued'}:
            raise ValueError('unsupported or unsuccessful host archive result')

        authorization = spec.get('authorization')
        if authorization is None:
            if spec.get('requireAuthorization') is True:
                raise ValueError('authorization envelope required')
        else:
            if not isinstance(authorization, dict):
                raise ValueError('authorization evidence must be an object')
            manifest = authorization.get('manifest')
            envelope_result = authorization.get('result')
            if not isinstance(manifest, dict) or not isinstance(envelope_result, dict):
                raise ValueError('authorization manifest/result required')
            pre_receipt = manifest.get('preInventoryReceipt')
            if (not isinstance(pre_receipt, dict)
                    or pre_receipt.get('operationId') != spec['preInventoryOperation']
                    or pre_receipt.get('requestSha') != provenance['proof']['inventoryRequestSha']
                    or pre_receipt.get('toolResultSha') != provenance['proof']['inventoryToolResultSha']):
                raise ValueError('authorization pre-inventory receipt mismatch')
            verified_auth = verify_envelope_for_archive(
                manifest, envelope_result, archive_record['response']['observedAt'])
            envelope = verified_auth['envelope']
            plan = manifest.get('approvedPlan', {}).get('plan', {})
            if (envelope.get('binding') != binding
                    or envelope.get('worktree') != worktree
                    or envelope.get('threadId') != plan.get('threadId')
                    or envelope.get('archiveRequestSha') != spec['expectedArchiveRequestSha']):
                raise ValueError('authorization envelope resource/request mismatch')
            result.update(
                authorizationBindingVerified=True,
                authorizationFingerprint=verified_auth['authorizationFingerprint'],
                authorizationSource=envelope.get('source'))

        completion_record, completion = _operation(
            journal, spec['completionInventoryOperation'], spec['completionInventoryRaw'],
            LIST_ARTIFACTS, binding)
        pre_at = _stamp(provenance.get('proof', {}).get('inventoryObservedAt'), 'pre-inventory')
        archive_at = _stamp(archive_record.get('response', {}).get('observedAt'), 'archive')
        completion_at = _stamp(
            completion_record.get('response', {}).get('observedAt'), 'completion-inventory')
        stamp_now = now or datetime.now(timezone.utc)
        if stamp_now.tzinfo is None:
            raise ValueError('current observation time requires timezone')
        age = (stamp_now - completion_at).total_seconds()
        if not 0 <= age <= 30 or archive_at < pre_at or completion_at < archive_at:
            raise ValueError('completion inventory stale, future or out of order')

        artifacts = normalize_artifacts(completion)
        active = [a for a in artifacts if a.get('identityKey') == worktree
                  and a['kind'] == 'worktree']
        archived = [a for a in artifacts if a.get('identityKey') == worktree
                    and a['kind'] == 'archived_worktree']
        if active:
            raise ValueError('worktree remains active in completion inventory')
        if len(archived) != 1:
            raise ValueError('exact archived worktree attachment missing or ambiguous')
        resource = archived[0].get('payload', {}).get('worktree')
        if (not isinstance(resource, dict) or resource.get('hostId') != 'local'
                or resource.get('root') != worktree
                or resource.get('workspaceRoot') != worktree
                or resource.get('sourceGitRoot') != repository):
            raise ValueError('archived worktree payload identity mismatch')

        if not Path(repository).is_dir():
            raise ValueError('source repository is unavailable')
        try:
            top = subprocess.run(
                ['git', '-C', repository, 'rev-parse', '--show-toplevel'],
                check=True, capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError('source Git root cannot be observed') from exc
        if str(Path(top).resolve()) != repository:
            raise ValueError('source Git root mismatch')
        if os.path.lexists(worktree):
            raise ValueError('archived worktree path still exists')
        if _worktree_registered(repository, worktree):
            raise ValueError('archived worktree remains in Git registration')

        result.update(
            status='host_archive_completion_observed',
            completionEvidenceVerified=True,
            hostArchiveCompleted=True,
            protectionDelegation=PROTECTION_POLICY,
            preInventoryShapeVerified=True,
            postArchiveTransitionObserved=True,
            binding=binding,
            worktree=worktree,
            proof={
                'preInventoryOperation': spec['preInventoryOperation'],
                'preInventoryRequestSha': provenance['proof']['inventoryRequestSha'],
                'preInventoryToolResultSha': provenance['proof']['inventoryToolResultSha'],
                'archiveOperation': archive_record['operationId'],
                'archiveRequestSha': archive_record['requestSha'],
                'archiveToolResultSha': archive_record['response']['toolResultSha'],
                'completionInventoryOperation': completion_record['operationId'],
                'completionInventoryRequestSha': completion_record['requestSha'],
                'completionInventoryToolResultSha': completion_record['response']['toolResultSha'],
                'completionInventoryObservedAt': completion_record['response']['observedAt'],
            },
            reason=('host archive completion observed through bound receipts, archived attachment '
                    'and local absence; caller continuity, approval and protection enforcement '
                    'remain unverified'))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result['reason'] = str(exc)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, help='absolute evidence JSON manifest')
    args = parser.parse_args(argv)
    try:
        result = verify_host_archive(read_state(args.manifest))
    except (ValueError, OSError) as exc:
        result = {'status': 'blocked', 'readOnly': True, 'cleanupEligible': False,
                  'deletionPerformed': False, 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'host_archive_completion_observed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

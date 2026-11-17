"""O16 本地归档授权信封；绑定计划/停止/附件/请求，但不冒充宿主签名或授予删除权。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from cleanup import cleanup_plan_fingerprint
from cleanup_apply import preview
from desktop_bridge import digest
from resource_provenance import normalize_artifacts
from state_store import read_state


SOURCE = 'local_explicit_authorization'
MAX_TTL_SECONDS = 300
SHA256 = re.compile(r'^[0-9a-f]{64}$')


def _stamp(value, label):
    if not isinstance(value, str):
        raise ValueError(label + ' time required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(label + ' time invalid') from exc
    if parsed.tzinfo is None:
        raise ValueError(label + ' time requires timezone')
    return parsed


def _binding(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + ' must be an object')
    required = ('runId', 'role', 'attempt', 'taskRevision')
    if any(key not in value for key in required):
        raise ValueError(label + ' binding missing')
    if (not isinstance(value['runId'], str) or not value['runId']
            or not isinstance(value['role'], str) or not value['role']
            or type(value['attempt']) is not int or value['attempt'] < 1
            or type(value['taskRevision']) is not int or value['taskRevision'] < 0):
        raise ValueError(label + ' binding invalid')
    return {key: value[key] for key in required}


def _receipt(receipt, label, binding, now):
    if (not isinstance(receipt, dict)
            or receipt.get('source') != 'desktop_tool_bridge'
            or not isinstance(receipt.get('operationId'), str)
            or not receipt['operationId']):
        raise ValueError(label + ' bridge identity missing')
    if _binding(receipt, label) != binding:
        raise ValueError(label + ' binding mismatch')
    observed = _stamp(receipt.get('observedAt'), label)
    age = (now - observed).total_seconds()
    if not 0 <= age <= 30:
        raise ValueError(label + ' observation stale or future')
    metadata = receipt.get('metadata')
    if (not isinstance(metadata, dict) or metadata.get('hostId') != 'local'
            or not isinstance(metadata.get('result'), dict)):
        raise ValueError(label + ' local result missing')
    return metadata['result'], observed


def _stop_evidence(receipt, binding, thread_id, now):
    result, observed = _receipt(receipt, 'stop receipt', binding, now)
    polls = [poll for poll in result.get('polls', [])
             if isinstance(poll, dict)
             and isinstance(poll.get('thread'), dict)
             and poll['thread'].get('id') == thread_id]
    if len(polls) != 1:
        raise ValueError('exact stopped thread poll required')
    poll = polls[0]
    turn = poll.get('latestTurn')
    final = poll.get('latestAssistantMessage')
    status = poll['thread'].get('status')
    if (not isinstance(status, dict) or status.get('type') != 'idle'
            or not isinstance(turn, dict) or turn.get('status') != 'completed'
            or not isinstance(turn.get('id'), str) or not turn['id']
            or not isinstance(final, dict) or final.get('turnId') != turn['id']
            or final.get('phase') != 'final_answer'):
        raise ValueError('stop evidence does not prove idle/completed/final')
    return digest(receipt), observed


def _inventory(receipt, binding, worktree, now):
    result, observed = _receipt(receipt, 'pre-inventory receipt', binding, now)
    artifacts = [a for a in normalize_artifacts(result)
                 if a.get('identityKey') == worktree]
    if len(artifacts) != 1 or artifacts[0]['kind'] != 'worktree':
        raise ValueError('exact active pre-archive worktree required')
    payload = artifacts[0].get('payload')
    if (not isinstance(payload, dict) or payload.get('hostId') != 'local'
            or payload.get('root') != worktree
            or payload.get('workspaceRoot') != worktree):
        raise ValueError('pre-archive worktree payload mismatch')
    return digest(receipt), observed


def build_envelope(manifest, *, now=None):
    """生成可复算的本地授权信封；不调用宿主，不写数据库。"""
    if not isinstance(manifest, dict):
        raise ValueError('authorization manifest must be an object')
    stamp_now = now or datetime.now(timezone.utc)
    if stamp_now.tzinfo is None:
        raise ValueError('current time requires timezone')
    declaration = manifest.get('declaration')
    approved = manifest.get('approvedPlan')
    if not isinstance(declaration, dict) or not isinstance(approved, dict):
        raise ValueError('declaration and approvedPlan required')
    current = preview(declaration)
    plan = approved.get('plan')
    if (not isinstance(plan, dict)
            or approved.get('fingerprint') != cleanup_plan_fingerprint(plan)
            or current.get('fingerprint') != approved.get('fingerprint')
            or current.get('fingerprint') != cleanup_plan_fingerprint(current.get('plan', {}))):
        raise ValueError('approved cleanup plan drift')
    worktree = declaration.get('worktree')
    if not isinstance(worktree, str) or not Path(worktree).is_absolute():
        raise ValueError('exact absolute worktree required')
    thread_id = plan.get('threadId')
    if (plan.get('executionKind') != 'host_thread'
            or not isinstance(thread_id, str) or not thread_id.strip()):
        raise ValueError('host_thread plan with exact threadId required')
    binding = {
        'runId': declaration.get('runId'),
        'role': manifest.get('role'),
        'attempt': manifest.get('attempt'),
        'taskRevision': manifest.get('taskRevision'),
    }
    binding = _binding(binding, 'authorization')
    stop_sha, stop_at = _stop_evidence(manifest.get('stopReceipt'), binding, thread_id, stamp_now)
    inventory_sha, inventory_at = _inventory(
        manifest.get('preInventoryReceipt'), binding, worktree, stamp_now)
    archive_request_sha = manifest.get('archiveRequestSha')
    if not isinstance(archive_request_sha, str) or not SHA256.fullmatch(archive_request_sha):
        raise ValueError('archiveRequestSha must be SHA256')
    nonce = manifest.get('authorizationNonce')
    if not isinstance(nonce, str) or not nonce.strip() or len(nonce) > 128:
        raise ValueError('authorizationNonce required')
    authorized_at = _stamp(manifest.get('authorizedAt'), 'authorizedAt')
    expires_at = _stamp(manifest.get('expiresAt'), 'expiresAt')
    ttl = (expires_at - authorized_at).total_seconds()
    if not 0 < ttl <= MAX_TTL_SECONDS:
        raise ValueError('authorization TTL must be 1..300 seconds')
    if not authorized_at <= stamp_now <= expires_at:
        raise ValueError('authorization window inactive')
    if stop_at > authorized_at or inventory_at > authorized_at:
        raise ValueError('authorization predates required evidence')
    envelope = {
        'version': 1,
        'source': SOURCE,
        'authorizationNonce': nonce,
        'authorizedAt': authorized_at.isoformat(),
        'expiresAt': expires_at.isoformat(),
        'binding': binding,
        'threadId': thread_id,
        'worktree': worktree,
        'cleanupPlanFingerprint': approved['fingerprint'],
        'cleanupEvidenceSha256': plan['evidenceSha256'],
        'integrationHead': plan['integrationHead'],
        'stopReceiptSha256': stop_sha,
        'preInventoryReceiptSha256': inventory_sha,
        'archiveRequestSha': archive_request_sha,
        'hostSignedIdentityVerified': False,
        'userApprovalCryptographicallyVerified': False,
        'cleanupEligible': False,
    }
    return {
        'status': 'envelope_prepared',
        'readOnly': True,
        'deletionPerformed': False,
        'cleanupEligible': False,
        'localAuthorizationBindingVerified': True,
        'hostSignedIdentityVerified': False,
        'userApprovalCryptographicallyVerified': False,
        'authorizationFingerprint': digest(envelope),
        'envelope': envelope,
    }


def verify_envelope(manifest, envelope_result, *, now=None):
    """用同一组证据复算授权指纹；任何输入变化都会 blocked。"""
    if (not isinstance(envelope_result, dict)
            or envelope_result.get('status') != 'envelope_prepared'
            or not isinstance(envelope_result.get('envelope'), dict)
            or envelope_result.get('authorizationFingerprint') != digest(envelope_result['envelope'])):
        raise ValueError('authorization envelope integrity failure')
    expected = build_envelope(manifest, now=now)
    if expected['authorizationFingerprint'] != envelope_result['authorizationFingerprint']:
        raise ValueError('authorization evidence or fingerprint changed')
    result = dict(expected)
    result['status'] = 'envelope_verified'
    result['localAuthorizationBindingVerified'] = True
    return result


def verify_envelope_for_archive(manifest, envelope_result, archive_observed_at):
    """在归档完成后复算信封，不重新读取已被归档的 worktree。"""
    if (not isinstance(envelope_result, dict)
            or envelope_result.get('status') != 'envelope_prepared'
            or not isinstance(envelope_result.get('envelope'), dict)
            or envelope_result.get('authorizationFingerprint') != digest(envelope_result['envelope'])):
        raise ValueError('authorization envelope integrity failure')
    declaration = manifest.get('declaration')
    approved = manifest.get('approvedPlan')
    if not isinstance(declaration, dict) or not isinstance(approved, dict):
        raise ValueError('declaration and approvedPlan required')
    plan = approved.get('plan')
    if not isinstance(plan, dict):
        raise ValueError('approved plan required')
    if plan.get('executionKind') != 'host_thread' or not isinstance(plan.get('threadId'), str):
        raise ValueError('host_thread plan with exact threadId required')
    worktree = declaration.get('worktree')
    if not isinstance(worktree, str) or not Path(worktree).is_absolute():
        raise ValueError('exact absolute worktree required')
    binding = _binding({
        'runId': declaration.get('runId'),
        'role': manifest.get('role'),
        'attempt': manifest.get('attempt'),
        'taskRevision': manifest.get('taskRevision'),
    }, 'authorization')
    archive_request_sha = manifest.get('archiveRequestSha')
    if not isinstance(archive_request_sha, str) or not SHA256.fullmatch(archive_request_sha):
        raise ValueError('archiveRequestSha must be SHA256')
    nonce = manifest.get('authorizationNonce')
    if not isinstance(nonce, str) or not nonce.strip() or len(nonce) > 128:
        raise ValueError('authorizationNonce required')
    authorized_at = _stamp(manifest.get('authorizedAt'), 'authorizedAt')
    expires_at = _stamp(manifest.get('expiresAt'), 'expiresAt')
    if not 0 < (expires_at - authorized_at).total_seconds() <= MAX_TTL_SECONDS:
        raise ValueError('authorization TTL must be 1..300 seconds')
    archive_at = _stamp(archive_observed_at, 'archiveObservedAt')
    if not authorized_at <= archive_at <= expires_at:
        raise ValueError('archive observation outside authorization window')
    expected = {
        'version': 1,
        'source': SOURCE,
        'authorizationNonce': manifest.get('authorizationNonce'),
        'authorizedAt': authorized_at.isoformat(),
        'expiresAt': expires_at.isoformat(),
        'binding': binding,
        'threadId': plan.get('threadId'),
        'worktree': worktree,
        'cleanupPlanFingerprint': approved.get('fingerprint'),
        'cleanupEvidenceSha256': plan.get('evidenceSha256'),
        'integrationHead': plan.get('integrationHead'),
        'stopReceiptSha256': digest(manifest.get('stopReceipt')),
        'preInventoryReceiptSha256': digest(manifest.get('preInventoryReceipt')),
        'archiveRequestSha': archive_request_sha,
        'hostSignedIdentityVerified': False,
        'userApprovalCryptographicallyVerified': False,
        'cleanupEligible': False,
    }
    if envelope_result['envelope'] != expected:
        raise ValueError('authorization envelope does not match archive-time inputs')
    result = dict(envelope_result)
    result.update(status='envelope_verified_for_archive', archiveObservedAt=archive_at.isoformat(),
                  localAuthorizationBindingVerified=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('build', 'verify'))
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--envelope', type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = read_state(args.manifest)
        if args.command == 'build':
            result = build_envelope(manifest)
        else:
            if args.envelope is None:
                raise ValueError('--envelope required for verify')
            result = verify_envelope(manifest, read_state(args.envelope))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result = {'status': 'blocked', 'readOnly': True, 'cleanupEligible': False,
                  'localAuthorizationBindingVerified': False, 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('envelope_prepared', 'envelope_verified') else 1


if __name__ == '__main__':
    raise SystemExit(main())

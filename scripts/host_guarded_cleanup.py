"""显式 opt-in 的宿主归档授权路径；授权信封通过后才调用宿主 archive。"""

from archive_authorization import verify_envelope
from desktop_bridge import digest
from desktop_lifecycle_bridge import ARCHIVE_WORKTREE


def execute_host_guarded_cleanup(adapter, declaration, approved, stop_receipt, inventory_receipt,
                                 owner_thread_id, authorization_manifest, authorization_result,
                                 *, authorized=False, now=None):
    if authorized is not True:
        raise ValueError('explicit host cleanup authorization required')
    if not isinstance(authorization_manifest, dict) or not isinstance(authorization_result, dict):
        raise ValueError('authorization manifest/result required')
    if authorization_manifest.get('declaration') != declaration:
        raise ValueError('authorization declaration mismatch')
    if authorization_manifest.get('approvedPlan') != approved:
        raise ValueError('authorization approved plan mismatch')
    if digest(authorization_manifest.get('stopReceipt')) != digest(stop_receipt):
        raise ValueError('authorization stop receipt mismatch')
    if digest(authorization_manifest.get('preInventoryReceipt')) != digest(inventory_receipt):
        raise ValueError('authorization inventory receipt mismatch')
    plan = approved.get('plan') if isinstance(approved, dict) else None
    if not isinstance(plan, dict) or plan.get('executionKind') != 'host_thread':
        raise ValueError('host-thread approved plan required')
    if plan.get('threadId') != owner_thread_id:
        raise ValueError('owner thread and approved plan mismatch')
    verified = verify_envelope(authorization_manifest, authorization_result, now=now)
    envelope = verified['envelope']
    binding = envelope['binding']
    expected_request = {
        'method': ARCHIVE_WORKTREE,
        'binding': binding,
        'params': {'root': declaration['worktree']},
    }
    if envelope.get('archiveRequestSha') != digest(expected_request):
        raise ValueError('archive request hash does not match authorization envelope')
    if envelope.get('worktree') != declaration.get('worktree'):
        raise ValueError('authorization worktree mismatch')
    host_result = adapter.archive_worktree(declaration['worktree'])
    return {
        'status': 'host_result',
        'readOnly': False,
        'deletionPerformed': None,
        'cleanupEligible': False,
        'hostSignedIdentityVerified': False,
        'preObservableProtection': False,
        'authorizationBindingVerified': True,
        'authorizationFingerprint': verified['authorizationFingerprint'],
        'protectionMode': 'host_archive_worktree_execution',
        'requiresHostCompletionVerification': True,
        'hostArchiveResult': host_result,
    }

"""验证 O14 verified / landed / pushVerified 三段交付证据。"""
import json
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from delivery import (  # noqa: E402
    add_landed,
    add_push_verified,
    add_verified,
    delivery_status,
    ensure_delivery,
)
from state_schema import validate_state  # noqa: E402

results = []
A = 'a' * 40
B = 'b' * 40
C = 'c' * 40
P = '1' * 64
R = '2' * 64


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def rejected(name, fn):
    try:
        fn()
    except (ValueError, RuntimeError):
        check(name, True)
    else:
        check(name, False)


state = {'integration': {'baseSha': A, 'currentSha': A}}
ensure_delivery(state)
check('delivery-initialized', set(state['delivery']) == {'verified', 'landed', 'pushVerified'})
check('unverified-current', delivery_status(state)['stage'] == 'unverified')

verified = add_verified(
    state, 'worker', 'a', 1, 1, B, report_sha=R, plan_hash=P,
    verified_at='2026-11-16T00:00:00+08:00')
check('verified-sha', verified['sha'] == B)
check('verified-report-sha', verified['reportSha'] == R)
check('verified-status', delivery_status(state, B)['stage'] == 'verified')
check('duplicate-verified-deduped',
      add_verified(state, 'worker', 'a', 1, 1, B, report_sha=R, plan_hash=P) is verified)

add_verified(state, 'integration', 'a', 1, 1, B, report_sha=R, plan_hash=P)
rejected('push-before-landed-rejected', lambda: add_push_verified(
    state, B, 'origin', 'refs/heads/main', B, 'user requested push'))
landed = add_landed(
    state, 'codex/a', B, A, 1, P,
    landed_at='2026-11-16T00:01:00+08:00')
check('landed-sha', landed['sha'] == B)
check('landed-default-status', delivery_status(state, B)['stage'] == 'landed')
rejected('wrong-remote-sha-rejected', lambda: add_push_verified(
    state, B, 'origin', 'refs/heads/main', C, 'user requested push'))
push = add_push_verified(
    state, B, 'origin', 'refs/heads/main', B, 'User explicitly requested push on 2026-11-16',
    observed_at='2026-11-16T00:02:00+08:00')
check('push-sha', push['sha'] == B)
check('push-status', delivery_status(state, B)['stage'] == 'pushVerified')
check('push-authorization-preserved', 'explicitly requested push' in push['authorizationEvidence'])

new_state = {'integration': {'baseSha': A, 'currentSha': C}, 'delivery': state['delivery']}
new_status = delivery_status(new_state)
check('new-head-unverified', new_status['stage'] == 'unverified')
check('old-verified-stale', len(new_status['stale']['verified']) == 2)
check('old-landed-stale', len(new_status['stale']['landed']) == 1)
check('old-push-stale', len(new_status['stale']['pushVerified']) == 1)
check('current-push-none', new_status['pushVerified'] is None)

rejected('invalid-sha-rejected', lambda: add_verified(
    state, 'worker', 'a', 1, 1, 'abc', report_sha=R, plan_hash=P))
rejected('invalid-stage-rejected', lambda: add_verified(
    state, 'release', 'a', 1, 1, B, report_sha=R, plan_hash=P))
rejected('missing-push-authorization-rejected', lambda: add_push_verified(
    state, B, 'origin', 'refs/heads/main', B, ''))
rejected('invalid-ref-rejected', lambda: add_push_verified(
    state, B, 'origin', 'main', B, 'user requested push'))

schema_state = json.loads((project / 'references/state.example.json').read_text())
schema_state['delivery'] = state['delivery']
validate_state(schema_state)
check('delivery-schema-compatible', True)
bad_schema_state = json.loads((project / 'references/state.example.json').read_text())
bad_schema_state['delivery'] = {'verified': [{'stage': 'worker'}], 'landed': [], 'pushVerified': []}
try:
    validate_state(bad_schema_state)
except ValueError:
    check('delivery-invalid-record-rejected', True)
else:
    check('delivery-invalid-record-rejected', False)

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O14 delivery records only; no push or host API call',
}, ensure_ascii=False, indent=2))

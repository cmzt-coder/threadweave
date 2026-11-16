"""验证 O09 不可变制品交接、身份校验和归档保全。"""
import json
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from handoff import (  # noqa: E402
    HandoffError,
    archive_handoff,
    load_manifest,
    verify_handoff,
)

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def rejected(name, fn):
    try:
        fn()
    except (HandoffError, ValueError):
        check(name, True)
    else:
        check(name, False)


with tempfile.TemporaryDirectory(prefix='threadweave-handoff-') as d:
    d = Path(d)
    producer = d / 'producer'; producer.mkdir()
    archive = d / 'archive'; archive.mkdir()
    (producer / 'result.json').write_text('{"result":1}\n', encoding='utf-8')
    (producer / 'other.txt').write_text('other\n', encoding='utf-8')

    result = archive_handoff(
        producer, archive, 'run-h', 'a', 1, 'a' * 40, 3, 'b',
        'integration fixture', ['result.json'],
        created_at='2026-11-16T00:00:00+08:00')
    manifest = result['manifest']
    manifest_path = Path(result['manifestPath'])
    check('manifest-created', manifest_path.is_file())
    check('manifest-id', manifest['handoffId'].startswith('handoff_'))
    check('manifest-producer', manifest['producer']['role'] == 'a')
    check('manifest-receiver', manifest['receiver']['role'] == 'b')
    check('manifest-artifact-sha', len(manifest['artifacts'][0]['sha256']) == 64)
    check('manifest-verified',
          verify_handoff(manifest_path, archive, run_id='run-h',
                         producer='a', receiver='b', purpose='integration fixture')['runId'] == 'run-h')

    rejected('wrong-run-rejected', lambda: verify_handoff(
        manifest_path, archive, run_id='other'))
    rejected('wrong-producer-rejected', lambda: verify_handoff(
        manifest_path, archive, producer='other'))
    rejected('wrong-receiver-rejected', lambda: verify_handoff(
        manifest_path, archive, receiver='other'))
    rejected('wrong-purpose-rejected', lambda: verify_handoff(
        manifest_path, archive, purpose='other'))

    archived = archive / manifest['artifacts'][0]['archivePath']
    saved = archived.read_bytes()
    archived.write_text('tampered\n', encoding='utf-8')
    rejected('tampered-archive-rejected', lambda: verify_handoff(
        manifest_path, archive, receiver='b'))
    archived.write_bytes(saved)
    (producer / 'result.json').unlink()
    check('producer-cleanup-preserves-archive', verify_handoff(
        manifest_path, archive, receiver='b')['handoffId'] == manifest['handoffId'])

    (producer / 'result.json').write_text('{"result":1}\n', encoding='utf-8')
    again = archive_handoff(
        producer, archive, 'run-h', 'a', 1, 'a' * 40, 3, 'b',
        'integration fixture', ['result.json'],
        created_at='2026-11-16T00:00:00+08:00')
    check('same-handoff-idempotent', again['manifest']['handoffId'] == manifest['handoffId'])
    check('same-artifact-path', again['manifest']['artifacts'][0]['archivePath']
          == manifest['artifacts'][0]['archivePath'])

    (producer / 'result.json').write_text('{"result":2}\n', encoding='utf-8')
    changed = archive_handoff(
        producer, archive, 'run-h', 'a', 1, 'a' * 40, 3, 'b',
        'integration fixture', ['result.json'],
        created_at='2026-11-16T00:01:00+08:00')
    check('changed-content-new-handoff', changed['manifest']['handoffId'] != manifest['handoffId'])
    check('changed-content-no-overwrite',
          (archive / manifest['artifacts'][0]['archivePath']).read_bytes() == saved)
    check('changed-content-verifiable', verify_handoff(
        Path(changed['manifestPath']), archive, receiver='b')['handoffId'] == changed['manifest']['handoffId'])

    rejected('path-traversal-rejected', lambda: archive_handoff(
        producer, archive, 'run-bad', 'a', 1, 'a' * 40, 1, 'b',
        'bad', ['../escape.txt']))

    bad_manifest = json.loads(manifest_path.read_text())
    bad_manifest['purpose'] = 'changed after seal'
    manifest_path.write_text(json.dumps(bad_manifest), encoding='utf-8')
    rejected('manifest-tamper-rejected', lambda: load_manifest(manifest_path))

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'O09 local immutable handoff only; no host API calls',
}, ensure_ascii=False, indent=2))

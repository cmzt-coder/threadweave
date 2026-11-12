"""验证 report/notes 归档、manifest 与哈希校验，不调用宿主 API。"""
import json
import sys
import tempfile
import threading
from unittest.mock import patch
from pathlib import Path

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from archival import ArchiveConflict, archive_artifacts, verify_archive  # noqa: E402

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


with tempfile.TemporaryDirectory(prefix='threadweave-archive-') as d:
    d = Path(d)
    run_dir = d / 'leader' / '.threadweave' / 'run-1'
    worker = d / 'worker'
    worker.mkdir(parents=True)
    report = worker / 'report.md'
    notes = worker / 'notes.md'
    report.write_text('# Report\n\nCOMPLETE\n', encoding='utf-8')
    notes.write_text('# Notes\n', encoding='utf-8')

    entries = archive_artifacts(run_dir, 'a', {'report.md': report, 'notes.md': notes})
    check('archive-two-entries', len(entries) == 2)
    target_report = run_dir / 'reports' / 'a' / '1' / '1' / 'report.md'
    target_notes = run_dir / 'reports' / 'a' / '1' / '1' / 'notes.md'
    check('archive-report-exists', target_report.is_file())
    check('archive-notes-exists', target_notes.is_file())
    check('archive-outside-worker', not str(target_report).startswith(str(worker)))

    report.unlink()
    notes.unlink()
    check('archive-survives-source-deletion',
          target_report.is_file() and target_notes.is_file())

    manifest = json.loads((run_dir / 'archive-manifest.json').read_text(encoding='utf-8'))
    check('manifest-has-two', len(manifest) == 2)
    check('manifest-has-sha', all(len(e['sha256']) == 64 for e in manifest))
    check('manifest-has-size', all(e['size'] > 0 for e in manifest))
    check('verify-clean',
          verify_archive(run_dir, [('a', 'report.md'), ('a', 'notes.md')]) == [])

    target_notes.unlink()
    problems = verify_archive(run_dir, [('a', 'report.md'), ('a', 'notes.md')])
    check('verify-missing-file', any('missing file' in p for p in problems))

    notes.write_text('# Notes restored\n', encoding='utf-8')
    archive_artifacts(run_dir, 'a', {'notes.md': notes})
    target_notes.write_text('# Tampered\n', encoding='utf-8')
    problems = verify_archive(run_dir, [('a', 'notes.md')])
    check('verify-hash-mismatch', any('hash mismatch' in p for p in problems))

    conflict_src = worker / 'report.md'
    conflict_src.write_text('# Different\n', encoding='utf-8')
    try:
        archive_artifacts(run_dir, 'a', {'report.md': conflict_src})
        check('archive-conflict-rejected', False)
    except ArchiveConflict:
        check('archive-conflict-rejected', True)

    same_src = worker / 'notes-same.md'
    same_src.write_text('# Notes restored\n', encoding='utf-8')
    archive_artifacts(run_dir, 'a', {'notes-same.md': same_src})
    archive_artifacts(run_dir, 'a', {'notes-same.md': same_src})
    manifest = json.loads((run_dir / 'archive-manifest.json').read_text(encoding='utf-8'))
    names = [e['name'] for e in manifest if e['role'] == 'a']
    check('archive-idempotent', len(names) == len(set(names)))

    concurrent_dir = run_dir / 'concurrent'
    barrier = threading.Barrier(2)
    errors = []

    def archive_one(name):
        try:
            barrier.wait()
            archive_artifacts(concurrent_dir, 'a', {name: worker / name})
        except Exception as exc:
            errors.append(exc)

    for name in ('report-a.md', 'report-b.md'):
        (worker / name).write_text(name, encoding='utf-8')
    threads = [
        threading.Thread(target=archive_one, args=(name,))
        for name in ('report-a.md', 'report-b.md')
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check('concurrent-archive-no-errors', not errors)
    concurrent_manifest = json.loads(
        (concurrent_dir / 'archive-manifest.json').read_text(encoding='utf-8'))
    check('concurrent-archive-both-entries', len(concurrent_manifest) == 2)

    retry_dir = run_dir / 'retry'
    retry_source = worker / 'retry.md'
    retry_source.write_text('retry', encoding='utf-8')
    with patch('archival.shutil.copy2', side_effect=OSError('copy failed')):
        try:
            archive_artifacts(retry_dir, 'a', {'retry.md': retry_source})
            check('copy-failure-raised', False)
        except OSError:
            check('copy-failure-raised', True)
    check('copy-failure-no-temp',
          not list((retry_dir / 'reports' / 'a' / '1' / '1').glob('*.tmp')))
    archive_artifacts(retry_dir, 'a', {'retry.md': retry_source})
    check('copy-retry-succeeds',
          (retry_dir / 'reports' / 'a' / '1' / '1' / 'retry.md').is_file())

    manifest_fail_dir = run_dir / 'manifest-fail'
    manifest_source = worker / 'manifest.md'
    manifest_source.write_text('m', encoding='utf-8')
    with patch('archival.write_state_atomic', side_effect=OSError('manifest failed')):
        try:
            archive_artifacts(manifest_fail_dir, 'a', {'manifest.md': manifest_source})
            check('manifest-failure-raised', False)
        except OSError:
            check('manifest-failure-raised', True)
    check('manifest-failure-no-manifest',
          not (manifest_fail_dir / 'archive-manifest.json').exists())
    archive_artifacts(manifest_fail_dir, 'a', {'manifest.md': manifest_source})
    check('manifest-retry-succeeds',
          (manifest_fail_dir / 'archive-manifest.json').is_file())

    escape_dir = run_dir / 'escape-check'
    escape_dir.mkdir(parents=True, exist_ok=True)
    escape_target = worker / 'outside.md'
    escape_target.write_text('outside', encoding='utf-8')
    escape_manifest = [{
        'role': 'a', 'attempt': 1, 'revision': 1, 'name': 'outside.md',
        'source': str(escape_target), 'target': str(escape_target),
        'sha256': '0' * 64, 'size': 7,
        'archivedAt': '2026-11-12T10:00:00+08:00',
    }]
    (escape_dir / 'archive-manifest.json').write_text(
        json.dumps(escape_manifest), encoding='utf-8')
    problems = verify_archive(escape_dir, [('a', 1, 1, 'outside.md')])
    check('manifest-target-escape-rejected',
          any('escapes reports root' in problem for problem in problems))

    attempt2_source = worker / 'report-attempt2.md'
    attempt2_source.write_text('# Attempt 2' + chr(10), encoding='utf-8')
    archive_artifacts(run_dir, 'a', {'report.md': attempt2_source}, attempt=2, revision=1)
    target_attempt2 = run_dir / 'reports' / 'a' / '2' / '1' / 'report.md'
    check('archive-attempt2-exists', target_attempt2.is_file())
    check('archive-attempts-isolated',
          target_report.read_text(encoding='utf-8') != target_attempt2.read_text(encoding='utf-8'))
    check('verify-multi-attempt',
          verify_archive(run_dir, [('a', 1, 1, 'report.md'), ('a', 2, 1, 'report.md')]) == [])

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Report/notes archival only; no host API calls',
}, ensure_ascii=False, indent=2))

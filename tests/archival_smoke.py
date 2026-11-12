"""验证 report/notes 归档、manifest 与哈希校验，不调用宿主 API。"""
import json
import sys
import tempfile
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
    target_report = run_dir / 'reports' / 'a' / 'report.md'
    target_notes = run_dir / 'reports' / 'a' / 'notes.md'
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

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'Report/notes archival only; no host API calls',
}, ensure_ascii=False, indent=2))

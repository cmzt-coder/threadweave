"""范围预留覆盖未来文件、路径边界、重命名和串行资源组。"""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from scope import check_changes, check_reservations

results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def worker(role, paths, exceptions=None):
    return {'role': role, 'attempt': 1, 'ownedPaths': paths,
            'scopeExceptions': [] if exceptions is None else exceptions}


check('literal-owned', check_changes(['src/a.py'], ['src/a.py']) == [])
check('future-addition', check_changes(['src/new.py'], ['src/**']) == [])
check('deleted-path', check_changes(['old/deleted.py'], ['old/*']) == [])
check('rename-both-ends', check_changes(['old/a.py', 'new/a.py'], ['old/**', 'new/**']) == [])
check('rename-outside-old', bool(check_changes(['old/a.py', 'new/a.py'], ['new/**'])))
check('rename-outside-new', bool(check_changes(['old/a.py', 'new/a.py'], ['old/**'])))
check('single-star-does-not-cross', bool(check_changes(['src/nested/a.py'], ['src/*'])))
check('double-star-crosses', check_changes(['src/nested/deep/a.py'], ['src/**']) == [])
check('globstar-slash-zero-dirs', check_changes(['a.py', 'a/b.py'], ['**/*.py']) == [])
check('globstar-slash-requires-slash', bool(check_changes(['src/xa.py'], ['src/**/a.py'])))
check('globstar-slash-multiple-dirs', check_changes(['src/x/y/a.py'], ['src/**/a.py']) == [])
check('exception-permits-path', check_changes(['README.md'], ['src/**'], ['README.md']) == [])
check('missing-ownership-denied', bool(check_changes([], [])))
check('missing-ownership-not-replaced-by-exception', bool(check_changes(['a'], [], ['a'])))
check('changed-path-list-required', bool(check_changes('a', ['**'])))
check('declaration-list-required', bool(check_changes(['a'], '**')))
check('question-mark', check_changes(['a.py'], ['?.py']) == [])
check('dotfiles-covered', check_changes(['.hidden'], ['*']) == [])
check('literal-wildcard-filename', bool(check_changes(['x*.py'], ['x.py'])))
check('literal-bracket-filename', check_changes(['x[1].py'], ['*']) == [])
for index, invalid in enumerate(['', '/tmp/a', '../a', 'a/../b', 'a//b', './a',
                                 'a/', 'a\\b', 'C:/a', 'a\x00b', None]):
    check(f'invalid-change-{index}', bool(check_changes([invalid], ['**'])))
    check(f'invalid-pattern-{index}', bool(check_changes([], [invalid])))
check('unsupported-pattern-fails-closed', bool(check_changes(['a'], ['[ab]'])))

with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
    root = Path(directory)
    def reserve(a, b, resources=None):
        return check_reservations([worker('a', a), worker('b', b)], root, resources)

    check('new-files-overlap-without-expansion', bool(reserve(['src/**'], ['src/future.py'])))
    check('different-recursive-directories', reserve(['src/**'], ['tests/**']) == [])
    check('different-literal-files', reserve(['src/a.py'], ['src/b.py']) == [])
    check('star-and-literal-overlap', bool(reserve(['src/*'], ['src/a.py'])))
    check('star-and-nested-disjoint', reserve(['src/*'], ['src/nested/a.py']) == [])
    check('recursive-suffix-disjoint', reserve(['src/**/*.py'], ['src/**/*.js']) == [])
    check('recursive-overlap', bool(reserve(['**/a.py'], ['src/**'])))
    check('zero-dir-overlap', bool(reserve(['src/**/a.py'], ['src/a.py'])))
    check('partial-star-overlap', bool(reserve(['src/a*.py'], ['src/*b.py'])))
    check('partial-star-disjoint', reserve(['src/a*.py'], ['src/b*.py']) == [])
    check('exception-reserved', bool(check_reservations([
        worker('a', ['src/**'], ['shared/**']), worker('b', ['shared/new.py'])], root)))
    check('missing-declaration', bool(check_reservations([{'role': 'a'}], root)))
    check('empty-workers', check_reservations([], root) == [])
    check('bad-workers', bool(check_reservations({}, root)))
    check('bad-worker-entry', bool(check_reservations([None], root)))
    check('unsupported-reservation-explained', '无法证明' in ' '.join(reserve(['a[bc]'], ['x'])))
    resource = [{'name': 'migration', 'paths': ['db/**']}]
    check('serialized-different-files', any('串行资源组' in e for e in reserve(
        ['db/a.sql'], ['db/b.sql'], resource)))
    check('serialized-nonmember', reserve(['db/a.sql'], ['src/a.py'], resource) == [])
    check('serialized-exception-counted', bool(check_reservations([
        worker('a', ['src/a'], ['db/a']), worker('b', ['db/b'])], root, resource)))
    check('resource-overlapping-pattern', bool(reserve(['a/**'], ['b/**'], [
        {'name': 'global', 'paths': ['**/*.py']}])))
    check('bad-resource-list', bool(reserve(['a'], ['b'], {})))
    check('bad-resource-entry', bool(reserve(['a'], ['b'], [None])))
    check('bad-resource-pattern', bool(reserve(['a'], ['b'], [{'name': 'bad', 'paths': ['../x']}])))
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    check('outside-link-future-child', bool(check_reservations([worker('a', ['escape/new.py'])], root)))
    check('outside-link-itself', bool(check_reservations([worker('a', ['escape'])], root)))
    check('outside-link-recursive', bool(check_reservations([worker('a', ['**/*.py'])], root)))
    check('outside-link-unrelated', check_reservations([worker('a', ['src/**'])], root) == [])
    (root / 'internal').mkdir()
    (root / 'inside').symlink_to(root / 'internal', target_is_directory=True)
    (root / 'internal' / 'nested-escape').symlink_to(outside, target_is_directory=True)
    check('inside-alias-outside-child-rejected', bool(check_reservations(
        [worker('a', ['inside/nested-escape/new.py'])], root)))
    check('inside-link-allowed', check_reservations([worker('a', ['inside/safe/**'])], root) == [])
    (root / 'broken').symlink_to(Path(outside) / 'missing')
    check('broken-outside-link-rejected', bool(check_reservations([worker('a', ['broken/**'])], root)))
    (root / 'cycle').symlink_to('cycle')
    check('cycle-link-rejected', bool(check_reservations([worker('a', ['cycle/**'])], root)))
    check('invalid-root', bool(check_reservations([], root / 'missing')))

print(json.dumps({'passed': len(results), 'checks': results}, ensure_ascii=False, indent=2))

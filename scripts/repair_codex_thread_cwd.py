"""修复 Codex thread 的陈旧 cwd；默认只读预演，应用前必须关闭 Codex 并备份数据库。"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess


def _canonical(path):
    value = Path(path)
    if not value.is_absolute():
        raise ValueError('cwd must be absolute')
    resolved = value.resolve()
    if str(value) != str(resolved) or not resolved.is_dir():
        raise ValueError('cwd must be canonical and exist')
    return str(resolved)


def _git(path, *args):
    result = subprocess.run(
        ['git', '-C', path, *args], capture_output=True, text=True)
    if result.returncode:
        raise ValueError('Git observation failed: ' + ' '.join(args[:2]))
    return result.stdout.rstrip('\n')


def _git_common_dir(path):
    value = Path(_git(path, 'rev-parse', '--git-common-dir'))
    return str((Path(path) / value).resolve() if not value.is_absolute() else value.resolve())


def _worktree_paths(repository):
    paths = []
    for line in _git(repository, 'worktree', 'list', '--porcelain').splitlines():
        if line.startswith('worktree '):
            paths.append(str(Path(line[len('worktree '):]).resolve()))
    return paths


def _connect(path, *, read_only):
    if read_only:
        uri = Path(path).resolve().as_uri() + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True)
        connection.execute('PRAGMA query_only=ON')
        return connection
    return sqlite3.connect(str(Path(path).resolve()))


def _thread(db, thread_id):
    connection = _connect(db, read_only=True)
    try:
        rows = connection.execute(
            'SELECT id, cwd, project_id, title, archived FROM threads WHERE id=?',
            (thread_id,)).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise ValueError('exact thread row required')
    keys = ('id', 'cwd', 'projectId', 'title', 'archived')
    return dict(zip(keys, rows[0]))


def _attachment_count(db, thread_id):
    connection = _connect(db, read_only=True)
    try:
        return connection.execute(
            'SELECT COUNT(*) FROM thread_attachments WHERE thread_id=?',
            (thread_id,)).fetchone()[0]
    finally:
        connection.close()


def _quick_check(connection):
    values = [row[0] for row in connection.execute('PRAGMA quick_check').fetchall()]
    if values != ['ok']:
        raise ValueError('SQLite quick_check failed')


def inspect_repair(db, thread_id, new_cwd, expected_cwd=None):
    """只读核对 thread cwd 修复条件，不写数据库。"""
    db = Path(db).resolve()
    if not db.is_file() or db.is_symlink():
        raise ValueError('non-symlink Codex state database required')
    new_cwd = _canonical(new_cwd)
    row = _thread(db, thread_id)
    old_cwd = _canonical(row['cwd'])
    if expected_cwd is not None and old_cwd != _canonical(expected_cwd):
        raise ValueError('thread cwd does not match expected stale cwd')
    if old_cwd == new_cwd:
        raise ValueError('thread cwd already equals target')
    if _git(new_cwd, 'rev-parse', '--show-toplevel') != new_cwd:
        raise ValueError('target must be an exact Git worktree root')
    if _git_common_dir(old_cwd) != _git_common_dir(new_cwd):
        raise ValueError('old and new cwd must share a Git common directory')
    paths = _worktree_paths(new_cwd)
    if new_cwd not in paths or old_cwd not in paths:
        raise ValueError('old and new cwd must be registered Git worktrees')
    connection = _connect(db, read_only=True)
    try:
        _quick_check(connection)
    finally:
        connection.close()
    return {
        'status': 'planned',
        'database': str(db),
        'thread': row,
        'oldCwd': old_cwd,
        'newCwd': new_cwd,
        'attachmentCount': _attachment_count(db, thread_id),
        'attachmentsWillChange': False,
        'gitMetadataWillChange': False,
        'worktreeWillChange': False,
        'requiresAppClosed': True,
    }


def repair(db, thread_id, new_cwd, expected_cwd, backup, *, apply=False, app_closed=False):
    """应用单个 thread 的 cwd 修复；任何前置条件失败都不写库。"""
    plan = inspect_repair(db, thread_id, new_cwd, expected_cwd)
    if not apply:
        return plan
    if app_closed is not True:
        raise ValueError('explicit app-closed confirmation required')
    db = Path(db).resolve()
    backup = Path(backup).resolve()
    if backup.exists():
        raise ValueError('backup path already exists')
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = _connect(db, read_only=True)
    target = sqlite3.connect(str(backup))
    try:
        source.backup(target)
        target.commit()
        _quick_check(target)
    finally:
        target.close()
        source.close()
    connection = _connect(db, read_only=False)
    try:
        connection.execute('BEGIN IMMEDIATE')
        cursor = connection.execute(
            'UPDATE threads SET cwd=? WHERE id=? AND cwd=?',
            (plan['newCwd'], thread_id, plan['oldCwd']))
        if cursor.rowcount != 1:
            connection.rollback()
            raise ValueError('thread changed during repair; no update applied')
        _quick_check(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    current = _thread(db, thread_id)
    if current['cwd'] != plan['newCwd']:
        raise ValueError('cwd update did not persist')
    if _attachment_count(db, thread_id) != plan['attachmentCount']:
        raise ValueError('attachment count changed unexpectedly')
    result = dict(plan)
    result.update(status='repaired', backup=str(backup), attachmentsWillChange=False)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path.home() / '.codex/state_5.sqlite')
    parser.add_argument('--thread-id', required=True)
    parser.add_argument('--new-cwd', required=True)
    parser.add_argument('--expected-cwd', required=True)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--app-closed', action='store_true')
    args = parser.parse_args(argv)
    try:
        backup = args.backup or args.db.with_name(
            args.db.name + '.thread-cwd-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.bak')
        result = repair(args.db, args.thread_id, args.new_cwd, args.expected_cwd, backup,
                        apply=args.apply, app_closed=args.app_closed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

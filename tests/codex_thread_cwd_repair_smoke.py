"""Codex thread cwd 修复工具的临时 Git/SQLite 受控测试；不修改真实 Codex 状态。"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from repair_codex_thread_cwd import repair

checks = []


def check(name, condition):
    assert condition, name
    checks.append(name)


def rejected(name, call):
    try:
        call()
    except ValueError:
        checks.append(name)
    else:
        raise AssertionError(name)


def git(root, *args):
    return subprocess.check_output(
        ['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()


with tempfile.TemporaryDirectory(prefix='threadweave-cwd-repair-') as td:
    base = Path(td).resolve()
    repo = base / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Validation')
    git(repo, 'config', 'user.email', 'validation@example.invalid')
    (repo / 'tracked').write_text('base')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    worktree = base / 'worker'
    git(repo, 'worktree', 'add', '-b', 'codex/cwd-repair', str(worktree))

    db = base / 'state.sqlite'
    connection = sqlite3.connect(db)
    connection.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA wal_autocheckpoint=0;
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            project_id TEXT,
            title TEXT NOT NULL,
            archived INTEGER NOT NULL
        );
        CREATE TABLE thread_attachments (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            attachment_type TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
    ''')
    thread_id = 'thread-cwd-repair'
    connection.execute('INSERT INTO threads VALUES (?,?,?,?,?)',
                       (thread_id, str(worktree), None, 'repair fixture', 0))
    connection.execute('INSERT INTO thread_attachments VALUES (?,?,?,?,?,?)',
                       ('attachment-1', thread_id, 'worktree', str(worktree), '{}', 1))
    connection.commit()
    connection.close()

    def db_state():
        item = sqlite3.connect(db)
        try:
            cwd = item.execute('SELECT cwd FROM threads WHERE id=?', (thread_id,)).fetchone()[0]
            count = item.execute(
                'SELECT COUNT(*) FROM thread_attachments WHERE thread_id=?', (thread_id,)).fetchone()[0]
            return cwd, count
        finally:
            item.close()

    backup = base / 'state.backup.sqlite'
    dry = repair(db, thread_id, str(repo), str(worktree), backup)
    check('dry-run-planned', dry['status'] == 'planned' and dry['oldCwd'] == str(worktree)
          and dry['newCwd'] == str(repo))
    check('dry-run-no-database-write', db_state() == (str(worktree), 1))
    check('dry-run-no-backup-write', not backup.exists())
    check('dry-run-preserves-attachment', dry['attachmentCount'] == 1)

    rejected('apply-requires-app-closed',
             lambda: repair(db, thread_id, str(repo), str(worktree), backup, apply=True))
    rejected('wrong-expected-cwd',
             lambda: repair(db, thread_id, str(repo), str(base / 'other'), backup))
    rejected('wrong-thread',
             lambda: repair(db, 'missing-thread', str(repo), str(worktree), backup))
    rejected('target-not-git-root',
             lambda: repair(db, thread_id, str(base), str(worktree), backup))
    check('failed-preconditions-no-write', db_state() == (str(worktree), 1) and not backup.exists())

    result = repair(db, thread_id, str(repo), str(worktree), backup,
                    apply=True, app_closed=True)
    check('repair-applied', result['status'] == 'repaired')
    check('thread-cwd-updated', db_state() == (str(repo), 1))
    check('attachment-preserved', db_state()[1] == 1)
    check('backup-created', backup.is_file())
    backup_db = sqlite3.connect(backup)
    try:
        backup_cwd = backup_db.execute(
            'SELECT cwd FROM threads WHERE id=?', (thread_id,)).fetchone()[0]
        backup_count = backup_db.execute(
            'SELECT COUNT(*) FROM thread_attachments WHERE thread_id=?', (thread_id,)).fetchone()[0]
        check('backup-contains-old-state', backup_cwd == str(worktree) and backup_count == 1)
        check('backup-integrity', backup_db.execute('PRAGMA quick_check').fetchone()[0] == 'ok')
    finally:
        backup_db.close()
    rejected('no-repeat-after-repair',
             lambda: repair(db, thread_id, str(repo), str(worktree), base / 'second.backup'))

    cli = subprocess.run([
        sys.executable, str(project / 'scripts/repair_codex_thread_cwd.py'),
        '--db', str(db), '--thread-id', thread_id, '--new-cwd', str(repo),
        '--expected-cwd', str(repo),
    ], text=True, capture_output=True, timeout=30)
    check('cli-blocked-after-repair', cli.returncode == 1
          and json.loads(cli.stdout)['status'] == 'blocked')

print(json.dumps({'scope': 'controlled_integration', 'realCodexStateModified': False,
                  'passed': len(checks), 'checks': checks}, ensure_ascii=False))

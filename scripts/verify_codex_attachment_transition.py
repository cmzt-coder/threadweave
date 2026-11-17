"""只读验证同一 Codex thread 的附件从 active worktree 变为 archived_worktree。"""
import argparse
import json
from pathlib import Path
import sqlite3

from state_store import read_state


def _database(path):
    value = Path(path).resolve()
    if not value.is_file() or value.is_symlink() or value.suffix not in ('.sqlite', '.db', '.bak'):
        raise ValueError('non-symlink SQLite database snapshot required')
    return value


def _connect(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.execute('PRAGMA query_only=ON')
    return connection


def _absolute(value, label):
    path = Path(value)
    if not isinstance(value, str) or not path.is_absolute() or str(path.resolve()) != value:
        raise ValueError(label + ' must be a canonical absolute path')
    return value


def _thread(connection, thread_id):
    rows = connection.execute(
        'SELECT id, cwd FROM threads WHERE id=?', (thread_id,)).fetchall()
    if len(rows) != 1:
        raise ValueError('exact thread row required')
    return {'id': rows[0][0], 'cwd': rows[0][1]}


def _attachment(connection, thread_id, identity_key):
    rows = connection.execute(
        'SELECT id, thread_id, attachment_type, identity_key FROM thread_attachments '
        'WHERE thread_id=? AND identity_key=?',
        (thread_id, identity_key)).fetchall()
    if len(rows) != 1:
        raise ValueError('exact attachment row required')
    return {'id': rows[0][0], 'threadId': rows[0][1],
            'type': rows[0][2], 'identityKey': rows[0][3]}


def _quick_check(connection, label):
    values = [row[0] for row in connection.execute('PRAGMA quick_check').fetchall()]
    if values != ['ok']:
        raise ValueError(label + ' SQLite quick_check failed')


def verify_transition(before_db, after_db, thread_id, identity_key, expected_cwd):
    """验证本地 Codex 状态库中的同一 thread 附件状态转换；不声称宿主签名。"""
    before_path = _database(before_db)
    after_path = _database(after_db)
    identity_key = _absolute(identity_key, 'identityKey')
    expected_cwd = _absolute(expected_cwd, 'expectedCwd')
    if not Path(expected_cwd).is_dir():
        raise ValueError('expectedCwd does not exist')
    before = _connect(before_path)
    after = _connect(after_path)
    try:
        _quick_check(before, 'before')
        _quick_check(after, 'after')
        before_thread = _thread(before, thread_id)
        after_thread = _thread(after, thread_id)
        before_attachment = _attachment(before, thread_id, identity_key)
        after_attachment = _attachment(after, thread_id, identity_key)
    finally:
        before.close()
        after.close()
    if before_thread['id'] != after_thread['id']:
        raise ValueError('thread identity changed across snapshots')
    if after_thread['cwd'] != expected_cwd:
        raise ValueError('after snapshot thread cwd mismatch')
    if before_attachment['type'] != 'worktree':
        raise ValueError('before attachment is not active worktree')
    if after_attachment['type'] != 'archived_worktree':
        raise ValueError('after attachment is not archived_worktree')
    if before_attachment['threadId'] != thread_id or after_attachment['threadId'] != thread_id:
        raise ValueError('attachment thread binding mismatch')
    return {
        'version': 1,
        'status': 'transition_verified',
        'readOnly': True,
        'deletionPerformed': False,
        'cleanupEligible': False,
        'localThreadContinuityVerified': True,
        'threadIdentitySource': 'local_codex_state_db',
        'hostSignedIdentityVerified': False,
        'threadId': thread_id,
        'identityKey': identity_key,
        'beforeCwd': before_thread['cwd'],
        'afterCwd': after_thread['cwd'],
        'beforeAttachmentId': before_attachment['id'],
        'afterAttachmentId': after_attachment['id'],
        'transition': 'worktree->archived_worktree',
        'beforeDatabase': str(before_path),
        'afterDatabase': str(after_path),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args(argv)
    try:
        spec = read_state(args.manifest)
        result = verify_transition(
            spec['beforeDatabase'], spec['afterDatabase'], spec['threadId'],
            spec['identityKey'], spec['expectedCwd'])
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        result = {'status': 'blocked', 'readOnly': True, 'cleanupEligible': False,
                  'localThreadContinuityVerified': False, 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'transition_verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())

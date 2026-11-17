"""Codex thread 附件状态转换验证器测试；使用临时 SQLite快照，不修改真实状态。"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'scripts'))
from verify_codex_attachment_transition import verify_transition

checks = []


def check(name, condition):
    assert condition, name
    checks.append(name)


def rejected(name, call):
    try:
        call()
    except (ValueError, sqlite3.Error):
        checks.append(name)
    else:
        raise AssertionError(name)


def create_db(path, cwd, attachment_type, attachment_id='attachment-1'):
    connection = sqlite3.connect(path)
    connection.executescript('''
        CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT NOT NULL);
        CREATE TABLE thread_attachments (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            attachment_type TEXT NOT NULL,
            identity_key TEXT NOT NULL
        );
    ''')
    thread_id = 'thread-transition'
    identity = str(Path('/Users/luzhentian/.codex/worktrees/fixture').resolve())
    connection.execute('INSERT INTO threads VALUES (?,?)', (thread_id, str(cwd)))
    connection.execute('INSERT INTO thread_attachments VALUES (?,?,?,?)',
                       (attachment_id, thread_id, attachment_type, identity))
    connection.commit()
    connection.close()
    return thread_id, identity


with tempfile.TemporaryDirectory(prefix='threadweave-attachment-transition-') as td:
    base = Path(td).resolve()
    repo = base / 'repo'
    repo.mkdir()
    before = base / 'before.sqlite'
    after = base / 'after.sqlite'
    thread_id, identity = create_db(before, base / 'old-worktree', 'worktree', 'before-attachment')
    create_db(after, repo, 'archived_worktree', 'after-attachment')
    result = verify_transition(before, after, thread_id, identity, str(repo))
    check('transition-verified', result['status'] == 'transition_verified')
    check('local-continuity-only', result['localThreadContinuityVerified'] is True
          and result['hostSignedIdentityVerified'] is False)
    check('no-cleanup-authorization', result['cleanupEligible'] is False
          and result['deletionPerformed'] is False)
    check('transition-recorded', result['transition'] == 'worktree->archived_worktree')

    rejected('wrong-expected-cwd',
             lambda: verify_transition(before, after, thread_id, identity, str(base)))
    rejected('wrong-thread',
             lambda: verify_transition(before, after, 'missing-thread', identity, str(repo)))
    rejected('wrong-identity',
             lambda: verify_transition(before, after, thread_id, str(base / 'other'), str(repo)))

    before_archived = base / 'before-archived.sqlite'
    create_db(before_archived, base / 'old-worktree', 'archived_worktree')
    rejected('before-not-active',
             lambda: verify_transition(before_archived, after, thread_id, identity, str(repo)))
    after_active = base / 'after-active.sqlite'
    create_db(after_active, repo, 'worktree')
    rejected('after-not-archived',
             lambda: verify_transition(before, after_active, thread_id, identity, str(repo)))

    malformed = base / 'malformed.sqlite'
    sqlite3.connect(malformed).close()
    rejected('malformed-database',
             lambda: verify_transition(malformed, after, thread_id, identity, str(repo)))

    manifest = base / 'manifest.json'
    manifest.write_text(json.dumps({
        'beforeDatabase': str(before), 'afterDatabase': str(after),
        'threadId': thread_id, 'identityKey': identity, 'expectedCwd': str(repo),
    }))
    cli = subprocess.run([
        sys.executable, str(project / 'scripts/verify_codex_attachment_transition.py'), str(manifest)
    ], text=True, capture_output=True, timeout=30)
    check('cli-success', cli.returncode == 0
          and json.loads(cli.stdout)['status'] == 'transition_verified')
    bad_manifest = base / 'bad-manifest.json'
    bad_manifest.write_text(json.dumps({
        'beforeDatabase': str(before), 'afterDatabase': str(after),
        'threadId': thread_id, 'identityKey': identity, 'expectedCwd': str(base),
    }))
    cli = subprocess.run([
        sys.executable, str(project / 'scripts/verify_codex_attachment_transition.py'), str(bad_manifest)
    ], text=True, capture_output=True, timeout=30)
    check('cli-blocked', cli.returncode == 1 and json.loads(cli.stdout)['status'] == 'blocked')

print(json.dumps({'scope': 'controlled_integration', 'realCodexStateModified': False,
                  'passed': len(checks), 'checks': checks}, ensure_ascii=False))

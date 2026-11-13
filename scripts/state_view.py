"""从 state.json 重建只读 Markdown 视图；Markdown 不是状态输入。"""
import os
from pathlib import Path
import tempfile

from state_schema import validate_state
from state_store import StateLock, read_state, run_state_path


def _cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def render_state_view(state):
    validate_state(state)
    lines = ['# Threadweave 状态视图', '',
             '由 state.json 派生；不得编辑本文件来驱动状态转换。', '',
             'Run: ' + _cell(state['runId']),
             'Revision: ' + str(state.get('revision', 0)),
             'Status: ' + _cell(state['status']), '',
             '| Worker | 状态 | Thread ID | 最近宿主查询 | 观察时间 | 操作 ID |',
             '| --- | --- | --- | --- | --- | --- |']
    for worker in state['workers']:
        observed = worker.get('hostObservation', {})
        metadata = observed.get('metadata', {})
        cells = [str(worker['role']) + '#' + str(worker['attempt']), worker['state'],
                 worker.get('threadId', ''), metadata.get('status', 'unknown'),
                 observed.get('observedAt', ''), observed.get('operationId', '')]
        lines.append('| ' + ' | '.join(_cell(c) for c in cells) + ' |')
    lines += ['', '宿主查询仅为一次历史观察；不代表已停止、已通过验收或当前实时状态。', '']
    return '\n'.join(lines)


def write_state_view(repo, run_id):
    path = run_state_path(repo, run_id)
    lock = path.with_suffix('.lock')
    if lock.is_symlink():
        raise ValueError('state lock must not be a symlink')
    with StateLock(lock):
        return _write_view(path, run_id)


def _write_view(path, run_id):
    state = read_state(path)
    if state.get('runId') != run_id:
        raise ValueError('state view runId mismatch')
    text = render_state_view(state)
    dest = path.with_suffix('.md')
    if dest.is_symlink():
        raise ValueError('state view must not be a symlink')
    fd, temp = tempfile.mkstemp(prefix='.view-', dir=str(dest.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, dest)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return dest

"""验证返工预算与拒绝时的状态保全，不调用真实宿主。"""
import copy
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from controller import Controller
from host_adapter import UnsupportedHostAdapter
from state_store import read_state

checks = []

def check(name, condition):
    assert condition, name
    checks.append(name)


def make_state():
    state = json.loads((Path(__file__).resolve().parents[1] / 'references/state.example.json').read_text())
    state.update(runId='rework-test', status='observing', revision=0)
    state['integrationLog'] = []
    state['workers'] = [state['workers'][0]]
    w = state['workers'][0]
    w.update(state='reported', activityRevision=1, reworkCount=0)
    return state

with tempfile.TemporaryDirectory() as d:
    c = Controller(UnsupportedHostAdapter(), d, 'rework-test', make_state())
    c.save()
    for i in range(2):
        check('rework-count-' + str(i + 1), c.rework('a', 1) == i + 1)
        w = c.state['workers'][0]
        check('revision-increments-' + str(i), w['activityRevision'] == i + 2)
        w['state'] = 'reported'
        c.save()
    before = copy.deepcopy(c.state)
    disk = c.state_path.read_bytes()
    try:
        c.rework('a', 1)
    except RuntimeError:
        check('third-rework-rejected', True)
    else:
        raise AssertionError('limit bypass')
    check('memory-unchanged-after-limit', c.state == before)
    check('disk-unchanged-after-limit', c.state_path.read_bytes() == disk)
    check('limit-audit-separate', 'rework.limit_reached' in (c.state_path.parent / 'audit.jsonl').read_text())
    c.state['workers'][0]['state'] = 'running'
    before = copy.deepcopy(c.state)
    try:
        c.rework('a', 1)
    except RuntimeError:
        check('running-not-reworkable', c.state == before)
    c.state['workers'][0]['state'] = 'reported'
    c.state['workers'][0]['reworkCount'] = 0
    c.state['workers'][0]['report'] = {'old': True}
    c.state['workers'][0]['acceptance'] = {'old': True}
    c.state['workers'][0]['verificationPlan'] = {'old': True}
    c.rework('a', 1)
    check('old-evidence-cleared', not any(k in c.state['workers'][0] for k in ('report', 'acceptance', 'verificationPlan')))
    c.state['workers'][0]['state'] = 'reported'
    before = copy.deepcopy(c.state)
    with patch('controller.write_state_atomic', side_effect=OSError('disk full')):
        try:
            c.rework('a', 1)
        except OSError:
            check('failed-write-restores-memory', c.state == before)
    c.state['workers'][0]['stopStatus'] = 'requested'
    try:
        c.rework('a', 1)
    except RuntimeError:
        check('stopping-rejected', True)
    c.state['workers'][0].pop('stopStatus')
    c.state['status'] = 'aborted'
    try:
        c.rework('a', 1)
    except RuntimeError:
        check('aborted-run-rejected', True)
    c.state['status'] = 'observing'
    c.state['workers'].append(dict(c.state['workers'][0], attempt=2, state='ready'))
    try:
        c.rework('a', 1)
    except RuntimeError:
        check('old-attempt-rejected', True)
print(json.dumps({'passed':len(checks), 'checks':checks}, ensure_ascii=False))

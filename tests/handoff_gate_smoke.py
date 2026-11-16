"""H01 纯逻辑/控制器测试；样例 runtime 不代表真实宿主。"""
import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from handoff_gate import begin_gate, evaluate_gate, require_gate
from controller import Controller
from host_adapter import UnsupportedHostAdapter
from state_store import read_state

checks=[]
def check(name,value):
    assert value,name
    checks.append(name)
def rejected(name,fn):
    try: fn()
    except (RuntimeError,ValueError): checks.append(name)
    else: raise AssertionError(name)
expected=dict(threadId='destination',root='/repo/destination',branch='codex/dest',head='b'*40,
              model='deepseek-flash',thinking='high',attachments=['worktree:destination'])
gate=begin_gate('source','op-1',expected)
receipt=dict(operationId='op-1',status='success',sourceThreadId='source',destinationThreadId='destination')
obs={k:expected[k] for k in ('threadId','root','branch','head','attachments')}
obs.update(cwd=expected['root'],observedAt=datetime.now(timezone.utc).isoformat(),gitSource='leader_git',clean=True,
           attachmentSource='host_artifacts',runtime=dict(source='destination_host_metadata',threadId='destination',operationId='op-1',model='deepseek-flash',thinking='high'))
check('full-evidence-passes',evaluate_gate(gate,receipt,obs)['status']=='passed')
for field in ('threadId','root','branch','head','cwd','gitSource','attachmentSource'):
    bad=copy.deepcopy(obs);bad[field]='wrong';check(field+'-blocked',evaluate_gate(gate,receipt,bad)['status']=='blocked')
for field in ('sourceThreadId','destinationThreadId','operationId','status'):
    bad={**receipt,field:'wrong'};check('receipt-'+field,evaluate_gate(gate,bad,obs)['status']=='blocked')
for field in ('model','thinking','source','threadId','operationId'):
    bad=copy.deepcopy(obs);bad['runtime'][field]='unknown';check('runtime-'+field,evaluate_gate(gate,receipt,bad)['status']=='blocked')
for field,value in [('clean',False),('attachments',[]),('runtime',{}),('observedAt','invalid')]:
    check('missing-'+field,evaluate_gate(gate,receipt,{**obs,field:value})['status']=='blocked')
for age in (301,-60):
    bad={**obs,'observedAt':(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()}
    check('age-'+str(age),evaluate_gate(gate,receipt,bad)['status']=='blocked')
rejected('pending-dispatch',lambda:require_gate({'handoffGate':gate}))
passed=evaluate_gate(gate,receipt,obs)
w={'handoffGate':passed,'threadId':'destination','worktree':expected['root'],'branch':expected['branch']}
require_gate(w);check('identity-matches',True)
rejected('worker-id-drift',lambda:require_gate({**w,'threadId':'source'}))
rejected('explicit-model-required',lambda:begin_gate('s','op',{**expected,'model':'unknown'}))
with tempfile.TemporaryDirectory() as td:
    state=json.loads((Path(__file__).resolve().parents[1]/'references/state.example.json').read_text())
    state.update(runId='h01-test',status='observing',revision=0)
    state['workers']=[state['workers'][0]];worker=state['workers'][0]
    worker.update(threadId='source',state='ready')
    c=Controller(UnsupportedHostAdapter(),td,'h01-test',state);c.save()
    c.begin_handoff_reconciliation('a',1,'op-1',expected)
    disk=read_state(c.state_path);check('pending-persisted',disk['workers'][0]['handoffGate']['status']=='pending')
    restart=Controller(UnsupportedHostAdapter(),td,'h01-test',disk)
    rejected('restart-dispatch-blocked',lambda:restart.dispatch('a',1,'TASK',None,None))
    rejected('restart-rework-blocked',lambda:restart.rework('a',1))
    rejected('unresolved-not-replaced',lambda:restart.begin_handoff_reconciliation('a',1,'op-2',expected))
    blocked=restart.reconcile_handoff('a',1,receipt,{**obs,'runtime':{}})
    check('unknown-runtime-persisted-blocked',blocked['status']=='blocked' and read_state(c.state_path)['workers'][0]['threadId']=='source')
    # 控制器必须自行观察 Git；不接受只有调用者声明的 leader_git。
    def fake_git(root,*args):
        return {'--git-common-dir':'/repo/.git','--show-toplevel':expected['root'],'--show-current':expected['branch'],'HEAD':expected['head'],'--porcelain':''}[args[-1]]
    with patch.object(restart,'_git',side_effect=fake_git):
        resolved=restart.reconcile_handoff('a',1,receipt,obs)
    worker=restart.state['workers'][0]
    check('destination-rebound',resolved['status']=='passed' and worker['threadId']=='destination')
    check('startup-must-revalidate',worker['state']=='validating')
    require_gate(worker)
    check('new-gate-survives-schema',read_state(c.state_path)['workers'][0]['handoffGate']['status']=='passed')
    with patch.object(restart,'_git',side_effect=fake_git):
        revision=worker['activityRevision'];restart.reconcile_handoff('a',1,receipt,obs)
        check('reconcile-replay-no-revision-bump',worker['activityRevision']==revision)
    with patch.object(restart,'_git',side_effect=RuntimeError('git unavailable')):
        refused=restart.reconcile_handoff('a',1,receipt,obs)
    check('git-failure-persists-blocked',refused['status']=='blocked' and read_state(c.state_path)['workers'][0]['handoffGate']['status']=='blocked')
    before=copy.deepcopy(restart.state)
    with patch.object(restart,'save',side_effect=OSError('disk failure')):
        try:restart.reconcile_handoff('a',1,receipt,{**obs,'runtime':{}})
        except OSError:pass
    check('save-failure-rolls-back',restart.state==before)
with tempfile.TemporaryDirectory() as td:
    state=json.loads((Path(__file__).resolve().parents[1]/'references/state.example.json').read_text())
    state.update(runId='h01-intent',status='observing',revision=0)
    state['workers']=[state['workers'][0]];state['workers'][0].update(threadId='source',state='ready')
    c=Controller(UnsupportedHostAdapter(),td,'h01-intent',state);c.save()
    intent=c.prepare_handoff_intent('a',1,expected)
    check('pre-host-intent-persisted',read_state(c.state_path)['workers'][0]['handoffGate']['operationBound'] is False)
    rejected('pre-host-dispatch-blocked',lambda:c.dispatch('a',1,'TASK',None,None))
    rejected('pre-host-contract-cannot-change',lambda:c.bind_handoff_operation('a',1,'real-op',{**expected,'model':'other'}))
    bound=c.bind_handoff_operation('a',1,'real-op',expected)
    check('real-operation-bound',bound['operationId']=='real-op' and bound['operationBound'])
    rejected('operation-cannot-rebind',lambda:c.bind_handoff_operation('a',1,'another',expected))
print(json.dumps({'passed':len(checks),'checks':checks}))

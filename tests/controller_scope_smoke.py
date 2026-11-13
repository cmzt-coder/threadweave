"""派发前写集与串行资源门禁的实际接线验证。"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from controller import Controller
from host_adapter import UnsupportedHostAdapter

results=[]
def check(name, condition):
    assert condition, name
    results.append(name)

class Adapter(UnsupportedHostAdapter):
    def __init__(self): self.calls=[]
    def send_message(self, thread_id, message): self.calls.append(thread_id)

def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()

with tempfile.TemporaryDirectory() as td:
    root=Path(td)/'repo';root.mkdir()
    git(root,'init','-b','main');git(root,'config','user.name','test');git(root,'config','user.email','test@example.invalid')
    (root/'base').write_text('base')
    git(root,'add','base');git(root,'commit','-m','base')
    (root/'.git/info/exclude').write_text('.threadweave/\n')
    base=git(root,'rev-parse','HEAD')
    common=str((root/'.git').resolve())
    wt=Path(td)/'worker';git(root,'worktree','add','-b','tw/a',str(wt))
    state=json.loads((Path(__file__).resolve().parents[1]/'references/state.example.json').read_text())
    state.update(runId='scope-gate',status='dispatching',revision=0,integrationLog=[])
    state['project'].update(canonicalRepositoryRoot=str(root),commonDirectory=common)
    state['integration'].update(baseSha=base,branch='main',repoPath=str(root))
    a=state['workers'][0];a.update(state='ready',branch='tw/a',baseSha=base,threadId='a',worktree=str(wt),ownedPaths=['src/**'])
    b=state['workers'][1];b.update(state='ready',branch='tw/b',baseSha=base,threadId='b',worktree=str(Path(td)/'b'),ownedPaths=['src/new.py'])
    adapter=Adapter();c=Controller(adapter,root,'scope-gate',state);c.save()
    report={'role':'a','attempt':1,'observedAt':datetime.now(timezone.utc).isoformat(),'cwd':str(wt),'worktree_root':str(wt),'git_common_dir':common,'branch':'tw/a','head':base,'status_porcelain':''}
    expected={'integration_head':base,'common_dir':common,'other_roots':[str(root),str(Path(td)/'b')]}
    original=copy.deepcopy(c.state)
    try:c.dispatch('a',1,'task',report,expected)
    except RuntimeError as exc: check('future-path-conflict-before-send','scope reservation conflict' in str(exc))
    else:raise AssertionError('scope conflict bypassed')
    check('no-message-on-conflict',not adapter.calls)
    check('no-intent-on-conflict',c.state==original)
    b['ownedPaths']=['tests/**']
    c.dispatch('a',1,'task',report,expected)
    check('disjoint-dispatch-sent',adapter.calls==['a'])
    c.state['workers'][0]['state']='ready';c.state['workers'][0].pop('dispatchIntent')
    c.state['workers'][0]['ownedPaths']=['migrations/a.sql'];b['ownedPaths']=['migrations/b.sql']
    (root/'threadweave.config.json').write_text(json.dumps({'serializedResources':[{'name':'migration-order','paths':['migrations/**']}]}))
    try:c.dispatch('a',1,'task',report,expected)
    except RuntimeError as exc:check('resource-group-before-send','scope reservation conflict' in str(exc))
    else:raise AssertionError('serialized resource bypassed')
    check('resource-conflict-no-additional-message',adapter.calls==['a'])
    try:Controller._scope_changes(c.state['workers'][0],{'filesChanged':['secret.txt']})
    except RuntimeError as exc:check('actual-drift-blocked','scope_drift' in str(exc))
    else:raise AssertionError('scope drift bypassed')
    # 派发后新增的符号链接也必须在验收阶段重新检查。
    c.state['workers'][0]['ownedPaths']=['src/**']
    (wt/'src').mkdir()
    (wt/'src/link').symlink_to(Path(td)/'outside')
    git(wt,'add','src/link');git(wt,'commit','-m','add escaping link')
    try:Controller._scope_changes(c.state['workers'][0],{'filesChanged':['src/link']})
    except RuntimeError as exc:check('post-dispatch-symlink-blocked','scope_drift' in str(exc))
    else:raise AssertionError('new symlink bypassed')
print(json.dumps({'passed':len(results),'checks':results},ensure_ascii=False))

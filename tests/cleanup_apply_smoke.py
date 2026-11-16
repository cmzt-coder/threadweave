"""使用真实临时 Git 仓库验证清理执行与拒绝边界。"""
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cleanup_apply import preview, apply, CleanupBlocked

passed = []
def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()

with tempfile.TemporaryDirectory() as td:
    base = Path(td).resolve(); repo = base/'repo'; repo.mkdir(); wt = base/'worker'
    git(repo, 'init', '-b', 'main'); git(repo, 'config', 'user.name', 'Validation')
    git(repo, 'config', 'user.email', 'validation@example.invalid')
    (repo/'tracked').write_text('base'); git(repo, 'add', '.'); git(repo, 'commit', '-m', 'base')
    git(repo, 'worktree', 'add', '-b', 'codex/test-cleanup', str(wt))
    report = base/'report.md'; report.write_text('verified')
    ep = base/'evidence.json'
    d = dict(runId='cleanup-test', repository=str(repo), worktree=str(wt), branch='codex/test-cleanup',
             head=git(wt,'rev-parse','HEAD'), integrationRef='main', report=str(report),
             reportSha256=hashlib.sha256(report.read_bytes()).hexdigest(), evidence=str(ep))
    e = {k:d[k] for k in ('runId','worktree','branch','head','reportSha256')}
    e.update(observedAt=datetime.now(timezone.utc).isoformat(), ownedByRun=True, shared=False,
             acceptanceVerified=True,executionKind='host_thread',threadId='unit-test-only',stop=dict(threadIdle=True,finalAnswerPresent=True,noActiveTurn=True))
    def save(obj): ep.write_text(json.dumps(obj))
    def blocked(name, fn):
        try: fn()
        except (CleanupBlocked, ValueError, OSError, KeyError, TypeError): passed.append(name)
        else: raise AssertionError(name)
        assert wt.exists()
    save(e); plan=preview(d); assert plan['readOnly'] and wt.exists(); passed.append('preview-readonly')
    blocked('authorization-required',lambda:apply(d,plan))
    for field,value in [('ownedByRun',False),('shared',True),('acceptanceVerified',False),('head','0'*40),('runId','other'),('reportSha256','0'*64)]:
        bad=copy.deepcopy(e);bad[field]=value;save(bad);blocked(field,lambda:preview(d))
    for age in (301,-60):
        bad=copy.deepcopy(e);bad['observedAt']=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat();save(bad);blocked('evidence-age-'+str(age),lambda:preview(d))
    bad=copy.deepcopy(e);bad['stop']['noActiveTurn']=False;save(bad);blocked('active-turn',lambda:preview(d));save(e)
    (wt/'extra').write_text('partial'); blocked('untracked',lambda:preview(d));(wt/'extra').unlink()
    (wt/'tracked').write_text('dirty');blocked('tracked-dirty',lambda:preview(d));git(wt,'checkout','--','tracked')
    (repo/'.git/info/exclude').write_text('ignored\n');(wt/'ignored').write_text('valuable');blocked('ignored',lambda:preview(d));(wt/'ignored').unlink()
    git(repo,'worktree','lock',str(wt));blocked('locked-shared',lambda:preview(d));git(repo,'worktree','unlock',str(wt))
    blocked('primary',lambda:preview({**d,'worktree':str(repo)}))
    blocked('branch-drift',lambda:preview({**d,'branch':'other'}))
    blocked('head-drift',lambda:preview({**d,'head':'0'*40}))
    blocked('report-in-worktree',lambda:preview({**d,'report':str(wt/'tracked')}))
    report.write_text('tampered');blocked('archive-tamper',lambda:preview(d));report.write_text('verified')
    symlink=base/'alias';symlink.symlink_to(wt,target_is_directory=True);blocked('symlink',lambda:preview({**d,'worktree':str(symlink)}))
    bad=copy.deepcopy(plan);bad['fingerprint']='0'*64;blocked('plan-tamper',lambda:apply(d,bad,authorized=True))
    # 新提交未合入，即使 leader 声明验收通过也不能删除。
    (wt/'tracked').write_text('unmerged');git(wt,'add','.');git(wt,'commit','-m','unmerged')
    newer={**d,'head':git(wt,'rev-parse','HEAD')};save({**e,'head':newer['head']})
    blocked('unmerged',lambda:preview(newer));git(wt,'reset','--hard',d['head']);save(e)
    # 集成 ref 前进使精确批准计划失效，即使 worker 仍已合入。
    (repo/'next').write_text('next');git(repo,'add','.');git(repo,'commit','-m','integration moved')
    blocked('integration-drift',lambda:apply(d,plan,authorized=True))
    local={**e,'executionKind':'local_git','threadId':None,'stop':{'localProcessStopped':False}};save(local)
    blocked('local-process-active',lambda:preview(d))
    local['stop']['localProcessStopped']=True;save(local)
    fresh=preview(d);result=apply(d,fresh,authorized=True)
    assert result['status']=='removed' and not wt.exists();passed.append('real-removal')
    assert git(repo,'rev-parse','codex/test-cleanup')==d['head'];passed.append('branch-preserved')
    assert report.exists() and ep.exists();passed.append('evidence-preserved')
print(json.dumps({'passed':len(passed),'checks':passed}))

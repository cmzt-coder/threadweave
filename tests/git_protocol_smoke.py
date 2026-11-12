"""验证本地 Git 原语和静态文档契约，不创建真实 worker 或模拟控制器。"""
import subprocess, tempfile, json, re
from pathlib import Path
project=Path(__file__).resolve().parents[1]
results=[]
def check(name, condition):
    if not condition: raise AssertionError(name)
    results.append(name)
def git(root,*args,ok=True):
    r=subprocess.run(['git','-c','user.name=Threadweave Test','-c','user.email=threadweave-test@example.invalid',*args],cwd=root,text=True,capture_output=True)
    if ok and r.returncode: raise RuntimeError(r.stderr)
    return r
with tempfile.TemporaryDirectory(prefix='threadweave-verify-') as d:
    root=Path(d); repo=root/'repo';repo.mkdir()
    git(repo,'init','-b','main'); (repo/'baseline.txt').write_text('baseline\n')
    git(repo,'add','baseline.txt');git(repo,'commit','-m','baseline')
    base=git(repo,'rev-parse','HEAD').stdout.strip()
    common=(repo/git(repo,'rev-parse','--git-common-dir').stdout.strip()).resolve()
    workers=[]
    for role in ('a','b'):
        wt=root/role;git(repo,'worktree','add','--detach',str(wt),base)
        check('detached-'+role,git(wt,'symbolic-ref','--quiet','--short','HEAD',ok=False).returncode!=0)
        check('common-directory-'+role,(wt/git(wt,'rev-parse','--git-common-dir').stdout.strip()).resolve()==common)
        branch='threadweave/test/'+role+'-1';git(wt,'switch','-c',branch)
        check('attached-clean-base-'+role,git(wt,'symbolic-ref','--short','HEAD').stdout.strip()==branch and git(wt,'rev-parse','HEAD').stdout.strip()==base and not git(wt,'status','--porcelain').stdout)
        workers.append((wt,branch))
    check('distinct-worktree-roots',len({repo.resolve(),*[x[0].resolve() for x in workers]})==3)
    alias=root/'alias';alias.symlink_to(workers[0][0],target_is_directory=True)
    check('symlink-alias-detected',alias.resolve()==workers[0][0].resolve())
    before=git(repo,'rev-parse',workers[0][1]).stdout.strip()
    check('branch-collision-refused',git(workers[1][0],'branch',workers[0][1],base,ok=False).returncode!=0 and git(repo,'rev-parse',workers[0][1]).stdout.strip()==before)
    foreign=root/'foreign';foreign.mkdir();git(foreign,'init','-b','main')
    check('foreign-repository-distinguishable',(foreign/git(foreign,'rev-parse','--git-common-dir').stdout.strip()).resolve()!=common)
    a=workers[0][0];(a/'unexpected.txt').write_text('keep\n')
    check('dirty-worktree-visible',bool(git(a,'status','--porcelain').stdout))
    check('unsafe-worktree-removal-refused',git(repo,'worktree','remove',str(a),ok=False).returncode!=0 and (a/'unexpected.txt').exists())
    (a/'unexpected.txt').unlink()
    exclude=repo/git(repo,'rev-parse','--git-path','info/exclude').stdout.strip()
    with exclude.open('a') as f:f.write('\n/.threadweave/\n')
    archived=root/'preserved';archived.mkdir()
    for wt,branch in workers:
        (wt/(wt.name+'.txt')).write_text(wt.name+' result\n')
        git(wt,'add',wt.name+'.txt');git(wt,'commit','-m','worker '+wt.name)
        head=git(wt,'rev-parse','HEAD').stdout.strip()
        check('base-drift-observable-'+wt.name,head!=base)
        git(wt,'merge-base','--is-ancestor',base,head)
        report=wt/'.threadweave/report.md';report.parent.mkdir();report.write_text('COMPLETE '+head+'\n')
        check('ignored-report-hidden-'+wt.name,not git(wt,'status','--porcelain').stdout and bool(git(wt,'ls-files','--others','--ignored','--exclude-standard').stdout))
        (archived/(wt.name+'.md')).write_text(report.read_text())
        git(repo,'merge','--no-ff',branch,'-m','integrate '+wt.name)
        check('serial-integration-'+wt.name,(repo/(wt.name+'.txt')).read_text()==wt.name+' result\n')
    check('integrated-results',all((repo/(role+'.txt')).exists() for role in ('a','b')))
    for wt,branch in workers:
        git(repo,'worktree','remove',str(wt))
        check('report-preserved-'+wt.name,not wt.exists() and (archived/(wt.name+'.md')).exists())
    check('worktree-cleanup',git(repo,'worktree','list','--porcelain').stdout.count('worktree ')==1)
# 静态检查只检查契约与引用，不冒充可执行状态机测试。
skill=(project/'SKILL.md').read_text()
refs=re.findall(r'`(references/[^`]+\.md)`',skill)
check('reference-paths-exist',all((project/x).is_file() for x in refs))
check('six-workflow-phases',len(re.findall(r'^### [1-6]\. ',skill,re.M))==6)
preflight=(project/'references/preflight-checklist.md').read_text()
check('update-plan-downgrade-defined','update_plan' in skill and ('notes.md' in skill or 'state file' in skill))
check('wait-threads-downgrade-defined','wait_threads' in skill and 'bounded status checks' in skill)
check('optional-tool-downgrade-defined','Optional-tool downgrade' in preflight and 'Do not silently replace' in preflight)
check('runtime-no-fixed-model',all('gpt-5.4' not in f.read_text() for f in [project/'SKILL.md',project/'README.md',project/'README.zh-CN.md',*(project/'references').glob('*.md')]))
check('no-path-project-id-placeholder','workspace-root-or-project-id' not in skill)
for f in [project/'SKILL.md',project/'README.md',project/'README.zh-CN.md',*(project/'references').glob('*.md')]:
    check('balanced-fences-'+f.name,sum(line.startswith('```') for line in f.read_text().splitlines())%2==0)
print(json.dumps({'passed':len(results),'checks':results,'scope':'Local Git and static contracts only; no real worker chats'},ensure_ascii=False,indent=2))

"""O16 宿主清理门禁测试；宿主返回为显式 fixture，不代替真实归档。"""
import copy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from managed_cleanup import recheck_managed_cleanup,execute_managed_cleanup
from cleanup import cleanup_plan_fingerprint
from desktop_lifecycle_bridge import LIST_ARTIFACTS,validate_lifecycle_params,validate_lifecycle_metadata
checks=[]
def check(n,v):assert v,n;checks.append(n)
def rejected(n,f):
 try:f()
 except ValueError:checks.append(n)
 else:raise AssertionError(n)
d={'runId':'run','worktree':'/repo/worker'}
plan={'git':'exact','executionKind':'host_thread','threadId':'thread'};approved={'plan':plan,'fingerprint':cleanup_plan_fingerprint(plan)}
base=dict(runId='run',role='a',attempt=1,taskRevision=0,source='desktop_tool_bridge',operationId='wait',observedAt=datetime.now(timezone.utc).isoformat())
s={**base,'metadata':{'hostId':'local','result':{'polls':[{'thread':{'id':'thread','hostId':'local','status':{'type':'idle'}},'latestTurn':{'id':'turn','status':'completed'},'latestAssistantMessage':{'turnId':'turn','phase':'final_answer'}}]}}}
a={**base,'operationId':'inventory','metadata':{'hostId':'local','result':{'artifacts':[{'type':'worktree','identityKey':'/repo/worker','payload':{'ownerThreadId':'thread','isPrimary':False,'isShared':False,'runId':'run'}}]}}}
def run(stop=s,inventory=a):return recheck_managed_cleanup(d,approved,stop,inventory,'thread')
with patch('managed_cleanup.preview',return_value=approved):
 check('eligible-fixture',run()['status']=='eligible')
 actual=copy.deepcopy(a);actual['metadata']['result']['artifacts'][0]['attachmentType']=actual['metadata']['result']['artifacts'][0].pop('type')
 check('observed-host-type-field-supported',run(inventory=actual)['status']=='eligible')
 unknown=copy.deepcopy(actual);unknown['metadata']['result']['artifacts'][0]['payload']={'hostId':'local','root':'/repo/worker','workspaceRoot':'/repo/worker'}
 check('observed-host-payload-does-not-infer-ownership','host ownership/primary/shared' in run(inventory=unknown)['reason'])
 conflicting=copy.deepcopy(actual);conflicting['metadata']['result']['artifacts'][0]['type']='archived_worktree'
 check('conflicting-host-type-fields-blocked',run(inventory=conflicting)['status']=='blocked')
 archived=copy.deepcopy(actual);archived['metadata']['result']['artifacts'][0]['attachmentType']='archived_worktree'
 check('archived-attachment-not-active',run(inventory=archived)['status']=='blocked')
 for field,value in [('ownerThreadId','other'),('isPrimary',True),('isShared',True),('runId','other')]:
  bad=copy.deepcopy(a);bad['metadata']['result']['artifacts'][0]['payload'][field]=value
  check(field,run(inventory=bad)['status']=='blocked')
 bad=copy.deepcopy(a);bad['metadata']['result']['artifacts']=[];check('empty-inventory',run(inventory=bad)['status']=='blocked')
 bad=copy.deepcopy(a);bad['metadata']['result']['artifacts'][0]['payload']={};check('unknown-ownership',run(inventory=bad)['status']=='blocked')
 bad=copy.deepcopy(s);bad['metadata']['result']['polls'][0]['thread']['status']['type']='active';check('active',run(stop=bad)['status']=='blocked')
 bad={**s,'observedAt':(datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat()};check('stale',run(stop=bad)['status']=='blocked')
 bad={**s,'runId':'other'};check('wrong-run',run(stop=bad)['status']=='blocked')
 bad={**a,'attempt':2};check('wrong-attempt',run(inventory=bad)['status']=='blocked')
 adapter=Mock();adapter.archive_worktree.return_value={'status':'archived'}
 rejected('authorization',lambda:execute_managed_cleanup(adapter,d,approved,s,a,'thread'))
 check('no-call-without-authorization',not adapter.archive_worktree.called)
 result=execute_managed_cleanup(adapter,d,approved,s,a,'thread',authorized=True)
 check('host-only',adapter.archive_worktree.call_args.args==('/repo/worker',) and result['requiresHostCompletionVerification'])
 adapter.reset_mock();bad=copy.deepcopy(a);bad['metadata']['result']['artifacts']=[]
 execute_managed_cleanup(adapter,d,approved,s,bad,'thread',authorized=True)
 check('no-fallback-on-block',not adapter.archive_worktree.called)
with patch('managed_cleanup.preview',return_value={**approved,'fingerprint':'different'}):check('git-drift',run()['status']=='blocked')
validate_lifecycle_params(LIST_ARTIFACTS,{});validate_lifecycle_metadata(LIST_ARTIFACTS,{}, {'hostId':'local','result':{'artifacts':[]}});check('empty-list-valid-observation',True)
rejected('no-target-inventory-spoof',lambda:validate_lifecycle_params(LIST_ARTIFACTS,{'threadId':'other'}))
print(json.dumps({'passed':len(checks),'checks':checks}))

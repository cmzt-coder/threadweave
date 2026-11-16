"""O10 只读拆分预演。

复用 scope 的写集/串行资源规则，输出冲突依据和未知项；不自动派发、不评分，
也不把“未发现声明冲突”等同于业务上一定可以并行。
"""
from pathlib import Path

from scope import check_reservations


def _worker_key(worker):
    return f"{worker.get('role')}#{worker.get('attempt', 1)}"


def _contract_list(worker, field, errors):
    value = worker.get(field, [])
    if not isinstance(value, list):
        errors.append(f'{_worker_key(worker)}.{field}: must be a list')
        return []
    result = []
    for path in value:
        if not isinstance(path, str) or not path or path.startswith('/'):
            errors.append(f'{_worker_key(worker)}.{field}: invalid contract path {path!r}')
            continue
        normalized = Path(path)
        if ('\\' in path or '\x00' in path or '..' in normalized.parts
                or any(char in path for char in '*?[]{}')):
            errors.append(f'{_worker_key(worker)}.{field}: contract must be a repository-relative exact path')
            continue
        if path not in result:
            result.append(path)
    return result


def build_split_preview(workers, serialized_resources=None, repo_root=None):
    """生成只读拆分预览；status 仅表示声明层面未发现冲突。"""
    if not isinstance(workers, list) or not workers:
        raise ValueError('workers must be a non-empty list')
    if any(not isinstance(worker, dict) for worker in workers):
        raise ValueError('workers entries must be objects')
    repo_root = Path(repo_root or '.').resolve()
    if serialized_resources is None:
        serialized_resources = []
    if not isinstance(serialized_resources, list):
        raise ValueError('serialized_resources must be a list')

    errors = check_reservations(workers, repo_root, serialized_resources)
    write_conflicts = []
    resource_conflicts = []
    unknowns = []
    for message in errors:
        if '范围冲突' in message:
            write_conflicts.append(message)
        elif '串行资源组冲突' in message:
            resource_conflicts.append(message)
        else:
            unknowns.append(message)

    contract_errors = []
    writers = {}
    readers = {}
    worker_contracts = {}
    for worker in workers:
        key = _worker_key(worker)
        read_contracts = _contract_list(worker, 'readContracts', contract_errors)
        write_contracts = _contract_list(worker, 'writeContracts', contract_errors)
        worker_contracts[key] = {
            'readContracts': read_contracts,
            'writeContracts': write_contracts,
        }
        for path in read_contracts:
            readers.setdefault(path, []).append(key)
        for path in write_contracts:
            writers.setdefault(path, []).append(key)

    contract_conflicts = []
    contract_dependencies = []
    for path in sorted(set(writers) | set(readers)):
        path_writers = writers.get(path, [])
        path_readers = readers.get(path, [])
        if len(path_writers) > 1:
            contract_conflicts.append({
                'contract': path,
                'writers': path_writers,
                'reason': 'multiple workers declare writes to the same stable contract',
            })
            continue
        writer = path_writers[0] if path_writers else None
        dependencies = sorted(set(path_readers) - ({writer} if writer else set()))
        if writer and dependencies:
            contract_dependencies.append({
                'contract': path,
                'writer': writer,
                'readers': dependencies,
                'reason': 'reader depends on a contract changed by another worker; freeze or serialize',
            })
        elif not writer and path_readers:
            contract_dependencies.append({
                'contract': path,
                'writer': None,
                'readers': sorted(set(path_readers)),
                'reason': 'contract has readers but no declared writer; treat as frozen or explicitly owned',
            })

    unknowns.extend(contract_errors)
    if write_conflicts or resource_conflicts or contract_conflicts:
        status = 'conflict'
    elif unknowns:
        status = 'needs-review'
    else:
        status = 'no-known-conflict'

    return {
        'schemaVersion': 1,
        'readOnly': True,
        'autoDispatch': False,
        'status': status,
        'workers': [
            {
                'worker': _worker_key(worker),
                'ownedPaths': worker.get('ownedPaths', []),
                'scopeExceptions': worker.get('scopeExceptions', []),
                **worker_contracts[_worker_key(worker)],
            }
            for worker in workers
        ],
        'writeSetConflicts': write_conflicts,
        'resourceConflicts': resource_conflicts,
        'contractConflicts': contract_conflicts,
        'contractDependencies': contract_dependencies,
        'unknowns': list(dict.fromkeys(unknowns)),
        'limitations': [
            'preview only checks declared paths, serialized resources and stable contracts',
            'directory proximity and declared disjointness do not prove business independence',
            'no risk score is generated',
            'no dispatch, merge, cleanup or host call is performed',
        ],
    }

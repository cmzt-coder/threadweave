"""仓库相对 glob 范围预留与实际变更校验，无第三方依赖。"""
from collections import deque
from pathlib import Path


def _validate(value, pattern=True):
    if not isinstance(value, str) or not value or value.startswith('/'):
        raise ValueError('路径必须是非空仓库相对路径')
    if '\\' in value or '\x00' in value or any(x in ('', '.', '..') for x in value.split('/')):
        raise ValueError('路径包含空组件、父目录穿越或非规范分隔符')
    if len(value) >= 2 and value[0].isalpha() and value[1] == ':':
        raise ValueError('路径不能是绝对盘符路径')
    if pattern and any(x in value for x in '[]{}'):
        raise ValueError('不支持的 glob；无法证明范围不相交，请拆分或精确声明')
    return value


def _machine(pattern):
    """* 不跨目录，** 跨目录，**/ 也允许零层目录；? 匹配单个非 / 字符。"""
    edges, eps = {}, {}
    current = 0
    count = 1

    def edge(a, label, b):
        edges.setdefault(a, []).append((label, b))

    i = 0
    while i < len(pattern):
        end = count
        count += 1
        if pattern.startswith('**/', i):
            loop = count
            count += 1
            eps.setdefault(current, []).extend([end, loop])
            edge(loop, 'ANY', loop)
            edge(loop, ('L', '/'), end)
            i += 3
        elif pattern.startswith('**', i):
            eps.setdefault(current, []).append(end)
            edge(current, 'ANY', current)
            i += 2
        elif pattern[i] == '*':
            eps.setdefault(current, []).append(end)
            edge(current, 'SEG', current)
            i += 1
        else:
            edge(current, 'SEG' if pattern[i] == '?' else ('L', pattern[i]), end)
            i += 1
        current = end
    return edges, eps, current


def _compatible(a, b):
    if a == 'ANY' or b == 'ANY':
        return True
    if a == 'SEG':
        return b != ('L', '/')
    if b == 'SEG':
        return a != ('L', '/')
    return a == b


def _overlap(left, right):
    """有限状态机乘积判空，包含未来文件，不展开现有磁盘文件。"""
    le, lp, lf = _machine(left)
    re, rp, rf = _machine(right)
    queue = deque([(0, 0)])
    seen = set()
    while queue:
        a, b = queue.popleft()
        if (a, b) in seen:
            continue
        seen.add((a, b))
        if a == lf and b == rf:
            return True
        queue.extend((n, b) for n in lp.get(a, []))
        queue.extend((a, n) for n in rp.get(b, []))
        for x, dest_a in le.get(a, []):
            for y, dest_b in re.get(b, []):
                if _compatible(x, y):
                    queue.append((dest_a, dest_b))
    return False


def _patterns(values, label, errors):
    if not isinstance(values, list) or not values:
        errors.append(f'{label}: 必须声明非空路径列表')
        return []
    result = []
    for value in values:
        try:
            result.append(_validate(value))
        except ValueError as exc:
            errors.append(f'{label}: {value!r}: {exc}')
    return result


def check_changes(changed_paths, owned_paths, scope_exceptions=None):
    """调用方应提供新增、删除及重命名的旧/新两端路径。"""
    errors = []
    patterns = _patterns(owned_paths, 'ownedPaths', errors)
    if scope_exceptions is not None and scope_exceptions != []:
        patterns += _patterns(scope_exceptions, 'scopeExceptions', errors)
    if not isinstance(changed_paths, list):
        return errors + ['changed_paths: 必须是路径列表']
    for path in changed_paths:
        try:
            _validate(path, pattern=False)
            if not any(_matches(pattern, path) for pattern in patterns):
                errors.append(f'越界变更: {path}')
        except ValueError as exc:
            errors.append(f'changed_paths: {path!r}: {exc}')
    return errors


def _matches(pattern, path):
    edges, eps, final = _machine(pattern)
    states = {0}

    def closure(values):
        values = set(values)
        pending = list(values)
        while pending:
            for dest in eps.get(pending.pop(), []):
                if dest not in values:
                    values.add(dest)
                    pending.append(dest)
        return values

    states = closure(states)
    for char in path:
        states = closure(dest for state in states for label, dest in edges.get(state, [])
                         if _compatible(label, ('L', char)))
    return final in states


def check_reservations(workers, repo_root, serialized_resources=None):
    errors = []
    if not isinstance(workers, list):
        return ['workers: 必须是列表']
    try:
        root = Path(repo_root).resolve()
    except (TypeError, ValueError, OSError, RuntimeError):
        return ['repo_root: 无法解析仓库目录']
    if not root.is_dir():
        return ['repo_root: 必须是现有目录']
    declarations = []
    for index, worker in enumerate(workers):
        if not isinstance(worker, dict):
            errors.append(f'workers[{index}]: 必须是对象')
            continue
        label = f"{worker.get('role', index)}#{worker.get('attempt', '?')}"
        patterns = _patterns(worker.get('ownedPaths'), label + '.ownedPaths', errors)
        exceptions = worker.get('scopeExceptions', [])
        if exceptions != []:
            patterns += _patterns(exceptions, label + '.scopeExceptions', errors)
        declarations.append((label, patterns))
    for i, (left, left_patterns) in enumerate(declarations):
        for right, right_patterns in declarations[i + 1:]:
            for a in left_patterns:
                for b in right_patterns:
                    if _overlap(a, b):
                        errors.append(f'范围冲突 {left} / {right}: {a!r} 与 {b!r} 相交；请拆分或精确声明')
    resources = [] if serialized_resources is None else serialized_resources
    if not isinstance(resources, list):
        errors.append('serialized_resources: 必须是列表')
        resources = []
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict):
            errors.append(f'serialized_resources[{index}]: 必须是对象')
            continue
        name = resource.get('name', index)
        paths = _patterns(resource.get('paths'), f'resource {name}', errors)
        users = [label for label, patterns in declarations
                 if any(_overlap(a, b) for a in patterns for b in paths)]
        if len(users) > 1:
            errors.append(f'串行资源组冲突 {name}: {", ".join(users)}')
    # 跟随仓库内链接检查别名路径；按祖先真实目录截断循环，禁止越界访问。
    pending = [(root, frozenset())]
    while pending:
        directory, ancestors = pending.pop()
        real_directory = directory.resolve()
        ancestors = ancestors | {real_directory}
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            errors.append(f'无法检查目录 {directory.relative_to(root)}: {exc}')
            continue
        for path in entries:
            relative = path.relative_to(root).as_posix()
            unsafe = False
            try:
                target = path.resolve()
                target.relative_to(root)
                cyclic = path.is_dir() and target in ancestors
                unsafe = cyclic
            except (ValueError, RuntimeError, OSError):
                unsafe = True
            if unsafe:
                for label, patterns in declarations:
                    if any(_matches(p, relative) or _overlap(p, relative + '/**') for p in patterns):
                        errors.append(f'{label}: 符号链接越界或无法解析/循环: {relative}')
            elif path.is_dir():
                pending.append((path, ancestors))
    return errors

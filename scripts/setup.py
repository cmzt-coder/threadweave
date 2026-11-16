"""O06a 显式 workspace setup 契约。

命令只来自已确认的项目配置；不使用 shell，不自动复制秘密文件，也不自动重试。
"""
import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


OUTPUT_LIMIT = 16384


class SetupConfigError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise SetupConfigError(f'{name} must be a non-empty string')
    return value.strip()


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()


def _validate_relative_cwd(value):
    value = _text(value, 'setup.cwd')
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or '\x00' in value:
        raise SetupConfigError('setup.cwd must be a repository-relative path')
    return value


def load_setup_config(repo_root, config_path=None):
    """读取并严格验证 setup 段；没有配置文件或 setup 段时返回 None。"""
    repo_root = Path(repo_root).resolve()
    path = Path(config_path) if config_path is not None else repo_root / 'threadweave.config.json'
    path = path.resolve()
    if path.parent != repo_root:
        raise SetupConfigError('setup config must stay in repository root')
    if not path.is_file():
        return None
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise SetupConfigError(f'invalid project config JSON: {exc}') from exc
    if not isinstance(config, dict):
        raise SetupConfigError('project config must be an object')
    setup = config.get('setup')
    if setup is None:
        return None
    if not isinstance(setup, dict):
        raise SetupConfigError('setup must be an object')
    allowed = {'version', 'argv', 'cwd', 'timeoutSeconds', 'failurePolicy', 'idempotent'}
    if set(setup) - allowed:
        raise SetupConfigError('setup contains unsupported fields')
    if setup.get('version', 1) != 1:
        raise SetupConfigError('setup.version must be 1')
    argv = setup.get('argv')
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in argv)):
        raise SetupConfigError('setup.argv must be a non-empty string array')
    cwd = _validate_relative_cwd(setup.get('cwd'))
    timeout = setup.get('timeoutSeconds')
    if type(timeout) is not int or timeout < 1 or timeout > 86400:
        raise SetupConfigError('setup.timeoutSeconds must be an integer in 1..86400')
    if setup.get('failurePolicy') != 'block':
        raise SetupConfigError('setup.failurePolicy must be block')
    if setup.get('idempotent') is not True:
        raise SetupConfigError('setup.idempotent must be true for automatic reuse')
    normalized = {
        'version': 1,
        'argv': argv,
        'cwd': cwd,
        'timeoutSeconds': timeout,
        'failurePolicy': 'block',
        'idempotent': True,
    }
    return {
        'configPath': str(path),
        'configHash': _hash(normalized),
        **normalized,
    }


def _run(argv, cwd, timeout_seconds):
    """执行准备命令并限制输出；超时终止整个进程组。"""
    started_at = _now()
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    truncated = False
    timed_out = False
    with subprocess.Popen(
            argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True) as proc:
        with selectors.DefaultSelector() as selector:
            for name, stream in [('stdout', proc.stdout), ('stderr', proc.stderr)]:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            deadline = time.monotonic() + timeout_seconds
            while selector.get_map() or proc.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                for key, _ in selector.select(min(remaining, 0.05)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = output[key.data]
                    room = OUTPUT_LIMIT - len(buffer)
                    buffer.extend(chunk[:room])
                    truncated = truncated or len(chunk) > room
            proc.wait()
        return {
            'status': 'timeout' if timed_out else ('passed' if proc.returncode == 0 else 'failed'),
            'exitCode': proc.returncode,
            'timedOut': timed_out,
            'outputTruncated': truncated,
            'stdout': bytes(output['stdout']).decode('utf-8', errors='replace'),
            'stderr': bytes(output['stderr']).decode('utf-8', errors='replace'),
            'startedAt': started_at,
            'finishedAt': _now(),
        }


def run_setup_command(worker_root, config):
    """在指定 worker worktree 中执行已验证 setup 配置。"""
    worker_root = Path(worker_root).resolve()
    if not worker_root.is_dir():
        raise SetupConfigError('worker worktree is unavailable')
    cwd = (worker_root / config['cwd']).resolve()
    if cwd != worker_root and worker_root not in cwd.parents:
        raise SetupConfigError('setup.cwd escapes worker worktree')
    if not cwd.is_dir():
        raise SetupConfigError('setup.cwd does not exist in worker worktree')
    result = _run(config['argv'], cwd, config['timeoutSeconds'])
    result.update({
        'configHash': config['configHash'],
        'cwd': str(cwd),
        'argv': list(config['argv']),
        'timeoutSeconds': config['timeoutSeconds'],
        'failurePolicy': config['failurePolicy'],
        'idempotent': config['idempotent'],
        'stdoutSha256': hashlib.sha256(result['stdout'].encode('utf-8')).hexdigest(),
        'stderrSha256': hashlib.sha256(result['stderr'].encode('utf-8')).hexdigest(),
        'stdoutBytes': len(result['stdout'].encode('utf-8')),
        'stderrBytes': len(result['stderr'].encode('utf-8')),
    })
    return result

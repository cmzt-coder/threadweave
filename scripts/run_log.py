"""Threadweave 结构化运行日志（JSON Lines）与增量游标。

只读写本地文件，不调用宿主 API；日志路径应位于 durable run 目录内。
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


class LogCursorError(ValueError):
    """增量读取游标无效、跨 run 或已因日志变化失效。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def log_event(log_path, event, level='info', **fields):
    """追加一条 JSONL 事件并返回写入记录。"""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'timestamp': _now_iso(),
        'event': event,
        'level': level,
    }
    record.update(fields)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    return record


def _decode_line(line):
    if not line.strip():
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        return {'event': 'log.corrupt', 'level': 'error', 'raw': line, 'error': str(exc)}
    return value


def read_log(log_path):
    """读取完整 JSONL；跳过空行，损坏行标记为 log.corrupt。"""
    log_path = Path(log_path)
    if not log_path.is_file():
        return []
    records = []
    for line in log_path.read_text(encoding='utf-8').splitlines():
        value = _decode_line(line)
        if value is not None:
            records.append(value)
    return records


def summarize_log(log_path):
    """按 event 统计次数。"""
    counts = {}
    for record in read_log(log_path):
        event = record.get('event', 'unknown')
        counts[event] = counts.get(event, 0) + 1
    return counts


def _scope_digest(scope):
    if scope is None:
        return 'none'
    return hashlib.sha256(str(scope).encode('utf-8')).hexdigest()[:16]


def make_cursor(log_path, offset=None, scope=None):
    """生成绑定 scope、文件偏移和已读前缀哈希的游标。"""
    log_path = Path(log_path)
    data = log_path.read_bytes() if log_path.is_file() else b''
    if offset is None:
        offset = len(data)
    if type(offset) is not int or offset < 0 or offset > len(data):
        raise ValueError('cursor offset out of range')
    prefix = hashlib.sha256(data[:offset]).hexdigest()
    return f'v1:{_scope_digest(scope)}:{offset}:{prefix}'


def _parse_cursor(cursor, scope):
    if not isinstance(cursor, str) or not cursor:
        raise LogCursorError('invalid_cursor', 'cursor must be a non-empty string')
    parts = cursor.split(':')
    if len(parts) != 4 or parts[0] != 'v1' or not parts[2].isdigit():
        raise LogCursorError('invalid_cursor', 'cursor format must be v1:<scope>:<offset>:<sha256>')
    if parts[1] != _scope_digest(scope):
        raise LogCursorError('scope_mismatch', 'cursor belongs to another run/scope')
    offset = int(parts[2])
    if not _is_sha256(parts[3]):
        raise LogCursorError('invalid_cursor', 'cursor prefix hash must be SHA256')
    return offset, parts[3]


def _is_sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in '0123456789abcdef' for char in value))


def read_since(log_path, cursor=None, scope=None):
    """读取游标后的完整 JSONL 行，返回 events、nextCursor 和新游标偏移。"""
    log_path = Path(log_path)
    data = log_path.read_bytes() if log_path.is_file() else b''
    if cursor is None:
        offset = 0
    else:
        offset, prefix = _parse_cursor(cursor, scope)
        if offset > len(data):
            raise LogCursorError('stale_cursor', 'log is shorter than the cursor offset')
        if offset and not data[:offset].endswith(b'\n'):
            raise LogCursorError('invalid_cursor', 'cursor is not at a record boundary')
        if hashlib.sha256(data[:offset]).hexdigest() != prefix:
            raise LogCursorError('stale_cursor', 'log prefix changed; previous cursor is stale')
    chunk = data[offset:]
    boundary = chunk.rfind(b'\n')
    if boundary < 0:
        consumed = b''
        next_offset = offset
    else:
        consumed = chunk[:boundary + 1]
        next_offset = offset + boundary + 1
    records = []
    for line in consumed.decode('utf-8', errors='strict').splitlines():
        value = _decode_line(line)
        if value is not None:
            records.append(value)
    return {
        'events': records,
        'nextCursor': make_cursor(log_path, next_offset, scope),
        'nextOffset': next_offset,
        'truncatedTail': next_offset < len(data),
    }

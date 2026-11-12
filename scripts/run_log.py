"""Threadweave 结构化运行日志（JSON Lines）。

只写本地文件，不调用宿主 API；日志路径应位于 durable run 目录内。
"""
import json
from datetime import datetime, timezone
from pathlib import Path


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


def read_log(log_path):
    """读取 JSONL；跳过空行，损坏行标记为 log.corrupt。"""
    log_path = Path(log_path)
    if not log_path.is_file():
        return []
    records = []
    for line in log_path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            records.append({
                'event': 'log.corrupt', 'level': 'error',
                'raw': line, 'error': str(exc),
            })
    return records


def summarize_log(log_path):
    """按 event 统计次数。"""
    counts = {}
    for record in read_log(log_path):
        event = record.get('event', 'unknown')
        counts[event] = counts.get(event, 0) + 1
    return counts

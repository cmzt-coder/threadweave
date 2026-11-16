# O15 compact 与增量读取验收记录

日期：2026-11-16。证据 Run：`20261116-o15-reading-01`。

## 结论

O15 已完成 `status --compact` 和基于 JSONL 的 `status --since <cursor>` 增量读取。读取命令只读取本地状态和日志，不创建线程、不调用宿主、不修改原始 state/report。

## Compact 行为

- `status --compact` 输出单行 JSON。
- 默认字节预算 `7000`，可用 `--max-bytes` 配置，最低 `512`。
- 输出超过预算时按以下顺序压缩：
  1. 保留 selected run 的身份、状态、交付阶段和下一步动作。
  2. 减少 run/worker 明细并增加省略计数。
  3. 只保留最高优先级 next action，并增加 `nextActionsOmitted`。
  4. 再压缩 reason，绝不静默省略下一步动作。
- 如果最小可解析摘要仍超过预算，返回明确错误，不截断 JSON。
- 原始 `state.json`、report 和 notes 不被修改。

## Delta 游标

游标格式：

```text
v1:<scopeHash>:<byteOffset>:<sha256(prefix)>
```

- `scopeHash` 绑定 run/scope，跨 run 使用会被拒绝。
- 读取从 byte offset 后开始，只消费完整换行记录。
- 末尾半行保留在 `truncatedTail`，下一次读取再消费。
- 日志截断、前缀变化或旧游标不匹配返回 `stale_cursor`。
- 损坏 JSONL 行返回 `log.corrupt` 事件，不静默丢弃。
- 事件已全部读取时返回空事件列表和新的等价游标。
- delta 输出超过 `--max-bytes` 时明确报错，不省略事件。

## 实际证据

`20261116-o15-reading-01` 使用一条 12KB report、真实本地 audit.jsonl 和 state：

| 项目 | 结果 |
| --- | --- |
| compact exit code | `0` |
| compact bytes | `1442`，上限 `1500` |
| compact JSON | 可解析 |
| compact state 修改 | 否 |
| compact report 修改 | 否 |
| delta 初始游标 | `v1:8d7c081b7707d8b6:212:...` |
| delta 新增事件数 | `1` |
| delta 输出 bytes | `442` |
| delta next cursor | `v1:8d7c081b7707d8b6:324:...` |
| host API 调用 | 无 |

## 测试与回归

- 新增 `tests/status_compact_smoke.py`：17 项。
- `tests/run_log_smoke.py` 扩展为 16 项。
- 全套回归：30 套、1098 项断言通过。
- 覆盖 compact 字节上限、UTF-8 JSON、next action 保留、显式省略计数、delta 单事件、重复读取、跨 run 游标、非法游标、半行、日志截断和只读性。
- 未推送。

## 边界

- delta 依赖 append-only `audit.jsonl`，不是完整事件总线，也没有跨 run 的全局排序。
- 没有自动清理旧日志；日志轮转后旧游标会明确失效。
- compact 是读取视图，不改变 state schema 或机器校验原件。

# Codex 线程 cwd 绑定修复方案

日期：2026-11-17。范围：修复当前 thread 在 Codex `state_5.sqlite` 中仍绑定测试 worktree、而界面和终端显示主项目的问题。状态：根因已定位，修复工具已实现并通过受控测试；真实状态库尚未修改，等待关闭 Codex 后显式执行。

## 根因

当前 thread：

```text
01a1cdd0-d938-7d93-863a-8ec0c54c7ff1
```

Codex 状态库中的实际记录：

```text
threads.cwd = /Users/luzhentian/.codex/worktrees/o16-real-guard-20261116/codex-threadweave
```

同时：

- `thread_attachments` 仍有一条该 worktree 的 active 附件；
- `.git/worktrees/codex-threadweave/codex-thread.json` 的 ownerThreadId 是当前 thread；
- 主项目 checkout 是 `/Users/luzhentian/lzt/codex/luzhentian/codex-threadweave`；
- 用户界面和终端均显示主项目，但数据库 thread cwd 未随之更新。

因此归档工具依据数据库中的 thread cwd，把测试 worktree 当成当前 task 的 primary checkout，而 `list_artifacts` 又依据附件表把它列为 active，形成状态冲突。

## 修复原则

只修正一个 thread 的 `threads.cwd`：

- 不修改 `thread_attachments`；
- 不修改 `.git/worktrees`；
- 不删除测试 worktree；
- 不修改 Git 分支、HEAD 或工作区文件；
- 修改前做一致性核对并自动备份；
- 默认 dry-run，必须显式 `--apply --app-closed` 才写库。

## 已实现工具

`scripts/repair_codex_thread_cwd.py`

功能节点与触发条件：

| 节点 | 成功条件 | 阻断条件 |
| --- | --- | --- |
| 数据库核对 | 数据库文件存在且不是符号链接，thread 唯一 | 路径/线程不唯一或不可读 |
| 旧 cwd 核对 | 与 `--expected-cwd` 精确一致 | 已经修复、旧 cwd 不一致或不存在 |
| 新 cwd 核对 | 规范化绝对路径，且精确为 Git worktree root | 不是 Git root 或路径不合法 |
| 仓库关联 | 新旧 cwd 的 Git common dir 相同，且都在 worktree list 中 | 跨仓库或未注册 worktree |
| 附件保护 | 只读统计附件数量，修复前后必须一致 | 数量变化即失败 |
| dry-run | 只读 `PRAGMA quick_check`，不写库、不创建备份 | 检查失败 |
| apply | `--apply --app-closed` 且所有前置条件通过；先做 SQLite 一致性备份 | 没有 app-closed 确认或备份冲突 |
| 更新 | `BEGIN IMMEDIATE`，只更新目标 thread 的 `cwd`，影响行数必须为 1 | thread 在修复期间变化 |
| 完成核对 | 新 cwd 持久化、附件数量不变、quick_check=ok | 任一项不成立 |

## 已验证

`tests/codex_thread_cwd_repair_smoke.py`：17 项受控检查通过，覆盖 dry-run、未关闭确认、错误 thread/路径、附件保持、备份内容、备份完整性、重复修复阻断和真实状态库不修改。

真实状态库 dry-run 结果：

```json
{
  "status": "planned",
  "threadId": "01a1cdd0-d938-7d93-863a-8ec0c54c7ff1",
  "oldCwd": "/Users/luzhentian/.codex/worktrees/o16-real-guard-20261116/codex-threadweave",
  "newCwd": "/Users/luzhentian/lzt/codex/luzhentian/codex-threadweave",
  "attachmentCount": 1,
  "attachmentsWillChange": false,
  "gitMetadataWillChange": false,
  "worktreeWillChange": false
}
```

真实 Codex 状态尚未修改。

## 执行步骤

### 1. 完全退出 Codex

使用 `Cmd+Q` 退出 Codex，确认后台进程已结束。不能在 Codex 运行时直接改状态库，否则 WAL 或内存状态可能覆盖修改。

### 2. 在外部 Terminal 执行修复

```sh
cd /Users/luzhentian/lzt/codex/luzhentian/codex-threadweave

python3 scripts/repair_codex_thread_cwd.py \
  --db /Users/luzhentian/.codex/state_5.sqlite \
  --thread-id 01a1cdd0-d938-7d93-863a-8ec0c54c7ff1 \
  --new-cwd /Users/luzhentian/lzt/codex/luzhentian/codex-threadweave \
  --expected-cwd /Users/luzhentian/.codex/worktrees/o16-real-guard-20261116/codex-threadweave \
  --backup /Users/luzhentian/.codex/state_5.sqlite.before-cwd-repair.sqlite \
  --apply \
  --app-closed
```

预期输出：

```json
{
  "status": "repaired",
  "oldCwd": "/Users/luzhentian/.codex/worktrees/o16-real-guard-20261116/codex-threadweave",
  "newCwd": "/Users/luzhentian/lzt/codex/luzhentian/codex-threadweave",
  "attachmentCount": 1,
  "attachmentsWillChange": false
}
```

### 3. 重新打开 Codex 并核对

```sh
pwd -P
git rev-parse --show-toplevel
sqlite3 /Users/luzhentian/.codex/state_5.sqlite \
  "select cwd from threads where id='01a1cdd0-d938-7d93-863a-8ec0c54c7ff1';"
```

预期三者都指向主项目。然后重新调用 `list_artifacts`，确认测试 worktree 仍作为 active 附件出现。

### 4. 重新执行归档

通过新的 bridge operation 调用一次 `archive_worktree`。若成功，必须继续验证：

- 原始结果为 `queued` 或明确成功；
- 新附件为唯一 `archived_worktree`；
- 测试目录消失；
- `git worktree list` 不再包含测试 worktree；
- 没有使用强制删除。

## 回滚

若重新打开 Codex 后异常，完全退出 Codex，然后保留现场文件并恢复备份：

```sh
cd /Users/luzhentian/.codex
mv state_5.sqlite state_5.sqlite.failed-repair
mv state_5.sqlite-wal state_5.sqlite-wal.failed-repair 2>/dev/null || true
mv state_5.sqlite-shm state_5.sqlite-shm.failed-repair 2>/dev/null || true
cp state_5.sqlite.before-cwd-repair.sqlite state_5.sqlite
```

不要删除任何 `.failed-repair` 文件，便于后续诊断。

## 当前状态

修复工具和测试已提交到 Threadweave；真实 `state_5.sqlite` 尚未修改。下一步需要用户关闭 Codex 并在外部 Terminal 执行上面的 `--apply --app-closed` 命令，然后重新打开本聊天继续验证归档。

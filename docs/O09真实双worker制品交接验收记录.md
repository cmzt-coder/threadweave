# O09 真实双 worker 制品交接验收记录

日期：2026-11-16。Run：`20261116-o09-real-01`。

## 范围与执行方式

沿用用户授权创建的独立测试项目 `threadweave-validation-lab`，两个真实 Codex 聊天在独立 managed worktree 执行。用户确认请求配置均为 `deepseek-flash · high`、fail_closed；创建接口接受该配置，实际运行模型/thinking 字段不可独立观察，记为 unknown。

本轮使用 leader 原生 Codex 工具调度，使用项目 `archive_handoff` / `verify_handoff` 与独立 `execute_plan` 验证模块。不宣称本轮经过 DesktopBridgeAdapter 或 Controller 自动调度，也不扩大 T06 完成范围。

项目登记前置条件已解除：canonical root 唯一匹配 `f8e72908-1584-41e8-9e0f-35aeb7302a93`。

## 身份与基线

共同基线：`bb8a6162a08ba2929d27811098b22e9ac4062ed5`。独立集成分支：`codex/o09-real-integration`，测试项目 main 不参与验收代码修改。

| 角色 | 真实 threadId | 分支 | 独立 worktree |
| --- | --- | --- | --- |
| A | `01a1e298-a2ee-7702-b1e9-da8b0d9b691f` | `codex/o09-real-a` | `/Users/luzhentian/.codex/worktrees/df96/threadweave-validation-lab` |
| B | `01a1e298-e274-7f93-80d5-2aaed1803828` | `codex/o09-real-b` | `/Users/luzhentian/.codex/worktrees/d663/threadweave-validation-lab` |

两者 registration 后由 leader 直接 Git 核验 root/common dir/branch/HEAD/clean；都 ready 后才派发 A。B 的正式任务在 A commit 核验并归档后派发。

## 完成结果

A commit：`968936c38921fb94c3aea9bee0358a63351bbf52`。独立生产者检查与 4 项测试通过，改动仅三个 owned 文件，API schema 未改，worktree clean，线程 completed/idle。

manifest：`handoff_36331effc2fa5bd599ebca04`，producer a/attempt 1/taskRevision 1/sourceSha 为上述真实 commit，receiver b，purpose task-client。

归档制品 SHA256：`7f4899a3a2d47d7ea38022b356abc259836b104274d1d149db6e85bf0a51aac8`，291 字节。

B commit：`791a1ae48f4926fb48315dc4b766e000de94f5a8`。消费者实际读取归档 manifest，核对 run、生产者/接收者、purpose、source SHA、attempt、revision、制品字节及 API schema 后输出消费结果。17 项正负例全部通过，原始归档未被负例修改。

A fast-forward 后独立验证通过；随后 B 以 merge commit `8ed1ff819160418b5d7fed2f8cdd146467d25e2a` 集成，离线 setup、应用自检、4 项生产者测试、生产检查、实际消费和 17 项消费者测试全部通过。两个 reviewed commit 均为集成 HEAD 祖先，schema 未变，集成工作区干净。主项目完整回归为 36 套 / 1193 项断言通过。

B 首次 leader 独立验证因 sandbox 无权写 worker worktree 的 `.build` 失败；保留原始记录，经自动审批允许后重跑通过，并非代码失败。

两个聊天均 completed/idle 后归档成功。报告、notes、源文件快照及消费输出已外置保全，两个 worker 和一个集成 worktree 均非强制移除，三个本地分支保留。测试仓库仅剩主 checkout，main 仍为共同基线。移除生产者 worktree 后再次 `verify_handoff` 通过。

O09 在新建受控应用的真实 A→B worker 场景范围内完成，标记 `[x]`。已有业务项目适用性不在本轮结论内。

## 证据目录

`.threadweave/20261116-o09-real-01/` 保留项目匹配、创建和消息回执、registration、启动 Git 观察、dispatch 全文、冻结验证计划、worker 报告/notes、SHA 绑定的独立验证、manifest 与制品归档。

原始创建返回 clientThreadId，后续通过唯一项目/title、完整初始 prompt、worktree 和基线核对真实 threadId。该关联属于 leader 核对，不声称宿主提供签名 client→thread 映射。

## 边界

这是新建受控应用上的真实 worker 验收，不等同于已有业务项目适用性验收。没有立即中断命令能力验证；没有修改模型/认证配置；没有推送。H01 和 O16 destructive apply 未纳入本轮。

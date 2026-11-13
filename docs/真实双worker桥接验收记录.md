# 真实双 worker 桌面桥接验收记录

日期：2026-11-13。Run：`20261113-desktop-dual-01`。基线：`043bf1d5b1c4db27237c7d28a73c0d6ba8fd38c5`。

本轮沿用用户确认的 A 路线：桌面 leader 执行真实工具，桥接账本持久化创建、消息和查询请求及回执，Controller 执行报告、独立验收、返工与集成门禁。没有连接私有 socket、启动 daemon 或变更认证配置。本轮本地提交，未推送。

## 真实 worker 与产物

| 角色 | 真实 threadId | 模型 / thinking | 独占工作分支 | 最终提交 | 独立验收 |
| --- | --- | --- | --- | --- | --- |
| A | `01a1d3fa-9c19-7c11-802b-eeb2732d8a2c` | `deepseek-flash / high` | `codex/host-dual-a-20261113` | `6b1eb697a404ef5e1411113fcc021207eaf02636` | 37 项 |
| B | `01a1d3fa-e80e-7f60-b870-71e2aefb69ea` | `deepseek-flash / high` | `codex/host-dual-b-20261113` | `c728ae2947e90341b62849cb863007410d2a4321` | 75 项 |

两者的模型来自本轮用户明确指定，禁止自动替换。实际创建参数及各自本地会话 `turn_context` 均为 `deepseek-flash / high`，A 有 3 条、B 有 6 条配置记录，无观察到的回退。该证据证明宿主记录的配置，不是上游供应商执行证明。worker 自报观察不到模型时仍记录 `unknown`，leader 独立配置证据另存，未混入报告 JSON 的封闭 schema。

A 仅新增 `tests/desktop_bridge_cli_write_smoke.py`：通过全新子进程调用公开 CLI，覆盖创建账本、参数绑定、异步 ID 解析、unknown 显式核对、终态保护和多行 UTF-8 消息文件。B 仅新增 `tests/desktop_bridge_send_recovery_smoke.py`：覆盖失败回执、最终状态保存失败后的内存回滚与磁盘恢复、unknown 核对、任务修订隔离。两份测试中的宿主返回均明确为本地 fixture；真实宿主证据来自本轮另外保存的工具原始返回。

## 创建、启动、派发与报告

1. `list_projects` 唯一匹配既有 Git 项目。两个真实 `create_thread` 返回 `clientThreadId`，未将异步 ID 当作真实 threadId 使用。
2. leader 对照项目、标题、创建提示及线程历史精确核对真实线程，并独立验证宿主分配目录、common directory、分支、基线和干净状态。当前宿主没有直接的 client→thread 映射，关联属于 leader 显式复核，不宣称宿主提供签名映射。
3. 启动时两者曾在创建 branch ref 后遭遇 managed worktree `HEAD.lock` 写权限问题。A 恢复后报告；B 按规则停止，leader 核对既存 ref 为精确基线且未被其他 worktree 使用，再发独立纠正消息。全部 ready 后才正式派发。
4. 每个写操作均先 prepare、一次 claim，再调用真实工具、保存原始返回并 complete。正式派发先保存 `dispatchIntent`，accepted 回执由 Controller 回读后推进。accepted 不被当作完成或停止证据。
5. 两者通过 final answer 给出报告路径与 SHA；leader 通过真实 completed/idle 观察确认停止，接收标准 `report.json`，独立核对 Git 与写集，再执行分支验证计划。
6. 初始消息尾部的模型元数据与报告封闭 schema 不一致，leader 在首次报告接收前发澄清消息：模型元数据放 Markdown，JSON 仅使用标准字段。生产 schema 未为探针临时放宽。

## 两次真实返工与上限

| B 阶段 | taskRevision | reworkCount | 提交 | 独立验收 |
| --- | --- | --- | --- | --- |
| 首版 | 1 | 0 | `e2ab604b8b25aec4cd69cd2f155d8364ff505b40` | 37 项 |
| 返工 1：unknown 核对 | 2 | 1 | `97252b2b92fba9a64dcc4595b9154a5f7d9a2bd5` | 57 项 |
| 返工 2：旧任务回执隔离 | 3 | 2 | `c728ae2947e90341b62849cb863007410d2a4321` | 75 项 |
| 第三次请求 | 保持 3 | 保持 2 | 无新提交、无消息 | Controller 拒绝 |

两次返工均有真实任务消息、worker 增量提交、完整报告与独立验收。每次递增任务修订并清除旧报告、acceptance、验证计划及停止证据；接收前版迟到报告时均拒绝，磁盘状态不变。第三次调用 `Controller.rework` 返回 `rework limit reached; leader decision required`，状态内容、修订、计数和消息账本不变，独立审计保留 `rework.limit_reached`。本轮没有准备或发送第三条返工消息；该上限由 Controller 强制，不是宿主内建限制。

## 串行集成、故障注入与恢复

冻结集成计划为 `integration-verify.json`：先运行 `tests/run_all.py`，再运行独立受控故障步骤。全程 planHash 为：

`f012be0108d77916ac200654f417855b768e8c146baeb2bc69fc76d3429ee0cd`

故障脚本及计划保持不变，通过 ignored 的 `fault.enabled` 文件控制退出码；这是显式验收故障注入，不是发现产品逻辑缺陷。

| 顺序 | 操作及实际结果 |
| --- | --- |
| 1 | A fast-forward 合入，实际 HEAD 为 `6b1eb69`；分支验收已通过 |
| 2 | 创建故障标记；集成回归 24 套 / 840 项通过，故障步骤实际 exit 17；保存失败日志和 `integrationBlocked`，A 保持 reported |
| 3 | B 集成调用在阻断处拒绝，尚未合并 B；Git HEAD 与状态字节不变 |
| 4 | 独立新 Python 进程从磁盘构造 Controller，B 仍被同一阻断拒绝；未把同进程对象重建称为进程恢复 |
| 5 | 真实阻断 run 执行只读 `next`，输出 `recover_integration` 和 `recoveryExecutionBlocked`；状态内容、mtime、Git HEAD、消息账本均不变 |
| 6 | 保留标记显式重验，仍 exit 17；阻断不解除，失败恢复记录保留 |
| 7 | 明确移除标记，同一冻结计划在相同实际 HEAD 重验通过，解除阻断并保留失败历史 |
| 8 | 再次调用 A 的 integrate 完成真正集成登记，A→integrated |
| 9 | B 以 merge commit `0ad4722509b5822f5d177216b6c505e50603c84c` 合入，再独立集成验收通过，B→integrated |

本轮验证了新进程读取真实 run 的持久阻断；没有主动杀死 leader 进程、没有真实网络超时或丢回执注入。unknown / 保存失败由新增本地测试覆盖，不能据此宣称真实宿主通信故障已验证。

## 证据、收尾与验证

本地审计根目录为 `<repo-root>/.threadweave/20261113-desktop-dual-01/`，受 Git 忽略，不随代码提交：

- `state.json` 为权威状态，最终 `complete` / revision 39；两个 worker 均 `archived`，停止证据与最终 taskRevision 一致，`integrationBlocked` 已解除，失败日志仍在。
- `state.md` 由 `write_state_view` 从最终 JSON 重建；`run-manifest.json` 六阶段全部完成。
- `bridge/` 保存创建、初始化、正式派发、澄清、返工和查询的请求、claim、回执与显式关联证据。
- `reports/{a,b}/r*/` 保留每版原始 JSON / Markdown / notes、停止证据和独立验收；标准归档副本在 `reports/<role>/1/<revision>/`，`archive-manifest.json` 记录 20 个文件的 SHA256，已核验。
- `reports/` 另存原始创建/消息/等待/归档返回、模型上下文、启动门禁、旧报告拒绝、第三次返工拒绝、阻断与恢复、只读 next、分支 patch、清理前核验和最终摘要。

两个真实聊天已归档。清理前确认分支已合入、目录干净、报告外置、worker 停止且停止修订新鲜；随后非强制移除两个专用 worktree，保留分支。最终 `git worktree list` 只剩 leader 主 checkout。未创建 heartbeat automation。

最终实际集成 HEAD 上全套 **25 套 / 915 项断言通过**，故障步骤恢复为 exit 0；新增测试编译检查、`git diff --check`、状态 schema 与 `doctor` 检查通过。后续文档提交会使 Git HEAD 前进，以上 acceptance 仍精确绑定已记录的集成 SHA，不自动替换为文档提交 SHA。

## 待办结论与能力边界

- O01、O02、O05：本地实现及本轮规定的真实双 worker 验收完成。
- O03：合法独占写集链已真实验证；越界 worker 的真实负例尚未注入，保持部分完成。
- O04：已在真实阻断 run 上验证只读恢复建议与不改写；真实进程崩溃后完整观察仍未注入，保持部分完成。
- O07：实际资源组项目尚未验证，保持部分完成。
- T06：结构化报告、独立验收、双 worker 串行集成、真实返工与新进程阻断恢复已覆盖；wait/worktree/归档依然由 leader 直接调用，尚无对应 HostAdapter 方法，仍为部分完成。未将桌面人工桥接描述成独立全自动适配器。

# Codex 线程团队项目调研与迭代建议

> 调研对象：`itmada/codex-thread-team`\
> 调研日期：2026-11-12\
> 文档性质：项目调研、功能拆解、问题复盘、后续迭代建议\
> 核心结论：**设计思路值得借鉴，工程实现偏轻，当前环境属于“条件可用”，不建议直接作为生产基座。**

---

## 一、执行摘要

`itmada/codex-thread-team` 是一个 Codex Skill，目标是把一个 Codex 会话组织成“leader + 多个 worker 线程”的小型工程团队：

- 当前线程作为 leader，负责分析、拆分、派发、轮询、决策、合并和最终复查；
- 每个 worker 是一个真实 Codex 线程，拥有独立任务边界、分支和工作目录；
- worker 完成后提交代码、写结构化报告，由 leader 逐个审查并串行合并；
- 整个运行过程通过状态文件、worker notes、报告文件和 heartbeat 进行恢复与审计。

项目最有价值的部分不是“如何创建多个线程”，而是它对并行 agent 常见失败模式的处理：

1. 共享 checkout 会污染代码；
2. leader 上下文会被大量转录和 diff 拖垮；
3. 长任务会超过单个会话的生命周期；
4. busy-wait 会持续消耗 leader；
5. worker 可能启动失败或中途卡死；
6. 一次性合并会让缺陷无法归因；
7. 含糊的任务派发会产生含糊的结果。

从设计上看，这些护栏是完整且有针对性的。\
但从工程实现看，它本质上只是 **Prompt/Skill 文件集合**，没有代码、测试、CI、状态机或真正的调度器。因此它更适合作为参考架构、Prompt 模板库和小范围试验，而不是企业级多 agent 基础设施。

### 结论评分

| 维度 | 判断 |
| --- | --- |
| 协作模型设计 | 强 |
| Prompt 与模板质量 | 较强 |
| 工程实现完整度 | 弱 |
| 当前 Codex 兼容性 | 条件可用 |
| 社区与维护信号 | 弱 |
| 可二次开发性 | 中等，适合改 Prompt，不适合直接当执行引擎 |
| 建议使用方式 | 参考、试验、二次开发起点 |

---

## 二、项目概况

### 2.1 基本信息

| 项目 | 情况 |
| --- | --- |
| GitHub 仓库 | <https://github.com/itmada/codex-thread-team> |
| 作者 | `itmada`，GitHub 显示名 `nanxu` |
| 创建时间 | 2026-06-08 |
| 最近提交 | 2026-07-02，commit `eb5a220` |
| 提交数 | 2 次 |
| 文件规模 | 10 个文件，约 613 行，全部为 Markdown/YAML |
| Star / Fork | 0 star、1 fork |
| Issue / PR / Release | 0 issue、0 PR、无 release |
| License | 无 LICENSE 文件 |
| Contributor | 仅 `itmada` 一人 |
| 主要形态 | Codex Skill，不是插件、MCP 服务或独立调度器 |

### 2.2 项目定位

项目把一个 Codex 会话组织成“小型工程团队”：

- **leader thread**：负责规划、拆分、派发、轮询、仲裁、合并和最终 review；
- **worker thread**：负责一个独立任务边界、一条分支和一个独立工作目录；
- **peer worker**：可以在 dispatch 明确允许时互相沟通接口、依赖和共享契约，但不能越权修改或合并；
- **用户**：只在拆分方案、worker 配置和真正需要拍板的 blocker 上介入。

它刻意拒绝使用 `spawn_agent` 模拟整个流程，理由是 subagent 通常不是用户可见、可长期交互、可独立归档和替换的线程。这个区分有合理之处，但 README 中“只有 Codex 有一等线程”“subagent 都是一次性的”这类表述有明显夸张成分。

### 2.3 项目演进

仓库只有两次提交：

1. **2026-06-25：Baseline**
   - 初版 thread-team skill；
   - 已有 leader/worker、分支/worktree、状态文件、报告模板等核心设计；
   - 但没有 README，部分流程描述较松。

2. **2026-07-02：Document thread-team workflow**
   - 增加中英文 README；
   - 重写 `SKILL.md`；
   - 强化 heartbeat、卡死替换、上下文经济、串行集成、最终清理；
   - 将 worker 之间的通信限制为 dispatch 明确声明的 peer；
   - 将 leader 状态文件细化为按 leader thread ID 分目录。

作者在掘金发布过介绍文章，页面显示约 184 阅读。检索到的 CSDN 内容主要是转载或推广性质，未发现独立 benchmark、真实案例复盘或大规模使用报告。

---

## 三、核心功能与新增机制

> 项目没有正式 release/version，所以这里把“仓库当前呈现的能力”称为核心功能与新增机制，不声称它们对应某个正式版本。

### 3.1 真实 worker 线程

worker 不是临时 subagent，而是通过 `create_thread` 创建的独立 Codex 线程。

设计意图：

- 保留自己的上下文；
- 使用独立模型和 thinking 配置；
- 可以被 leader 中途发消息；
- 可以在卡死时归档并替换；
- 可在 Codex App 中作为独立任务存在。

这使 worker 更像“长期团队成员”，而不是一次性任务调用。

### 3.2 每个 worker 独占 worktree

这是项目最重要的隔离机制。

项目要求：

- 每个 worker 使用独立工作目录；
- 不允许两个 worker 共享 checkout；
- 无法保证目录独占时，不进入团队模式，退回单线程执行；
- 集成完成后移除 leader 创建的 worktree。

它正确识别了一个关键事实：**分支本身不隔离工作树，只有独立 worktree 才能避免并行覆盖。**

### 3.3 六阶段 leader 工作流

| 阶段 | 主要动作 | 退出条件 |
| --- | --- | --- |
| 1. 分析与拆分 | 读代码、做可行性评估、拆分任务、确认 worker 配置、写状态文件 | 用户授权，状态文件存在 |
| 2. worker 初始化 | 创建 worker、分配分支和 worktree、发送注册信息 | 每个 worker 有独立目录并已注册 |
| 3. 深度设计与派发 | 为每个 worker 设计执行步骤、边界、验收标准和验证命令 | 每个 worker 收到正式 dispatch |
| 4. 轮询与协作 | 启动检查、heartbeat、处理 blocker、卡死替换 | 所有 active worker 发出完成信号 |
| 5. 报告与合并 | 读报告、审查 diff、返工、逐分支 merge、每次 merge 后验证 | 所有选定分支已审查合并并验证 |
| 6. 最终复查与清理 | 检查跨 worker 集成问题、修复、删除 heartbeat、移除 worktree | 最终 review 通过，清理确认 |

### 3.4 自包含 dispatch

正式 dispatch 要求包含：

- leader thread ID；
- worker thread ID；
- worker branch；
- 工作目录；
- base commit；
- integration branch；
- 角色和完整花名册；
- 责任边界与依赖关系；
- 允许直接沟通的 peer；
- 详细执行步骤；
- 验收标准；
- 共享契约；
- 验证命令；
- 报告格式。

这套模板能显著降低 worker 因上下文不足而跑偏的概率。

### 3.5 leader 状态持久化

状态文件位置：

```text
.thread-team/<leader-thread-id>/state.md
```

记录内容包括：

- leader/worker 花名册；
- 分支和 worktree；
- dispatch 状态；
- 依赖关系；
- 决策日志；
- 轮询状态；
- heartbeat ID；
- 报告和 commit SHA；
- 合并顺序；
- 集成结果；
- 最终 review 和清理状态。

设计目标是让 leader 在上下文压缩、会话中断或重启后，能够先读状态文件再继续。

### 3.6 上下文经济

项目对 leader context 的处理比较成熟：

- 大块内容不通过消息传输；
- worker 把完整报告写到 `.thread-team/report.md`；
- worker 把进度记录到 `.thread-team/notes.md`；
- 跨线程消息只传状态、路径和 commit SHA；
- `read_thread` 只用于启动检查、疑似卡死和消息含糊，不作为例行巡查。

这是整个项目中最值得借鉴的设计之一。

### 3.7 heartbeat 与卡死处理

项目使用两分钟 heartbeat 替代 busy-wait：

- leader 不持续轮询；
- 每个 heartbeat 周期处理 worker 消息、blocker 和完成信号；
- 如果 worker 一直没有启动，归档并替换；
- 如果 worker 中途长时间无活动，先请求状态，再决定重新派发、替换或收回工作；
- 工作流创建的 heartbeat 必须在结束前删除。

### 3.8 串行合并与最终集成复查

项目不采用“所有分支一次性 merge”的方式，而是：

1. 读取 worker 报告；
2. 对照 dispatch 审查 diff；
3. 检查规格符合度和代码质量；
4. 需要返工的退回原 worker；
5. 合并；
6. 跑构建和测试；
7. 再处理下一个分支；
8. 全部合并后，再检查跨 worker 的契约、重复逻辑和接缝问题。

这样能让缺陷和回归更容易归因到具体 worker。

### 3.9 abort 协议

用户取消或 leader 判断无法继续时，项目要求：

- 通知所有 active worker 停止；
- 让 worker 提交成形的 WIP；
- 删除本工作流创建的 heartbeat；
- 归档 worker 线程；
- 报告哪些分支有未合并 commit；
- 移除不需要保留的 worktree；
- 将状态文件标记为 `aborted`。

这比“直接停止响应”更完整。

---

## 四、目录与文件结构

```text
thread-team/
├── SKILL.md                             # leader 运行工作流
├── agents/
│   └── openai.yaml                      # skill 展示名和默认 prompt
├── references/
│   ├── worker-registration-template.md  # worker 身份和团队规则
│   ├── worker-dispatch-template.md      # 正式任务派发模板
│   ├── worker-report-template.md        # worker 完成报告模板
│   ├── leader-state-template.md         # leader 状态文件模板
│   └── final-report-template.md         # leader 最终报告模板
├── README.md
└── README.zh-CN.md
```

运行时还会在仓库根目录产生：

```text
.thread-team/<leader-thread-id>/state.md
<worker-worktree>/.thread-team/notes.md
<worker-worktree>/.thread-team/report.md
```

这些文件通过 Git 的 `info/exclude` 排除，不进入提交。

---

## 五、项目的优点

### 5.1 对并行失败模式认识准确

项目的重点不是“如何 spawn 更多 agent”，而是“如何避免并行 agent 的典型翻车方式”。这比很多只做并发调用的工具更成熟。

### 5.2 worktree 隔离思路正确

把每个 worker 限制在独立 worktree，是处理并行写代码冲突的正确方向。

### 5.3 dispatch 模板质量较高

模板把任务范围、base commit、文件所有权、验收标准、验证命令和共享契约都列出来了，适合直接作为内部 agent 任务协议参考。

### 5.4 上下文管理意识强

“消息传信号、文件传 payload”的做法值得保留。它能避免 leader 被大量 diff、日志和转录污染。

### 5.5 串行集成和逐次验证设计合理

逐分支审查和逐次构建/测试，是把并行产出变成可归因结果的关键。

### 5.6 有恢复和审计意识

状态文件、worker notes、结构化报告和 abort 协议，说明作者考虑了长任务的中断和恢复。

### 5.7 中英文文档和模板齐全

README 有英文和简体中文版本，worker/leader 模板也相对完整，便于二次修改。

---

## 六、发现的问题

### 6.1 问题总览

| 级别 | 问题 | 影响 |
| --- | --- | --- |
| P0 | managed worktree 默认 detached HEAD，skill 未要求 worker 建分支 | worker 提交无法形成可合并分支 |
| P0 | 把 `projectId` 当作文件路径 | `create_thread` 可能直接失败 |
| P0 | 未处理 `pendingWorktreeId` / `clientThreadId` | leader 无法寻址、发消息、轮询或清理 worker |
| P1 | worktree 路径没有可靠解析步骤 | dispatch 可能缺少真实工作目录 |
| P1 | worker 报告随 worktree 删除而丢失 | 审计和恢复记录不完整 |
| P1 | leader 状态文件可能位于临时 worktree | 状态可能随 worktree 清理一起消失 |
| P1 | 并发运行的 branch/worktree 命名没有 run 级隔离 | 两个 run 可能发生分支冲突 |
| P1 | preflight 只评估任务经济学，不检查 Git/工具/模型 | 失败往往在创建 worker 后才暴露 |
| P2 | 默认模型 `gpt-5.4` 与当前环境脱节 | 需要用户手动覆盖，否则可能创建失败 |
| P2 | 对 `update_plan` 等非核心工具没有降级方案 | worker 计划阶段可能失败 |
| P2 | 两分钟 heartbeat 可能带来不必要的 token 成本 | 长任务成本上升 |
| P2 | `From: thread ID` 只是文本约定 | 不能作为安全认证 |
| P2 | 假设单仓库，未定义多仓库协作 | 跨仓任务无法正确隔离和集成 |
| P3 | 无测试、CI、release、LICENSE | 质量和法律可复用性存在不确定性 |

---

### 6.2 P0：Codex managed worktree 默认 detached HEAD，未要求 worker 建分支

OpenAI 官方 [Worktrees 文档](https://learn.chatgpt.com/docs/environments/git-worktrees)明确说明：

- Codex 创建 managed worktree 时，默认处于 **detached HEAD**；
- 要形成可合并分支，需要点击 **Create branch here**，或显式执行 `git switch -c <branch>`；
- 同一个分支不能同时在多个 worktree 中 checkout。

但项目的首选创建方式只写了：

```text
startingState: {
  type: "branch",
  branchName: <integration-branch>
}
```

这里的 `branchName` 只是 worktree 的起始分支，并不会自动创建 worker 分支。dispatch 模板只写：

```text
Your branch: <worker-branch>
```

没有要求 worker 执行：

```bash
git switch -c <worker-branch>
```

实际后果：

- worker 可能在 detached HEAD 上提交；
- 报告里有 commit SHA，但不存在真正的 worker branch；
- leader 后续执行 `git merge <worker-branch>` 时找不到分支；
- worker 如果自行猜一个分支名，可能和 base commit 或其他 worker 冲突。

项目的 fallback 方式：

```bash
git worktree add <path> -b <worker-branch> <base-commit>
```

反而没有这个问题。问题集中在首选的 `create_thread` worktree 路径。

**建议修复：**

- 在 phase 2 显式创建 worker 分支；
- 或在 `startingState` 中使用 `onMissing: "create-branch"`，并传入唯一的 worker 分支名；
- worker 启动后先验证：

```bash
git symbolic-ref --short HEAD
git rev-parse HEAD
git status --short
```

确认当前不是 detached HEAD，且 base commit 正确。

---

### 6.3 P0：把 `projectId` 当成文件路径

当前 `create_thread` 的 project target 需要 `list_projects` 返回的 **projectId**，而不是任意仓库路径。

但 `SKILL.md` 写的是：

```text
projectId: <workspace-root-or-project-id>
```

fallback 里还说可以创建“rooted at that path”的 project target。\
按当前工具定义，project target 没有任意目录字段；直接用 `/path/to/repo` 作为 `projectId` 可能触发 worktree 初始化失败。

GitHub 上也有类似问题：[#28204](https://github.com/openai/codex/issues/28204) 展示了使用路径作为 projectId 后出现 `Worktree init failed` 的情况。

**建议修复：**

1. 先调用 `list_projects`；
2. 按仓库根目录匹配真实 projectId；
3. 找不到就停止，不要用路径代替；
4. 如果要做 fallback，必须使用已注册项目，或明确切换到“本地 checkout + 手工 worktree”的另一条路径。

---

### 6.4 P0：没有处理 `pendingWorktreeId` / `clientThreadId`

当前 `create_thread` 可能是异步的。上游 issue 已经多次报告：

- [#26861](https://github.com/openai/codex/issues/26861)：只返回 `pendingWorktreeId`，没有可寻址的 `threadId`；
- [#28204](https://github.com/openai/codex/issues/28204)：worktree 初始化失败后只留下 `Worktree init failed`，父线程无法查询失败原因、取消或清理。

但项目的流程默认：

- 创建后马上有 `threadId`；
- 可以马上 `send_message_to_thread`；
- 可以马上知道 worktree 路径；
- 可以马上进入注册和 dispatch。

这在实际环境里并不成立。

**建议修复：**

- 创建后先用 `wait_threads` 等待；
- 通过 `list_threads` 或线程元数据确认 `threadId`、`cwd` 和状态；
- 对 `pendingWorktreeId` 建立显式映射；
- 初始化失败要进入可恢复状态，而不是继续按正常流程走；
- 如果没有 `threadId`，不要发送 dispatch，也不要写入正常 worker 状态。

---

### 6.5 P1：worktree 路径没有可靠解析步骤

状态模板要求记录：

```text
| worker | thread ID | branch | working directory | ... |
```

dispatch 也要求写入：

```text
Your working directory: <absolute-path>
```

但 `create_thread` 的成功返回不一定直接包含 worktree 路径。实际路径可能要从线程元数据、`list_threads` 结果或 worker 自己回报中获取。

项目没有定义“如何取得并验证 cwd”的步骤。这样一来：

- leader 可能在不知道 worker 真实目录的情况下发送 dispatch；
- worker 可能误在 leader checkout 中操作；
- 状态文件中的 worktree path 可能不准确。

**建议修复：**

```text
创建线程
→ wait_threads
→ 读取 thread 元数据或让 worker 回报 pwd
→ 验证 cwd 是独立 worktree
→ 再发送正式 dispatch
```

---

### 6.6 P1：worker 报告会随着 worktree 清理一起消失

README 说 `.thread-team` 是“让恢复、替换和审计成为可能的书面记录”。但 phase 6 又要移除 leader 创建的 worktree。

而 worker 的完整报告在：

```text
<worker-worktree>/.thread-team/report.md
```

worktree 一旦被 `git worktree remove`，这些报告和 notes 也会消失。leader 状态文件里通常只留下摘要，不是完整证据。

**建议修复：**

在清理 worktree 之前：

```bash
mkdir -p .thread-team/<leader-id>/workers/<role>
cp <worker-worktree>/.thread-team/report.md \
   .thread-team/<leader-id>/workers/<role>/report.md
cp <worker-worktree>/.thread-team/notes.md \
   .thread-team/<leader-id>/workers/<role>/notes.md
```

然后再移除 worktree。

---

### 6.7 P1：leader 状态文件可能位于临时 worktree

如果 leader 线程本身运行在 Codex-managed worktree 中，那么：

```text
.thread-team/<leader-id>/state.md
```

也在这个 worktree 里。Codex 归档聊天或触发 worktree 清理时，这个状态文件可能一起消失。

这会让“长任务比会话活得久”这个设计目标变成有条件成立。

**建议修复：**

把关键状态放到更持久的位置，例如：

- 源码仓库的本地 checkout；
- `$CODEX_HOME` 下的独立目录；
- 或至少在运行结束前导出一份经过脱敏的最终报告到持久目录。

---

### 6.8 P1：并发运行的 branch/worktree 命名没有 run 级隔离

状态目录按 leader thread ID 隔离，但分支名和 worktree 路径没有明确要求带 run ID。

两个 leader 同时运行，可能都创建：

```text
worker-api
worker-frontend
```

最终发生：

- `git worktree add -b worker-api` 失败；
- 两个 run 误用同一个分支；
- leader 合并了另一个 run 的分支。

**建议修复：**

使用 run-scoped 命名，例如：

```text
tt/<run-id>/<worker-role>
```

并在 preflight 中检查：

```bash
git branch --list
git worktree list
```

---

### 6.9 P1：preflight 没有真正检查运行环境

项目会让 leader 判断任务是否适合并行，但没有强制检查：

- 当前目录是否是 Git 仓库；
- leader checkout 是否有未提交改动；
- integration branch 是否存在；
- 是否已有同名 worktree/branch；
- 仓库是否 bare；
- `$CODEX_HOME/worktrees` 是否可写；
- 当前线程是否真的暴露 `create_thread`、`send_message_to_thread`、`read_thread`；
- 目标模型是否可用；
- `update_plan` 是否存在；
- worker 是否继承正确的 sandbox/approval 配置。

这与它自己“如果无法保证独占目录就不进入团队模式”的原则不匹配：原则是对的，但没有落到可执行的 preflight 命令。

---

### 6.10 P2：默认模型 `gpt-5.4` 与当前环境脱节

README 和 `SKILL.md` 把默认 worker 配置固定为：

```text
model: gpt-5.4
thinking: high
```

`gpt-5.4` 并非全线下线，OpenAI 手册里仍能看到该模型；但当前这台调用主机暴露给 `create_thread` 的可用模型清单中没有 `gpt-5.4`。如果用户直接使用默认值，worker 创建可能失败。

这反映的是更深层的问题：Skill 把模型名硬编码在提示词里，而不是从当前运行时配置中发现模型。

**建议修复：**

- 用运行时可用的模型列表动态选择；
- 或者在用户确认阶段明确要求用户指定；
- 不要在 Skill 里写死一个会过期的默认模型。

---

### 6.11 P2：对 `update_plan` 等非核心工具的依赖没有降级方案

worker 模板要求每个 worker 调用 `update_plan`，使用固定的三阶段计划。但 `update_plan` 并不是所有 Codex 会话、远程环境或 CLI 版本都一定暴露。

`SKILL.md` 只检查了三个 thread 工具，没有检查：

- `update_plan`；
- `automation_update`；
- `set_thread_archived`；
- `set_thread_title`；
- `wait_threads`。

结果是核心流程可能启动，但在 worker 计划或 cleanup 阶段才暴露缺工具。

**建议修复：**

在 phase 1 做 capability probe，并准备降级：

- 没有 `update_plan`：用 notes 文件记录三阶段；
- 没有 heartbeat：用 `wait_threads`；
- 没有 archive：保留线程并明确报告；
- 没有 `send_message_to_thread`：直接停止，不要假装能协作。

---

### 6.12 P2：heartbeat 可能带来不必要的成本

两分钟 heartbeat 的设计解决了 busy-wait，但也会在长任务中反复唤醒 leader，产生额外模型调用和上下文消耗。

更好的方式是：

- 优先使用 `wait_threads` 做事件等待；
- 只有 `wait_threads` 不可用或需要定期处理团队决策时，才使用 heartbeat；
- heartbeat 周期可配置，并设置最大次数；
- 任务结束时做一次强制清理验证，而不是只“口头确认已删除”。

---

### 6.13 P2：`From: thread ID` 不是安全认证

模板要求所有消息以：

```text
From: <role> (thread ID: <id>)
```

开头，并通过花名册核对。但这是纯文本，不是密码学认证。任何能写消息的线程都可以伪造这个 header。

对模型协作来说，这足够减少误认；但不应把它描述成安全边界。

---

### 6.14 P2：单仓库假设

项目围绕一个 repo root、一个 integration branch 和一组 worktree 设计，没有定义：

- 多仓库任务；
- 跨仓库共享契约；
- 多仓库合并顺序；
- 不同 repo 的 CI 验证；
- 跨仓库 worker 的沟通和状态。

如果任务涉及多个服务或多个仓库，需要上层再包一层协调器。

---

### 6.15 P3：无测试、CI、release 和 LICENSE

仓库没有：

- 自动化测试；
- dry-run；
- smoke test；
- CI；
- 版本 tag；
- release；
- LICENSE。

README 称其“已开源”，但 GitHub 没有许可证文件。按开源惯例，这更接近 **source-available**，而不是标准开源授权。公司内部使用、二次修改或再分发前应先确认授权。

---

## 七、当前 Codex 兼容性

当前 Codex App 确实提供了 `create_thread`、`send_message_to_thread`、`read_thread`、`list_threads`、`wait_threads`、`set_thread_archived` 等工具，所以这个 Skill 的核心思路不是空中楼阁。

但 2026 年下半年出现了多次与它强相关的平台问题：

- [#40852](https://github.com/openai/codex/issues/40852)：macOS Desktop 某版本中，`list_threads`、`read_thread`、`wait_threads` 仍可用，但 `create_thread`、`send_message_to_thread` 等 effectful 工具从可调用工具目录中消失；
- [#40865](https://github.com/openai/codex/issues/40865)：Remote SSH 场景下旧 inter-task 工具被禁用，`codex_app` MCP replacement 又不可用；
- [#26861](https://github.com/openai/codex/issues/26861)：pending worktree 没有可寻址 `threadId`；
- [#28204](https://github.com/openai/codex/issues/28204)：worktree 初始化失败后缺少可恢复错误和清理入口。

这些不是 `codex-thread-team` 自身能修复的，但它们直接决定这个 Skill 是否可用。它的“工具缺失就停止”是正确态度，但也意味着它在这些环境里只能停止，不能提供降级方案。

### 与原生 subagent 的关系

OpenAI 官方 [Subagents 文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)说明：

- 当前 Codex 默认支持 subagent workflow；
- Codex App 会展示每个 subagent thread，可查看活动；
- 主线程负责收集结果；
- 本地 Codex 还支持自定义 agent 的 model 和 instructions。

因此，README 中“subagent 都是用完后丢弃的，只有 thread-team 才是真实线程”这个说法有些过头。更准确的区别是：

| 维度 | 原生 subagent | codex-thread-team |
| --- | --- | --- |
| 生命周期 | 通常嵌套在主任务里，偏短期 | 用户可见的独立线程，可长期存在 |
| 交互 | 主线程收集结果 | 可中途发消息、归档、替换 |
| 适用场景 | 读代码、探索、测试、日志分析 | 多个独立实现分支、长任务、串行集成 |
| 复杂度 | 低 | 高 |
| 稳定性 | 官方原生，通常更稳 | 依赖 thread 工具和 worktree 行为 |
| 成本 | 多 agent token 成本 | 多 agent + leader 轮询、审查、合并成本 |

如果只需要并行读代码、跑测试或做调研，原生 subagent 更简单；如果确实需要“多个可回复、可归档、各自有分支的长期 worker”，这个 Skill 才有独立价值。

---

## 八、竞品与相近项目对比

| 项目 | 形态 | 优点 | 风险 |
| --- | --- | --- | --- |
| `itmada/codex-thread-team` | Prompt-only Codex Skill | 六阶段流程清晰，worktree/dispatch/审计模板完整 | 无代码/测试/许可证，模型和工具接口易漂移 |
| [JaisingZ/codex-thread-orchestrator](https://github.com/JaisingZ/codex-thread-orchestrator) | 类似 Skill | 有 `TASK_BOARD.md`、`HANDOFF.md`、`INTEGRATION_REPORT.md` 等固定产物 | 同样只有 2 个 commit、0 star，隔离和恢复细节不如前者 |
| [mlabo-org/codex-task-control-plane](https://github.com/mlabo-org/codex-task-control-plane) | 插件 + JS + 测试 + schema + dashboard | 有持久 ledger、测试、MIT 许可证、控制面板，工程化程度高 | 已于 2026-10-06 归档，说明这个方向本身不稳定 |
| [code-yeongyu/oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) 的 TeamMode | 大型 Agent Harness 的一个模式 | 约 7 万 star，已适配 native MultiAgentV2，并把 Codex App threads 作为 fallback | 体量大、依赖多，不是轻量 Skill，接入成本高 |
| Codex 原生 subagent | 官方能力 | 简单、默认可用、App 中可见 | 不是用户独立管理的长期任务线程 |

`mlabo` 这个归档项目尤其值得注意：它说明“把 thread 编排做可靠”需要 schema、ledger、测试、dashboard 和生命周期管理，而不是一份 Markdown 提示词。

---

## 九、后续迭代方向

下面按优先级给出建议。P0 是“未修复前不建议实际跑团队模式”的问题，P1 是可靠性和可审计性，P2 是产品化，P3 是生态与治理。

### P0：先修执行正确性

#### 1. 正确解析 projectId

- 调用 `list_projects`；
- 匹配仓库根目录；
- 找不到就停止；
- 禁止把文件路径直接作为 projectId；
- 状态文件中记录真实 projectId 和 hostId。

验收标准：

```text
create_thread 不再出现 Worktree init failed
worker 的 projectId 能在 list_projects 中找到
```

#### 2. 显式创建 worker 分支

- 在 `create_thread` 前或 worker 启动后创建唯一分支；
- 优先使用：

```text
tt/<run-id>/<worker-role>
```

- 如果使用 managed worktree，必须执行：

```bash
git switch -c <worker-branch>
```

- 启动后验证：

```bash
git symbolic-ref --short HEAD
git rev-parse HEAD
git status --short
```

验收标准：

```text
每个 worker 的 HEAD 指向自己的 branch，而不是 detached HEAD
leader 能通过 branch 名称找到 worker 的提交
```

#### 3. 处理 pending / client thread

- 创建后调用 `wait_threads`；
- 如果返回 pendingWorktreeId，记录并等待；
- 通过 `list_threads` 或状态查询解析成 threadId；
- 失败时标记为 `failed`，不要进入正常 dispatch；
- 支持取消和清理失败的 pending。

验收标准：

```text
没有 threadId 的 worker 不会收到 dispatch
失败 worktree 有可查询状态和清理路径
```

#### 4. 验证 worker cwd

- 从线程元数据读取 cwd；
- 或让 worker 回报 `pwd`；
- leader 验证该路径是独立 worktree；
- 把真实路径写入 state 和 dispatch。

验收标准：

```text
dispatch 中的 working directory 与 worker 实际 cwd 一致
```

### P1：提升可靠性和可审计性

#### 5. 归档 worker 报告

在移除 worktree 前复制：

```text
<worker-worktree>/.thread-team/report.md
<worker-worktree>/.thread-team/notes.md
```

到：

```text
.thread-team/<leader-id>/workers/<role>/
```

验收标准：

```text
worktree 清理后仍能查看每个 worker 的完整报告
```

#### 6. 持久化 leader state

- 不要把唯一状态放在 Codex-managed worktree；
- 优先放到本地 checkout 或 `$CODEX_HOME`；
- 使用原子写入，避免中断产生半截文件；
- 状态文件使用 JSON/YAML 时增加 schema 校验。

#### 7. run 级命名空间

- 分支名、worktree 路径、报告目录都带 run ID；
- 启动时检查同名分支和 worktree；
- 支持清理历史 run。

#### 8. 完整 preflight

至少检查：

```bash
git rev-parse --is-inside-work-tree
git status --porcelain
git branch --show-current
git worktree list
```

并检查线程工具、模型、sandbox、approval 和 automation 能力。

#### 9. 成本和并发上限

- 默认最多 2–3 个 worker；
- 记录 token/成本预算；
- 超过预算时停止新增 worker；
- 给每个 worker 设置最大运行时间；
- heartbeat 周期可配置；
- 优先使用 `wait_threads` 而不是固定两分钟轮询。

#### 10. 事件驱动替代 busy-wait

建议的等待顺序：

```text
wait_threads
→ 处理完成/失败事件
→ 仅在需要团队级决策时使用 heartbeat
```

### P2：从 Prompt 走向可执行控制器

#### 11. 提供真正的状态机

建议用一个轻量控制器维护：

```text
run_id
leader_thread_id
worker_id
thread_id
host_id
project_id
worktree_path
branch
base_sha
status
last_heartbeat
report_path
merge_status
```

Markdown 可以继续作为人类可读视图，但机器状态应由代码维护。

#### 12. 增加 dry-run

dry-run 只输出：

- 拆分方案；
- worker 数量；
- 分支名；
- worktree 路径；
- 预计验证命令；
- 预计合并顺序。

不实际创建线程、不修改仓库。

#### 13. 增加 smoke test

用一个临时仓库验证：

- 两个 worker；
- 两个互不重叠的文件；
- 每个 worker 创建分支；
- 每个 worker 写报告；
- leader 串行 merge；
- 最终测试通过；
- worktree 清理；
- 报告归档。

#### 14. 增加结构化日志

记录每次：

- worker 创建；
- dispatch；
- 消息；
- heartbeat；
- report；
- merge；
- verification；
- cleanup。

日志要能按 run ID 和 thread ID 检索。

### P3：产品化与生态

#### 15. 权限和安全边界

- 为 worker 设置独立 sandbox/approval policy；
- 明确哪些 worker 能读 secrets；
- 禁止 worker 未经授权 push/merge；
- 对跨线程消息增加更强的身份校验；
- 把 `From:` 从“约定”升级为“可验证元数据”。

#### 16. 多仓库支持

如果要用于微服务或多仓库项目，需要增加：

- repo 级 worker；
- 跨 repo 契约；
- 依赖图；
- 分仓库验证；
- 统一 run 状态。

#### 17. PR/CI 集成

当前设计是本地 merge，不负责远端 PR。后续可以增加：

- 每个 worker 自动创建 PR；
- leader 汇总 PR；
- 等待 CI；
- 读取 CI 失败日志；
- 按依赖顺序合并。

#### 18. 可视化管理界面

可以借鉴 `codex-task-control-plane` 的 dashboard：

- run 列表；
- worker 状态；
- 当前分支/worktree；
- 最后 heartbeat；
- merge 进度；
- 报告查看；
- 失败清理。

#### 19. 许可证和发布

- 增加明确 LICENSE；
- 提供版本 tag/release；
- 使用 CHANGELOG；
- 固定安装版本；
- 给二次开发提供兼容性矩阵。

---

## 十、试用与落地建议

### 10.1 适合使用的情况

- 本地 Codex Desktop；
- 已保存的 Git 项目；
- worktree 功能正常；
- 当前会话确实暴露 `create_thread` 和 `send_message_to_thread`；
- 任务可拆成 2–3 个文件几乎不重叠的独立工作块；
- 你愿意监督分支、审查 diff 和合并；
- 你接受它是实验性工具，而不是生产调度器。

### 10.2 不适合使用的情况

- Remote SSH、Docker、code-mode 等 effectful thread 工具不稳定的环境；
- 任务很小，只有一两个子任务；
- 多个 worker 必须修改同一个核心模块；
- 需要跨多个仓库协调；
- 需要严格权限隔离、审计和可恢复性；
- 需要直接在公司仓库中长期使用。

### 10.3 最小验证方案

建议先在一个临时仓库做 2 个 worker 的 smoke test：

1. worker A 修改独立文件 A；
2. worker B 修改独立文件 B；
3. 检查 `git worktree list` 是否有两个独立目录；
4. 检查每个 worker 是否真的创建了分支；
5. 检查 `.thread-team/report.md` 是否存在；
6. 检查 leader 是否能读取报告；
7. 检查串行 merge 和验证；
8. 检查 worktree 清理后报告是否仍保留。

不要在第一次就使用 3 个以上 worker，也不要在公司核心仓库上首跑。

### 10.4 如果只是试用

固定 commit：

```bash
git clone https://github.com/itmada/codex-thread-team.git
cd codex-thread-team
git checkout eb5a220ec7a13885d7dbd3b0ecd15959a7f33e9f
```

安装到：

```text
${CODEX_HOME:-$HOME/.codex}/skills/thread-team
```

不要直接跟随 `main` 自动更新；先固定版本，确认行为后再决定是否升级。

---

## 十一、最终判断

`itmada/codex-thread-team` 是一个“设计大于实现”的项目。

值得学习的地方：

- worktree 隔离；
- 自包含 dispatch；
- 文件承载上下文；
- 状态文件和恢复；
- 串行合并和逐次验证；
- 卡死替换和 abort 协议。

不能直接依赖的地方：

- 没有真正的执行引擎；
- 没有测试和 CI；
- 没有许可证；
- 对当前 Codex 异步线程和 worktree 行为的假设过强；
- 分支创建、projectId、pending thread、worktree path 这几处存在实际阻断问题；
- 社区验证很少。

**推荐定位：**

> 作为“多 agent 协作协议”的参考实现和 Prompt 模板库，适合阅读、借鉴和小范围试验；如果要用于真实生产，应先补齐执行正确性、状态持久化、报告归档、权限控制和自动化测试。

---

## 附录 A：主要调研依据

### 一手来源

- [GitHub 仓库](https://github.com/itmada/codex-thread-team)
- [OpenAI Codex Worktrees 官方文档](https://learn.chatgpt.com/docs/environments/git-worktrees)
- [OpenAI Subagents 官方文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [OpenAI Codex issue #26861](https://github.com/openai/codex/issues/26861)
- [OpenAI Codex issue #28204](https://github.com/openai/codex/issues/28204)
- [OpenAI Codex issue #40852](https://github.com/openai/codex/issues/40852)
- [OpenAI Codex issue #40865](https://github.com/openai/codex/issues/40865)

### 二手来源

- [作者掘金介绍文章](https://juejin.cn/post/7648912168421130267)
- [CSDN 转载/推广文章](https://blog.csdn.net/weixin_35750953/article/details/167123167)

### 对比项目

- [JaisingZ/codex-thread-orchestrator](https://github.com/JaisingZ/codex-thread-orchestrator)
- [mlabo-org/codex-task-control-plane](https://github.com/mlabo-org/codex-task-control-plane)
- [code-yeongyu/oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)

---

## 附录 B：调研限制

1. 本次为静态代码/文档审查，没有实际创建 worker 线程。
2. 不同 Codex App 版本、账号、操作系统和 Remote SSH 环境暴露的工具可能不同。
3. Star/Fork/Issue 数据为 2026-11-12 的 GitHub 页面/API 快照。
4. 项目没有正式 release，本文所说的“新功能”指仓库当前呈现的机制，不对应某个正式版本。
5. 未发现独立 benchmark，因此无法量化其相对单线程或原生 subagent 的实际加速比。

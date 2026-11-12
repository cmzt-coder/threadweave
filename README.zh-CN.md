# Thread Team

[English](README.md) | 简体中文

**把一个 Codex 会话,跑成一支小型工程团队。**

Thread Team 是一个 Codex skill:当前线程担任 **leader**——负责规划、拆分、派发——并创建真实的 Codex **worker 线程**,每个 worker 独占一个任务边界、一条分支和一个专属 git worktree。leader 轮询进度、解决阻塞、逐条分支审查合并,最后做一次整体集成复查,再把结果交还给你。

这个 skill 只可能存在于 Codex,因为只有 Codex 把线程当作一等公民:`create_thread` 能创建带独立模型配置的真实会话,线程之间还能通过 `send_message_to_thread` 互发消息。所以 worker 是真实线程,而不是用完即弃的 subagent——它们保有自己的上下文、按自己的配置运行(默认 `gpt-5.4` + `high` thinking,可覆盖)、可以中途沟通,卡死时还能归档换人。skill 拒绝用 `spawn_agent` 模拟这套流程:线程工具不可用时,它会指出缺哪个能力,然后停下。

## 针对并行 agent 的真实翻车方式而设计

这个 skill 的主体不是"怎么开线程",而是护栏——每条对应一种失败模式。

**共享 checkout 会毁掉一切。** 两个线程共用一个工作目录,会互相覆盖工作树、悄悄把改动混进彼此的分支——这是 thread team 最常见的死法。每个 worker 都拿到一个专属 git worktree,尽可能在创建线程时就建好。如果无法保证目录独占,skill 干脆不会进入团队模式。

**leader 的上下文是全队最稀缺的资源。** 每次 `read_thread` 的转录、每段贴进消息的 diff,都会在 leader 上下文里常驻到运行结束。所以跨线程消息只携带信号——状态行、文件路径、commit SHA;大块内容走文件:每个 worker 在自己 worktree 里用 `.thread-team/notes.md` 记进度,把完整完成报告写进 `.thread-team/report.md`。`read_thread` 是昂贵的兜底手段,只用于启动确认、疑似卡死和消息含糊的场合,绝不做例行巡查。

**长任务比会话活得久。** leader 上下文随时可能被压缩或中断,只活在对话里的状态会跟着一起死。花名册、worktree、派发、决策、换人、合并、heartbeat——每次变更后立刻记入 `.thread-team/<leader-thread-id>/state.md`;任何中断之后,leader 先重读状态文件再行动。这个目录被 git 排除、永不提交,运行结束后留作审计记录。

**忙等会把 leader 活活耗死。** leader 不做轮询死循环,而是排一个两分钟的 heartbeat、结束回合,被唤醒时再处理 worker 消息。工作流创建的每个 heartbeat 都必须在结束前删除——泄漏的 heartbeat 比任务活得久,会永远唤醒这个线程。

**worker 会卡死。** 一直没启动的 worker 会被归档换人——新身份、新分支、新 worktree,派发原文不变,并向全队广播换人。任务中途沉默的 worker(大约三个 heartbeat 周期没动静、不在长命令里、也不在等同伴)会先收到点名,然后 leader 三选一:重新派发、换人、或把活收回自己做。有实际进展的 worker 永远不会被换掉。

**一把梭的合并让缺陷无从归因。** 集成是串行的:读 worker 的报告,拿分支 diff 对照它的派发审查(先看规格符合度,再看代码质量),真正需要返工的发回原 worker,然后合并、跑构建和测试——之后才碰下一条分支。全部合并后,leader 再对合并整体做跨 worker 复查:契约不匹配、重复逻辑、只有拼起来才会坏的接缝。

**含糊的派发只能换来含糊的产出。** worker 掌握的上下文可能比 leader 少,所以每份派发都自包含:精确范围、工作目录和基线 commit、哪些文件归你哪些不归、验收标准、验证命令、必须保持的不变量、报告格式——外加带线程 ID 的完整花名册,让每条跨线程消息都能核验身份(`From: <role> (thread ID: <id>)`)。

## 一次运行长什么样

leader 严格按六个阶段执行,每个阶段都有明确的退出条件:

1. **分析与拆分** —— 读代码、做可行性评估、拆任务、征得你的确认。worker 模型配置在创建任何东西*之前*就会讲明。状态文件在这里写下。
2. **worker 初始化** —— 为每个 worker 创建专属分支和 worktree,发送注册消息:只讲身份和团队规则,不含任务内容。
3. **设计与派发** —— 为每个 worker 设计完整执行方案,再发正式派发。一次一个,先设计后派发。
4. **轮询与决策** —— 确认每个 worker 真的启动了,然后切到 heartbeat 唤醒模式:处理阻塞、拍团队级决策、把决定同步给受影响的 worker、处理卡死和换人。
5. **审查与串行合并** —— 逐分支对照派发审查,返工发回原 worker,一次只合并并验证一条分支。
6. **最终复查与清理** —— 对合并整体做集成级复查,直接修复,然后确认收尾:heartbeat 删净、leader 建的 worktree 移除、状态文件标记完成。

每个 worker 走同样的三步:执行、自查并验证、提交并报告;它只能和派发里点名的同伴直接沟通,其余一切经由 leader。worker 从不擅自修改共享契约,也从不擅自合入 leader 分支。

随时问状态,你会得到花名册、各 worker 状态、阻塞、合并进度和 leader 的下一步。随时取消,团队会被有序拆除——worker 把成形的半成品提交到各自分支、heartbeat 删除、线程归档、未合并分支上报、状态文件标记 `aborted`。

## 什么时候有用——什么时候没用

触发 skill 并不等于创建 worker。leader 会先判断团队模式是否真的胜过单线程,不划算时它会直说。

团队模式赢在:多块独立工作能低冲突并行、所有权和验收标准清晰、省下的墙钟时间大于搭建、轮询、审查、合并的开销。worker 数量由分析决定:一块真正独立的工作配一个 worker,一个不多。

单线程赢在:拆完只有一两个小子任务、紧耦合的核心重构靠连续上下文比并行吞吐更值、或者子任务共享同一批文件和同一套心智模型。

skill 自带的校准例子:**好的拆分**——一个 worker 写 API 端点,一个写消费它的前端页面,一个写迁移和种子数据,文件几乎不重叠,唯一的共享契约(API schema)由 leader 提前锁死。**坏的拆分**——三个 worker 重构同一个核心模块,合并和审查成本吃光并行收益。

## 安装

克隆到 Codex skills 目录,命名为 `thread-team`:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone https://github.com/itmada/codex-thread-team.git "${CODEX_HOME:-$HOME/.codex}/skills/thread-team"
```

更新:进入该目录执行 `git pull`。

要求 Codex 环境提供线程工具——`create_thread`、`send_message_to_thread`、`read_thread` 是硬性要求;`list_threads`、`set_thread_archived` 和 heartbeat automation 补全完整工作流——外加支持 worktree 的 git。

## 使用

直接点名:

> Use $thread-team to split this task across a leader thread and collaborating worker threads.

提到 thread team、并行 Codex 线程、worker 线程或 leader/worker 分支协作都会触发它。当任务看起来足够大、可并行、可分支隔离时,Codex 也可能主动提议这个模式——但未经你确认,它绝不创建 worker。

它擅长的任务形状:

> Use $thread-team to implement the new billing settings page. Split backend API work, frontend UI work, and tests across workers.

> Use $thread-team to parallelize this migration: one worker handles schema changes, one updates the data access layer, and one updates tests/docs.

预期开头有一次确认:认可拆分方案和 worker 配置(`gpt-5.4`、thinking `high`,除非你另有要求)。之后运行自主推进;只有需要你拍板的阻塞才会找你,而你随时可以查状态或中止。

## 目录结构

```
thread-team/
├── SKILL.md                             # leader 工作流(Codex 读的就是它)
├── agents/
│   └── openai.yaml                      # 展示名与默认 prompt
└── references/                          # 按阶段加载;面向 worker 的模板原文发送
    ├── worker-registration-template.md  # 身份 + 团队规则,先于任务派发
    ├── worker-dispatch-template.md      # 正式、自包含的任务派发
    ├── worker-report-template.md        # 结构化完成报告
    ├── leader-state-template.md         # 抗中断的运行日志
    └── final-report-template.md         # leader 面向用户的收尾报告
```

运行时还会在仓库根留下 `.thread-team/`(git 排除):leader 的状态日志,加上每个 worker 的 `notes.md` 和 `report.md`——正是这份书面记录让恢复、换人和审计成为可能。

## 修改这个 skill

`SKILL.md` 只放 leader 运行时真正需要的内容;可复用的 prompt 和报告格式放在 `references/`,按阶段加载。面向 worker 的模板会被原文发送,所以把它们当契约改,而不是当散文改。改模板尽量用小 diff 而非推倒重写,保住可审查的历史;也永远别让改出来的版本用临时 subagent 冒充 worker。

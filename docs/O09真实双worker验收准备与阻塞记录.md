# O09 真实双 worker 验收准备与阻塞记录

日期：2026-11-16。Run：`20261116-o09-real-01`。

## 当前状态

用户已确认 A、B 两个真实验收 worker，均为 `deepseek-flash · high`，禁止自动回退；结束后归档聊天并清理专用 worktree，只本地提交、不推送。

测试仓库 `/Users/luzhentian/lzt/codex/luzhentian/threadweave-validation-lab`，基线 `bb8a6162a08ba2929d27811098b22e9ac4062ed5`，main 工作区干净且无远程。

宿主 `list_projects` 未返回 canonical root 精确匹配的 Git 项目，故无法通过项目解析门禁。未调用 create_thread，未创建 worker/worktree，也未派发任务。电脑控制工具对 Codex 返回 `Computer Use is not allowed to use the app 'com.openai.codex' for safety reasons.`，没有可用的项目登记 API；未尝试修改宿主配置或绕过该限制。

用户需将上述已有文件夹添加为 Codex 本地项目。登记后 leader 重新调用 list_projects，仅在 canonical root、host=local、isGitRepository=true 唯一匹配时继续。已确认模型授权保持，不重复询问。

证据：`.threadweave/20261116-o09-real-01/projects-tool-result.json` 和 `preflight.json`。

## 已准备的执行边界

1. A、B 从同一基线创建独立 worktree，各自分支 `codex/o09-real-a`、`codex/o09-real-b`；启动时直接核验 cwd、common dir、branch、HEAD、clean。两者都 ready 后才开始正式任务。
2. A 仅新增 `artifacts/task-client.json` 和生产验证脚本；从现有 API schema 生成制品，不更改共享契约或 B 的文件。自检、提交、报告后停止写入。
3. leader 独立验证 A 的实际 commit 和文件范围；使用 archive_handoff 绑定实际 SHA、任务修订、生产者 a、接收者 b 和用途 task-client。制品归档到主项目 durable run 目录，不放在 A worktree。
4. B 的消费任务等待 A 的归档核验通过才派发。B 仅新增消费者脚本与消费结果；先调用 verify_handoff 校验精确 manifest 和归档，不从 A checkout 读取原始文件。
5. 验证 B 正常消费；对隔离副本注入同长度内容篡改，消费者必须非零退出且不产生成功结果；不破坏原归档。
6. 收齐报告并直接执行独立验收，绑定各自实际 SHA；仅由 leader 按 A→B 串行集成到独立验收分支，每次合入后运行相关测试。
7. 确认所有 worker completed/idle、报告已外置、分支成果已保全、工作树 clean 后归档聊天并非强制清理专用 worktree。主 checkout、共享资源及未合并成果均不删除。

## 计划与证据要求

- Phase 1 Deep task analysis and parallel split：任务和用户配置已确认；等待项目登记。
- Phase 2 Worker thread initialization：未开始。
- Phase 3 Per-worker deep task design and dispatch：未开始。
- Phase 4 Progress polling and collaboration decisions：未开始。
- Phase 5 Report collection and merge orchestration：未开始。
- Phase 6 Final leader review and repair：未开始。

当前工具无 update_plan，阶段记录使用文件；不虚构工具调用。不创建轮询 automation，使用有界 wait_threads。

保存原始创建/读取/发送/等待响应、启动 Git 观察、worker 模型观察（不可见时记 unknown）、各 commit SHA、报告哈希、manifest、独立验收、集成结果和清理证据。create 仅受理或发消息成功均不算 ready/completed。

O09 保持部分完成：此前 28 项综合验收和本地进程证据不变，本轮真实 worker 验收尚未发生。H01、O16 实际删除及其他待办不在本轮范围。

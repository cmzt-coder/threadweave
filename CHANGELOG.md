# Changelog

## Unreleased（2026-11-17 本地基线）

本段汇总尚未推送的本地开发成果。真实使用前请同时阅读 README、生产力插件评估和 O16 外部待办冻结记录。

### Added

- O16 资源来源只读映射与真实 `attachmentType` / `archived_worktree` payload 适配。
- Codex thread cwd 安全修复工具：默认 dry-run，应用前备份 SQLite，并校验附件数量不变。
- O16 宿主归档完成观察：`queued → archived_worktree → 目录/Git 注册消失`。
- 修复前后同一 Codex thread 的本地附件连续性验证。
- 本地归档授权信封：绑定 approved plan、停止回执、归档前附件、archive request 和授权窗口。
- 授权信封接入 `host_archive_guard` 只读完成验证。
- 显式 opt-in `execute_host_guarded_cleanup`：授权信封完整通过后才调用宿主归档。
- O16 宿主保护负向矩阵、最终完成度评估和外部待办冻结记录。
- Threadweave 生产力插件定位评估。

### Fixed

- 修复 O16 归档完成观察早期过度声明调用者连续性、批准和宿主保护执行的问题。
- 修复 Codex thread cwd 与附件状态冲突导致的 primary checkout 归档失败路径。
- 保留默认 `execute_managed_cleanup` 的严格门禁，避免静默扩大删除授权。

### Verified

- 本地回归：46 套 smoke 测试 / 1466 项断言通过。
- 真实宿主正向归档：专用 managed worktree 已从 active `worktree` 变为 `archived_worktree`，目录和 Git worktree 注册均消失。
- primary checkout 真实拒绝、本地 thread 连续性、授权信封和 guarded opt-in 执行路径均有记录。
- 测试 worktree 已不存在，主工作区干净。

### Known limitations

- O16 外部证据已冻结：宿主签名身份、当前版本 owner mismatch、shared/protected/submodule/embedded Git 负例和预归档授权信封下的真实端到端验收仍未完成。
- H01 的真实模型/附件保持、P0 的多 projectId/create timeout/wait 缺失仍需受控宿主验收。
- 当前主要是 Codex skill + Python helper suite，不是一键安装的通用桌面插件。
- 默认清理路径保持严格，guarded 路径需要显式授权。

### Next

- 将本地 `codex/r4-six-workers` 分支推送至远端（需用户当次明确授权）。
- 在真实但可回滚的 Git 项目中进行首次实际使用。
- 外部宿主能力具备后再恢复 O16/H01/P0 的对应验收。

## 0.1.0（2026-11-12，本地版本）

首个本地可用版本，尚未发布到远端。

### Added

- Threadweave 品牌、`threadweave` skill 与 `.threadweave/` 运行时目录。
- 第一轮执行闭环：preflight、worker 启动门禁、消息往返、串行集成、报告与清理。
- 第二轮可靠性：结构化状态 schema、原子写入与文件锁、durable 位置与恢复扫描、恢复分类、归档 manifest、幂等清理分类。
- 第三轮工程保障：本地辅助 CLI、`HostAdapter` 接口与最小控制器骨架、JSONL 运行日志、预算/停滞/重试检查。
- 第四轮兼容：`codex-cli 0.165.0` 宿主兼容矩阵与 `tests/run_all.py`。

### Verified

- 真实双 worker 正向冒烟：创建、初始化、消息、提交、串行集成、报告、清理。
- 部分故障路径：不可用模型、无效 reasoning、零 projectId、取消、停滞、替换、managed worktree 生命周期。
- 当时 `tests/run_all.py` 汇总 12 套 smoke 测试、181 项断言通过。

### License

MIT License. See [LICENSE](LICENSE).

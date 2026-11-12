# Changelog

## 0.1.0 (2026-11-12, unreleased)

首个本地可用版本，尚未发布到远端。

### Added

- Threadweave 品牌、`threadweave` skill 与 `.threadweave/` 运行时目录。
- 第一轮执行闭环：preflight、worker 启动门禁、消息往返、串行集成、报告与清理。
- 第二轮可靠性：结构化状态 schema、原子写入与文件锁、durable 位置与恢复扫描、
  恢复分类、归档 manifest、幂等清理分类。
- 第三轮工程保障：本地辅助 CLI、`HostAdapter` 接口与最小控制器骨架、
  JSONL 运行日志、预算/停滞/重试检查。
- 第四轮兼容：`codex-cli 0.165.0` 宿主兼容矩阵与 `tests/run_all.py`。

### Verified

- 真实双 worker 正向冒烟：创建、初始化、消息、提交、串行集成、报告、清理。
- 部分故障路径：不可用模型、无效 reasoning、零 projectId、取消、停滞、替换、
  managed worktree 生命周期。
- `tests/run_all.py` 汇总 12 套 smoke 测试，181 项断言通过。

### Known limitations

- 未验证：多 projectId 匹配、create 超时、隐藏可选工具环境、真实错误启动报告注入、
  完整真实恢复流程。
- 原项目压缩包与新项目均无 `LICENSE` / `COPYING` / `NOTICE`，授权不明确。
- 尚未配置 remote、CI、版本 tag 或发布 URL。

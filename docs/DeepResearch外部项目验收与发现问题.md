# DeepResearch 外部项目验收与发现问题

日期：2026-11-16。Run：`20261116-backlog-sequential-01`。

## 授权与隔离

用户提供 DeepResearch 源码目录用于已有业务项目验收。实际路径为 `/Users/luzhentian/lzt/project/DeepResearch-master`（输入的 Izt 为路径拼写差异），原工作区 Git 干净，分支 `bounded`，HEAD `e4792fe5c2a69a9bfdd5a6fd55c260ec14193686`。

读取并遵循该项目 AGENTS.md。在 `/private/tmp/threadweave-deepresearch-20261116` 创建专用 worktree 和 `codex/threadweave-acceptance-20261116` 分支。没有切换源分支、复制 .env、修改认证、启动应用服务或调用研究模型/数据库/搜索服务。依赖仅安装到临时 worktree 的独立 .venv。

## O06a：真实依赖准备

通过 Threadweave `load_setup_config` / `run_setup_command` 运行显式 argv，超时 180 秒，失败策略 block。

| 步骤 | 结果 | 结论 |
| --- | --- | --- |
| `uv sync --locked --offline --extra dev --no-install-project` | 拒绝：uv.lock 需要更新 | 已有项目依赖清单与锁文件不一致，正确阻断 |
| 临时 worktree `uv lock --offline` | passed | 单一所有者生成真实锁文件修订，保留补丁 |
| 更新锁后离线 sync | 拒绝：Ruff 缓存缺失 | 不把缓存缺包冒充安装成功 |
| 更新锁后联网 locked sync | passed | 按锁文件真实安装，未接入业务服务 |
| 重复离线 locked sync | passed | 幂等准备可重复执行 |
| 选定项目测试 | 31 passed | 分支引擎及深度配置离线行为通过 |
| 全套测试，仅 dev extra | 收集失败：缺少 pptx | 导出测试需要 exports extra |
| locked sync，dev + exports | passed | 只补可选 Python 依赖，未安装浏览器运行时 |
| 全套项目离线测试重试 | 255 passed，1 warning | 项目离线测试通过 |

随后为补齐 Controller 准备后 Git 门禁，新建第二个临时 worktree，将锁修订与显式 setup 配置提交到独立分支 `codex/threadweave-setup-verified-20261116`，得到隔离验收基线 `9e1adac4c04978827e7e62e5f1e2ddde007ba4bb`。该基线仅用于验收，未合入原 bounded。

真实 `Controller.run_worker_setup` 使用该基线进行离线 dev + exports 安装，返回 passed，root/common-dir/branch/HEAD 全部吻合、Git statusPorcelain 为空；再次调用返回 reused。Controller 状态外置，不污染被验收的 worktree。没有为此创建或模拟宿主 worker。第二个 checkout 也已清理，独立验收分支和提交保留。

O06a 的真实业务依赖准备、失败阻断、成功安装及准备后 Git 门禁验收完成；这不是线上部署或研究质量验收。

## O07：真实资源组

实际资源组为 `pyproject.toml` + `uv.lock`：

1. 两个声明分别写 pyproject 和 lock，即使文件不重叠也因同一串行资源组被 `check_reservations` 拒绝。
2. 同一所有者持有两个文件的声明通过。
3. 由该单一验收执行者实际运行 uv lock，Git 实际变更仅 uv.lock，`check_changes` 通过。
4. 后续 locked sync 通过，锁修订与契约内容分别校验。

本轮使用真实项目与实际依赖锁生成，没有创建宿主 worker，不将纯声明检查称为真实多 worker 调度。此前资源组与派发协议测试继续作为互补证据。

## O08：真实契约

冻结 `app/schemas/research.py`、`app/schemas/depth.py` 的精确 SHA256。未改时通过；仅在临时 worktree 向 research.py 添加受控注释，漂移检测拒绝；逐字节恢复后通过。依赖安装和项目测试后再次确认契约无漂移。

该结果验证既有 Pydantic 契约文件的冻结/检测适用性；不宣称 schema 语义向后兼容或线上 API 变更通过。

## 发现问题与后续建议

- **DR-01：现有 pyproject 与 uv.lock 不一致。** 原目录仍保留该问题。建议由 DeepResearch 维护任务审阅本轮归档的锁修订补丁，确认 exports 依赖变更后单独提交；本轮没有将修订合入业务分支。
- **DR-02：完整测试需要 dev + exports。** 仅 dev 安装时存在测试收集失败。建议在该项目开发指南/CI 中明确两个 extra；不因此把 exports 改成所有用户的必需运行依赖。
- **DR-03：1 条 Starlette/httpx 弃用警告。** 本次不影响 255 项测试结果；应由业务项目维护任务单独评估依赖升级。

## 保全与清理

`.threadweave/20261116-backlog-sequential-01/` 保存每次真实 setup 结果、失败输出、资源声明结果、冻结哈希、`business-lock-update.patch`、生成的 uv.lock 以及最终检查。业务源码/锁补丁未加入 Threadweave Git 提交。

清理前确认唯一 tracked 变更为 uv.lock，并先外置保全；恢复该临时变更后移除本轮生成的 .venv、测试缓存，再非强制移除 worktree。专用分支保留。原目录分支、HEAD、uv.lock 和 Git 干净状态未变化。没有推送。

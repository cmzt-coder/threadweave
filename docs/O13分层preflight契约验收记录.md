# O13 分层 preflight 契约验收记录

日期：2026-11-16。证据 Run：`20261116-o13-preflight-01`。

## 结论

O13 已完成分层 preflight 契约与失效判断：

- run 级输入变化使全部 worker 证据失效。
- 单个 worker 的 ownedPaths、branch、base 等输入变化只使该 worker 失效。
- 观测时间单独校验，不把每次观测时间变化误判为契约内容变化。
- 旧版本/旧格式 preflight 明确返回 `unsupported_legacy`，不冒充已验证。
- Controller 可持久化 contract，startup_gate 可校验 preflightId 绑定。

## 契约结构

`state.preflight` 包含：

- `version`
- `runId`
- `preflightId`
- `contractHash`
- `createdAt`
- `run.inputHash` / `run.inputs`
- `workers[role#attempt]`：role、attempt、inputHash、inputs

`contract inputs` 只放稳定身份、范围和环境契约字段；证据时间不参与 contractHash。

## 失效规则

| 变化 | 结果 |
| --- | --- |
| run 级输入变化 | 全部 worker invalid |
| 单个 worker ownedPaths 变化 | 仅该 worker invalid |
| 当前 worker 缺失 | 对应 worker invalid |
| 新 worker 未登记 | `worker_not_in_contract` |
| 证据缺失 | invalid |
| 证据过期/来自未来 | invalid |
| 旧格式 | `unsupported_legacy` |

## 实际证据

`20261116-o13-preflight-01`：

- contractHash：`829e3c6f735bf67d0903f6296efb0840de737c66fedb6d4824ac885d10f46911`
- preflightId：`pf_829e3c6f735bf67d`
- 原始契约：`valid`
- run 变更：`invalidated`，全部 workers 失效
- worker 变更加入 `src/shared/**`：仅 `a#1` 失效，`b#1` 保持有效
- 无宿主 API 调用

## 测试与回归

- 新增 `tests/preflight_contract_smoke.py`：20 项。
- 全套回归：31 套、1128 项断言通过。
- 覆盖 hash 稳定性、run/worker 失效范围、缺失 worker、新 worker、证据新鲜度、旧格式、schema、startup gate 和 Controller 持久化。
- 未推送。

## 边界

- 当前契约 inputs 的字段由 leader 显式提供，不从任意宿主响应自动推导。
- 证据新鲜度不替代实际环境复核；内容 hash 相同不代表运行环境实时健康。
- O06 setup 字段未来接入时可作为 worker inputs 扩展，不需要改变 run/worker 的失效分区。

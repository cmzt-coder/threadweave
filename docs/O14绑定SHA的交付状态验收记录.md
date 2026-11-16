# O14 绑定 SHA 的交付状态验收记录

日期：2026-11-16。证据 Run：`20261116-o14-delivery-01`。

## 结论

O14 已完成：交付状态统一为 `unverified`、`verified`、`landed`、`pushVerified` 四态，所有成功态绑定实际 SHA 和时间，不再使用无 SHA 的字符串代表当前交付结果。

默认交付停在本地 `landed`。`pushVerified` 需要当次明确推送授权、已存在的本地 landed 记录和实际观察到的远端 ref SHA；系统不会自动执行推送。

## 数据结构

`state.delivery` 包含三段追加式记录：

- `verified`
  - worker 或 integration 阶段
  - role / attempt / taskRevision
  - 验收 SHA
  - reportSha、planHash
  - verifiedAt
- `landed`
  - branch、集成后 SHA、beforeSha
  - taskRevision、集成 planHash
  - verification、landedAt
- `pushVerified`
  - local SHA、remote、ref、remoteSha
  - observedAt
  - authorizationEvidence
  - recordedAt

旧记录只用于审计。`delivery_status()` 仅把 SHA 等于当前 integration HEAD 的记录视为当前交付状态；新提交会让旧 `verified`、`landed`、`pushVerified` 进入 `stale`，当前状态回到 `unverified`。

## 触发规则

| 事件 | 记录 |
| --- | --- |
| worker 独立验收通过 | `verified(stage=worker)` |
| 串行合入并完成独立集成验收 | `verified(stage=integration)` + `landed` |
| 用户明确要求推送且远端观察 SHA 匹配 | `pushVerified` |
| 没有推送 | 当前交付最多停在 `landed` |
| 当前 HEAD 前进 | 旧记录保留审计，但不能代表当前 HEAD |

`pushVerified` 写入前检查：

- 当前 SHA 必须是 integration currentSha。
- 必须存在匹配 SHA 的 `landed` 记录。
- remote 必须已配置。
- remoteSha 必须等于 local SHA。
- ref 必须是合法的 `refs/heads/*` 或 `refs/tags/*`。
- authorizationEvidence 必须非空。
- 不执行 Git 推送，只记录调用方提供的远端实际观察。

## Controller 接线

- `verify_worker` 成功后追加 worker `verified`。
- `integrate` 成功后追加 integration `verified` 和 `landed`。
- `record_push_verification` 只保存远端观察，不推送、不改本地 HEAD。
- `delivery_status` 可读取当前交付阶段并列出旧 SHA 记录。
- CLI `inspect` 输出每个 run 的 `currentSha` 和 `deliveryStage`。

## 本地验证

新增 `tests/delivery_smoke.py`：24 项。

覆盖：

- 初始 `unverified`。
- worker 和 integration verified 去重。
- landed 默认状态。
- 未 landed 时 pushVerified 拒绝。
- remoteSha 不匹配拒绝。
- 非法 ref、缺失授权证据拒绝。
- pushVerified 成功记录。
- 新 current SHA 使旧记录全部 stale。
- schema 接受合法 delivery，拒绝缺字段记录。

Controller 回归额外覆盖：

- 验收到集成后 delivery 记录正确。
- landed 阶段正确。
- 错误 remote SHA 和缺失 remote 拒绝。
- pushVerified 只记录观察结果。
- deliver new-head 回到 unverified。

全套回归：29 套、1073 项断言通过。未推送。

## 模板更新

- `references/leader-state-template.md` 增加 Delivery status 段。
- `references/final-report-template.md` 增加 Verified、Landed、Push verified、当前阶段和陈旧记录。
- 最终报告默认停在 `landed`；没有实际远端观察时必须写未请求或未观察。

## 边界

- `pushVerified` 不是远端服务端签名，只是本地持久化的远端 ref 观察记录。
- 本轮没有执行推送，也没有连接远端。
- 旧状态没有 `delivery` 字段时保持可读，并在 inspect 中显示 `unverified`。

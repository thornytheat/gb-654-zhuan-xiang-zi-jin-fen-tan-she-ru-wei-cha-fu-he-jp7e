# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。

## 批次拨款更正台账

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `batch_id` | string | 否 | 批复金额与条目规范摘要派生的稳定批次号 |
| `data_version` | integer | 否 | 快照版本，初始为 0，每次发布更正 +1 |
| `entitled` | string | 否 | 当前资格/封顶口径下重算的应得金额（两位小数） |
| `entitled_before` | string | 是 | 上一版本应得，首版为空 |
| `paid` | string | 否 | 项目累计已拨款（凭证原值合计，不可变） |
| `recovered` | string | 否 | 项目累计已收回追回，已闭环部分单列 |
| `topup` | string | 否 | 待补 = max(0, 应得 − (已拨 − 已收回)) |
| `clawback` | string | 否 | 待追回净额 = max(0, (已拨 − 已收回) − 应得)，即未收回部分 |
| `outstanding` | string | 否 | 未收回金额，与 `clawback` 同口径 |
| `available_cash` | string | 否 | 可再分配现金 = 批复 − 已拨 + 已收回；未收回款不计入 |
| `funding_gap` | string | 否 | 资金缺口 = max(0, 待补合计 − 可再分配现金) |
| `voucher_id` / `recovery_id` | string | 否 | 拨款/追回凭证号，作为幂等键，重复提交不重复入账 |
| `base_version` | integer | 否 | 更正登记时的基线版本，发布时与当前版本比对 |
| `correction_id` | string | 否 | 更正单号；状态 `PENDING`/`PUBLISHED` |
| `reason_code` | string | 否 | 拨款、追回或更正的稳定原因码（见 `app/reasons.py`） |

持久化表：`batches`、`batch_snapshots`、`disbursements`、`recoveries`、`corrections`（见 `app/store.py`）。快照与凭证只追加不更新；更正发布在单事务内完成，失败回滚。

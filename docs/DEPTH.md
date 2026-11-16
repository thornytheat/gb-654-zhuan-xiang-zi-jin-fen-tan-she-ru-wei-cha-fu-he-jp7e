# 深度登记 专项资金分摊与舍入尾差复核

        - 规模：中型
        - 核心规则：先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。
        - 使用者与工作流：导入随仓样例，逐模块复核，最后导出单一证据清单。

        ## 功能模块

        | 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
        |---|---|---|---|---|
        | M1 预算采信 | 运行样例并复核证据 | `/api/budgets/import` / `/budgets` | `app/modules/m1_budgets/core.py` `app/modules/m1_budgets/service.py` | `tests/test_m1_budgets.py` |
| M2 资格校验 | 运行样例并复核证据 | `/api/eligibility/evaluate` / `/eligibility` | `app/modules/m2_eligibility/core.py` `app/modules/m2_eligibility/service.py` | `tests/test_m2_eligibility.py` |
| M3 权重测算 | 运行样例并复核证据 | `/api/weights/calculate` / `/weights` | `app/modules/m3_weights/core.py` `app/modules/m3_weights/service.py` | `tests/test_m3_weights.py` |
| M4 金额分摊 | 运行样例并复核证据 | `/api/allocations/calculate` / `/allocations` | `app/modules/m4_allocations/core.py` `app/modules/m4_allocations/service.py` | `tests/test_m4_allocations.py` |
| M5 尾差裁决 | 运行样例并复核证据 | `/api/remainders/resolve` / `/remainders` | `app/modules/m5_remainders/core.py` `app/modules/m5_remainders/service.py` | `tests/test_m5_remainders.py` |
| M6 批次复核 | 运行样例并复核证据 | `/api/export/allocations.csv` / `/runs` | `app/modules/m6_runs/core.py` `app/modules/m6_runs/service.py` | `tests/test_m6_runs.py` |

        ## 陷阱登记

        | TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
        |---|---|---|---|---|---|
        | TRAP-05 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_budgets/core.py` | `tests/test_m1_budgets.py::test_trap_1` |
| TRAP-06 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_eligibility/core.py` | `tests/test_m2_eligibility.py::test_trap_1` |
| TRAP-07 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_weights/core.py` | `tests/test_m3_weights.py::test_trap_1` |
| TRAP-09 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_allocations/core.py` | `tests/test_m4_allocations.py::test_trap_1` |
| TRAP-10 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m5_remainders/core.py` | `tests/test_m5_remainders.py::test_trap_1` |
| TRAP-14 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m6_runs/core.py` | `tests/test_m6_runs.py::test_trap_1` |
| TRAP-19 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_budgets/core.py` | `tests/test_m1_budgets.py::test_trap_2` |
| TRAP-22 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_eligibility/core.py` | `tests/test_m2_eligibility.py::test_trap_2` |
| TRAP-24 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_weights/core.py` | `tests/test_m3_weights.py::test_trap_2` |
| TRAP-26 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_allocations/core.py` | `tests/test_m4_allocations.py::test_trap_2` |

        ## 对账规则

        - 输入记录数 = 有效记录数 + 隔离记录数 + 空记录数。
        - 核心数值或事件数在转换、判定和导出之间保持守恒。
        - 同一输入摘要重跑复用原运行编号，不增加结果行。
        - 批次台账逐项目守恒：应得 + 待追回 − 待补 = 已拨 − 已收回；应得合计 + 尾差 = 批复。
        - 未收回款不回资金池：可再分配现金 = 批复 − 累计已拨 + 累计已收回；资金缺口 = max(0, 待补合计 − 可再分配现金)。

        ## 批次拨款更正台账（M6 扩展）

        | 能力 | 入口 | 核心文件 | 测试 |
        |---|---|---|---|
        | 批次建立与四列台账 | `POST/GET /api/batches` | `app/modules/m6_runs/ledger.py` `app/modules/m6_runs/service.py` | `tests/test_m6_ledger.py` |
        | 拨款/追回凭证（幂等） | `POST /api/batches/{id}/disbursements` `.../recoveries` | `app/store.py` `app/modules/m6_runs/service.py` | `tests/test_m6_ledger.py` |
        | 更正登记与乐观发布 | `POST /api/batches/{id}/corrections` `POST /api/corrections/{id}/publish` | `app/store.py::publish_correction` | `tests/test_m6_batch_api.py` |
        | 同批次导出 | `GET /api/batches/{id}/export.csv` | `app/modules/m6_runs/ledger.py::ledger_csv` | `tests/test_m6_batch_api.py` |

        | TRAP | 朴素实现的问题 | 本项目处理 | 处理位置 | 测试 |
        |---|---|---|---|---|
        | 覆盖旧账 | 更正后直接改写原分摊，实付凭证被抹掉 | 快照只追加版本，凭证表不可变，旧版随时可查 | `app/store.py` `batch_snapshots/disbursements` | `test_publish_rollback_keeps_old_version` |
        | 冻结旧账继续多拨 | 不重算应得，超额拨款持续发生 | 按更正资格重算应得，超额转待追回，未收回不回池 | `app/modules/m6_runs/ledger.py::reconcile` | `test_overpaid_after_correction_becomes_clawback` |
        | 并发更正覆盖 | 后写覆盖先写，版本静默丢失 | 发布时乐观锁，基线过期返回 `STALE_VERSION` 422 | `app/store.py::publish_correction` | `test_stale_correction_rejected_at_publish` |
        | 发布中断 | 半写状态污染台账，重试重复记账 | 单事务回滚保留旧版；已发布更正重试回放原快照 | `app/store.py::publish_correction` | `test_failed_switch_keeps_old_version` `test_resume_and_retry_do_not_duplicate` |
        | 凭证重试重复入账 | 网络重试生成第二张凭证 | 按 voucher_id/recovery_id 幂等，重复提交回原记录 | `app/store.py::add_disbursement/add_recovery` | `test_disbursement_idempotent_and_ledger_split` |
        | 未收回款被再分配 | 待追回挂账却计入可用现金导致超分 | 仅实际收回回池，缺口按现金口径单独列示 | `app/modules/m6_runs/ledger.py::reconcile` | `test_real_funding_gap_unrecovered_not_reused` |
        | 分到分尾差 | 浮点分摊合计与批复差 1 分 | 最大余额法落分并校验应得+尾差=批复 | `app/modules/m6_runs/ledger.py::settle` | `test_cents_rounding_conserves_total` |

        ## 已知不覆盖

        - 生产系统连接、身份认证和外部密钥不在演示范围内。

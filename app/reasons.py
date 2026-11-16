REASONS = {
    "BAD_COLUMNS": "字段数量不符", "EMPTY_VALUE": "必要值为空",
    "DUPLICATE_CONFLICT": "同一业务键取值冲突", "TRUNCATED": "尾记录不完整",
    "INVALID_VALUE": "值不符合定义", "STALE_VERSION": "提交版本已过期",
    # 批次拨款与更正台账
    "BATCH_NOT_FOUND": "批次不存在", "CORRECTION_NOT_FOUND": "更正单不存在",
    "OVER_ENTITLEMENT": "累计拨款超过当前应得", "OVER_CLAWBACK": "累计追回超过待追回",
    "VOUCHER_NOT_FOUND": "原拨款凭证不存在", "RECONCILE_FAIL": "尾差对账不平",
    "INITIAL": "批次初始分摊", "ELIGIBILITY_REVOKED": "撤销受助资格",
    "ELIGIBILITY_RESTORED": "恢复受助资格", "CAP_REDUCED": "调低封顶",
    "CAP_RAISED": "调高封顶", "WEIGHT_REVISED": "权重重测",
    "GRANT_DISBURSE": "拨款凭证登记", "GRANT_RECOVERY": "超额拨款追回",
    "FUND_GAP": "待补资金缺口", "INELIGIBLE": "资格无效",
}

def reason(code: str) -> str:
    return REASONS.get(code, "未登记原因")

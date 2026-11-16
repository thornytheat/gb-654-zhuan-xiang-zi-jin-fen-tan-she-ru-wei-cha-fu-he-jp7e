"""M6 批次复核：拨款登记、资格/封顶更正、四列台账与同批次导出。"""
from __future__ import annotations
from decimal import Decimal

from app.errors import DomainError
from . import ledger
from . import core as legacy_core


# 兼容旧入口（/api/export/allocations.csv 的遗留样例流程保持不变）。
def execute(payload: dict | None = None) -> dict:
    return legacy_core.run(payload or legacy_core.SAMPLE)


# ---------------------------------------------------------------------------
# 批次建立与查看
# ---------------------------------------------------------------------------
def create_batch(store, payload: dict) -> dict:
    approved = ledger.money(payload.get("approved"), "approved")
    items = ledger.normalize_items(payload.get("items", []))
    batch_id = str(payload.get("batch_id") or ledger.batch_id_for(approved, items))
    if store.get_batch(batch_id):
        raise DomainError("DUPLICATE_CONFLICT", f"批次 {batch_id} 已存在")
    snap = ledger.snapshot(0, approved, items, "INITIAL")
    store.create_batch(batch_id, str(approved), snap, ledger.now_iso())
    return {"batch_id": batch_id, "snapshot": snap}


def _sums(rows, key: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in rows:
        out[row["project_id"]] = out.get(row["project_id"], Decimal("0.00")) + ledger.money(row[key])
    return out


def view_batch(store, batch_id: str) -> dict:
    batch = store.get_batch(batch_id)
    if not batch:
        raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")
    snapshots = store.list_snapshots(batch_id)
    current = snapshots[-1]["data"]
    previous = snapshots[-2]["data"] if len(snapshots) > 1 else None
    disbursements = store.list_disbursements(batch_id)
    recoveries = store.list_recoveries(batch_id)
    paid = _sums(disbursements, "amount")
    recovered = _sums(recoveries, "amount")
    report = ledger.reconcile(current, previous, paid, recovered)
    return {
        "batch": batch,
        "current": current,
        "previous": previous,
        "snapshots": [{"version": s["version"], "reason_code": s["reason_code"],
                       "reason_text": s["data"].get("reason_text", ""), "created_at": s["created_at"]}
                      for s in snapshots],
        "disbursements": disbursements,
        "recoveries": recoveries,
        "corrections": store.list_corrections(batch_id),
        "ledger": report,
        "csv": ledger.ledger_csv(batch_id, current["version"], report["rows"]),
    }


def list_batches(store) -> dict:
    return {"batches": store.list_batches()}


# ---------------------------------------------------------------------------
# 拨款与追回（凭证不可变；重试按凭证号幂等，不重复记账）
# ---------------------------------------------------------------------------
def register_disbursement(store, payload: dict) -> dict:
    batch_id = str(payload.get("batch_id", ""))
    voucher_id = str(payload.get("voucher_id", ""))
    project_id = str(payload.get("project_id", ""))
    if not voucher_id:
        raise DomainError("EMPTY_VALUE", "voucher_id 必填")
    amount = ledger.positive_money(payload.get("amount"), "amount")
    reason_code = str(payload.get("reason_code") or "GRANT_DISBURSE")

    def guard(db):
        # 拨款时点：累计净拨款不得超过“当时”应得。更正后形成的超额不在此拦截，
        # 由台账列为待追回，凭证本身原样保留。
        snap = store.latest_snapshot(batch_id)
        entitled = next((ledger.money(r["entitled"]) for r in snap["rows"]
                         if r["project_id"] == project_id), None)
        if entitled is None:
            raise DomainError("INVALID_VALUE", f"项目 {project_id} 不在批次 {batch_id} 内")
        paid_rows = db.execute(
            "SELECT amount FROM disbursements WHERE batch_id=? AND project_id=?",
            (batch_id, project_id)).fetchall()
        recovered_rows = db.execute(
            "SELECT amount FROM recoveries WHERE batch_id=? AND project_id=?",
            (batch_id, project_id)).fetchall()
        already = sum((ledger.money(r["amount"]) for r in paid_rows), Decimal("0.00"))
        recovered = sum((ledger.money(r["amount"]) for r in recovered_rows), Decimal("0.00"))
        if already - recovered + amount > entitled:
            raise DomainError("OVER_ENTITLEMENT",
                              f"项目 {project_id} 累计净拨款将超过拨款时点应得 {entitled}")

    result = store.add_disbursement(batch_id, voucher_id, project_id, str(amount),
                                    reason_code, ledger.now_iso(), guard)
    result["batch_id"] = batch_id
    return result


def register_recovery(store, payload: dict) -> dict:
    batch_id = str(payload.get("batch_id", ""))
    recovery_id = str(payload.get("recovery_id", ""))
    voucher_ref = str(payload.get("disbursement_ref", ""))
    project_id = str(payload.get("project_id", ""))
    if not recovery_id:
        raise DomainError("EMPTY_VALUE", "recovery_id 必填")
    amount = ledger.positive_money(payload.get("amount"), "amount")
    reason_code = str(payload.get("reason_code") or "GRANT_RECOVERY")

    def guard(db):
        voucher = db.execute(
            "SELECT 1 FROM disbursements WHERE voucher_id=? AND batch_id=?",
            (voucher_ref, batch_id)).fetchone()
        if not voucher:
            raise DomainError("VOUCHER_NOT_FOUND", f"原拨款凭证 {voucher_ref} 不存在")
        paid_rows = db.execute(
            "SELECT amount FROM disbursements WHERE batch_id=? AND project_id=?",
            (batch_id, project_id)).fetchall()
        recovered_rows = db.execute(
            "SELECT amount FROM recoveries WHERE batch_id=? AND project_id=?",
            (batch_id, project_id)).fetchall()
        paid = sum((ledger.money(r["amount"]) for r in paid_rows), Decimal("0.00"))
        recovered = sum((ledger.money(r["amount"]) for r in recovered_rows), Decimal("0.00"))
        # 硬约束：累计追回不得超过累计拨款；具体待追回由四列台账对账体现。
        if recovered + amount > paid:
            raise DomainError("OVER_CLAWBACK", f"项目 {project_id} 累计追回不能超过累计拨款")

    result = store.add_recovery(batch_id, recovery_id, voucher_ref, project_id, str(amount),
                                reason_code, ledger.now_iso(), guard)
    result["batch_id"] = batch_id
    return result


# ---------------------------------------------------------------------------
# 更正：登记（乐观版本控制）→ 发布（事务、可续跑、失败回滚）
# ---------------------------------------------------------------------------
def submit_correction(store, payload: dict) -> dict:
    batch_id = str(payload.get("batch_id", ""))
    batch = store.get_batch(batch_id)
    if not batch:
        raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")
    base_version = int(payload.get("base_version", batch["current_version"]))
    reason_code = str(payload.get("reason_code", ""))
    if not reason_code:
        raise DomainError("EMPTY_VALUE", "更正 reason_code 必填")
    changes = payload.get("changes", [])
    if not changes:
        raise DomainError("EMPTY_VALUE", "更正 changes 不能为空")
    # 先在当前快照上试算，非法对象/金额在登记阶段即拒绝。
    current = store.latest_snapshot(batch_id)
    ledger.apply_changes(current["items"], changes)
    correction_id = str(payload.get("correction_id")
                        or f"corr-{batch_id}-v{base_version}-{ledger.canonical_digest(changes)[:8]}")
    return store.insert_correction(correction_id, batch_id, base_version, reason_code,
                                   ledger.reason(reason_code), changes, ledger.now_iso())


def publish_correction(store, payload: dict) -> dict:
    correction_id = str(payload.get("correction_id", ""))
    expected_version = int(payload.get("expected_version", -1))
    corr = store.get_correction(correction_id)
    if not corr:
        raise DomainError("CORRECTION_NOT_FOUND", f"更正 {correction_id} 不存在")
    if expected_version < 0:
        expected_version = corr["base_version"]
    return store.publish_correction(
        corr["batch_id"], correction_id, expected_version,
        settle_fn=lambda approved, items, version, code: ledger.snapshot(version, approved, items, code),
        apply_fn=ledger.apply_changes, now_fn=ledger.now_iso)


def export_batch(store, batch_id: str) -> dict:
    view = view_batch(store, batch_id)
    return {"batch_id": batch_id, "version": view["current"]["version"],
            "filename": f"ledger_{batch_id}_v{view['current']['version']}.csv",
            "csv": view["csv"], "totals": view["ledger"]["totals"]}

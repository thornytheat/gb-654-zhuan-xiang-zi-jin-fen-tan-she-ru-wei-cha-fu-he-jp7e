"""批次拨款更正台账的领域不变量测试。"""
from decimal import Decimal

import pytest

from app.errors import DomainError
from app.modules.m6_runs import ledger, service
from app.modules.m6_runs.ledger import money
from app.store import Store

ITEMS = [
    {"id": "A", "weight": 5, "cap": "300.00", "eligible": True},
    {"id": "B", "weight": 3, "cap": "500.00", "eligible": True},
    {"id": "C", "weight": 2, "cap": "500.00", "eligible": True},
]


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "ledger.db"))


@pytest.fixture
def batch(store):
    out = service.create_batch(store, {"batch_id": "B1", "approved": "1000.00", "items": ITEMS})
    return out["batch_id"]


def settle_view(store, batch_id):
    return service.view_batch(store, batch_id)


# -- 结算与尾差 -------------------------------------------------------------
def test_initial_settle_caps_and_cents(store, batch):
    view = settle_view(store, batch)
    by_id = {r["project_id"]: r for r in view["current"]["rows"]}
    assert by_id["A"]["entitled"] == "300.00"   # 触顶
    assert by_id["B"]["entitled"] == "420.00"
    assert by_id["C"]["entitled"] == "280.00"
    assert view["current"]["balanced"] is True
    assert all(money(r["entitled"]).as_tuple().exponent == -2 for r in view["current"]["rows"])


def test_settle_order_independent():
    left = ledger.settle("1000.00", ITEMS)["rows"]
    right = ledger.settle("1000.00", list(reversed(ITEMS)))["rows"]
    assert {r["project_id"]: r["entitled"] for r in left} == \
           {r["project_id"]: r["entitled"] for r in right}


def test_cents_rounding_conserves_total():
    items = [{"id": f"P{i}", "weight": 1, "cap": "999.00", "eligible": True} for i in range(3)]
    snap = ledger.settle("10.00", items)
    assert sum((money(r["entitled"]) for r in snap["rows"]), Decimal("0.00")) == Decimal("10.00")
    assert snap["leftover"] == "0.00"


def test_ineligible_project_gets_zero_and_leftover_held():
    items = [{"id": "A", "weight": 1, "cap": "100.00", "eligible": True},
             {"id": "B", "weight": 9, "cap": "100.00", "eligible": False}]
    snap = ledger.settle("500.00", items)
    by_id = {r["project_id"]: r for r in snap["rows"]}
    assert by_id["A"]["entitled"] == "100.00"
    assert by_id["B"]["entitled"] == "0.00"
    assert snap["leftover"] == "400.00"
    assert snap["balanced"] is True


# -- 拨款凭证与四列台账 ------------------------------------------------------
def test_disbursement_idempotent_and_ledger_split(store, batch):
    first = service.register_disbursement(store, {"batch_id": batch, "voucher_id": "V1",
                                                   "project_id": "A", "amount": "300.00"})
    again = service.register_disbursement(store, {"batch_id": batch, "voucher_id": "V1",
                                                   "project_id": "A", "amount": "300.00"})
    assert first["deduplicated"] is False and again["deduplicated"] is True
    view = settle_view(store, batch)
    row_a = next(r for r in view["ledger"]["rows"] if r["project_id"] == "A")
    assert row_a["paid"] == "300.00" and row_a["topup"] == "0.00"
    row_b = next(r for r in view["ledger"]["rows"] if r["project_id"] == "B")
    assert row_b["topup"] == "420.00" and row_b["paid"] == "0.00"
    assert view["ledger"]["balanced"] is True


def test_disbursement_cannot_target_unknown_project(store, batch):
    with pytest.raises(DomainError) as exc:
        service.register_disbursement(store, {"batch_id": batch, "voucher_id": "V2",
                                               "project_id": "ZZ", "amount": "1.00"})
    assert exc.value.code == "INVALID_VALUE"


def test_overpaid_after_correction_becomes_clawback(store, batch):
    # 拨款时点合规（A 应得 300）；随后 A 封顶降到 200，超额 100 原样保留为待追回。
    service.register_disbursement(store, {"batch_id": batch, "voucher_id": "V2",
                                          "project_id": "A", "amount": "300.00"})
    corr = service.submit_correction(store, {"batch_id": batch, "reason_code": "CAP_REDUCED",
                                             "changes": [{"id": "A", "cap": "200.00"}]})
    service.publish_correction(store, {"correction_id": corr["correction_id"]})
    view = settle_view(store, batch)
    row_a = next(r for r in view["ledger"]["rows"] if r["project_id"] == "A")
    assert row_a["paid"] == "300.00" and row_a["entitled"] == "200.00"
    assert row_a["clawback"] == "100.00" and row_a["outstanding"] == "100.00"
    assert all(c["passed"] for c in view["ledger"]["checks"])


def test_recovery_clawback_split_and_recovered_reuse(store, batch):
    # C 先拿满 280，随后资格被撤销，应得归零，形成 280 待追回。
    service.register_disbursement(store, {"batch_id": batch, "voucher_id": "VC",
                                          "project_id": "C", "amount": "280.00"})
    corr = service.submit_correction(store, {"batch_id": batch, "reason_code": "ELIGIBILITY_REVOKED",
                                             "changes": [{"id": "C", "eligible": False}]})
    service.publish_correction(store, {"correction_id": corr["correction_id"]})
    view = settle_view(store, batch)
    row_c = next(r for r in view["ledger"]["rows"] if r["project_id"] == "C")
    assert row_c["clawback"] == "280.00" and row_c["outstanding"] == "280.00"
    assert view["ledger"]["totals"]["available_cash"] == "720.00"  # 1000-280，未收回不回池

    rec = service.register_recovery(store, {"batch_id": batch, "recovery_id": "RC1",
                                            "disbursement_ref": "VC", "project_id": "C",
                                            "amount": "100.00"})
    assert rec["deduplicated"] is False
    again = service.register_recovery(store, {"batch_id": batch, "recovery_id": "RC1",
                                              "disbursement_ref": "VC", "project_id": "C",
                                              "amount": "100.00"})
    assert again["deduplicated"] is True
    view = settle_view(store, batch)
    row_c = next(r for r in view["ledger"]["rows"] if r["project_id"] == "C")
    # 净超拨 280-100=180 即待追回（未收回）；已收回 100 单列留痕。
    assert row_c["clawback"] == "180.00" and row_c["outstanding"] == "180.00"
    assert row_c["recovered"] == "100.00"
    assert view["ledger"]["totals"]["available_cash"] == "820.00"  # 已收回部分才回池
    assert view["ledger"]["balanced"] is True


def test_recovery_requires_real_voucher_and_cannot_overpay(store, batch):
    service.register_disbursement(store, {"batch_id": batch, "voucher_id": "VA",
                                          "project_id": "A", "amount": "100.00"})
    with pytest.raises(DomainError) as exc:
        service.register_recovery(store, {"batch_id": batch, "recovery_id": "RX",
                                          "disbursement_ref": "NOPE", "project_id": "A",
                                          "amount": "10.00"})
    assert exc.value.code == "VOUCHER_NOT_FOUND"
    with pytest.raises(DomainError) as exc:
        service.register_recovery(store, {"batch_id": batch, "recovery_id": "RY",
                                          "disbursement_ref": "VA", "project_id": "A",
                                          "amount": "101.00"})
    assert exc.value.code == "OVER_CLAWBACK"


# -- 更正：版本控制、回滚、续跑、对照 ----------------------------------------
def test_stale_correction_rejected_at_publish(store, batch):
    # 两份并发更正都基于 v0 登记；先发布者推进到 v1，第二份发布即被乐观锁拒绝。
    first = service.submit_correction(store, {"batch_id": batch, "reason_code": "CAP_REDUCED",
                                              "changes": [{"id": "A", "cap": "200.00"}]})
    second = service.submit_correction(store, {"batch_id": batch, "base_version": 0,
                                               "reason_code": "CAP_REDUCED",
                                               "changes": [{"id": "B", "cap": "100.00"}]})
    service.publish_correction(store, {"correction_id": first["correction_id"]})
    with pytest.raises(DomainError) as exc:
        service.publish_correction(store, {"correction_id": second["correction_id"]})
    assert exc.value.code == "STALE_VERSION"
    # 被拒后旧版（v1）保持不变，第二份更正仍挂起，可按新基线重提。
    view = settle_view(store, batch)
    assert view["batch"]["current_version"] == 1
    assert store.get_correction(second["correction_id"])["status"] == "PENDING"


def test_publish_rollback_keeps_old_version(store, batch):
    corr = service.submit_correction(store, {"batch_id": batch, "reason_code": "CAP_REDUCED",
                                             "changes": [{"id": "A", "cap": "200.00"}]})
    original = store.publish_correction

    def broken_publish(*args, **kwargs):
        raise RuntimeError("发布中断：模拟提交阶段失败")

    store.publish_correction = broken_publish
    with pytest.raises(RuntimeError):
        service.publish_correction(store, {"correction_id": corr["correction_id"]})
    store.publish_correction = original

    view = settle_view(store, batch)
    assert view["batch"]["current_version"] == 0
    assert len(view["snapshots"]) == 1  # 旧版原样保留
    pending = store.get_correction(corr["correction_id"])
    assert pending["status"] == "PENDING"


def test_resume_and_retry_do_not_duplicate(store, batch):
    corr = service.submit_correction(store, {"batch_id": batch, "reason_code": "CAP_REDUCED",
                                             "changes": [{"id": "A", "cap": "200.00"}]})
    snap1 = service.publish_correction(store, {"correction_id": corr["correction_id"]})
    # 中断后续跑/重复重试：回放已存快照，不新增版本、不重复记账。
    snap2 = service.publish_correction(store, {"correction_id": corr["correction_id"]})
    assert snap1["version"] == snap2["version"] == 1
    view = settle_view(store, batch)
    assert view["batch"]["current_version"] == 1
    assert len(view["snapshots"]) == 2
    assert len(store.list_disbursements(batch)) == 0


def test_before_after_comparison_and_cap_recompute(store, batch):
    corr = service.submit_correction(store, {"batch_id": batch, "reason_code": "CAP_REDUCED",
                                             "changes": [{"id": "A", "cap": "200.00"}]})
    service.publish_correction(store, {"correction_id": corr["correction_id"]})
    view = settle_view(store, batch)
    row_a = next(r for r in view["ledger"]["rows"] if r["project_id"] == "A")
    assert row_a["entitled_before"] == "300.00" and row_a["entitled"] == "200.00"
    # A 让出的 100 在 B/C 间按 3:2 重分：B 480、C 320。
    by_id = {r["project_id"]: r for r in view["current"]["rows"]}
    assert by_id["B"]["entitled"] == "480.00" and by_id["C"]["entitled"] == "320.00"
    assert view["current"]["balanced"] is True


def test_correction_cannot_introduce_new_project(store, batch):
    with pytest.raises(DomainError) as exc:
        service.submit_correction(store, {"batch_id": batch, "reason_code": "WEIGHT_REVISED",
                                          "changes": [{"id": "ZZ", "cap": "1.00"}]})
    assert exc.value.code == "INVALID_VALUE"


# -- 资金缺口与同批次导出 ----------------------------------------------------
def test_funding_gap_when_cash_short(store, batch):
    # 批复 1000 全拨：A 300、B 420、C 280；撤销 C 后 B 按权重升至 500（触顶），
    # 剩余 200 为尾差；C 多拨 280 待追回且未收回，现金池为 0，B 待补 80 → 缺口 80。
    for vid, pid, amt in [("V1", "A", "300.00"), ("V2", "B", "420.00"), ("V3", "C", "280.00")]:
        service.register_disbursement(store, {"batch_id": batch, "voucher_id": vid,
                                              "project_id": pid, "amount": amt})
    corr = service.submit_correction(store, {"batch_id": batch, "reason_code": "ELIGIBILITY_REVOKED",
                                             "changes": [{"id": "C", "eligible": False}]})
    service.publish_correction(store, {"correction_id": corr["correction_id"]})
    view = settle_view(store, batch)
    totals = view["ledger"]["totals"]
    assert money(view["current"]["leftover"]) == Decimal("200.00")
    assert money(totals["available_cash"]) == Decimal("0.00")
    assert money(totals["topup_total"]) == Decimal("80.00")
    assert money(totals["funding_gap"]) == Decimal("80.00")
    assert money(totals["clawback_total"]) == Decimal("280.00")
    assert money(totals["outstanding_total"]) == Decimal("280.00")
    assert view["ledger"]["balanced"] is True


def test_recovered_cash_closes_gap(store, batch):
    # 接上一情景：追回 C 的 80 后，现金池恢复 80，B 的待补被覆盖，缺口归零；
    # 仍有 200 未收回，只挂账不回池。
    for vid, pid, amt in [("V1", "A", "300.00"), ("V2", "B", "420.00"), ("V3", "C", "280.00")]:
        service.register_disbursement(store, {"batch_id": batch, "voucher_id": vid,
                                              "project_id": pid, "amount": amt})
    corr = service.submit_correction(store, {"batch_id": batch, "reason_code": "ELIGIBILITY_REVOKED",
                                             "changes": [{"id": "C", "eligible": False}]})
    service.publish_correction(store, {"correction_id": corr["correction_id"]})
    service.register_recovery(store, {"batch_id": batch, "recovery_id": "RC1",
                                      "disbursement_ref": "V3", "project_id": "C", "amount": "80.00"})
    totals = settle_view(store, batch)["ledger"]["totals"]
    assert money(totals["available_cash"]) == Decimal("80.00")
    assert money(totals["funding_gap"]) == Decimal("0.00")
    assert money(totals["outstanding_total"]) == Decimal("200.00")
    assert money(totals["recovered_total"]) == Decimal("80.00")


def test_real_funding_gap_unrecovered_not_reused(store, tmp_path):
    # B 初始无资格：A 按 900 封顶拿 900，尾差 100；恢复 B 后各应得 500，
    # A 超拨 400 待追回（未收回不回池），B 待补 500，现金池仅尾差 100 → 缺口 400。
    items = [{"id": "A", "weight": 5, "cap": "900.00", "eligible": True},
             {"id": "B", "weight": 5, "cap": "900.00", "eligible": False}]
    bid = service.create_batch(store, {"batch_id": "BGAP", "approved": "1000.00", "items": items})["batch_id"]
    service.register_disbursement(store, {"batch_id": bid, "voucher_id": "VA",
                                          "project_id": "A", "amount": "900.00"})
    corr = service.submit_correction(store, {"batch_id": bid, "reason_code": "ELIGIBILITY_RESTORED",
                                             "changes": [{"id": "B", "eligible": True}]})
    service.publish_correction(store, {"correction_id": corr["correction_id"]})
    view = settle_view(store, bid)
    totals = view["ledger"]["totals"]
    assert money(totals["available_cash"]) == Decimal("100.00")
    assert money(totals["topup_total"]) == Decimal("500.00")
    assert money(totals["funding_gap"]) == Decimal("400.00")
    # A 超拨 400 挂待追回；在实际收回前不得当作可用资金。
    assert money(totals["clawback_total"]) == Decimal("400.00")
    assert money(totals["outstanding_total"]) == Decimal("400.00")
    assert all(c["passed"] for c in view["ledger"]["checks"])


def test_export_uses_same_batch_and_version(store, batch):
    corr = service.submit_correction(store, {"batch_id": batch, "reason_code": "CAP_REDUCED",
                                             "changes": [{"id": "A", "cap": "250.00"}]})
    service.publish_correction(store, {"correction_id": corr["correction_id"]})
    exported = service.export_batch(store, batch)
    assert exported["version"] == 1
    lines = exported["csv"].strip().splitlines()
    assert lines[0].startswith("batch_id,data_version,")
    assert all(line.split(",", 2)[0] == batch and line.split(",", 2)[1] == "1" for line in lines[1:])
    view = settle_view(store, batch)
    assert exported["csv"] == view["csv"]

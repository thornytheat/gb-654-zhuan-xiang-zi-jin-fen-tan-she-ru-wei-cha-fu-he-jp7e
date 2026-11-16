"""批次拨款与更正台账的纯领域逻辑。

设计原则（对应业务要求）：
- 已拨款凭证与原批次金额原样保留，任何更正只追加新版本快照，绝不覆盖旧账。
- 应得按更正后的资格/封顶重算；已拨、待补、待追回按同一口径分列。
- 未收回款不进入可再分配资金；封顶迭代与分到分尾差全程对账。
- 所有金额一律 Decimal，对外输出统一两位小数字符串。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN

from app.errors import DomainError
from app.manifest import canonical_digest
from app.reasons import reason

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def money(value, field: str = "amount") -> Decimal:
    """把外部输入归一为两位小数的非负金额。"""
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        raise DomainError("INVALID_VALUE", f"{field} 不是合法金额: {value!r}")
    if not amount.is_finite() or amount < 0:
        raise DomainError("INVALID_VALUE", f"{field} 必须是非负有限金额: {value!r}")
    return amount.quantize(CENT)


def positive_money(value, field: str = "amount") -> Decimal:
    amount = money(value, field)
    if amount <= 0:
        raise DomainError("INVALID_VALUE", f"{field} 必须大于 0: {value!r}")
    return amount


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def batch_id_for(approved: Decimal, items: list[dict]) -> str:
    digest = canonical_digest({"approved": str(approved), "items": normalize_items(items)})
    return "batch-" + digest[:12]


def normalize_items(items: list[dict]) -> list[dict]:
    """归一化批次条目，重复 id 直接拒绝，顺序原样保留（结果本身与顺序无关）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for raw in items or []:
        if "id" not in raw:
            raise DomainError("BAD_COLUMNS", "条目缺少 id 字段")
        pid = str(raw["id"])
        if pid in seen:
            raise DomainError("DUPLICATE_CONFLICT", f"项目 {pid} 在同一批次中重复")
        seen.add(pid)
        out.append({
            "id": pid,
            "weight": Decimal(str(raw.get("weight", 0))).quantize(Decimal("0.000001")),
            "cap": money(raw.get("cap", 0), f"{pid}.cap"),
            "eligible": bool(raw.get("eligible", True)),
        })
    return out


def apply_changes(items: list[dict], changes: list[dict]) -> list[dict]:
    """把更正项叠加到当前资格/封顶快照上，得到更正后条目。"""
    index = {x["id"]: dict(x) for x in items}
    for change in changes or []:
        pid = str(change.get("id", ""))
        if not pid:
            raise DomainError("BAD_COLUMNS", "更正条目缺少 id")
        if pid not in index:
            raise DomainError("INVALID_VALUE", f"更正对象 {pid} 不在批次内，不允许借更正新增项目")
        target = index[pid]
        if "eligible" in change:
            target["eligible"] = bool(change["eligible"])
        if "cap" in change:
            target["cap"] = money(change["cap"], f"{pid}.cap")
        if "weight" in change:
            target["weight"] = Decimal(str(change["weight"])).quantize(Decimal("0.000001"))
    return [index[x["id"]] for x in items]


# ---------------------------------------------------------------------------
# 结算：迭代封顶 + 权重分配 + 分到分（最大余额法）
# ---------------------------------------------------------------------------
def settle(approved, items: list[dict]) -> dict:
    """对给定资格/封顶快照结算每个项目的应得金额。

    返回每个项目的应得（两位小数）、未分余额（封顶吃不下的部分）以及守恒校验。
    """
    approved = money(approved, "approved")
    norm = normalize_items(items)
    entitlements = {x["id"]: ZERO for x in norm}
    remaining = approved

    # 第一轮：迭代封顶，触顶项目退出，剩余资金继续按有效权重分摊。
    pending = {x["id"]: x for x in norm if x["eligible"]}
    while pending and remaining > 0:
        total_weight = sum((x["weight"] for x in pending.values()), Decimal(0))
        if total_weight <= 0:
            break
        capped: list[str] = []
        for pid, item in pending.items():
            proposal = remaining * item["weight"] / total_weight
            room = item["cap"] - entitlements[pid]
            if proposal >= room:
                entitlements[pid] += room
                capped.append(pid)
        if not capped:
            for pid, item in pending.items():
                entitlements[pid] += remaining * item["weight"] / total_weight
            remaining = ZERO
            break
        remaining = approved - sum(entitlements.values())
        for pid in capped:
            pending.pop(pid)

    leftover = remaining.quantize(CENT, rounding=ROUND_DOWN) if remaining > 0 else ZERO
    distribution_pool = approved - leftover

    # 第二轮：未触顶项目按最大余额法落到分，触顶项目保持封顶整数分。
    raw = dict(entitlements)
    capped_ids = {x["id"] for x in norm
                  if entitlements[x["id"]] >= x["cap"] and x["cap"] > 0}
    base: dict[str, Decimal] = {}
    for pid, value in raw.items():
        base[pid] = value.quantize(CENT, rounding=ROUND_DOWN) if pid not in capped_ids else value.quantize(CENT)
    units = int(((distribution_pool - sum(base.values())) / CENT).to_integral_value())
    if units < 0:
        raise DomainError("RECONCILE_FAIL", "封顶金额已超过可分配资金，尾差无法裁决")
    order = sorted(
        (pid for pid in raw if pid not in capped_ids),
        key=lambda k: (-(raw[k] - base[k]), k),
    )
    for pid in order[:units]:
        base[pid] += CENT

    rows = []
    for item in norm:
        pid = item["id"]
        rows.append({
            "project_id": pid,
            "eligible": item["eligible"],
            "weight": str(item["weight"]),
            "cap": str(item["cap"]),
            "entitled": str(base[pid]),
            "capped": pid in capped_ids,
            "reason": "CAP" if pid in capped_ids else ("WEIGHT" if item["eligible"] else "INELIGIBLE"),
        })
    total_entitled = sum(base.values(), ZERO)
    return {
        "rows": rows,
        "approved": str(approved),
        "entitled_total": str(total_entitled),
        "leftover": str(leftover),
        "balanced": total_entitled + leftover == approved,
    }


def snapshot(version: int, approved: Decimal, items: list[dict], reason_code: str) -> dict:
    result = settle(approved, items)
    result.update({
        "version": version,
        "reason_code": reason_code,
        "reason_text": reason(reason_code),
        "items": [
            {"id": x["id"], "weight": str(x["weight"]), "cap": str(x["cap"]), "eligible": x["eligible"]}
            for x in normalize_items(items)
        ],
    })
    return result


# ---------------------------------------------------------------------------
# 台账对账：应得 / 已拨 / 待补 / 待追回 / 缺口
# ---------------------------------------------------------------------------
def reconcile(current: dict, previous: dict | None, paid: dict[str, Decimal],
              recovered: dict[str, Decimal]) -> dict:
    """按当前应得快照与逐项目已拨/已收回凭证，生成分列台账。"""
    current_by_id = {r["project_id"]: r for r in current["rows"]}
    prev_by_id = {r["project_id"]: r for r in (previous["rows"] if previous else [])}
    all_ids = sorted(set(prev_by_id) | set(current_by_id) | set(paid) | set(recovered))

    rows = []
    sum_paid = sum_topup = sum_claw = sum_recovered = sum_outstanding = ZERO
    for pid in all_ids:
        cur = current_by_id.get(pid)
        entitled = money(cur["entitled"], f"{pid}.entitled") if cur else ZERO
        paid_amt = paid.get(pid, ZERO)
        rec_amt = recovered.get(pid, ZERO)
        net_paid = paid_amt - rec_amt
        topup = max(ZERO, entitled - net_paid)
        clawback = max(ZERO, net_paid - entitled)
        # clawback 即净待追回（尚未收回的部分）；recovered 为已闭环的追回，单列留痕。
        outstanding = clawback
        rows.append({
            "project_id": pid,
            "eligible": bool(cur["eligible"]) if cur else False,
            "entitled_before": prev_by_id[pid]["entitled"] if pid in prev_by_id else "",
            "entitled": str(entitled),
            "paid": str(paid_amt),
            "recovered": str(rec_amt),
            "topup": str(topup),
            "clawback": str(clawback),
            "outstanding": str(outstanding),
            "reason": cur["reason"] if cur else "REMOVED",
        })
        sum_paid += paid_amt
        sum_topup += topup
        sum_claw += clawback
        sum_recovered += rec_amt
        sum_outstanding += outstanding

    approved = money(current["approved"], "approved")
    entitled_total = money(current["entitled_total"], "entitled_total")
    leftover = money(current["leftover"], "leftover")
    # 可再分配现金 = 批复总额 - 累计已拨 + 累计已收回；未收回款不参与。
    available_cash = approved - sum_paid + sum_recovered
    funding_gap = max(ZERO, sum_topup - available_cash)

    checks = [
        ("应得+尾差=批复", entitled_total + leftover == approved),
        ("净拨=应得+待追回-待补", all(
            money(r["entitled"]) + money(r["clawback"]) - money(r["topup"])
            == money(r["paid"]) - money(r["recovered"]) for r in rows)),
        ("待追回即未收回净额", all(
            money(r["clawback"]) == money(r["outstanding"]) for r in rows)),
        ("已拨不超过原应得", all(
            (not r["entitled_before"]) or money(r["paid"]) <= money(r["entitled_before"])
            for r in rows)),
    ]
    return {
        "rows": rows,
        "totals": {
            "approved": str(approved),
            "entitled_total": str(entitled_total),
            "leftover": str(leftover),
            "paid_total": str(sum_paid),
            "recovered_total": str(sum_recovered),
            "topup_total": str(sum_topup),
            "clawback_total": str(sum_claw),
            "outstanding_total": str(sum_outstanding),
            "available_cash": str(available_cash),
            "funding_gap": str(funding_gap),
        },
        "checks": [{"code": code, "passed": passed} for code, passed in checks],
        "balanced": all(passed for _, passed in checks),
    }


# ---------------------------------------------------------------------------
# 导出（同一批次同一口径）
# ---------------------------------------------------------------------------
LEDGER_FIELDS = [
    "batch_id", "data_version", "project_id", "eligible",
    "entitled_before", "entitled", "paid", "recovered",
    "topup", "clawback", "outstanding", "reason",
]


def ledger_csv(batch_id: str, version: int, rows: list[dict]) -> str:
    import csv
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LEDGER_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        record = {key: row.get(key, "") for key in LEDGER_FIELDS}
        record["batch_id"] = batch_id
        record["data_version"] = version
        writer.writerow(record)
    return buf.getvalue()

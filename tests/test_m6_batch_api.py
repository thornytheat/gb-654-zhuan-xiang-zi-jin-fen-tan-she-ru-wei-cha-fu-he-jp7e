"""M6 批次台账的 HTTP 端到端测试。"""
import threading

import pytest

from app.errors import DomainError


def _create(client, batch_id="B1", approved="1000.00", items=None):
    items = items or [
        {"id": "A", "weight": 5, "cap": "300.00", "eligible": True},
        {"id": "B", "weight": 3, "cap": "500.00", "eligible": True},
        {"id": "C", "weight": 2, "cap": "500.00", "eligible": True},
    ]
    res = client.post("/api/batches", json={"batch_id": batch_id, "approved": approved, "items": items})
    assert res.status_code == 200, res.text
    return res.json()


def test_runs_page_renders_new_ledger(client):
    res = client.get("/runs")
    assert res.status_code == 200
    assert "批次拨款台账" in res.text and "batch.js" in res.text


def test_full_batch_lifecycle(client):
    created = _create(client)
    bid = created["batch_id"]
    assert created["snapshot"]["version"] == 0

    listing = client.get("/api/batches").json()["batches"]
    assert any(b["batch_id"] == bid for b in listing)

    pay = client.post(f"/api/batches/{bid}/disbursements",
                      json={"voucher_id": "V1", "project_id": "A", "amount": "300.00"})
    assert pay.status_code == 200 and pay.json()["deduplicated"] is False
    retry = client.post(f"/api/batches/{bid}/disbursements",
                        json={"voucher_id": "V1", "project_id": "A", "amount": "300.00"})
    assert retry.json()["deduplicated"] is True

    view = client.get(f"/api/batches/{bid}").json()
    row_a = next(r for r in view["ledger"]["rows"] if r["project_id"] == "A")
    assert row_a["paid"] == "300.00"
    assert all(c["passed"] for c in view["ledger"]["checks"])


def test_stale_version_returns_422(client):
    bid = _create(client)["batch_id"]
    first = client.post(f"/api/batches/{bid}/corrections",
                        json={"reason_code": "CAP_REDUCED", "changes": [{"id": "A", "cap": "200.00"}]}).json()
    second = client.post(f"/api/batches/{bid}/corrections",
                         json={"base_version": 0, "reason_code": "CAP_REDUCED",
                               "changes": [{"id": "B", "cap": "100.00"}]}).json()
    assert client.post(f"/api/corrections/{first['correction_id']}/publish", json={}).status_code == 200
    stale = client.post(f"/api/corrections/{second['correction_id']}/publish", json={})
    assert stale.status_code == 422 and stale.json()["error"] == "STALE_VERSION"


def test_publish_and_same_batch_export(client):
    bid = _create(client)["batch_id"]
    corr = client.post(f"/api/batches/{bid}/corrections",
                       json={"reason_code": "ELIGIBILITY_REVOKED",
                             "changes": [{"id": "C", "eligible": False}]}).json()
    published = client.post(f"/api/corrections/{corr['correction_id']}/publish", json={})
    assert published.status_code == 200 and published.json()["version"] == 1
    # 重试发布幂等，不推进版本。
    again = client.post(f"/api/corrections/{corr['correction_id']}/publish", json={})
    assert again.json()["version"] == 1

    res = client.get(f"/api/batches/{bid}/export.csv")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    lines = res.text.strip().splitlines()
    assert lines[0].startswith("batch_id,data_version,")
    assert all(line.split(",", 2)[1] == "1" for line in lines[1:])


def test_failed_switch_keeps_old_version(client):
    bid = _create(client)["batch_id"]
    corr = client.post(f"/api/batches/{bid}/corrections",
                       json={"reason_code": "CAP_REDUCED", "changes": [{"id": "A", "cap": "200.00"}]}).json()
    store = client.app.state.store
    original = store.publish_correction

    def boom(*args, **kwargs):
        raise RuntimeError("switch fail")

    store.publish_correction = boom
    with pytest.raises(RuntimeError):
        client.post(f"/api/corrections/{corr['correction_id']}/publish", json={})
    store.publish_correction = original

    view = client.get(f"/api/batches/{bid}").json()
    assert view["batch"]["current_version"] == 0
    assert len(view["snapshots"]) == 1
    # 更正仍挂起，可按已存更正续跑成功。
    resumed = client.post(f"/api/corrections/{corr['correction_id']}/publish", json={})
    assert resumed.status_code == 200 and resumed.json()["version"] == 1


def test_legacy_endpoints_unchanged(client):
    assert client.get("/api/export/allocations.csv").status_code == 200
    runs = client.get("/api/runs").json()
    assert "runs" in runs


def test_unknown_batch_422(client):
    res = client.get("/api/batches/nope")
    assert res.status_code == 422 and res.json()["error"] == "BATCH_NOT_FOUND"


def test_concurrent_publish_only_one_wins(client):
    from app.modules.m6_runs import service
    bid = _create(client)["batch_id"]
    store = client.app.state.store
    c1 = service.submit_correction(store, {"batch_id": bid, "reason_code": "CAP_REDUCED",
                                           "changes": [{"id": "A", "cap": "200.00"}]})["correction_id"]
    c2 = service.submit_correction(store, {"batch_id": bid, "base_version": 0,
                                           "reason_code": "CAP_REDUCED",
                                           "changes": [{"id": "B", "cap": "200.00"}]})["correction_id"]
    outcomes = {}

    def publish(cid):
        try:
            service.publish_correction(store, {"correction_id": cid})
            outcomes[cid] = "ok"
        except DomainError as exc:
            outcomes[cid] = exc.code

    t1 = threading.Thread(target=publish, args=(c1,))
    t2 = threading.Thread(target=publish, args=(c2,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(outcomes.values()) == ["STALE_VERSION", "ok"]
    view = client.get(f"/api/batches/{bid}").json()
    assert view["batch"]["current_version"] == 1
    assert len(view["snapshots"]) == 2  # v0 与唯一一次成功发布的 v1

from __future__ import annotations
import json
from pathlib import Path
import sqlite3
from typing import Callable

from .errors import DomainError

_DUMPS = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            # 旧表：模块运行摘要，保持原样不动。
            db.execute("CREATE TABLE IF NOT EXISTS runs (digest TEXT PRIMARY KEY, run_id TEXT NOT NULL, result TEXT NOT NULL)")
            self._init_ledger(db)

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=4000")
        return db

    # -- 旧 API（M1-M6 运行摘要） ------------------------------------------
    def record(self, digest: str, run_id: str, result: dict) -> dict:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?)",
                       (digest, run_id, json.dumps(result, ensure_ascii=False, sort_keys=True)))
            row = db.execute("SELECT run_id, result FROM runs WHERE digest=?", (digest,)).fetchone()
        return {"run_id": row[0], "result": json.loads(row[1])}

    def count(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def all(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT run_id, digest, result FROM runs ORDER BY run_id").fetchall()
        return [{"run_id": r[0], "digest": r[1], "result": json.loads(r[2])} for r in rows]

    # -- 批次拨款更正台账 ---------------------------------------------------
    def _init_ledger(self, db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS batches(
            batch_id TEXT PRIMARY KEY, approved TEXT NOT NULL,
            current_version INTEGER NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS batch_snapshots(
            batch_id TEXT NOT NULL, version INTEGER NOT NULL,
            reason_code TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(batch_id, version))""")
        db.execute("""CREATE TABLE IF NOT EXISTS disbursements(
            voucher_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
            project_id TEXT NOT NULL, amount TEXT NOT NULL,
            reason_code TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS recoveries(
            recovery_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
            disbursement_ref TEXT NOT NULL, project_id TEXT NOT NULL,
            amount TEXT NOT NULL, reason_code TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS corrections(
            correction_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
            base_version INTEGER NOT NULL, reason_code TEXT NOT NULL,
            reason_text TEXT NOT NULL, changes TEXT NOT NULL,
            status TEXT NOT NULL, published_version INTEGER,
            snapshot TEXT, created_at TEXT NOT NULL)""")

    def create_batch(self, batch_id: str, approved: str, snapshot: dict, created_at: str) -> None:
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if exists:
                raise DomainError("DUPLICATE_CONFLICT", f"批次 {batch_id} 已存在")
            db.execute("INSERT INTO batches VALUES (?,?,?,?)",
                       (batch_id, approved, 0, created_at))
            db.execute("INSERT INTO batch_snapshots VALUES (?,?,?,?,?)",
                       (batch_id, 0, snapshot["reason_code"], _DUMPS(snapshot), created_at))

    def list_batches(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT batch_id, approved, current_version, created_at FROM batches ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT batch_id, approved, current_version, created_at FROM batches WHERE batch_id=?",
                             (batch_id,)).fetchone()
        return dict(row) if row else None

    def list_snapshots(self, batch_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT version, reason_code, data, created_at FROM batch_snapshots WHERE batch_id=? ORDER BY version",
                              (batch_id,)).fetchall()
        return [{"version": r["version"], "reason_code": r["reason_code"],
                 "data": json.loads(r["data"]), "created_at": r["created_at"]} for r in rows]

    def latest_snapshot(self, batch_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT data FROM batch_snapshots WHERE batch_id=? ORDER BY version DESC LIMIT 1",
                             (batch_id,)).fetchone()
        if not row:
            raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")
        return json.loads(row["data"])

    def add_disbursement(self, batch_id: str, voucher_id: str, project_id: str,
                         amount: str, reason_code: str, created_at: str,
                         guard: Callable[[sqlite3.Connection], None] | None = None) -> dict:
        """登记拨款凭证。同一 voucher_id 重试直接回原记录，不重复入账。"""
        with self.connect() as db:
            self._require_batch(db, batch_id)
            existing = db.execute("SELECT voucher_id, amount FROM disbursements WHERE voucher_id=?",
                                  (voucher_id,)).fetchone()
            if existing:
                return {"voucher_id": existing["voucher_id"], "amount": existing["amount"], "deduplicated": True}
            if guard is not None:
                guard(db)
            db.execute("INSERT INTO disbursements VALUES (?,?,?,?,?,?)",
                       (voucher_id, batch_id, project_id, amount, reason_code, created_at))
        return {"voucher_id": voucher_id, "amount": amount, "deduplicated": False}

    def list_disbursements(self, batch_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT voucher_id, project_id, amount, reason_code, created_at FROM disbursements WHERE batch_id=? ORDER BY created_at, voucher_id",
                              (batch_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_recovery(self, batch_id: str, recovery_id: str, disbursement_ref: str,
                     project_id: str, amount: str, reason_code: str, created_at: str,
                     guard: Callable[[sqlite3.Connection], None] | None = None) -> dict:
        """登记追回凭证。同一 recovery_id 重试回原记录；未收回款只在表外体现，不会被再分配。"""
        with self.connect() as db:
            self._require_batch(db, batch_id)
            existing = db.execute("SELECT recovery_id, amount FROM recoveries WHERE recovery_id=?",
                                  (recovery_id,)).fetchone()
            if existing:
                return {"recovery_id": existing["recovery_id"], "amount": existing["amount"], "deduplicated": True}
            if guard is not None:
                guard(db)
            db.execute("INSERT INTO recoveries VALUES (?,?,?,?,?,?,?)",
                       (recovery_id, batch_id, disbursement_ref, project_id, amount, reason_code, created_at))
        return {"recovery_id": recovery_id, "amount": amount, "deduplicated": False}

    def list_recoveries(self, batch_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT recovery_id, disbursement_ref, project_id, amount, reason_code, created_at FROM recoveries WHERE batch_id=? ORDER BY created_at, recovery_id",
                              (batch_id,)).fetchall()
        return [dict(r) for r in rows]

    def insert_correction(self, correction_id: str, batch_id: str, base_version: int,
                          reason_code: str, reason_text: str, changes: list[dict],
                          created_at: str) -> dict:
        """登记更正。并发更正的过期版本在发布时按当前版本拒绝（乐观锁）。"""
        with self.connect() as db:
            self._require_batch(db, batch_id)
            existing = db.execute("SELECT * FROM corrections WHERE correction_id=?", (correction_id,)).fetchone()
            if existing:
                return self._correction_row(existing)
            db.execute("INSERT INTO corrections VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (correction_id, batch_id, base_version, reason_code, reason_text,
                        _DUMPS(changes), "PENDING", None, None, created_at))
            row = db.execute("SELECT * FROM corrections WHERE correction_id=?", (correction_id,)).fetchone()
        return self._correction_row(row)

    def list_corrections(self, batch_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM corrections WHERE batch_id=? ORDER BY created_at", (batch_id,)).fetchall()
        return [self._correction_row(r) for r in rows]

    def get_correction(self, correction_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM corrections WHERE correction_id=?", (correction_id,)).fetchone()
        return self._correction_row(row) if row else None

    def publish_correction(self, batch_id: str, correction_id: str, expected_version: int,
                           settle_fn: Callable[[str, list[dict], int, str], dict],
                           apply_fn: Callable[[list[dict], list[dict]], list[dict]],
                           now_fn: Callable[[], str]) -> dict:
        """发布更正：单事务、乐观版本控制、失败整体回滚保留旧版、重试幂等。"""
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            corr = db.execute("SELECT * FROM corrections WHERE correction_id=?", (correction_id,)).fetchone()
            if not corr:
                raise DomainError("CORRECTION_NOT_FOUND", f"更正 {correction_id} 不存在")
            if corr["status"] == "PUBLISHED":
                # 发布中断后续跑：直接回放已存更正结果，不再记新版本。
                return json.loads(corr["snapshot"])
            if corr["base_version"] != expected_version:
                raise DomainError("STALE_VERSION",
                                  f"提交版本 v{expected_version} 已过期，更正基线为 v{corr['base_version']}")
            batch = db.execute("SELECT current_version, approved FROM batches WHERE batch_id=?",
                               (batch_id,)).fetchone()
            if not batch:
                raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")
            if batch["current_version"] != expected_version:
                raise DomainError("STALE_VERSION",
                                  f"批次已推进到 v{batch['current_version']}，拒绝基于 v{expected_version} 的发布")
            latest_row = db.execute(
                "SELECT data FROM batch_snapshots WHERE batch_id=? AND version=?",
                (batch_id, expected_version)).fetchone()
            latest = json.loads(latest_row["data"])
            changes = json.loads(corr["changes"])
            new_version = expected_version + 1
            new_snapshot = settle_fn(batch["approved"], apply_fn(latest["items"], changes),
                                     new_version, corr["reason_code"])
            stamp = now_fn()
            db.execute("INSERT INTO batch_snapshots VALUES (?,?,?,?,?)",
                       (batch_id, new_version, corr["reason_code"], _DUMPS(new_snapshot), stamp))
            db.execute("UPDATE corrections SET status='PUBLISHED', published_version=?, snapshot=? WHERE correction_id=?",
                       (new_version, _DUMPS(new_snapshot), correction_id))
            db.execute("UPDATE batches SET current_version=? WHERE batch_id=?", (new_version, batch_id))
            db.commit()
            return new_snapshot
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # -- 辅助 ---------------------------------------------------------------
    @staticmethod
    def _require_batch(db: sqlite3.Connection, batch_id: str) -> None:
        if not db.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone():
            raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")

    @staticmethod
    def _correction_row(row: sqlite3.Row) -> dict:
        return {
            "correction_id": row["correction_id"], "batch_id": row["batch_id"],
            "base_version": row["base_version"], "reason_code": row["reason_code"],
            "reason_text": row["reason_text"], "changes": json.loads(row["changes"]),
            "status": row["status"], "published_version": row["published_version"],
            "snapshot": json.loads(row["snapshot"]) if row["snapshot"] else None,
            "created_at": row["created_at"],
        }

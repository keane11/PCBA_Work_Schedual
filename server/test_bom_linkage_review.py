"""A BOM catalog mapping must be explicit; material checks track their evidence."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException


_import_temp = tempfile.TemporaryDirectory()
os.environ["PCBA_DB_PATH"] = str(Path(_import_temp.name) / "import.db")
from server import app as backend


class BomLinkageReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        backend.DB_PATH = Path(self.temp.name) / "pcba.db"
        backend.init_db()
        with backend.writing() as conn:
            user_id = conn.execute(
                "INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) "
                "VALUES(?,?,?,?,?,?)", ("warehouse", "仓管", "warehouse", "salt", "hash", backend.utcnow())
            ).lastrowid
            backend.import_bom_row(conn, {
                "product_code": "KIS-MOTHER", "bom_version": "V2", "source_line_no": "1",
                "material_code": "RES-1", "material_name": "电阻", "quantity_per": "2",
                "loss_rate": "0", "unit_price": "0.10",
            }, {"product_code", "bom_version", "source_line_no", "material_code", "material_name",
                "quantity_per", "loss_rate", "unit_price"}, "source-1", user_id, None)
        self.user = {"id": user_id, "role": "warehouse"}
        order = backend.create_order({"order_no": "SO-123", "customer": "客户 A", "project_name": "项目 A",
                                      "lines": [{"line_no": "1", "product_code": "CUSTOMER-SKU",
                                                 "product_name": "控制板", "quantity": "10"}]}, self.user)
        self.line_id = order["lines"][0]["id"]
        self.batch = backend.create_production_batch({"order_line_id": self.line_id,
                                                      "batch_no": "MP-1", "quantity": "10", "phase": "mp"}, self.user)

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_mapping_preserves_customer_product_code_and_locks_exact_catalog(self):
        detail = backend.get_bom_linkage(self.line_id, self.user)
        self.assertEqual(detail["status"], "unlinked")
        self.assertEqual(detail["catalogs"][0]["product_code"], "KIS-MOTHER")
        with self.assertRaises(HTTPException) as mismatch:
            backend.lock_bom_from_catalog(self.batch["id"], {"version": self.batch["version"],
                                                              "bom_version": "V2", "reason": "工程确认"}, self.user)
        self.assertEqual(mismatch.exception.status_code, 409)

        linked = backend.set_bom_linkage(self.line_id, {
            "version": detail["order_line"]["version"], "bom_product_code": "KIS-MOTHER",
            "bom_version": "V2", "reason": "工程确认母件编码不同",
        }, self.user)
        self.assertEqual(linked["status"], "catalog_ready")
        self.assertEqual(linked["order_line"]["product_code"], "CUSTOMER-SKU")
        self.assertEqual(linked["order_line"]["bom_product_code"], "KIS-MOTHER")
        summary = backend.list_bom_linkages(self.user)[0]
        self.assertEqual(summary["linked_product_code"], "KIS-MOTHER")
        self.assertEqual(summary["candidate_count"], 1)

        locked = backend.lock_bom_from_catalog(self.batch["id"], {
            "version": self.batch["version"], "bom_version": "V2", "reason": "核对 KIS BOM 后锁定",
        }, self.user)
        self.assertEqual(locked["bom_item_count"], 1)
        self.assertEqual(backend.get_bom_linkage(self.line_id, self.user)["status"], "locked")
        self.assertEqual(backend.get_production_batch(self.batch["id"], self.user)["bom_items"][0]["material_code"], "RES-1")
        with backend.writing() as conn:
            backend.import_bom_row(conn, {
                "product_code": "OTHER-MOTHER", "bom_version": "V3", "source_line_no": "1",
                "material_code": "RES-2", "quantity_per": "1", "loss_rate": "0",
            }, {"product_code", "bom_version", "source_line_no", "material_code", "quantity_per", "loss_rate"},
                "source-2", self.user["id"], None)
        with self.assertRaises(HTTPException) as remap:
            backend.set_bom_linkage(self.line_id, {
                "version": linked["order_line"]["version"], "bom_product_code": "OTHER-MOTHER",
                "bom_version": "V3", "reason": "试图改版",
            }, self.user)
        self.assertEqual(remap.exception.status_code, 409)

    def test_review_is_separate_from_readiness_and_becomes_stale_when_evidence_changes(self):
        line = backend.get_bom_linkage(self.line_id, self.user)["order_line"]
        backend.set_bom_linkage(self.line_id, {"version": line["version"],
                                                "bom_product_code": "KIS-MOTHER", "bom_version": "V2",
                                                "reason": "工程确认"}, self.user)
        backend.lock_bom_from_catalog(self.batch["id"], {"version": self.batch["version"],
                                                          "bom_version": "V2", "reason": "工程确认"}, self.user)
        materials = backend.get_batch_materials(self.batch["id"], self.user)
        item = materials["items"][0]
        self.assertEqual(item["review"]["status"], "unreviewed")
        self.assertEqual(materials["status"], "unknown")
        reviewed = backend.put_material_review({"production_batch_id": self.batch["id"],
                                               "material_code": "RES-1",
                                               "state_hash": item["review"]["state_hash"],
                                               "confirmed": True, "note": "逐项核对"}, self.user)
        self.assertEqual(reviewed["review"]["status"], "current")
        self.assertEqual(backend.get_batch_materials(self.batch["id"], self.user)["status"], "unknown")

        backend.create_inventory_snapshot({"as_of": "2026-09-29", "basis": "available",
                                           "items": [{"material_code": "RES-1", "quantity": "50"}]}, self.user)
        changed = backend.get_batch_materials(self.batch["id"], self.user)["items"][0]
        self.assertEqual(changed["review"]["status"], "stale")
        self.assertIn("库存口径或可用量已变化", changed["review"]["stale_reasons"])
        with self.assertRaises(HTTPException) as stale:
            backend.put_material_review({"production_batch_id": self.batch["id"],
                                         "material_code": "RES-1",
                                         "state_hash": item["review"]["state_hash"],
                                         "confirmed": True, "version": reviewed["review"]["version"]}, self.user)
        self.assertEqual(stale.exception.status_code, 409)
        refreshed = backend.put_material_review({"production_batch_id": self.batch["id"],
                                                "material_code": "RES-1",
                                                "state_hash": changed["review"]["state_hash"],
                                                "confirmed": True, "version": changed["review"]["version"]}, self.user)
        self.assertEqual(refreshed["review"]["status"], "current")
        backend.put_reservation({"production_batch_id": self.batch["id"],
                                 "material_code": "RES-1", "quantity": "10"}, self.user)
        changed_again = backend.get_batch_materials(self.batch["id"], self.user)["items"][0]
        self.assertEqual(changed_again["review"]["status"], "stale")
        self.assertIn("预留或发料数量已变化", changed_again["review"]["stale_reasons"])
        all_materials = backend.all_materials(self.user)[0]
        self.assertEqual(all_materials["project_name"], "项目 A")
        self.assertEqual(all_materials["bom_version"], "V2")
        self.assertEqual(all_materials["items"][0]["review"]["status"], "stale")
        self.assertEqual(backend.request_permission("PUT", "/api/material-reviews"), "materials_edit")

    def test_different_phase_can_lock_another_version_of_same_mother_code(self):
        pp = backend.create_production_batch({"order_line_id": self.line_id,
                                              "batch_no": "PP-1", "quantity": "10", "phase": "pp"}, self.user)
        with backend.writing() as conn:
            backend.import_bom_row(conn, {
                "product_code": "KIS-MOTHER", "bom_version": "V3", "source_line_no": "1",
                "material_code": "RES-3", "quantity_per": "3", "loss_rate": "0",
            }, {"product_code", "bom_version", "source_line_no", "material_code", "quantity_per", "loss_rate"},
                "source-v3", self.user["id"], None)
        line = backend.get_bom_linkage(self.line_id, self.user)["order_line"]
        backend.set_bom_linkage(self.line_id, {"version": line["version"],
                                                "bom_product_code": "KIS-MOTHER", "bom_version": "V2",
                                                "reason": "工程确认母件"}, self.user)
        backend.lock_bom_from_catalog(self.batch["id"], {"version": self.batch["version"],
                                                          "bom_version": "V2", "reason": "MP 版本"}, self.user)
        backend.lock_bom_from_catalog(pp["id"], {"version": pp["version"],
                                                 "bom_version": "V3", "reason": "PP 版本"}, self.user)
        self.assertEqual(backend.get_production_batch(pp["id"], self.user)["bom_items"][0]["material_code"], "RES-3")
        self.assertEqual(backend.get_bom_linkage(self.line_id, self.user)["locked_batch_count"], 2)

    def test_existing_order_line_is_migrated_without_changing_customer_code(self):
        old_db = Path(self.temp.name) / "old.db"
        with closing(sqlite3.connect(old_db)) as conn, conn:
            conn.execute("""CREATE TABLE order_lines (
                         id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL,line_no TEXT NOT NULL,
                         product_code TEXT NOT NULL,product_name TEXT NOT NULL,quantity TEXT NOT NULL,
                         bom_version TEXT,version INTEGER NOT NULL DEFAULT 1,
                         created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
            conn.execute("INSERT INTO order_lines VALUES (1,1,'1','CUSTOMER-SKU','控制板','10','V1',1,'old','old')")
        backend.DB_PATH = old_db
        backend.init_db()
        with backend.reading() as conn:
            line = backend.must_get(conn, "order_lines", 1)
        self.assertEqual(line["product_code"], "CUSTOMER-SKU")
        self.assertEqual(line["bom_version"], "V1")
        self.assertIsNone(line["bom_product_code"])

    def test_legacy_temporary_project_can_map_catalog_without_rewriting_locked_snapshot(self):
        legacy = backend.create_order({"order_no": "PCBA-TEMP-1", "customer": "待补录",
                                       "lines": [{"line_no": "1", "product_code": "PCBA-TEMP-1",
                                                  "product_name": "旧产品", "quantity": "10"}]}, self.user)
        old_line = legacy["lines"][0]
        old_batch = backend.create_production_batch({"order_line_id": old_line["id"],
                                                     "batch_no": "旧批次", "quantity": "10", "phase": "mp",
                                                     "bom_version": "OLD", "bom_items": [
                                                         {"material_code": "OLD-R", "quantity_per": "1"},
                                                     ]}, self.user)
        before = backend.get_production_batch(old_batch["id"], self.user)
        linked = backend.set_bom_linkage(old_line["id"], {
            "version": old_line["version"] + 1, "bom_product_code": "KIS-MOTHER",
            "bom_version": "V2", "reason": "补录 KIS 母件编码；旧批次快照保留",
        }, self.user)
        self.assertEqual(linked["order_line"]["product_code"], "PCBA-TEMP-1")
        self.assertEqual(linked["order_line"]["bom_product_code"], "KIS-MOTHER")
        self.assertEqual(linked["locked_version_mismatch_count"], 1)
        after = backend.get_production_batch(old_batch["id"], self.user)
        self.assertEqual(after["bom_version"], before["bom_version"])
        self.assertEqual(after["bom_items"], before["bom_items"])
        with backend.reading() as conn:
            event = backend.one(conn, "SELECT * FROM audit WHERE entity_type='order_lines' AND entity_id=? AND action='bom_link_legacy'",
                                (old_line["id"],))
        self.assertEqual(event["reason"], "补录 KIS 母件编码；旧批次快照保留")

        with backend.writing() as conn:
            backend.import_bom_row(conn, {
                "product_code": "OTHER-MOTHER", "bom_version": "V3", "source_line_no": "1",
                "material_code": "OTHER-R", "quantity_per": "1", "loss_rate": "0",
            }, {"product_code", "bom_version", "source_line_no", "material_code", "quantity_per", "loss_rate"},
                "legacy-other", self.user["id"], None)
        with self.assertRaises(HTTPException) as remap:
            backend.set_bom_linkage(old_line["id"], {
                "version": linked["order_line"]["version"], "bom_product_code": "OTHER-MOTHER",
                "bom_version": "V3", "reason": "二次更改",
            }, self.user)
        self.assertEqual(remap.exception.status_code, 409)

    def test_stage_progress_counts_completed_and_skips_in_one_batch_summary(self):
        initial = next(item for item in backend.get_stage_progress(self.user)
                       if item["production_batch_id"] == self.batch["id"])
        stages = backend.get_stages(self.batch["id"], self.user)
        self.assertEqual(initial["total"], len(stages))
        self.assertEqual(initial["completed"], 0)
        self.assertEqual(initial["current_name"], stages[0]["name"])
        self.assertEqual(initial["current_status"], "pending")
        with backend.writing() as conn:
            conn.execute("UPDATE stages SET status='done',actual_finish='2026-09-29' WHERE id=?", (stages[0]["id"],))
            conn.execute("UPDATE stages SET status='skipped' WHERE id=?", (stages[1]["id"],))
        progressed = next(item for item in backend.get_stage_progress(self.user)
                          if item["production_batch_id"] == self.batch["id"])
        self.assertEqual(progressed["total"], len(stages) - 1)
        self.assertEqual(progressed["completed"], 1)
        self.assertEqual(progressed["current_name"], stages[2]["name"])


if __name__ == "__main__":
    unittest.main()

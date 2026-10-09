"""BOM prices are snapshots, and incomplete prices must not look like a total."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException


_import_temp = tempfile.TemporaryDirectory()
os.environ["PCBA_DB_PATH"] = str(Path(_import_temp.name) / "import.db")
from server import app as backend
from server.kis_import import preview_import


class BomPricingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        backend.DB_PATH = Path(self.temp.name) / "pcba.db"
        backend.init_db()
        with backend.writing() as conn:
            user_id = conn.execute(
                "INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) "
                "VALUES(?,?,?,?,?,?)", ("admin", "管理员", "admin", "salt", "hash", backend.utcnow())
            ).lastrowid
        self.user = {"id": user_id, "role": "admin"}
        order = backend.create_order({"order_no": "SO-PRICE", "customer": "客户 A",
                                      "lines": [{"line_no": "1", "product_code": "PCB-A",
                                                 "product_name": "控制板", "quantity": "20"}]}, self.user)
        self.line_id = order["lines"][0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_batch_total_uses_whole_piece_demand_and_requires_every_price(self):
        batch = backend.create_production_batch({
            "order_line_id": self.line_id, "batch_no": "MP-1", "quantity": "10", "phase": "mp",
            "bom_version": "V1", "bom_items": [
                {"material_code": "R1", "quantity_per": "1", "loss_rate": "0.1", "unit_price": "0.333"},
                {"material_code": "C1", "quantity_per": "0.5", "loss_rate": "0"},
            ],
        }, self.user)
        detail = backend.get_production_batch(batch["id"], self.user)
        pricing = detail["bom_pricing"]
        self.assertEqual(pricing["currency"], "CNY")
        self.assertEqual(pricing["priced_subtotal"], "3.66")
        self.assertIsNone(pricing["total"])
        self.assertEqual(pricing["missing_price_count"], 1)
        self.assertEqual([line["required_quantity"] for line in pricing["lines"]], ["11", "5"])
        material = next(item for item in backend.get_batch_materials(batch["id"], self.user)["items"]
                        if item["material_code"] == "C1")
        self.assertEqual(material["required"], "5")

        missing = next(item for item in detail["bom_items"] if item["material_code"] == "C1")
        updated = backend.patch_batch_bom_prices({
            "production_batch_id": batch["id"], "reason": "采购报价确认",
            "items": [{"id": missing["id"], "version": missing["version"], "unit_price": "0.02"}],
        }, self.user)
        self.assertEqual(updated["bom_pricing"]["total"], "3.76")
        self.assertEqual(backend.get_production_batch(batch["id"], self.user)["bom_pricing"]["total"], "3.76")

    def test_catalog_price_is_copied_into_batch_but_future_changes_stay_in_catalog(self):
        self.assertEqual(backend.request_permission("PATCH", "/api/bom-catalog/prices"), "materials_edit")
        self.assertEqual(backend.request_permission("PATCH", "/api/bom-items/prices"), "materials_edit")
        with backend.writing() as conn:
            for index, material in enumerate(("R1", "C1"), 1):
                backend.import_bom_row(conn, {
                    "product_code": "PCB-A", "bom_version": "V1", "source_line_no": str(index),
                    "material_code": material, "quantity_per": "2", "loss_rate": "0",
                    "unit_price": None,
                }, {"product_code", "bom_version", "source_line_no", "material_code", "quantity_per", "loss_rate"},
                    f"price-{index}", self.user["id"], None)
        catalog = backend.get_bom_catalog("PCB-A", "V1", self.user)
        self.assertIsNone(backend.get_bom_catalog_summary("PCB-A", "V1", "10", self.user)["total"])
        updated = backend.patch_bom_catalog_prices({
            "quantity": "10", "reason": "供应商报价单",
            "items": [{"id": row["id"], "version": row["version"], "unit_price": "0.15"}
                      for row in catalog],
        }, self.user)
        self.assertEqual(updated["bom_pricing"]["total"], "6")
        batch = backend.create_production_batch({
            "order_line_id": self.line_id, "batch_no": "MP-1", "quantity": "10", "phase": "mp",
            "bom_version": "V1",
        }, self.user)
        self.assertEqual(backend.get_production_batch(batch["id"], self.user)["bom_pricing"]["total"], "6")
        newest = updated["items"][0]
        backend.patch_bom_catalog_item(newest["id"], {
            "version": newest["version"], "changes": {"unit_price": "0.25"},
            "reason": "新报价",
        }, self.user)
        self.assertEqual(backend.get_bom_catalog_summary("PCB-A", "V1", "10", self.user)["total"], "8")
        self.assertEqual(backend.get_production_batch(batch["id"], self.user)["bom_pricing"]["total"], "6")

    def test_bulk_price_update_rolls_back_on_stale_version(self):
        batch = backend.create_production_batch({
            "order_line_id": self.line_id, "batch_no": "MP-1", "quantity": "10", "phase": "mp",
            "bom_version": "V1", "bom_items": [
                {"material_code": "R1", "quantity_per": "1", "unit_price": "1"},
                {"material_code": "C1", "quantity_per": "1", "unit_price": "1"},
            ],
        }, self.user)
        before = backend.get_production_batch(batch["id"], self.user)
        first, second = before["bom_items"]
        with self.assertRaises(HTTPException) as failure:
            backend.patch_batch_bom_prices({
                "production_batch_id": batch["id"], "reason": "复核报价",
                "items": [{"id": first["id"], "version": first["version"], "unit_price": "2"},
                          {"id": second["id"], "version": second["version"] + 1, "unit_price": "2"}],
            }, self.user)
        self.assertEqual(failure.exception.status_code, 409)
        after = backend.get_production_batch(batch["id"], self.user)
        self.assertEqual(after["bom_pricing"]["total"], before["bom_pricing"]["total"])
        self.assertEqual(after["bom_items"][0]["unit_price"], "1")
        self.assertEqual(after["bom_items"][0]["version"], first["version"])

    def test_import_preview_recognizes_and_validates_unit_price(self):
        header = "product_code,bom_version,source_line_no,material_code,quantity_per,unit_price\n"
        good = preview_import("bom", "bom.csv", (header + "PCB-A,V1,1,R1,2,0.00000001\n").encode(), {})
        self.assertFalse(good["errors"])
        self.assertEqual(good["rows"][0]["values"]["unit_price"], "0.00000001")
        bad = preview_import("bom", "bom.csv", (header + "PCB-A,V1,1,R1,2,0.000000001\n").encode(), {})
        self.assertTrue(any(issue["field"] == "unit_price" for issue in bad["errors"]))

    def test_existing_database_adds_nullable_prices_without_changing_old_rows(self):
        old_db = Path(self.temp.name) / "old.db"
        with closing(sqlite3.connect(old_db)) as conn, conn:
            conn.execute("CREATE TABLE bom_items (id INTEGER PRIMARY KEY, production_batch_id INTEGER, "
                         "source_line_no TEXT, material_code TEXT, material_name TEXT, quantity_per TEXT, "
                         "loss_rate TEXT, version INTEGER)")
            conn.execute("CREATE TABLE bom_catalog (id INTEGER PRIMARY KEY, product_code TEXT, "
                         "bom_version TEXT, source_line_no TEXT, material_code TEXT, material_name TEXT, "
                         "quantity_per TEXT, loss_rate TEXT, source_key TEXT, version INTEGER)")
            conn.execute("INSERT INTO bom_catalog VALUES (1,'PCB-A','V1','1','R1','电阻','2','0','source-1',1)")
        backend.DB_PATH = old_db
        backend.init_db()
        with backend.reading() as conn:
            old = backend.one(conn, "SELECT material_code,quantity_per,unit_price FROM bom_catalog WHERE id=1")
            item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(bom_items)")}
        self.assertEqual(old, {"material_code": "R1", "quantity_per": "2", "unit_price": None})
        self.assertIn("unit_price", item_columns)


if __name__ == "__main__":
    unittest.main()

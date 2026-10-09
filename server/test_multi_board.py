"""One finished product may require several PCBA boards without multiplying delivery."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException


_import_temp = tempfile.TemporaryDirectory()
os.environ["PCBA_DB_PATH"] = str(Path(_import_temp.name) / "import.db")
from server import app as backend


class MultiBoardTests(unittest.TestCase):
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
        self.order = backend.create_order({
            "order_no": "SO-TAV3000", "customer": "客户", "project_name": "TAV3000",
            "lines": [{"line_no": "1", "product_code": "TAV3000", "product_name": "整机", "quantity": "1000"}],
        }, self.user)
        self.line_id = self.order["lines"][0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def catalog(self, code: str, rows: list[tuple[str, str, str, str]]):
        with backend.writing() as conn:
            for source_no, material, quantity, price in rows:
                backend.import_bom_row(conn, {
                    "product_code": code, "bom_version": "V1", "source_line_no": source_no,
                    "material_code": material, "material_name": material,
                    "quantity_per": quantity, "loss_rate": "0", "unit_price": price,
                }, {"product_code", "bom_version", "source_line_no", "material_code",
                    "material_name", "quantity_per", "loss_rate", "unit_price"},
                    f"{code}:{source_no}", self.user["id"], None)

    def add_board(self, ref: str, name: str, catalog_code: str, ratio: str = "1"):
        line = backend.get_order_line_boards(self.line_id, self.user)["order_line"]
        return backend.add_order_line_board(self.line_id, {
            "version": line["version"], "board_ref": ref, "board_name": name,
            "bom_product_code": catalog_code, "quantity_per_product": ratio,
            "default_bom_version": "V1", "reason": "工程确认产品板卡组成",
        }, self.user)

    def confirm(self):
        line = backend.get_order_line_boards(self.line_id, self.user)["order_line"]
        return backend.confirm_order_line_boards(self.line_id, {
            "version": line["version"], "reason": "工程确认两板及每套用量",
        }, self.user)

    def lock(self, batch_id: int, board_ref: str):
        current = backend.get_batch_boards(batch_id, self.user)
        board = next(row for row in current["boards"] if row["board_ref"] == board_ref)
        return backend.lock_batch_board_from_catalog(batch_id, board["id"], {
            "version": board["version"], "batch_version": current["batch"]["version"],
            "bom_version": "V1", "reason": f"工程确认 {board_ref} V1",
        }, self.user)

    def configure_two_boards(self):
        self.catalog("TAV3000-MAIN", [("1", "SHARED-R", "2", "0.10"),
                                      ("2", "MAIN-IC", "1", "5")])
        self.catalog("TAV3000-KEY", [("1", "SHARED-R", "1", "0.10"),
                                     ("2", "KEY-SW", "3", "0.20")])
        self.add_board("MAIN", "主板", "TAV3000-MAIN")
        self.add_board("KEY", "按键板", "TAV3000-KEY", "2")
        self.confirm()

    def test_two_board_snapshot_aggregates_shared_material_and_cost_without_duplicate_delivery(self):
        self.configure_two_boards()
        batch = backend.create_production_batch({
            "order_line_id": self.line_id, "batch_no": "MP-1", "quantity": "1000", "phase": "mp",
            "vendor": "外协厂", "schedule_start": "2099-01-01",
            "commit_start": "2099-02-01", "commit_finish": "2099-12-31",
        }, self.user)
        self.assertEqual(batch["bom_scope"], "multi")
        self.assertEqual(len(backend.get_batch_boards(batch["id"], self.user)["boards"]), 2)
        delivery = backend.create_delivery_batch({
            "order_line_id": self.line_id, "batch_no": "D-1", "quantity": "1000",
            "due_date": "2099-12-31", "promised_date": "2099-12-31",
        }, self.user)
        backend.create_allocation({"production_batch_id": batch["id"],
                                   "delivery_batch_id": delivery["id"], "quantity": "1000"}, self.user)

        partial = self.lock(batch["id"], "MAIN")
        self.assertEqual(partial["batch"]["bom_locked"], 0)
        partial_materials = backend.get_batch_materials(batch["id"], self.user)
        self.assertEqual(partial_materials["status"], "bom_unlocked")
        self.assertEqual({item["material_code"] for item in partial_materials["items"]}, {"SHARED-R", "MAIN-IC"})
        partial_detail = backend.get_production_batch(batch["id"], self.user)
        self.assertIsNone(partial_detail["bom_pricing"]["total"])
        self.assertEqual(partial_detail["bom_pricing"]["missing_board_count"], 1)
        self.assertEqual(partial_detail["forecast"]["status"], "unknown")
        task = next(item for item in backend.get_tasks(None, self.user)
                    if item["task_key"] == f"bom:{batch['id']}")
        self.assertIn("按键板", task["title"])
        assessment = backend.overview(self.user)["assessment"][0]
        self.assertTrue(any("按键板" in issue["label"] for issue in assessment["missing_reasons"]))

        complete = self.lock(batch["id"], "KEY")
        self.assertEqual(complete["batch"]["bom_locked"], 1)
        materials = backend.get_batch_materials(batch["id"], self.user)["items"]
        shared = next(item for item in materials if item["material_code"] == "SHARED-R")
        self.assertEqual(shared["required"], "4000")
        self.assertEqual(len(shared["bom_lines"]), 2)
        self.assertEqual({part["board_ref"] for part in shared["bom_lines"]}, {"MAIN", "KEY"})
        self.assertEqual({part["catalog_source_line_no"] for part in shared["bom_lines"]}, {"1"})
        self.assertEqual(len({part["source_line_no"] for part in shared["bom_lines"]}), 2)
        detail = backend.get_production_batch(batch["id"], self.user)
        self.assertEqual(detail["bom_pricing"]["total"], "6600")
        self.assertEqual({part["board_ref"] for part in detail["bom_pricing"]["lines"]}, {"MAIN", "KEY"})
        self.assertEqual(backend.get_order(self.order["id"], self.user)["total_quantity"], "1000")
        self.assertEqual(sum(float(row["quantity"]) for row in detail["allocations"]), 1000)

        backend.create_inventory_snapshot({"as_of": backend.today(), "basis": "available",
                                           "items": [{"material_code": item["material_code"],
                                                      "quantity": item["required"]} for item in materials]}, self.user)
        for item in materials:
            backend.put_reservation({"production_batch_id": batch["id"],
                                     "material_code": item["material_code"],
                                     "quantity": item["required"]}, self.user)
        self.assertEqual(backend.get_batch_materials(batch["id"], self.user)["status"], "ready")
        self.assertEqual(backend.get_production_batch(batch["id"], self.user)["forecast"]["status"], "forecast")

    def test_board_definition_write_permission_requires_orders_edit(self):
        for method in ("PATCH", "DELETE"):
            permission = backend.request_permission(method, "/api/order-line-boards/1")
            self.assertEqual(permission, "orders_edit")
            with self.assertRaises(HTTPException) as denied:
                backend.require_permission({"role": "business", "permissions_json": '{"orders_edit":false}'},
                                           permission)
            self.assertEqual(denied.exception.status_code, 403)

    def test_fractional_board_or_batch_quantities_are_rejected(self):
        with self.assertRaises(HTTPException) as fractional_board:
            self.add_board("MAIN", "主板", "TAV3000-MAIN", "0.5")
        self.assertEqual(fractional_board.exception.status_code, 422)
        self.add_board("MAIN", "主板", "TAV3000-MAIN", "1")
        self.confirm()
        with self.assertRaises(HTTPException) as fractional_batch:
            backend.create_production_batch({"order_line_id": self.line_id,
                                             "batch_no": "MP-HALF", "quantity": "0.5", "phase": "mp"}, self.user)
        self.assertEqual(fractional_batch.exception.status_code, 422)

    def test_old_unlocked_single_batch_must_activate_after_project_adds_boards(self):
        batch = backend.create_production_batch({"order_line_id": self.line_id,
                                                 "batch_no": "MP-OLD", "quantity": "1000", "phase": "mp"}, self.user)
        self.add_board("MAIN", "主板", "TAV3000-MAIN")
        self.confirm()
        with self.assertRaises(HTTPException) as single_lock:
            backend.lock_bom(batch["id"], {"version": batch["version"], "bom_version": "V1",
                                              "items": [{"material_code": "R1", "quantity_per": "1"}]}, self.user)
        self.assertEqual(single_lock.exception.status_code, 409)
        activated = backend.activate_batch_boards(batch["id"], {
            "version": batch["version"], "reason": "按已确认板卡组成启用旧批次"}, self.user)
        self.assertEqual(activated["batch"]["bom_scope"], "multi")
        self.assertEqual(len(activated["boards"]), 1)

    def test_composition_revision_keeps_locked_batch_snapshot_and_updates_new_batch(self):
        self.configure_two_boards()
        first = backend.create_production_batch({"order_line_id": self.line_id,
                                                  "batch_no": "MP-1", "quantity": "1000", "phase": "mp"}, self.user)
        self.lock(first["id"], "MAIN")
        self.lock(first["id"], "KEY")
        first_revision = first["board_set_revision"]
        self.catalog("TAV3000-LED", [("1", "LED", "1", "1")])
        self.add_board("LED", "灯板", "TAV3000-LED")
        overview = backend.board_overview(self.user)
        self.assertGreater(overview["order_lines"][str(self.line_id)]["board_set_revision"], first_revision)
        first_view = overview["production_batches"][str(first["id"])]
        self.assertEqual(first_view["board_set_revision"], first_revision)
        self.assertEqual(first_view["board_count"], 2)
        self.assertEqual(first_view["locked_board_count"], 2)
        with self.assertRaises(HTTPException) as old_lock:
            backend.activate_batch_boards(first["id"], {
                "version": backend.get_batch_boards(first["id"], self.user)["batch"]["version"],
                "reason": "试图覆盖旧版"}, self.user)
        self.assertEqual(old_lock.exception.status_code, 409)
        self.confirm()
        second = backend.create_production_batch({"order_line_id": self.line_id,
                                                   "batch_no": "PP-1", "quantity": "1000", "phase": "pp"}, self.user)
        self.assertEqual(len(backend.get_batch_boards(second["id"], self.user)["boards"]), 3)

    def test_old_single_bom_batch_is_not_auto_converted(self):
        legacy = backend.create_production_batch({
            "order_line_id": self.line_id, "batch_no": "MP-legacy", "quantity": "1000", "phase": "mp",
            "bom_version": "OLD", "bom_items": [{"material_code": "OLD-C", "quantity_per": "1"}],
        }, self.user)
        self.assertEqual(legacy["bom_scope"], "single")
        self.add_board("MAIN", "主板", "TAV3000-MAIN")
        self.assertEqual(backend.get_batch_boards(legacy["id"], self.user)["boards"], [])
        self.assertEqual(backend.get_production_batch(legacy["id"], self.user)["bom_items"][0]["material_code"], "OLD-C")
        self.confirm()
        with self.assertRaises(HTTPException) as overwrite:
            backend.activate_batch_boards(legacy["id"], {
                "version": legacy["version"], "reason": "不可覆盖旧快照"}, self.user)
        self.assertEqual(overwrite.exception.status_code, 409)


class MultiBoardMigrationTests(unittest.TestCase):
    def test_old_rows_keep_quantity_and_bom_without_inferred_boards(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.db"
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                    CREATE TABLE orders(id INTEGER PRIMARY KEY,order_no TEXT,customer TEXT,status TEXT,
                                        version INTEGER,created_at TEXT,updated_at TEXT);
                    CREATE TABLE order_lines(id INTEGER PRIMARY KEY,order_id INTEGER,line_no TEXT,
                                             product_code TEXT,product_name TEXT,quantity TEXT,bom_version TEXT,
                                             version INTEGER,created_at TEXT,updated_at TEXT);
                    CREATE TABLE production_batches(id INTEGER PRIMARY KEY,order_line_id INTEGER,batch_no TEXT,
                        quantity TEXT,vendor TEXT,commit_start TEXT,commit_finish TEXT,phase TEXT,phase_source TEXT,
                        phase_order_no TEXT,schedule_start TEXT,schedule_mode TEXT,target_kind TEXT,target_date TEXT,
                        sampling_order_date TEXT,board_eta TEXT,board_received_at TEXT,debug_accepted_at TEXT,
                        vendor_confirmed_at TEXT,bom_version TEXT,bom_locked INTEGER,material_ready_at TEXT,
                        conflict_ack_reason TEXT,conflict_ack_by INTEGER,conflict_ack_at TEXT,version INTEGER,
                        created_at TEXT,updated_at TEXT);
                    CREATE TABLE bom_items(id INTEGER PRIMARY KEY,production_batch_id INTEGER,
                        source_line_no TEXT,material_code TEXT,material_name TEXT,quantity_per TEXT,
                        loss_rate TEXT,unit_price TEXT,version INTEGER);
                    INSERT INTO orders VALUES(1,'SO-OLD','客户','open',1,'old','old');
                    INSERT INTO order_lines VALUES(1,1,'1','TAV3000','整机','1000','OLD',1,'old','old');
                    INSERT INTO production_batches(id,order_line_id,batch_no,quantity,phase,phase_source,
                        schedule_mode,bom_version,bom_locked,version,created_at,updated_at)
                        VALUES(1,1,'MP-OLD','1000','mp','manual','workday','OLD',1,1,'old','old');
                    INSERT INTO bom_items VALUES(1,1,'1','R1','电阻','2','0','0.1',1);
                """)
            backend.DB_PATH = path
            backend.init_db()
            with backend.reading() as conn:
                self.assertEqual(backend.one(conn, "SELECT quantity FROM order_lines WHERE id=1")["quantity"], "1000")
                self.assertEqual(backend.one(conn, "SELECT bom_scope,bom_locked FROM production_batches WHERE id=1"),
                                 {"bom_scope": "single", "bom_locked": 1})
                self.assertEqual(backend.one(conn, "SELECT board_schedule_mode FROM order_lines WHERE id=1"),
                                 {"board_schedule_mode": "shared"})
                self.assertEqual(backend.one(conn, "SELECT board_schedule_mode FROM production_batches WHERE id=1"),
                                 {"board_schedule_mode": "shared"})
                self.assertEqual(backend.one(conn, "SELECT material_code,batch_board_id FROM bom_items WHERE id=1"),
                                 {"material_code": "R1", "batch_board_id": None})
                self.assertEqual(backend.rows(conn, "SELECT * FROM order_line_boards"), [])
                self.assertEqual(backend.rows(conn, "SELECT * FROM batch_boards"), [])


if __name__ == "__main__":
    unittest.main()

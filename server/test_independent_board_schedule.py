"""Independent board schedules remain tied to one finished-product delivery."""

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


class IndependentBoardScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        backend.DB_PATH = Path(self.temp.name) / "pcba.db"
        backend.init_db()
        with backend.writing() as conn:
            user_id = conn.execute(
                "INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                ("admin", "管理员", "admin", "salt", "hash", backend.utcnow())).lastrowid
        self.user = {"id": user_id, "role": "admin"}
        self.order = backend.create_order({
            "order_no": "SO-TAV", "customer": "客户", "project_name": "TAV",
            "lines": [{"line_no": "1", "product_code": "TAV", "product_name": "整机", "quantity": "1000"}],
        }, self.user)
        self.line_id = self.order["lines"][0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def catalog(self, product: str, material: str, quantity: str, price: str):
        with backend.writing() as conn:
            backend.import_bom_row(conn, {
                "product_code": product, "bom_version": "V1", "source_line_no": "1",
                "material_code": material, "material_name": material,
                "quantity_per": quantity, "loss_rate": "0", "unit_price": price,
            }, {"product_code", "bom_version", "source_line_no", "material_code",
                "material_name", "quantity_per", "loss_rate", "unit_price"},
                f"{product}:1", self.user["id"], None)

    def configure(self):
        self.catalog("MAIN", "COMMON", "2", "0.10")
        self.catalog("KEY", "COMMON", "1", "0.10")
        for ref in ("MAIN", "KEY"):
            line = backend.get_order_line_boards(self.line_id, self.user)["order_line"]
            backend.add_order_line_board(self.line_id, {
                "version": line["version"], "board_ref": ref,
                "board_name": "主板" if ref == "MAIN" else "按键板",
                "bom_product_code": ref, "quantity_per_product": "1",
                "default_bom_version": "V1", "reason": "工程确认板卡组成",
            }, self.user)
        line = backend.get_order_line_boards(self.line_id, self.user)["order_line"]
        backend.confirm_order_line_boards(self.line_id, {"version": line["version"], "reason": "工程确认完整组成"}, self.user)
        line = backend.get_order_line_boards(self.line_id, self.user)["order_line"]
        backend.patch_board_schedule_mode(self.line_id, {"version": line["version"],
                                          "mode": "independent", "reason": "两板需分别排产"}, self.user)

    def create_batch(self, phase="mp", number="MP-1"):
        return backend.create_production_batch({"order_line_id": self.line_id, "batch_no": number,
                                                "quantity": "1000", "phase": phase}, self.user)

    def boards(self, batch_id):
        return {board["board_ref"]: board for board in backend.get_batch_boards(batch_id, self.user)["boards"]}

    def lock_both(self, batch_id):
        for ref in ("MAIN", "KEY"):
            current = backend.get_batch_boards(batch_id, self.user)
            board = next(item for item in current["boards"] if item["board_ref"] == ref)
            backend.lock_batch_board_from_catalog(batch_id, board["id"], {
                "version": board["version"], "batch_version": current["batch"]["version"],
                "bom_version": "V1", "reason": "工程确认目录快照",
            }, self.user)

    def patch_board(self, board, changes):
        return backend.patch_batch_board(board["id"], {"version": board["version"],
                                                      "changes": changes, "reason": "外协与物料重新确认"}, self.user)

    def test_mode_snapshot_and_board_nodes_do_not_inherit_parent_progress(self):
        self.configure()
        batch = self.create_batch()
        self.assertEqual(batch["board_schedule_mode"], "independent")
        boards = self.boards(batch["id"])
        self.assertEqual({board["planned_quantity"] for board in boards.values()}, {"1000"})
        self.assertTrue(backend.get_batch_board(boards["MAIN"]["id"], self.user)["stages"])
        line = backend.get_order_line_boards(self.line_id, self.user)["order_line"]
        backend.patch_board_schedule_mode(self.line_id, {"version": line["version"],
                                          "mode": "shared", "reason": "下阶段改为共用排期"}, self.user)
        self.assertEqual(backend.get_batch_boards(batch["id"], self.user)["batch"]["board_schedule_mode"], "independent")
        next_batch = self.create_batch("pp", "PP-1")
        self.assertEqual(next_batch["board_schedule_mode"], "shared")
        self.assertEqual(backend.board_overview(self.user)["order_lines"][str(self.line_id)]["board_schedule_mode"], "shared")
        backend.patch_production_batch(next_batch["id"], {"version": next_batch["version"],
                                                            "changes": {"quantity": "900"},
                                                            "reason": "调整共用批次成品生产数量"}, self.user)
        self.assertEqual({item["planned_quantity"] for item in self.boards(next_batch["id"]).values()}, {"900"})
        refresh = backend.activate_batch_boards(batch["id"], {
            "version": backend.get_batch_boards(batch["id"], self.user)["batch"]["version"],
            "reason": "旧批次尚未锁 BOM，显式改按项目共用排期"}, self.user)
        self.assertEqual(refresh["batch"]["board_schedule_mode"], "shared")
        with backend.reading() as conn:
            self.assertEqual(backend.rows(conn, "SELECT * FROM board_stages WHERE batch_board_id IN "
                                                "(SELECT id FROM batch_boards WHERE production_batch_id=?)",
                                          (batch["id"],)), [])

    def test_board_quantity_changes_material_demand_price_and_review(self):
        self.configure()
        batch = self.create_batch()
        self.lock_both(batch["id"])
        boards = self.boards(batch["id"])
        with self.assertRaises(HTTPException) as fractional:
            self.patch_board(boards["KEY"], {"planned_quantity": "0.5"})
        self.assertEqual(fractional.exception.status_code, 422)
        self.patch_board(boards["KEY"], {"planned_quantity": "1200"})
        materials = backend.get_batch_materials(batch["id"], self.user)
        self.assertEqual(len(materials["items"]), 1)
        self.assertEqual(materials["items"][0]["required"], "3200")
        self.assertEqual(len(materials["items"][0]["bom_lines"]), 2)
        self.assertEqual(backend.get_production_batch(batch["id"], self.user)["bom_pricing"]["total"], "320")
        backend.create_inventory_snapshot({"as_of": backend.today(), "basis": "available",
                                           "items": [{"material_code": "COMMON", "quantity": "4000"}]}, self.user)
        backend.put_reservation({"production_batch_id": batch["id"], "material_code": "COMMON",
                                 "quantity": "3200"}, self.user)
        before = backend.get_batch_materials(batch["id"], self.user)["items"][0]
        backend.put_material_review({"production_batch_id": batch["id"], "material_code": "COMMON",
                                     "confirmed": True, "state_hash": before["review"]["state_hash"],
                                     "note": "核对完成"}, self.user)
        key = self.boards(batch["id"])["KEY"]
        self.patch_board(key, {"planned_quantity": "1201"})
        after = backend.get_batch_materials(batch["id"], self.user)["items"][0]
        self.assertEqual(after["required"], "3201")
        self.assertEqual(after["review"]["status"], "stale")
        self.assertEqual(after["review"]["stale_reasons"], ["BOM 或生产数量已变化", "需求缺口或预计齐套日已变化"])
        key = self.boards(batch["id"])["KEY"]
        self.patch_board(key, {"planned_quantity": "999"})
        forecast = backend.get_production_batch(batch["id"], self.user)["forecast"]
        self.assertEqual(forecast["status"], "unknown")
        self.assertIn("按键板", forecast["reason"])

    def test_two_vendors_later_board_sets_forecast_and_actual_finish_gate(self):
        self.configure()
        batch = self.create_batch()
        self.lock_both(batch["id"])
        missing_dates = backend.get_production_batch(batch["id"], self.user)["forecast"]
        self.assertEqual(missing_dates["status"], "unknown")
        self.assertIn("主板", missing_dates["reason"])
        self.assertIn("按键板", missing_dates["reason"])
        for ref, vendor, finish in (("MAIN", "板厂甲", "2099-12-01"),
                                    ("KEY", "板厂乙", "2099-12-10")):
            board = self.boards(batch["id"])[ref]
            self.patch_board(board, {"vendor": vendor, "commit_start": "2099-01-03", "commit_finish": finish})
        backend.create_inventory_snapshot({"as_of": backend.today(), "basis": "available",
                                           "items": [{"material_code": "COMMON", "quantity": "3000"}]}, self.user)
        backend.put_reservation({"production_batch_id": batch["id"], "material_code": "COMMON",
                                 "quantity": "3000"}, self.user)
        forecast = backend.get_production_batch(batch["id"], self.user)["forecast"]
        self.assertEqual(forecast["status"], "forecast")
        self.assertEqual(forecast["formal_date"], "2099-12-10")
        main = self.boards(batch["id"])["MAIN"]
        backend.complete_batch_board(main["id"], {"version": main["version"], "quantity": "1000",
                                                   "event_date": backend.today(), "reason": "外协完工回报"}, self.user)
        with self.assertRaises(HTTPException) as incomplete:
            backend.production_event(batch["id"], {"kind": "finished", "quantity": "1000",
                                                   "event_date": backend.today()}, self.user)
        self.assertEqual(incomplete.exception.status_code, 409)
        self.assertIn("按键板", incomplete.exception.detail)
        key = self.boards(batch["id"])["KEY"]
        backend.complete_batch_board(key["id"], {"version": key["version"], "quantity": "1000",
                                                  "event_date": backend.today(), "reason": "外协完工回报"}, self.user)
        finished = backend.production_event(batch["id"], {"kind": "finished", "quantity": "1000",
                                                          "event_date": backend.today()}, self.user)
        self.assertEqual(finished["kind"], "finished")
        self.assertEqual(backend.get_production_batch(batch["id"], self.user)["forecast"]["status"], "complete")

    def test_stage_checkbox_and_vendor_conflicts_with_shared_batch(self):
        self.configure()
        batch = self.create_batch()
        main = self.boards(batch["id"])["MAIN"]
        detail = backend.get_batch_board(main["id"], self.user)
        first = detail["stages"][0]
        with self.assertRaises(HTTPException) as unassigned:
            backend.complete_board_stage(first["id"], {"version": first["version"],
                                                         "completed": True}, self.user)
        self.assertEqual(unassigned.exception.status_code, 422)
        backend.patch_board_stage(first["id"], {"version": first["version"],
                                                 "changes": {"owner_id": self.user["id"]},
                                                 "reason": "指定板卡节点责任人"}, self.user)
        first = backend.get_batch_board(main["id"], self.user)["stages"][0]
        done = backend.complete_board_stage(first["id"], {"version": first["version"],
                                                            "completed": True}, self.user)
        self.assertTrue(done["is_complete"])
        self.assertEqual(done["completed_by"], self.user["id"])
        self.assertEqual(backend.get_batch_board(main["id"], self.user)["board"]["stage_progress"]["completed"], 1)
        shared_line = backend.create_order({"order_no": "SO-SHARED", "customer": "客户",
                                            "lines": [{"line_no": "1", "product_code": "S", "product_name": "共享批",
                                                       "quantity": "1000"}]}, self.user)["lines"][0]["id"]
        shared = backend.create_production_batch({"order_line_id": shared_line, "batch_no": "MP-S",
                                                  "quantity": "1000", "phase": "mp", "vendor": "板厂甲",
                                                  "commit_start": "2099-01-01", "commit_finish": "2099-01-31"}, self.user)
        main = self.boards(batch["id"])["MAIN"]
        updated = self.patch_board(main, {"vendor": "板厂甲", "commit_start": "2099-01-10",
                                          "commit_finish": "2099-01-20"})["board"]
        self.assertTrue(updated["conflicts"])
        acknowledged = backend.acknowledge_board_conflicts(main["id"], {"version": updated["version"],
                                                                         "reason": "厂商确认可并行"}, self.user)
        self.assertTrue(acknowledged["board"]["conflict_ack_at"])
        self.assertTrue(acknowledged["board"]["conflict_acknowledged"])
        backend.patch_production_batch(shared["id"], {"version": shared["version"],
                                                        "changes": {"commit_finish": "2099-02-01"},
                                                        "reason": "厂商调整另一批次承诺"}, self.user)
        self.assertFalse(backend.get_batch_board(main["id"], self.user)["board"]["conflict_acknowledged"])
        self.assertEqual(backend.request_permission("POST", f"/api/batch-boards/{main['id']}/conflicts/acknowledge"),
                         "production_edit")

    def test_rd_acceptance_alone_cannot_close_project_without_both_actual_boards(self):
        self.configure()
        batch = self.create_batch("rd", "RD-1")
        updated = backend.patch_production_batch(batch["id"], {
            "version": batch["version"], "changes": {"debug_accepted_at": backend.today()},
            "reason": "研发确认调试验收"}, self.user)
        self.assertEqual(updated["debug_accepted_at"], backend.today())
        self.assertEqual(backend.overview(self.user)["summary"]["orders_open"], 1)
        self.lock_both(batch["id"])
        for ref, expected_open in (("MAIN", 1), ("KEY", 0)):
            board = self.boards(batch["id"])[ref]
            backend.complete_batch_board(board["id"], {
                "version": board["version"], "quantity": "1000", "event_date": backend.today(),
                "reason": "打样板到板并验收"}, self.user)
            self.assertEqual(backend.overview(self.user)["summary"]["orders_open"], expected_open)

    def test_permissions_and_old_database_default_shared(self):
        for path in ("/api/batch-boards/1", "/api/board-stages/1", "/api/order-lines/1/board-schedule-mode"):
            permission = backend.request_permission("PATCH", path)
            self.assertIn(permission, {"production_edit", "orders_edit"})
            with self.assertRaises(HTTPException) as denied:
                backend.require_permission({"role": "business", "permissions_json": f'{{"{permission}":false}}'}, permission)
            self.assertEqual(denied.exception.status_code, 403)
        with sqlite3.connect(backend.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT board_schedule_mode FROM order_lines WHERE id=?", (self.line_id,)).fetchone()[0], "shared")


if __name__ == "__main__":
    unittest.main()

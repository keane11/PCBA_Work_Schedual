"""Read-only import preview checks for the original browser-only workbench."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from server.legacy_import import MAX_BYTES, MAX_PRODUCTS, preview_legacy_json, project_legacy_schedule, suggest_phase
from fastapi import HTTPException, UploadFile

_import_temp = tempfile.TemporaryDirectory()
os.environ["PCBA_DB_PATH"] = str(Path(_import_temp.name) / "legacy_import_test.db")
from server import app as backend


def preview(value, filename="PCBA多产品排期.json"):
    return preview_legacy_json(filename, json.dumps(value, ensure_ascii=False).encode("utf-8"))


class LegacyPreviewTests(unittest.TestCase):
    def test_quantity_suggests_but_does_not_assign_phase(self):
        self.assertIsNone(suggest_phase(None))
        self.assertEqual([suggest_phase(value) for value in (1, 20, 21, 1000, 1001)],
                         ["rd", "rd", "pp", "pp", "mp"])
        result = preview([{"name": "板", "qty": "20", "startDate": "2026-09-25",
                           "mode": "workday", "nodes": [{"name": "SMT", "duration": 1}]}])
        self.assertEqual(result["products"][0]["suggested_phase"], "rd")
        self.assertEqual(result["products"][0]["legacy_schedule"]["suggested_phase"], "rd")
        self.assertNotIn("phase", result["products"][0]["source"])

    def test_old_schedule_projection_preserves_half_days_weekends_and_raw_node_values(self):
        source = {
            "name": "样板 A", "owner": "唐林林", "qty": "10", "startDate": "2026-09-25", "mode": "workday",
            "nodes": [
                {"id": 11, "name": "物料采购", "type": "normal", "duration": 0.5,
                 "owner": "采购甲", "remark": "首批", "actual": "", "actualDone": "2026-10-01"},
                {"id": 12, "name": "到货入库", "type": "normal", "duration": 0.5},
                {"id": 13, "name": "到料确认", "type": "milestone", "duration": 0,
                 "actual": "2026-09-29"},
                {"id": 14, "name": "SMT", "type": "normal", "duration": 1.5},
            ],
        }
        schedule = project_legacy_schedule(source)
        self.assertEqual(schedule["estimate_kind"], "legacy_calculated")
        self.assertEqual((schedule["name"], schedule["owner"], schedule["quantity"]),
                         ("样板 A", "唐林林", "10"))
        self.assertEqual(schedule["start_date"], "2026-09-25")
        self.assertEqual(schedule["estimated_delivery_date"], "2026-09-30")
        self.assertEqual(schedule["elapsed_calendar_days"], "5.5")
        self.assertEqual(schedule["node_count"], 4)
        self.assertEqual(schedule["nodes"][0]["estimated_finish_at"], "2026-09-25T12:00:00")
        self.assertEqual(schedule["nodes"][1]["estimated_finish_at"], "2026-09-28T00:00:00")
        self.assertEqual(schedule["nodes"][2]["planned_start"], "2026-09-29")
        self.assertEqual(schedule["nodes"][3]["estimated_finish_at"], "2026-09-30T12:00:00")
        self.assertEqual(schedule["nodes"][0]["owner"], "采购甲")
        self.assertEqual(schedule["nodes"][0]["remark"], "首批")
        self.assertEqual(schedule["nodes"][0]["actual_finish_date"], "2026-10-01")
        self.assertEqual(preview([source])["products"][0]["legacy_schedule"], schedule)

    def test_missing_start_date_keeps_estimate_unknown(self):
        result = project_legacy_schedule({"name": "打样板", "mode": "workday",
                                          "nodes": [{"name": "SMT", "type": "normal", "duration": 0.5}]})
        self.assertIsNone(result["estimated_delivery_date"])
        self.assertIsNone(result["nodes"][0]["planned_start"])

    def test_array_preserves_exact_source_and_flags_fractional_workdays(self):
        original = [{
            "id": 1234, "name": "控制板 A", "owner": "张三", "qty": "5000",
            "startDate": "2026-10-01", "mode": "workday", "extra": "原样保留",
            "nodes": [
                {"id": 1, "name": "采购物料到料确认", "type": "milestone",
                 "duration": 0, "actual": "2026-10-03", "actualDone": "", "remark": "早到", "owner": "仓库"},
                {"id": 2, "name": "SMT 贴片", "type": "normal", "duration": 1.5,
                 "actual": "", "actualDone": "2026-10-06", "remark": "", "owner": "外协"},
            ],
        }]
        result = preview(original)
        self.assertEqual(result["source_format"], "array")
        self.assertEqual((result["product_count"], result["node_count"]), (1, 2))
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["products"][0]["source"], original[0])
        self.assertEqual(result["products"][0]["nodes"][1]["source"], original[0]["nodes"][1])
        self.assertEqual(result["products"][0]["nodes"][1]["duration_days"], "1.5")
        self.assertTrue(result["products"][0]["valid"])
        self.assertIn("fractional_day", {issue["code"] for issue in result["warnings"]})
        self.assertIn("BOM 版本与逐项物料明细", result["missing_new_system_fields"])
        self.assertNotIn("order_no", result["products"][0]["source"])

    def test_wrapper_and_single_old_object_are_supported_without_defaulting_fields(self):
        old_product = {"product": "旧板", "nodes": [{"name": "SMT", "type": "normal", "duration": 1}]}
        for value, expected_shape in (({"products": [old_product]}, "wrapper"), (old_product, "single_product")):
            with self.subTest(shape=expected_shape):
                result = preview(value)
                self.assertEqual(result["source_format"], expected_shape)
                self.assertEqual(result["products"][0]["name"], "旧板")
                self.assertIsNone(result["products"][0]["quantity"])
                self.assertIsNone(result["products"][0]["start_date"])
                self.assertEqual(result["errors"], [])
                self.assertTrue({"legacy_name", "missing_quantity", "missing_date", "missing_mode"}
                                <= {issue["code"] for issue in result["warnings"]})

    def test_invalid_rows_remain_visible_and_are_not_coerced(self):
        data = [{"name": "  ", "qty": "1.5", "startDate": "2026-02-30", "mode": "bad",
                 "nodes": [{"name": "", "type": "other", "duration": -1, "actualDone": "明天"},
                           {"name": "测试", "type": "normal", "duration": 0.25}]}]
        result = preview(data)
        self.assertEqual(result["products"][0]["source"], data[0])
        self.assertFalse(result["products"][0]["valid"])
        paths = {issue["path"] for issue in result["errors"]}
        self.assertTrue({"products[0].name", "products[0].qty", "products[0].startDate",
                         "products[0].mode", "products[0].nodes[0].name",
                         "products[0].nodes[0].type", "products[0].nodes[0].duration",
                         "products[0].nodes[0].actualDone"} <= paths)
        self.assertTrue({"fractional_day", "legacy_rounding"}
                        <= {issue["code"] for issue in result["warnings"]})

    def test_file_limits_and_ambiguous_json_fail_without_partial_import(self):
        examples = [
            ("wrong.txt", b"[]", "extension"),
            ("x.json", b"", "empty"),
            ("x.json", b" " * (MAX_BYTES + 1), "size"),
            ("x.json", b'{"products":[]}', "empty"),
            ("x.json", b'{"products":{}}', "format"),
            ("x.json", b'{"products":[],"products":[]}', "format"),
            ("x.json", b'{"products":NaN}', "format"),
            ("x.json", b'{"products":1e999}', "format"),
            ("x.json", b"\xff", "format"),
        ]
        for filename, content, code in examples:
            with self.subTest(filename=filename, content=content[:40]):
                result = preview_legacy_json(filename, content)
                self.assertEqual(result["products"], [])
                self.assertEqual(result["errors"][0]["code"], code)
        many = preview([{"name": "x", "nodes": []} for _ in range(MAX_PRODUCTS + 1)])
        self.assertEqual(many["products"], [])
        self.assertEqual(many["errors"][0]["code"], "limit")


class LegacyRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_path = backend.DB_PATH
        backend.DB_PATH = Path(self.temp.name) / "legacy.db"
        backend.init_db()
        salt = secrets.token_hex(16)
        with backend.writing() as conn:
            cur = conn.execute("INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                               ("admin", "管理员", "admin", salt, backend.password_digest("securepass123", salt), backend.utcnow()))
            self.user = backend.must_get(conn, "users", cur.lastrowid)

    def tearDown(self):
        backend.DB_PATH = self.previous_path
        self.temp.cleanup()

    def preview_route(self, products):
        content = json.dumps(products, ensure_ascii=False).encode("utf-8")
        file = UploadFile(file=BytesIO(content), filename="PCBA多产品排期.json")
        return backend.preview_legacy(file=file, user=self.user)

    def test_selected_products_create_pending_orders_and_keep_original_nodes(self):
        products = [
            {"id": 1700, "name": "控制板", "qty": "5000", "owner": "张三", "startDate": "2026-09-01",
             "mode": "workday", "nodes": [{"id": 1, "name": "SMT 贴片", "type": "normal", "duration": 0.5,
                                          "actual": "", "actualDone": "2026-09-05", "remark": "旧节点", "owner": "小王"}]},
            {"id": 1701, "name": "无数量板", "qty": "", "nodes": [{"name": "测试", "type": "normal", "duration": 1}]},
        ]
        previewed = self.preview_route(products)
        self.assertEqual([item["action"] for item in previewed["products"]], ["create", "create"])
        applied = backend.apply_legacy({"preview_id": previewed["preview_id"],
                                        "selections": [{"index": 0}, {"index": 1}]}, user=self.user)
        self.assertEqual(len(applied["created"]), 2)
        self.assertTrue((Path(self.temp.name) / "backups" / applied["pre_import_backup"]).is_file())
        first, second = applied["created"]
        self.assertTrue(first["order_no"].startswith("PCBA-TEMP-"))
        self.assertIsNotNone(first["production_batch_id"])
        self.assertIsNone(second["production_batch_id"])
        self.assertIn("订单数量", second["pending_fields"])

        first_order = backend.get_order(first["order_id"], user=self.user)
        second_order = backend.get_order(second["order_id"], user=self.user)
        self.assertEqual(first_order["customer"], "待补录")
        self.assertEqual(first_order["legacy_record"]["status"], "pending")
        self.assertEqual(first_order["legacy_owner_name"], "张三")
        self.assertEqual(first_order["owner_name"], "张三")
        self.assertEqual(first_order["owner_binding_status"], "legacy_unbound")
        self.assertIn("负责人账号待绑定", first_order["legacy_record"]["missing_fields"])
        self.assertEqual(first_order["lines"][0]["product_name"], "控制板")
        self.assertEqual(second_order["owner_binding_status"], "missing")
        self.assertEqual(second_order["lines"], [])
        self.assertEqual(second_order["status"], "open")
        records = backend.get_legacy_records(order_id=first["order_id"], user=self.user)
        self.assertEqual(records[0]["source"], products[0])
        self.assertEqual(records[0]["source"]["nodes"][0]["duration"], 0.5)
        self.assertEqual(records[0]["production_batch_id"], first["production_batch_id"])
        batch = backend.get_production_batch(first["production_batch_id"], user=self.user)
        self.assertTrue(batch["legacy_imported"])
        materials_batch = next(item for item in backend.all_materials(user=self.user)
                               if item["id"] == batch["id"])
        self.assertTrue(materials_batch["legacy_imported"])
        self.assertIsNone(batch["commit_start"])
        self.assertIsNone(batch["commit_finish"])
        self.assertFalse(batch["bom_locked"])
        self.assertEqual(batch["phase"], "mp")
        self.assertEqual(batch["phase_source"], "quantity_rule")
        self.assertEqual([step["name"] for step in batch["stages"]], ["SMT 贴片"])
        self.assertEqual(batch["stages"][0]["owner_name"], "小王")
        self.assertEqual(batch["stages"][0]["duration_days"], "0.5")
        self.assertEqual(batch["stages"][0]["actual_finish"], "2026-09-05")
        self.assertEqual(batch["stages"][0]["note"], "旧节点")
        self.assertEqual(first_order["legacy_schedule"]["estimated_delivery_date"], "2026-09-01")
        tasks = backend.get_tasks(status="open", user=self.user)
        self.assertEqual(sum(task["kind"] == "legacy_pending" for task in tasks), 2)
        first_delivery_task = next(task for task in tasks if task["kind"] == "missing_delivery_batch"
                                   and task["entity_id"] == first["order_line_id"])
        self.assertTrue(first_delivery_task["title"].startswith("项目 控制板（PCBA-TEMP-"))
        self.assertNotIn("产品 PCBA-TEMP-", first_delivery_task["title"])
        self.assertTrue(any(task["kind"] == "bom_unlocked" and "产品 控制板" in task["title"]
                            for task in tasks))
        self.assertTrue(any(task["kind"] == "legacy_pending" and "项目 控制板（" in task["title"]
                            for task in tasks))
        audit = backend.get_audit(entity_type="legacy_records", entity_id=first["legacy_record_id"], user=self.user)
        self.assertEqual(audit[0]["actor_id"], self.user["id"])

    def test_legacy_owner_is_shown_until_workbench_account_is_bound(self):
        source = [{"id": 1800, "name": "项目甲", "owner": "唐林林", "qty": "12",
                   "nodes": [{"name": "SMT", "duration": 1}]}]
        previewed = self.preview_route(source)
        created = backend.apply_legacy({"preview_id": previewed["preview_id"],
                                        "selections": [{"index": 0}]}, user=self.user)["created"][0]
        order_id = created["order_id"]
        order = backend.get_order(order_id, user=self.user)
        self.assertIsNone(order["owner_id"])
        self.assertEqual(order["owner_name"], "唐林林")
        self.assertEqual(order["owner_binding_status"], "legacy_unbound")
        self.assertIn("负责人账号待绑定", created["pending_fields"])
        self.assertEqual(order["lines"][0]["product_name"], "项目甲")
        task = next(item for item in backend.get_tasks(status="open", user=self.user)
                    if item["kind"] == "missing_owner")
        self.assertIn("原负责人 唐林林 尚未绑定工作台账号", task["title"])

        backend.patch_order(order_id, {"version": order["version"],
                                       "changes": {"owner_id": self.user["id"]}}, user=self.user)
        bound = backend.get_order(order_id, user=self.user)
        self.assertEqual(bound["legacy_owner_name"], "唐林林")
        self.assertEqual(bound["owner_name"], "管理员")
        self.assertEqual(bound["owner_binding_status"], "bound")
        self.assertNotIn("负责人账号待绑定", bound["legacy_record"]["missing_fields"])
        tasks = backend.get_tasks(status="open", user=self.user)
        updated = next(item for item in tasks if item["id"] == task["id"])
        self.assertEqual(updated["condition_active"], 0)

    def test_inactive_user_cannot_be_assigned_as_order_owner(self):
        with backend.writing() as conn:
            cur = conn.execute("INSERT INTO users(username,display_name,role,password_salt,password_hash,active,created_at) VALUES(?,?,?,?,?,?,?)",
                               ("legacy_import", "历史导入员", "business", "unused", "unused", 0, backend.utcnow()))
            inactive_id = cur.lastrowid
        payload = {"order_no": "SO-OWNER-1", "customer": "客户甲", "owner_id": inactive_id,
                   "lines": [{"line_no": "1", "product_code": "P1", "product_name": "项目甲", "quantity": "5"}]}
        with self.assertRaises(HTTPException) as rejected_create:
            backend.create_order(payload, user=self.user)
        self.assertEqual(rejected_create.exception.status_code, 422)
        self.assertIn("已停用", rejected_create.exception.detail)
        with backend.reading() as conn:
            self.assertIsNone(backend.one(conn, "SELECT id FROM orders WHERE order_no=?", (payload["order_no"],)))

        created = backend.create_order({**payload, "owner_id": self.user["id"]}, user=self.user)
        self.assertEqual(created["owner_name"], "管理员")
        unassigned = backend.create_order({**payload, "order_no": "SO-OWNER-2", "owner_id": None}, user=self.user)
        with self.assertRaises(HTTPException) as rejected_patch:
            backend.patch_order(unassigned["id"], {"version": unassigned["version"],
                                                   "changes": {"owner_id": inactive_id}}, user=self.user)
        self.assertEqual(rejected_patch.exception.status_code, 422)
        self.assertIn("已停用", rejected_patch.exception.detail)
        assigned = backend.patch_order(unassigned["id"], {"version": unassigned["version"],
                                                 "changes": {"owner_id": self.user["id"]}}, user=self.user)
        self.assertEqual(assigned["owner_id"], self.user["id"])

    def test_reimport_deduplicates_stable_id_and_never_overwrites_prior_source(self):
        original = [{"id": 41, "name": "旧板", "qty": "10", "nodes": [{"name": "测试", "duration": 1}]}]
        first = self.preview_route(original)
        created = backend.apply_legacy({"preview_id": first["preview_id"], "selections": [{"index": 0}]}, user=self.user)
        changed = [{**original[0], "name": "旧板新版", "nodes": [{"name": "测试", "duration": 2}]}]
        second = self.preview_route(changed)
        self.assertEqual(second["products"][0]["action"], "duplicate")
        self.assertEqual(second["products"][0]["existing_order_id"], created["created"][0]["order_id"])
        self.assertIn("changed_duplicate", {warning["code"] for warning in second["warnings"]})
        applied = backend.apply_legacy({"preview_id": second["preview_id"], "selections": [{"index": 0}]}, user=self.user)
        self.assertEqual(applied["created"], [])
        self.assertIsNone(applied["pre_import_backup"])
        self.assertEqual(backend.get_legacy_records(user=self.user)[0]["source"], original[0])
        with backend.reading() as conn:
            self.assertEqual(backend.one(conn, "SELECT COUNT(*) AS n FROM orders")["n"], 1)

    def test_duplicate_id_in_file_is_invalid_and_cannot_apply(self):
        data = [{"id": 1, "name": "A", "qty": "1", "nodes": [{"name": "SMT", "duration": 1}]},
                {"id": 1, "name": "B", "qty": "2", "nodes": [{"name": "测试", "duration": 1}]}]
        previewed = self.preview_route(data)
        self.assertEqual([item["action"] for item in previewed["products"]], ["invalid", "invalid"])
        with self.assertRaises(HTTPException) as raised:
            backend.apply_legacy({"preview_id": previewed["preview_id"], "selections": [{"index": 0}]}, user=self.user)
        self.assertEqual(raised.exception.status_code, 422)
        with backend.reading() as conn:
            self.assertEqual(backend.one(conn, "SELECT COUNT(*) AS n FROM orders")["n"], 0)

    def test_pending_fields_clear_only_after_real_order_bom_vendor_and_delivery_are_filled(self):
        source = [{"id": 91, "name": "控制板", "qty": "10", "nodes": [{"name": "SMT", "duration": 1}]}]
        previewed = self.preview_route(source)
        created = backend.apply_legacy({"preview_id": previewed["preview_id"],
                                        "selections": [{"index": 0}]}, user=self.user)["created"][0]
        order_id, batch_id = created["order_id"], created["production_batch_id"]
        backend.get_tasks(status="open", user=self.user)
        order = backend.get_order(order_id, user=self.user)
        backend.patch_order(order_id, {"version": order["version"],
                                       "changes": {"order_no": "SO-REAL-91", "customer": "客户 A", "owner_id": self.user["id"]}}, user=self.user)
        line = backend.get_order(order_id, user=self.user)["lines"][0]
        backend.patch_order_line(line["id"], {"version": line["version"], "changes": {"product_code": "PCB-A"}}, user=self.user)
        batch = backend.get_production_batch(batch_id, user=self.user)
        batch = backend.patch_production_batch(batch_id, {"version": batch["version"],
                            "changes": {"phase": "mp", "phase_order_no": "SO-REAL-91", "vendor": "外协厂 A",
                                        "commit_start": "2026-10-05", "commit_finish": "2026-10-10"},
                            "reason": "外协邮件确认"}, user=self.user)
        backend.lock_bom(batch_id, {"version": batch["version"], "bom_version": "V1",
                                    "items": [{"material_code": "M1", "quantity_per": "1"}]}, user=self.user)
        delivery = backend.create_delivery_batch({"order_line_id": line["id"], "batch_no": "D1", "quantity": "10",
                                                   "due_date": "2026-10-15", "promised_date": "2026-10-16"}, user=self.user)
        backend.create_allocation({"production_batch_id": batch_id, "delivery_batch_id": delivery["id"],
                                   "quantity": "10"}, user=self.user)
        record = backend.get_legacy_records(order_id=order_id, user=self.user)[0]
        self.assertEqual(record["missing_fields"], [])
        self.assertEqual(record["status"], "complete")
        self.assertEqual(backend.get_order(order_id, user=self.user)["legacy_record"]["status"], "complete")
        tasks = backend.get_tasks(status="open", user=self.user)
        pending = next(task for task in tasks if task["kind"] == "legacy_pending")
        self.assertEqual(pending["condition_active"], 0)

    def test_blank_quantity_order_can_be_linked_to_later_line_and_batch(self):
        source = [{"id": 92, "name": "后补数量板", "qty": "", "nodes": [{"name": "SMT", "duration": 0.5}]}]
        previewed = self.preview_route(source)
        created = backend.apply_legacy({"preview_id": previewed["preview_id"],
                                        "selections": [{"index": 0}]}, user=self.user)["created"][0]
        self.assertIn("订单数量", created["pending_fields"])
        self.assertIn("生产批次", created["pending_fields"])
        line = backend.create_order_line(created["order_id"], {"line_no": "1", "product_code": "PCB-LATE",
                                                                  "product_name": "后补数量板", "quantity": "25"}, user=self.user)
        record = backend.get_legacy_records(order_id=created["order_id"], user=self.user)[0]
        self.assertEqual(record["order_line_id"], line["id"])
        self.assertNotIn("订单数量", record["missing_fields"])
        missing_batch = next(task for task in backend.get_tasks(status="open", user=self.user)
                             if task["kind"] == "missing_batch" and task["entity_id"] == line["id"])
        self.assertIn("项目 后补数量板（PCBA-TEMP-", missing_batch["title"])
        self.assertNotIn("产品 PCBA-TEMP-", missing_batch["title"])
        batch = backend.create_production_batch({"order_line_id": line["id"], "batch_no": "P1", "quantity": "25"}, user=self.user)
        self.assertFalse(backend.get_production_batch(batch["id"], user=self.user)["legacy_imported"])
        record = backend.get_legacy_records(order_id=created["order_id"], user=self.user)[0]
        self.assertEqual(record["production_batch_id"], batch["id"])
        self.assertNotIn("生产批次", record["missing_fields"])
        self.assertIsNone(batch["commit_start"])


if __name__ == "__main__":
    unittest.main()

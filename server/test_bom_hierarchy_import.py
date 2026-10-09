"""Explicit parent/child BOM import keeps product groups and source line identity."""

from __future__ import annotations

import io
import json
import secrets
import tempfile
import unittest
from pathlib import Path

from fastapi import UploadFile
from openpyxl import Workbook

from server import app as backend
from server.kis_import import inspect_upload, preview_import


def workbook_bytes(rows: list[list[object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue()


class BomHierarchyImportTests(unittest.TestCase):
    def setUp(self):
        self.head = ["级次", "物料代码", "物料名称", "数量", "参考材料单价"]
        self.rows = [
            ["说明", None, None, None, None],
            self.head,
            ["母件", "BOARD-A", "母件 A", 1, None],
            ["1级子件", "R-1", "电阻", 2, "0.125"],
            ["备注", None, "仅供参考", None, None],
            ["母件", "BOARD-B", "母件 B", 1, None],
            ["1级子件", "R-2", "电阻", 3, 0],
        ]
        self.mapping = {"material_code": "物料代码", "material_name": "物料名称",
                        "quantity_per": "数量", "unit_price": "参考材料单价"}
        self.content = workbook_bytes(self.rows)

    def preview(self, *, content=None, existing_rows=None, mapping=None, fixed=None, column="级次"):
        return preview_import("bom", "bom.xlsx", content or self.content,
                              mapping if mapping is not None else self.mapping,
                              existing_rows=existing_rows or [], header_row=2,
                              fixed_fields=fixed if fixed is not None else {"bom_version": "V1"},
                              bom_parent_column=column)

    def test_inspect_suggests_only_when_parent_and_child_are_present(self):
        inspected = inspect_upload("bom.xlsx", self.content, header_row=2, kind="bom")
        self.assertEqual(inspected["suggested_bom_parent_column"], "级次")
        without_child = workbook_bytes(self.rows[:3])
        self.assertIsNone(inspect_upload("bom.xlsx", without_child, header_row=2,
                                         kind="bom")["suggested_bom_parent_column"])

    def test_two_parent_groups_skip_parent_and_notes_and_keep_physical_row_numbers(self):
        outcome = self.preview()
        self.assertEqual(outcome["errors"], [])
        self.assertEqual(outcome["summary"]["create"], 2)
        self.assertEqual([row["row_number"] for row in outcome["rows"]], [4, 7])
        self.assertEqual([row["row_index"] for row in outcome["rows"]], [0, 1])
        self.assertEqual([row["values"]["product_code"] for row in outcome["rows"]],
                         ["BOARD-A", "BOARD-B"])
        self.assertEqual([row["values"]["source_line_no"] for row in outcome["rows"]], ["4", "7"])
        self.assertEqual([row["values"]["unit_price"] for row in outcome["rows"]], ["0.125", "0"])
        self.assertEqual({warning["code"] for warning in outcome["warnings"]},
                         {"bom_parent_skipped", "bom_summary_skipped", "source_row_key_review"})

    def test_explicit_mode_rejects_ambiguous_mapping_or_missing_version(self):
        bad_mapping = {**self.mapping, "product_code": "物料代码"}
        self.assertIn("取消", self.preview(mapping=bad_mapping)["errors"][0]["message"])
        self.assertIn("bom_version", self.preview(fixed={})["errors"][0]["message"])
        self.assertIn("级次列不存在", self.preview(column="错误列")["errors"][0]["message"])

    def test_unknown_level_with_data_is_invalid_and_clears_parent_context(self):
        rows = self.rows[:4] + [["次级", "X-1", "不明确", 1, None],
                                ["1级子件", "R-2", "归属不明", 1, None]]
        outcome = self.preview(content=workbook_bytes(rows))
        self.assertEqual([row["action"] for row in outcome["rows"]], ["create", "invalid", "invalid"])
        self.assertIn("bom_level_ambiguous", {error["code"] for error in outcome["errors"]})
        self.assertIn("bom_parent_missing", {error["code"] for error in outcome["errors"]})

    def test_reordered_lines_are_invalid_in_existing_version(self):
        initial = self.preview()
        existing = [row["values"] for row in initial["rows"]]
        reordered = self.rows[:3] + [["1级子件", "R-NEW", "新件", 2, 1],
                                     ["1级子件", "R-1", "电阻", 2, "0.125"]] + self.rows[5:]
        outcome = self.preview(content=workbook_bytes(reordered), existing_rows=existing)
        self.assertEqual(outcome["rows"][0]["action"], "invalid")
        self.assertEqual(outcome["rows"][1]["action"], "invalid")
        self.assertIn("bom_row_reassigned", {error["code"] for error in outcome["errors"]})
        self.assertIn("bom_row_key_drift", {error["code"] for error in outcome["errors"]})

    def test_existing_flat_bom_without_line_number_cannot_be_silently_duplicated(self):
        existing = [{"product_code": "BOARD-A", "bom_version": "V1", "material_code": "R-1",
                     "quantity_per": "2", "source_line_no": None}]
        outcome = self.preview(existing_rows=existing)
        self.assertEqual(outcome["rows"][0]["action"], "invalid")
        self.assertIn("bom_row_key_drift", {error["code"] for error in outcome["errors"]})


class BomHierarchyAuditTests(unittest.TestCase):
    def test_apply_replays_hierarchy_options_and_preserves_original_file(self):
        data = workbook_bytes([
            ["级次", "物料代码", "数量", "参考材料单价"],
            ["母件", "BOARD-A", 1, None],
            ["1级子件", "R-1", 2, "0.25"],
            ["母件", "BOARD-B", 1, None],
            ["1级子件", "R-2", 3, "0.5"],
        ])
        with tempfile.TemporaryDirectory() as directory:
            original_path = backend.DB_PATH
            try:
                backend.DB_PATH = Path(directory) / "imports.db"
                backend.init_db()
                salt = secrets.token_hex(16)
                with backend.writing() as conn:
                    created = conn.execute(
                        "INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                        ("bom_admin", "BOM 导入员", "admin", salt,
                         backend.password_digest("securepass123", salt), backend.utcnow()),
                    )
                    user = backend.must_get(conn, "users", created.lastrowid)
                preview = backend.preview_import_route(
                    file=UploadFile(file=io.BytesIO(data), filename="bom.xlsx"), kind="bom",
                    column_map=json.dumps({"material_code": "物料代码", "quantity_per": "数量",
                                           "unit_price": "参考材料单价"}),
                    fixed_fields=json.dumps({"bom_version": "V1"}), bom_parent_column="级次",
                    sheet_name=None, header_row=1, key_fields=None, snapshot_as_of=None,
                    snapshot_basis=None, user=user,
                )
                self.assertEqual(preview["errors"], [])
                self.assertEqual([row["row_index"] for row in preview["rows"]], [0, 1])
                applied = backend.apply_import({"preview_id": preview["preview_id"],
                                                "selections": [{"row_index": 0}, {"row_index": 1}]}, user=user)
                self.assertEqual(applied["rows_applied"], 2)
                with backend.reading() as conn:
                    records = backend.rows(conn, "SELECT product_code,source_line_no,unit_price FROM bom_catalog ORDER BY product_code")
                    batch = backend.one(conn, "SELECT * FROM import_batches WHERE id=?", (applied["id"],))
                self.assertEqual([(row["product_code"], row["source_line_no"], row["unit_price"]) for row in records],
                                 [("BOARD-A", "3", "0.25"), ("BOARD-B", "5", "0.5")])
                self.assertEqual(batch["source_blob"], data)
                self.assertEqual(json.loads(batch["selection_json"])["metadata"]["bom_parent_column"], "级次")
            finally:
                backend.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()

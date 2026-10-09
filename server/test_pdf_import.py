"""PDF table extraction stays inside the ordinary inspect/preview import flow."""

from __future__ import annotations

import io
import json
import secrets
import tempfile
import unittest
import zlib
from pathlib import Path

import pdfplumber
from fastapi import UploadFile

from server import app as backend
from server.kis_import import inspect_upload, preview_import


def make_pdf(pages: list[list[list[str]]], *, ruled: bool = True) -> bytes:
    """Build a small text PDF without a PDF test-only dependency."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # Pages tree, filled after all page objects are added.
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_ids = []
    positions = [40, 160, 230, 385, 500]
    for rows in pages:
        page_id = len(objects) + 1
        content_id = page_id + 1
        page_ids.append(page_id)
        objects.append((
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode())
        commands = []
        for row_index, row in enumerate(rows):
            top = 780 - row_index * 35
            for column, value in enumerate(row):
                escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                commands.append(f"BT /F1 10 Tf {positions[column] + 5} {top - 24} Td ({escaped}) Tj ET")
            if ruled:
                commands.append(f"40 {top} m 500 {top} l S")
        if ruled and rows:
            bottom = 780 - len(rows) * 35
            commands.append(f"40 {bottom} m 500 {bottom} l S")
            for x in positions[:len(rows[0]) + 1]:
                commands.append(f"{x} 780 m {x} {bottom} l S")
        stream = "\n".join(commands).encode("ascii")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] "
                  f"/Count {len(page_ids)} >>").encode()
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    startxref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode())
    return bytes(output)


def rasterize_pdf(source: bytes) -> bytes:
    """Turn one text PDF page into an image-only PDF, like a scanner export."""
    with pdfplumber.open(io.BytesIO(source)) as pdf:
        image = pdf.pages[0].to_image(resolution=200).original.convert("RGB")
    width, height = image.size
    pixels = zlib.compress(image.tobytes())
    content = b"q 595 0 0 842 0 0 cm /Im0 Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
        (f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
         f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(pixels)} >>\nstream\n").encode()
        + pixels + b"\nendstream",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    startxref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode())
    return bytes(output)


class PdfImportTests(unittest.TestCase):
    def setUp(self):
        self.header = ["order_no", "line_no", "product_code", "quantity"]

    def test_ruled_multipage_table_merges_repeated_header_and_previews_rows(self):
        pdf = make_pdf([
            [self.header, ["SO-001", "1", "BOARD-A", "100"]],
            [self.header, ["SO-002", "1", "BOARD-B", "250"]],
        ])
        inspected = inspect_upload("orders.PDF", pdf)
        self.assertEqual(inspected["errors"], [])
        self.assertEqual(inspected["columns"], self.header)
        self.assertEqual(inspected["row_count"], 2)
        self.assertEqual(len(inspected["sheets"]), 1)
        preview = preview_import("orders", "orders.PDF", pdf, {})
        self.assertEqual(preview["errors"], [])
        self.assertEqual(preview["summary"]["create"], 2)
        self.assertEqual([row["values"]["order_no"] for row in preview["rows"]], ["SO-001", "SO-002"])

    def test_text_columns_without_rules_can_be_mapped(self):
        pdf = make_pdf([[self.header, ["SO-003", "1", "BOARD-C", "10"]]], ruled=False)
        preview = preview_import("orders", "text-table.pdf", pdf, {})
        self.assertEqual(preview["errors"], [])
        self.assertEqual(preview["summary"]["create"], 1)

    def test_each_document_kind_uses_the_same_pdf_mapping_and_diff(self):
        cases = [
            ("orders", ["order_no", "line_no", "product_code", "quantity"], ["SO-004", "1", "BOARD-D", "50"]),
            ("bom", ["product_code", "bom_version", "material_code", "quantity_per"], ["BOARD-D", "V1", "R100", "2"]),
            ("purchases", ["reference", "material_code", "quantity"], ["PO-1", "R100", "100"]),
            ("receipts", ["reference", "material_code", "quantity", "received_at"], ["RC-1", "R100", "100", "2026-09-28"]),
        ]
        for kind, headers, values in cases:
            with self.subTest(kind=kind):
                mapping = {headers[0]: "source_id"}
                pdf = make_pdf([[["source_id", *headers[1:]], values]])
                inspected = inspect_upload(f"{kind}.pdf", pdf)
                self.assertEqual(inspected["errors"], [])
                self.assertEqual(inspected["row_count"], 1)
                preview = preview_import(kind, f"{kind}.pdf", pdf, mapping)
                self.assertEqual(preview["errors"], [])
                self.assertEqual(preview["summary"]["create"], 1)
                self.assertEqual(preview["rows"][0]["values"][headers[0]], values[0])

    def test_pdf_document_header_values_fill_only_unmapped_columns(self):
        cases = [
            ("orders", ["line_no", "product_code", "quantity"], ["12", "BOARD-X", "50"],
             {"order_no": "SO-HEADER"}, "order_no"),
            ("bom", ["material_code", "quantity_per", "unit_price"], ["R100", "2", "0.15"],
             {"product_code": "BOARD-X", "bom_version": "V2"}, "bom_version"),
            ("purchases", ["material_code", "quantity"], ["R100", "100"],
             {"reference": "PO-HEADER"}, "reference"),
            ("receipts", ["material_code", "quantity", "received_at"], ["R100", "100", "2026-09-28"],
             {"reference": "RC-HEADER"}, "reference"),
        ]
        for kind, headers, values, fixed, expected_field in cases:
            with self.subTest(kind=kind):
                pdf = make_pdf([[headers, values]])
                preview = preview_import(kind, f"{kind}.pdf", pdf, {}, fixed_fields=fixed)
                self.assertEqual(preview["errors"], [])
                self.assertEqual(preview["summary"]["create"], 1)
                self.assertEqual(preview["rows"][0]["values"][expected_field], fixed[expected_field])

        pdf = make_pdf([[self.header, ["SO-FILE", "12", "BOARD-X", "50"]]])
        preview = preview_import("orders", "orders.pdf", pdf, {}, fixed_fields={"order_no": "SO-FIXED"})
        self.assertEqual(preview["rows"][0]["values"]["order_no"], "SO-FILE")
        self.assertIn("fixed_ignored", [item["code"] for item in preview["warnings"]])

    def test_fixed_values_are_validated_and_never_fill_blank_mapped_cells(self):
        pdf = make_pdf([[self.header, ["", "12", "BOARD-X", "50"]]])
        preview = preview_import("orders", "orders.pdf", pdf, {}, fixed_fields={"order_no": "SO-FIXED"})
        self.assertEqual(preview["rows"][0]["action"], "invalid")
        self.assertEqual(preview["rows"][0]["values"]["order_no"], None)
        self.assertIn("missing_value", [item["code"] for item in preview["errors"]])
        unknown = preview_import("orders", "orders.pdf", pdf, {}, fixed_fields={"not_a_field": "x"})
        self.assertIn("未知固定字段", unknown["errors"][0]["message"])
        invalid = preview_import("orders", "orders.pdf", pdf, {}, fixed_fields={"due_date": "2026-02-31"})
        self.assertIn("日期", invalid["errors"][0]["message"])

    def test_date_remarks_column_accepts_empty_separator_but_not_hidden_remarks(self):
        pdf = make_pdf([[self.header, ["SO-DATE", "12", "BOARD-X", "50"]]])
        good = preview_import("orders", "date.pdf", pdf, {}, fixed_fields={"due_date": "2026-09-16 /"})
        self.assertEqual(good["errors"], [])
        self.assertEqual(good["rows"][0]["values"]["due_date"], "2026-09-16")
        with_remark = preview_import("orders", "date.pdf", pdf, {},
                                     fixed_fields={"due_date": "2026-09-16 / 备注待确认"})
        self.assertIn("日期", with_remark["errors"][0]["message"])

    def test_order_inspection_suggests_sample_pdf_columns_without_guessing_header_number(self):
        csv_data = ("项目,物料编号,物 料 描 述,数量,交货日期/备注\n"
                    "1,BOARD-X,控制板,50,2026-09-16 /\n").encode("utf-8")
        inspected = inspect_upload("orders.csv", csv_data, kind="orders")
        self.assertEqual(inspected["suggested_column_map"], {
            "product_code": "物料编号", "product_name": "物 料 描 述",
            "quantity": "数量", "due_date": "交货日期/备注", "line_no": "项目",
        })
        self.assertNotIn("order_no", inspected["suggested_column_map"])
        named_project = inspect_upload("orders.csv", csv_data.replace(b"1,BOARD", b"A,BOARD"), kind="orders")
        self.assertNotIn("line_no", named_project["suggested_column_map"])

    def test_scanned_pdf_requires_explicit_ai_action_without_local_ocr(self):
        source = make_pdf([[self.header, ["SO-005", "12", "BOARD-E", "100"]]])
        scanned = rasterize_pdf(source)
        with pdfplumber.open(io.BytesIO(scanned)) as pdf:
            self.assertFalse(pdf.pages[0].chars)
        inspected = inspect_upload("scan.pdf", scanned)
        self.assertEqual(inspected["sheets"], [])
        self.assertIn("DeepSeek 智能解析", inspected["errors"][0]["message"])
        preview = preview_import("orders", "scan.pdf", scanned, {})
        self.assertEqual(preview["rows"], [])
        self.assertIn("DeepSeek 智能解析", preview["errors"][0]["message"])

    def test_blank_pdf_explains_ai_requirement(self):
        inspected = inspect_upload("scan.pdf", make_pdf([[]]))
        self.assertIn("DeepSeek 智能解析", inspected["errors"][0]["message"])

    def test_invalid_pdf_is_rejected_without_rows(self):
        preview = preview_import("orders", "broken.pdf", b"not a PDF", {})
        self.assertEqual(preview["rows"], [])
        self.assertIn("无法读取 PDF", preview["errors"][0]["message"])


class PdfImportAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = backend.DB_PATH
        backend.DB_PATH = Path(self.temp.name) / "imports.db"
        backend.init_db()
        salt = secrets.token_hex(16)
        with backend.writing() as conn:
            created = conn.execute(
                "INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                ("pdf_admin", "PDF 导入员", "admin", salt, backend.password_digest("securepass123", salt), backend.utcnow()),
            )
            self.user = backend.must_get(conn, "users", created.lastrowid)

    def tearDown(self):
        backend.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_preview_apply_preserve_original_pdf_and_fixed_values_for_audit(self):
        pdf = make_pdf([[["line_no", "product_code", "quantity"], ["12", "BOARD-X", "50"]]])
        fixed = {"order_no": "SO-PDF", "customer": "客户 A"}
        preview = backend.preview_import_route(
            file=UploadFile(file=io.BytesIO(pdf), filename="order.pdf"),
            kind="orders", column_map="{}", fixed_fields=json.dumps(fixed, ensure_ascii=False),
            sheet_name=None, header_row=1, key_fields=None, snapshot_as_of=None,
            snapshot_basis=None, user=self.user,
        )
        self.assertEqual(preview["errors"], [])
        with backend.reading() as conn:
            saved = backend.one(conn, "SELECT * FROM import_previews WHERE id=?", (preview["preview_id"],))
            self.assertEqual(saved["source_blob"], pdf)
            self.assertEqual(json.loads(saved["preview_json"])["fixed_fields"], fixed)
        result = backend.apply_import({"preview_id": preview["preview_id"],
                                       "selections": [{"row_index": 0}]}, user=self.user)
        self.assertEqual(result["rows_applied"], 1)
        with backend.reading() as conn:
            batch = backend.one(conn, "SELECT * FROM import_batches WHERE id=?", (result["id"],))
            self.assertEqual(batch["source_blob"], pdf)
            self.assertEqual(json.loads(batch["selection_json"])["metadata"]["fixed_fields"], fixed)
            order = backend.one(conn, "SELECT order_no,customer FROM orders WHERE order_no='SO-PDF'")
            self.assertEqual((order["order_no"], order["customer"]), ("SO-PDF", "客户 A"))


if __name__ == "__main__":
    unittest.main()

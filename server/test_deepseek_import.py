"""AI PDF import tests use synthetic PDFs and never contact DeepSeek."""

from __future__ import annotations

import io
import json
import secrets
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from server import app as backend
from server import deepseek_import
from server.test_pdf_import import make_pdf, rasterize_pdf


def candidate() -> dict:
    return {
        "columns": ["order_no", "line_no", "product_code", "quantity"],
        "rows": [["SO-AI-1", "1", "BOARD-A", "50"]],
        "suggested_fixed_fields": {"customer": "仅供核对"},
        "page_count": 1,
    }


def upload(content: bytes, filename: str = "scan.pdf") -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=filename)


class DeepSeekCandidateTests(unittest.TestCase):
    def test_model_request_uses_images_and_rejects_malformed_output(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "columns": ["order_no", "quantity"],
            "rows": [["SO-AI-1", "50"]],
            "header_fields": {"customer": "仅供核对"},
        })}}]}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, *_):
                return json.dumps(response).encode()

        def fake_urlopen(request, timeout):
            self.assertEqual(timeout, 90)
            self.assertEqual(request.get_header("Authorization"), "Bearer mock-key")
            payload = json.loads(request.data)
            self.assertEqual(payload["model"], deepseek_import.MODEL)
            image = payload["messages"][0]["content"][1]
            self.assertTrue(image["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            return FakeResponse()

        with patch.object(deepseek_import, "render_pdf_images", return_value=["data:image/jpeg;base64,ZmFrZQ=="]), \
                patch.object(deepseek_import, "urlopen", side_effect=fake_urlopen):
            result = deepseek_import.extract_pdf_candidate(b"%PDF-synthetic", "orders", "mock-key")
        self.assertEqual(result["suggested_fixed_fields"], {"customer": "仅供核对"})
        with self.assertRaisesRegex(deepseek_import.AiExtractionError, "重复列名"):
            deepseek_import._validate_candidate({"columns": ["A", "A"], "rows": [["1", "2"]]}, "orders")
        with self.assertRaisesRegex(deepseek_import.AiExtractionError, "列数异常"):
            deepseek_import._validate_candidate({"columns": ["A", "B"], "rows": [["1", "2", "3"]]}, "orders")

    def test_page_limit_rejects_before_render_or_api_call(self):
        nine_pages = make_pdf([[[]] for _ in range(9)])
        with self.assertRaisesRegex(deepseek_import.AiExtractionError, "8 页"):
            deepseek_import.render_pdf_images(nine_pages)

        class TooWidePdf:
            pages = [type("Page", (), {"width": 4000, "height": 100})()]

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        with patch.object(deepseek_import.pdfplumber, "open", return_value=TooWidePdf()):
            with self.assertRaisesRegex(deepseek_import.AiExtractionError, "尺寸过大"):
                deepseek_import.render_pdf_images(b"%PDF-wide")


class AiImportRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = backend.DB_PATH
        backend.DB_PATH = Path(self.temp.name) / "ai-imports.db"
        backend.init_db()
        salt = secrets.token_hex(16)
        with backend.writing() as conn:
            created = conn.execute(
                "INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                ("ai_admin", "AI 导入员", "admin", salt, backend.password_digest("securepass123", salt), backend.utcnow()),
            )
            self.user = backend.must_get(conn, "users", created.lastrowid)
            second = conn.execute(
                "INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                ("other_admin", "其他用户", "admin", salt, backend.password_digest("securepass123", salt), backend.utcnow()),
            )
            self.other = backend.must_get(conn, "users", second.lastrowid)
        self.pdf = rasterize_pdf(make_pdf([[["order_no", "line_no", "product_code", "quantity"],
                                             ["SO-AI-1", "1", "BOARD-A", "50"]]]))

    def tearDown(self):
        backend.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_status_and_missing_key_do_not_start_ai(self):
        self.assertEqual(backend.request_permission("POST", "/api/imports/ai-extract"), "imports_manage")
        with patch.object(deepseek_import, "api_key", return_value=None), \
                patch.object(deepseek_import, "extract_pdf_candidate") as extract:
            self.assertEqual(backend.ai_import_status(user=self.user),
                             {"configured": False, "model": deepseek_import.MODEL})
            with self.assertRaises(HTTPException) as raised:
                backend.ai_extract_import(upload(self.pdf), kind="orders", user=self.user)
            self.assertEqual(raised.exception.status_code, 503)
            extract.assert_not_called()

    def test_extraction_cache_preview_binding_and_apply_preserve_original(self):
        with patch.object(deepseek_import, "api_key", return_value="mock-key"), \
                patch.object(deepseek_import, "extract_pdf_candidate", return_value=candidate()) as extract:
            first = backend.ai_extract_import(upload(self.pdf), kind="orders", user=self.user)
            cached = backend.ai_extract_import(upload(self.pdf), kind="orders", user=self.user)
        self.assertEqual(extract.call_count, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(cached["cached"])
        self.assertEqual(first["ai_extraction_id"], cached["ai_extraction_id"])
        self.assertTrue(first["ai"])
        self.assertFalse(first["ocr"])
        self.assertEqual(first["suggested_fixed_fields"], {"customer": "仅供核对"})
        self.assertEqual(first["sheets"][0]["row_count"], 1)
        with backend.reading() as conn:
            stored = backend.one(conn, "SELECT * FROM ai_extractions WHERE id=?", (first["ai_extraction_id"],))
            self.assertEqual(stored["source_blob"], self.pdf)
            self.assertIn(b"SO-AI-1", stored["csv_blob"])
            self.assertEqual(stored["model"], deepseek_import.MODEL)
        for changed_kind, changed_pdf, changed_user in (
            ("bom", self.pdf, self.user),
            ("orders", b"%PDF-other", self.user),
            ("orders", self.pdf, self.other),
        ):
            with self.subTest(kind=changed_kind, user=changed_user["id"]):
                with self.assertRaises(HTTPException) as raised:
                    backend.preview_import_route(
                        file=upload(changed_pdf), kind=changed_kind, column_map="{}", sheet_name=None,
                        header_row=1, key_fields=None, snapshot_as_of=None, snapshot_basis=None,
                        fixed_fields="{}", ai_extraction_id=first["ai_extraction_id"], user=changed_user,
                    )
                self.assertEqual(raised.exception.status_code, 422)
        preview = backend.preview_import_route(
            file=upload(self.pdf), kind="orders", column_map="{}", sheet_name="CSV",
            header_row=1, key_fields=None, snapshot_as_of=None, snapshot_basis=None,
            fixed_fields="{}", ai_extraction_id=first["ai_extraction_id"], user=self.user,
        )
        self.assertEqual(preview["errors"], [])
        self.assertEqual(preview["summary"]["create"], 1)
        self.assertTrue(preview["summary"]["ai"])
        self.assertIn("ai_review", [item["code"] for item in preview["warnings"]])
        applied = backend.apply_import({"preview_id": preview["preview_id"],
                                        "selections": [{"row_index": 0}]}, user=self.user)
        self.assertEqual(applied["rows_applied"], 1)
        with backend.reading() as conn:
            batch = backend.one(conn, "SELECT * FROM import_batches WHERE id=?", (applied["id"],))
            self.assertEqual(batch["source_blob"], self.pdf)
            metadata = json.loads(batch["selection_json"])["metadata"]
            self.assertEqual(metadata["ai_extraction_id"], first["ai_extraction_id"])
            self.assertEqual(metadata["ai_model"], deepseek_import.MODEL)
            self.assertIsNotNone(backend.one(conn, "SELECT id FROM orders WHERE order_no='SO-AI-1'"))

    def test_invalid_ai_result_never_creates_candidate(self):
        with patch.object(deepseek_import, "api_key", return_value="mock-key"), \
                patch.object(deepseek_import, "extract_pdf_candidate",
                             side_effect=deepseek_import.AiExtractionError("AI 表格无法核对")):
            with self.assertRaises(HTTPException) as raised:
                backend.ai_extract_import(upload(self.pdf), kind="orders", user=self.user)
        self.assertEqual(raised.exception.status_code, 422)
        with backend.reading() as conn:
            self.assertEqual(backend.one(conn, "SELECT COUNT(*) AS n FROM ai_extractions")["n"], 0)

    def test_file_limit_and_unapplied_cache_retention(self):
        too_large = b"%PDF-" + b"x" * backend.MAX_BYTES
        with patch.object(deepseek_import, "extract_pdf_candidate") as extract:
            with self.assertRaises(HTTPException) as raised:
                backend.ai_extract_import(upload(too_large), kind="orders", user=self.user)
            self.assertEqual(raised.exception.status_code, 413)
            extract.assert_not_called()
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
        with backend.writing() as conn:
            batch = conn.execute(
                "INSERT INTO import_batches(kind,filename,source_blob,selection_json,rows_applied,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                ("orders", "applied.pdf", b"%PDF-applied", "{}", 1, self.user["id"], old),
            ).lastrowid
            for index, linked in enumerate((None, batch, None)):
                conn.execute(
                    "INSERT INTO ai_extractions(id,kind,filename,source_sha256,source_blob,csv_blob,metadata_json,provider,model,created_by,created_at,applied_import_batch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"old-{index}", "orders", f"old-{index}.pdf", str(index), b"%PDF-old", b"A,B\n1,2", "{}",
                     "DeepSeek", deepseek_import.MODEL, self.user["id"], old, linked),
                )
            conn.execute(
                "INSERT INTO import_previews(id,kind,filename,preview_json,source_blob,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                ("active-preview", "orders", "old-2.pdf", json.dumps({"ai_extraction_id": "old-2"}),
                 b"%PDF-old", self.user["id"], backend.utcnow()),
            )
            backend.prune_unapplied_ai_extractions(conn)
        with backend.reading() as conn:
            remaining = [row["id"] for row in backend.rows(conn, "SELECT id FROM ai_extractions ORDER BY id")]
        self.assertEqual(remaining, ["old-1", "old-2"])


if __name__ == "__main__":
    unittest.main()

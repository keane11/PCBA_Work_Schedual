"""Bulk owner setup previews must bind only the intended unassigned records."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from server import app as backend


class OwnershipSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        backend.DB_PATH = Path(self.temp.name) / "pcba.db"
        backend.init_db()
        with backend.writing() as conn:
            created = backend.utcnow()
            conn.execute("INSERT INTO users(id,username,display_name,role,password_salt,password_hash,created_at,status,active) VALUES(?,?,?,?,?,?,?,?,?)",
                         (1, "admin", "管理员", "admin", "00" * 16, "unused", created, "active", 1))
            conn.execute("INSERT INTO users(id,username,display_name,role,password_salt,password_hash,created_at,status,active) VALUES(?,?,?,?,?,?,?,?,?)",
                         (2, "business", "唐林林", "business", "00" * 16, "unused", created, "active", 1))
            conn.execute("INSERT INTO users(id,username,display_name,role,password_salt,password_hash,created_at,status,active) VALUES(?,?,?,?,?,?,?,?,?)",
                         (3, "factory", "生产员", "warehouse", "00" * 16, "unused", created, "active", 1))
            conn.execute("INSERT INTO import_batches(id,kind,filename,source_blob,selection_json,rows_applied,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (1, "legacy", "old.json", b"{}", "{}", 1, 1, created))
            for order_id, product in ((1, "板 A"), (2, "板 B")):
                conn.execute("INSERT INTO orders(id,order_no,customer,project_name,owner_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                             (order_id, f"PCBA-TEMP-{order_id}", "待补录", product, None, created, created))
                conn.execute("INSERT INTO order_lines(id,order_id,line_no,product_code,product_name,quantity,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                             (order_id, order_id, "1", f"P{order_id}", product, "10", created, created))
                conn.execute("INSERT INTO production_batches(id,order_line_id,batch_no,quantity,phase,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                             (order_id, order_id, "P1", "10", "mp", created, created))
                conn.execute("INSERT INTO stages(id,production_batch_id,sequence,name,planned_start,planned_finish,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (order_id, order_id, 1, "SMT 贴片", "2026-10-01", "2026-10-02", "pending", created, created))
                conn.execute("INSERT INTO legacy_records(id,source_key,source_hash,filename,source_index,source_json,order_id,order_line_id,production_batch_id,import_batch_id,missing_fields_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (order_id, f"old-{order_id}", f"hash-{order_id}", "old.json", order_id,
                              json.dumps({"name": product, "owner": "唐林林"}, ensure_ascii=False),
                              order_id, order_id, order_id, 1, "[]", 1, created))
        self.admin = {"id": 1, "role": "admin"}
        self.bindings = {"order_bindings": [{"legacy_name": "唐林林", "user_id": 2}],
                         "stage_bindings": [{"phase": "mp", "name": "SMT 贴片", "user_id": 3}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_then_apply_is_atomic_and_audited(self):
        groups = backend.get_ownership_setup(user=self.admin)
        self.assertEqual(groups["order_groups"], [{"legacy_name": "唐林林", "count": 2}])
        self.assertEqual(groups["stage_groups"][0]["count"], 2)
        preview = backend.preview_ownership_setup(self.bindings, user=self.admin)
        self.assertEqual(preview["summary"], {"orders": 2, "stages": 2})
        with backend.reading() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders WHERE owner_id IS NOT NULL").fetchone()[0], 0)
        result = backend.apply_ownership_setup({**self.bindings, "signature": preview["signature"],
                                                "reason": "核对历史责任分工"}, user=self.admin)
        self.assertTrue(result["ok"])
        with backend.reading() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders WHERE owner_id=2").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM stages WHERE owner_id=3").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit WHERE action='bulk_bind_owner' AND reason='核对历史责任分工'").fetchone()[0], 4)

    def test_stale_preview_or_non_admin_cannot_apply(self):
        preview = backend.preview_ownership_setup(self.bindings, user=self.admin)
        with backend.writing() as conn:
            conn.execute("UPDATE stages SET version=version+1 WHERE id=1")
        with self.assertRaises(HTTPException) as stale:
            backend.apply_ownership_setup({**self.bindings, "signature": preview["signature"],
                                           "reason": "历史分工"}, user=self.admin)
        self.assertEqual(stale.exception.status_code, 409)
        with backend.reading() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders WHERE owner_id IS NOT NULL").fetchone()[0], 0)
        with self.assertRaises(HTTPException) as forbidden:
            backend.preview_ownership_setup(self.bindings, user={"id": 2, "role": "business"})
        self.assertEqual(forbidden.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()

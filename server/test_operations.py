"""Operational checks must remain reliable with bad logins and an unavailable mirror."""

from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException, Request

_import_temp = tempfile.TemporaryDirectory()
os.environ["PCBA_DB_PATH"] = str(Path(_import_temp.name) / "import.db")
from server import app as backend
from server.backup import backup_database, backup_status


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = backend.DB_PATH
        self.old_mirror = os.environ.get("PCBA_BACKUP_MIRROR_DIR")
        backend.DB_PATH = Path(self.temp.name) / "pcba.db"
        backend.init_db()
        salt = secrets.token_hex(16)
        with backend.writing() as conn:
            conn.execute("INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                         ("admin", "管理员", "admin", salt,
                          backend.password_digest("correctPassword123", salt), backend.utcnow()))

    def tearDown(self):
        backend.DB_PATH = self.old_db
        if self.old_mirror is None:
            os.environ.pop("PCBA_BACKUP_MIRROR_DIR", None)
        else:
            os.environ["PCBA_BACKUP_MIRROR_DIR"] = self.old_mirror
        self.temp.cleanup()

    def test_failed_login_is_limited_and_success_clears_attempts(self):
        request = Request({"type": "http", "client": ("192.0.2.10", 1234), "headers": []})
        with self.assertRaises(HTTPException) as incorrect:
            backend.login({"username": "admin", "password": "wrong"}, request)
        self.assertEqual(incorrect.exception.status_code, 401)
        self.assertTrue(backend.login({"username": "admin", "password": "correctPassword123"}, request)["token"])
        with backend.reading() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0], 0)
        for _ in range(10):
            with self.assertRaises(HTTPException) as failure:
                backend.login({"username": "admin", "password": "wrong"}, request)
            self.assertEqual(failure.exception.status_code, 401)
        with self.assertRaises(HTTPException) as blocked:
            backend.login({"username": "admin", "password": "correctPassword123"}, request)
        self.assertEqual(blocked.exception.status_code, 429)
        self.assertEqual(blocked.exception.headers["Retry-After"], "900")
        other_host = Request({"type": "http", "client": ("192.0.2.11", 1234), "headers": []})
        self.assertTrue(backend.login({"username": "admin", "password": "correctPassword123"}, other_host)["token"])

    def test_backup_mirror_failure_keeps_local_copy_and_health_reports_it(self):
        os.environ["PCBA_BACKUP_MIRROR_DIR"] = str(Path(self.temp.name) / "mirror")
        first = backup_database(backend.DB_PATH)
        mirror = Path(os.environ["PCBA_BACKUP_MIRROR_DIR"])
        self.assertTrue(first.is_file())
        with closing(sqlite3.connect(mirror / first.name)) as copied:
            self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertTrue(backup_status(backend.DB_PATH)["mirror_ok"])
        self.assertEqual(backend.health()["backup"]["mirror_ok"], True)

        unusable = Path(self.temp.name) / "not-a-directory"
        unusable.write_text("occupied", encoding="utf-8")
        os.environ["PCBA_BACKUP_MIRROR_DIR"] = str(unusable)
        second = backup_database(backend.DB_PATH)
        self.assertTrue(second.is_file())
        status = backup_status(backend.DB_PATH)
        self.assertFalse(status["mirror_ok"])
        self.assertEqual(backend.health()["backup"]["mirror_ok"], False)
        self.assertIsNotNone(status["last_mirror_success_at"])

    def test_health_uses_actual_backup_file_and_reports_low_disk_space(self):
        self.assertEqual(backend.health()["backup"]["local_status"], "missing")
        created = backup_database(backend.DB_PATH)
        fresh = backend.health()["backup"]
        self.assertEqual(fresh["local_status"], "fresh")
        self.assertEqual(fresh["last_local_filename"], created.name)
        self.assertLess(fresh["local_age_hours"], 1)

        old = created.stat().st_mtime - 48 * 3600
        os.utime(created, (old, old))
        with mock.patch("server.backup.shutil.disk_usage",
                        return_value=SimpleNamespace(free=512 * 1024**2)):
            stale = backend.health()["backup"]
        self.assertEqual(stale["local_status"], "stale")
        self.assertGreater(stale["local_age_hours"], 36)
        self.assertTrue(stale["disk_low"])
        self.assertEqual(stale["disk_free_gib"], 0.5)

        created.unlink()
        self.assertEqual(backend.health()["backup"]["local_status"], "missing")

    def test_backup_failure_is_visible_until_next_success(self):
        with mock.patch("server.backup.sqlite3.connect",
                        side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                backup_database(backend.DB_PATH)
        self.assertIsNotNone(backend.health()["backup"]["last_local_failure_at"])
        backup_database(backend.DB_PATH)
        self.assertIsNone(backend.health()["backup"]["last_local_failure_at"])

    def test_health_detects_unusable_database(self):
        self.assertTrue(backend.health()["ok"])
        with backend.writing() as conn:
            conn.execute("DROP TABLE users")
        with self.assertRaises(HTTPException) as failure:
            backend.health()
        self.assertEqual(failure.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()

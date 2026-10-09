"""Targeted API checks for the shared quantities and forecast rules."""

from __future__ import annotations

import tempfile
import unittest
import inspect
import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from starlette.routing import Match

_import_temp = tempfile.TemporaryDirectory()
os.environ["PCBA_DB_PATH"] = str(Path(_import_temp.name) / "import.db")
from server import app as backend


class WorkbenchApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        backend.DB_PATH = Path(self.temp.name) / "pcba.db"
        backend.init_db()
        salt = secrets.token_hex(16)
        with backend.writing() as conn:
            conn.execute("INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                         ("admin", "管理员", "admin", salt, backend.password_digest("securepass123", salt), backend.utcnow()))
        self.headers = {"Authorization": f"Bearer {backend.login({'username': 'admin', 'password': 'securepass123'})['token']}"}

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, path, data=None, *, headers=None):
        class Result:
            def __init__(self, status_code, value):
                self.status_code = status_code
                self.value = value
                self.text = json.dumps(value, ensure_ascii=False, default=str)

            def json(self):
                return self.value

        parsed = urlsplit(path)
        route, path_params = next(
            (route, child.get("path_params", {}))
            for route in backend.app.routes
            if hasattr(route, "methods") and (matched := route.matches({"type": "http", "method": method, "path": parsed.path, "root_path": ""}))[0] == Match.FULL
            for child in [matched[1]]
        )
        signature = inspect.signature(route.endpoint)
        kwargs = {key: int(value) if key in signature.parameters
                  and signature.parameters[key].annotation in (int, "int") else value
                  for key, value in path_params.items()}
        query = parse_qs(parsed.query)
        for key, values in query.items():
            if key in signature.parameters:
                kwargs[key] = int(values[0]) if key in {"entity_id", "limit"} else values[0]
        if "data" in signature.parameters:
            kwargs["data"] = data
        if "user" in signature.parameters:
            kwargs["user"] = backend.require_user((headers or self.headers)["Authorization"])
        try:
            return Result(200, route.endpoint(**kwargs))
        except HTTPException as exc:
            return Result(exc.status_code, {"detail": exc.detail})

    def order(self, number="SO1", quantity="10"):
        response = self.call("POST", "/api/orders", {"order_no": number, "customer": "客户 A",
                         "lines": [{"line_no": "1", "product_code": "PCB-A", "product_name": "控制板", "quantity": quantity}]})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        return result, result["lines"][0]

    def batch(self, line, number="P1", quantity="5", material="M1", **extra):
        payload = {"order_line_id": line["id"], "batch_no": number, "quantity": quantity,
                   "bom_version": "V1", "bom_items": [{"material_code": material, "quantity_per": "1"}], **extra}
        response = self.call("POST", "/api/production-batches", payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_internal_rd_project_can_start_without_customer_sales_order(self):
        created = self.call("POST", "/api/orders", {
            "project_mode": "internal", "project_name": "K52 话筒接收研发打样",
            "lines": [{"line_no": "1", "product_code": "K52-RX",
                       "product_name": "K52 话筒接收", "quantity": "10"}],
        })
        self.assertEqual(created.status_code, 200, created.text)
        project = created.json()
        self.assertEqual(project["project_mode"], "internal")
        self.assertEqual(project["project_name"], "K52 话筒接收研发打样")
        self.assertTrue(project["order_no"].startswith("PCBA-PROJ-"))
        self.assertEqual(project["customer"], "内部研发")
        line = project["lines"][0]
        batch = self.call("POST", "/api/production-batches", {
            "order_line_id": line["id"], "batch_no": "研发打样-1", "quantity": "10",
            "phase": "rd", "phase_source": "quantity_rule", "phase_order_no": "嘉立创-1",
            "schedule_start": "2026-09-28", "sampling_order_date": "2026-09-29",
            "board_eta": "2026-10-08", "target_kind": "internal",
            "target_date": "2026-10-16",
        })
        self.assertEqual(batch.status_code, 200, batch.text)
        details = self.call("GET", f"/api/production-batches/{batch.json()['id']}").json()
        self.assertEqual(details["phase"], "rd")
        self.assertEqual(details["phase_order_no"], "嘉立创-1")
        self.assertTrue(any("嘉立创" in stage["name"] for stage in details["stages"]))
        self.assertTrue(any(stage["name"] == "样板到料" for stage in details["stages"]))
        self.assertFalse(any("板厂" in stage["name"] for stage in details["stages"]))
        incomplete = self.call("PATCH", f"/api/orders/{project['id']}", {
            "version": project["version"], "changes": {"project_mode": "customer"},
            "reason": "收到客户销售单",
        })
        self.assertEqual(incomplete.status_code, 422)
        converted = self.call("PATCH", f"/api/orders/{project['id']}", {
            "version": project["version"],
            "changes": {"project_mode": "customer", "order_no": "SO-K52-001", "customer": "客户甲"},
            "reason": "收到客户销售单",
        })
        self.assertEqual(converted.status_code, 200, converted.text)
        self.assertEqual(converted.json()["project_mode"], "customer")

    def test_permission_middleware_accepts_starlette_keyword_constructor(self):
        # Starlette 0.41 passes app=...; older test environments pass positional.
        middleware = backend.PermissionMiddleware(app=backend.app)
        self.assertIs(middleware.inner_app, backend.app)

    def test_registration_waits_for_admin_approval_and_keeps_password_private(self):
        self.assertEqual(self.call("GET", "/api/auth/status").json()["initialized"], True)
        requested = self.call("POST", "/api/auth/register", {
            "username": "tanglinlin", "display_name": "唐林林", "password": "registrationSecret123",
        })
        self.assertEqual(requested.status_code, 200, requested.text)
        self.assertEqual(requested.json()["status"], "pending")
        self.assertEqual(self.call("POST", "/api/auth/register", {
            "username": "tanglinlin", "display_name": "唐林林", "password": "registrationSecret123",
        }).status_code, 409)
        pending = self.call("GET", "/api/users?status=pending").json()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["display_name"], "唐林林")
        self.assertEqual(pending[0]["active"], 0)
        self.assertNotIn("registrationSecret123", json.dumps(pending, ensure_ascii=False))
        self.assertNotIn("password_hash", pending[0])
        self.assertEqual(self.call("POST", "/api/auth/login", {
            "username": "tanglinlin", "password": "registrationSecret123",
        }).status_code, 403)
        approved = self.call("POST", f"/api/users/{pending[0]['id']}/approve", {"role": "business"})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "active")
        self.assertTrue(backend.login({"username": "tanglinlin", "password": "registrationSecret123"})["token"])

    def test_account_migration_preserves_old_admin_and_disabled_user(self):
        migrated_db = Path(self.temp.name) / "preapproval.db"
        with closing(sqlite3.connect(migrated_db)) as conn, conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, role TEXT NOT NULL, password_salt TEXT NOT NULL, password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
            for username, enabled in (("oldadmin", 1), ("oldworker", 0)):
                salt = secrets.token_hex(16)
                conn.execute("INSERT INTO users(username,display_name,role,password_salt,password_hash,active,created_at) VALUES(?,?,?,?,?,?,?)",
                             (username, username, "admin" if enabled else "business", salt,
                              backend.password_digest("legacyPassword123", salt), enabled, backend.utcnow()))
        backend.DB_PATH = migrated_db
        backend.init_db()
        migrated = {item["username"]: item for item in backend.users(user={"role": "admin"})}
        self.assertEqual(migrated["oldadmin"]["status"], "active")
        self.assertEqual(migrated["oldworker"]["status"], "disabled")
        self.assertTrue(backend.login({"username": "oldadmin", "password": "legacyPassword123"})["token"])
        self.assertEqual(self.call("POST", "/api/auth/login", {
            "username": "oldworker", "password": "legacyPassword123",
        }).status_code, 403)

    def test_reject_disable_and_password_management_revoke_sessions(self):
        self.call("POST", "/api/auth/register", {
            "username": "baixuefei", "display_name": "白雪飞", "password": "registrationSecret123",
        })
        pending = self.call("GET", "/api/users?status=pending").json()[0]
        rejected = self.call("POST", f"/api/users/{pending['id']}/reject", {"reason": "资料待核对"})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["status"], "rejected")
        self.assertEqual(self.call("POST", "/api/auth/login", {
            "username": "baixuefei", "password": "registrationSecret123",
        }).status_code, 403)
        self.assertEqual(self.call("PATCH", f"/api/users/{pending['id']}", {"active": True}).status_code, 409)
        self.assertEqual(self.call("POST", f"/api/users/{pending['id']}/approve", {"role": "engineering"}).status_code, 200)
        old_token = backend.login({"username": "baixuefei", "password": "registrationSecret123"})["token"]
        reset = self.call("POST", f"/api/users/{pending['id']}/reset-password", {"password": "newSecurePassword123"})
        self.assertEqual(reset.status_code, 200, reset.text)
        with self.assertRaises(HTTPException):
            backend.require_user(f"Bearer {old_token}")
        self.assertEqual(self.call("POST", "/api/auth/login", {
            "username": "baixuefei", "password": "registrationSecret123",
        }).status_code, 401)
        fresh = backend.login({"username": "baixuefei", "password": "newSecurePassword123"})["token"]
        changed = self.call("POST", "/api/auth/change-password", {
            "current_password": "newSecurePassword123", "new_password": "evenNewerPassword123",
        }, headers={"Authorization": f"Bearer {fresh}"})
        self.assertEqual(changed.status_code, 200, changed.text)
        with self.assertRaises(HTTPException):
            backend.require_user(f"Bearer {fresh}")
        self.assertTrue(backend.login({"username": "baixuefei", "password": "evenNewerPassword123"})["token"])
        disabled = self.call("PATCH", f"/api/users/{pending['id']}", {"active": False})
        self.assertEqual(disabled.json()["status"], "disabled")
        self.assertEqual(self.call("POST", "/api/auth/login", {
            "username": "baixuefei", "password": "evenNewerPassword123",
        }).status_code, 403)
        self.assertEqual(self.call("PATCH", "/api/users/1", {"active": False}).status_code, 409)

    def test_delete_account_transfers_open_work_and_preserves_history(self):
        departing = self.call("POST", "/api/users", {
            "username": "departing", "display_name": "离岗业务", "role": "business",
            "password": "departingPassword123",
        }).json()
        successor = self.call("POST", "/api/users", {
            "username": "successor", "display_name": "接替业务", "role": "business",
            "password": "successorPassword123",
        }).json()
        departing_token = backend.login({"username": "departing", "password": "departingPassword123"})["token"]
        departing_headers = {"Authorization": f"Bearer {departing_token}"}
        created = self.call("POST", "/api/orders", {
            "order_no": "SO-ACCOUNT-DELETE", "customer": "客户 A", "owner_id": departing["id"],
            "lines": [{"line_no": "1", "product_code": "BOARD-A", "product_name": "主板", "quantity": "10"}],
        })
        self.assertEqual(created.status_code, 200, created.text)
        order = created.json()
        changed = self.call("PATCH", f"/api/orders/{order['id']}", {
            "version": order["version"], "changes": {"project_name": "历史项目"},
        }, headers=departing_headers)
        self.assertEqual(changed.status_code, 200, changed.text)
        batch = self.batch(order["lines"][0], quantity="10")
        stages = self.call("GET", f"/api/production-batches/{batch['id']}/stages").json()
        self.assertGreaterEqual(len(stages), 2)
        active_stage = self.call("PATCH", f"/api/stages/{stages[0]['id']}", {
            "version": stages[0]["version"], "changes": {"owner_id": departing["id"]},
        })
        self.assertEqual(active_stage.status_code, 200, active_stage.text)
        with backend.writing() as conn:
            conn.execute("UPDATE stages SET owner_id=?,completed_by=?,status='done',actual_finish=? WHERE id=?",
                         (departing["id"], departing["id"], backend.today(), stages[1]["id"]))
            now = backend.utcnow()
            line_board_id = conn.execute("""INSERT INTO order_line_boards
                (order_line_id,board_ref,board_name,bom_product_code,quantity_per_product,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""", (order["lines"][0]["id"], "MAIN", "主板", "BOARD-A", "1", now, now)).lastrowid
            batch_board_id = conn.execute("""INSERT INTO batch_boards
                (production_batch_id,order_line_board_id,board_ref,board_name,bom_product_code,
                 quantity_per_product,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                (batch["id"], line_board_id, "MAIN", "主板", "BOARD-A", "1", now, now)).lastrowid
            board_stage_id = conn.execute("""INSERT INTO board_stages
                (batch_board_id,sequence,name,owner_id,owner_name,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (batch_board_id, 1, "SMT", departing["id"], "离岗业务", now, now)).lastrowid
            task_id = conn.execute("""INSERT INTO tasks(task_key,kind,entity_type,entity_id,title,assignee_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""", ("account-delete-hand-off", "manual", "orders", order["id"],
                "离岗前待交接", departing["id"], backend.utcnow(), backend.utcnow())).lastrowid
        preview = self.call("GET", f"/api/users/{departing['id']}/delete-preview")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["assignments"]["orders"], 1)
        self.assertEqual(preview.json()["assignments"]["stages"], 1)
        self.assertEqual(preview.json()["assignments"]["board_stages"], 1)
        self.assertGreaterEqual(preview.json()["assignments"]["tasks"], 1)

        deleted = self.call("DELETE", f"/api/users/{departing['id']}", {
            "replacement_user_id": successor["id"], "reason": "人员离职，工作交接",
        })
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["transferred"], preview.json()["assignments"])
        with self.assertRaises(HTTPException):
            backend.require_user(f"Bearer {departing_token}")
        self.assertEqual(self.call("POST", "/api/auth/login", {
            "username": "departing", "password": "departingPassword123",
        }).status_code, 401)
        listed = self.call("GET", "/api/users").json()
        self.assertNotIn(departing["id"], [item["id"] for item in listed])
        archived = self.call("GET", "/api/users?status=deleted").json()
        self.assertEqual([item["id"] for item in archived], [departing["id"]])
        self.assertEqual((archived[0]["active"], archived[0]["status"]), (0, "deleted"))
        self.assertEqual(self.call("POST", "/api/users", {
            "username": "departing", "display_name": "同名新账号", "role": "business",
            "password": "replacementPassword123",
        }).status_code, 409)
        self.assertEqual(self.call("PATCH", f"/api/users/{departing['id']}", {"active": True}).status_code, 409)
        self.assertEqual(self.call("POST", f"/api/users/{departing['id']}/reset-password", {
            "password": "anotherPassword123",
        }).status_code, 409)
        with backend.reading() as conn:
            live_order = backend.must_get(conn, "orders", order["id"])
            live_stage = backend.must_get(conn, "stages", stages[0]["id"])
            live_board_stage = backend.must_get(conn, "board_stages", board_stage_id)
            completed_stage = backend.must_get(conn, "stages", stages[1]["id"])
            task = backend.must_get(conn, "tasks", task_id)
            historical_change = backend.one(conn, "SELECT actor_id FROM audit WHERE entity_type='orders' AND entity_id=? AND action='update' ORDER BY id DESC LIMIT 1", (order["id"],))
            account_audit = backend.one(conn, "SELECT actor_id,reason,before_json,after_json FROM audit WHERE entity_type='user' AND entity_id=? AND action='delete'", (departing["id"],))
            handoff_audit = backend.one(conn, "SELECT actor_id,reason FROM audit WHERE entity_type='tasks' AND entity_id=? AND action='transfer_on_user_delete'", (task_id,))
        self.assertEqual(live_order["owner_id"], successor["id"])
        self.assertEqual((live_stage["owner_id"], live_stage["owner_name"]),
                         (successor["id"], "接替业务"))
        self.assertEqual((live_board_stage["owner_id"], live_board_stage["owner_name"]),
                         (successor["id"], "接替业务"))
        self.assertEqual((completed_stage["owner_id"], completed_stage["completed_by"]),
                         (departing["id"], departing["id"]))
        self.assertEqual(task["assignee_id"], successor["id"])
        self.assertEqual(historical_change["actor_id"], departing["id"])
        self.assertEqual((account_audit["actor_id"], account_audit["reason"]), (1, "人员离职，工作交接"))
        self.assertEqual(json.loads(account_audit["before_json"])["status"], "active")
        self.assertEqual(json.loads(account_audit["after_json"])["status"], "deleted")
        self.assertEqual((handoff_audit["actor_id"], handoff_audit["reason"]), (1, "人员离职，工作交接"))

    def test_delete_account_rejects_self_and_invalid_handoff_without_changes(self):
        departing = self.call("POST", "/api/users", {
            "username": "deleteguard", "display_name": "待交接人员", "role": "business",
            "password": "deleteguardPassword123",
        }).json()
        unavailable = self.call("POST", "/api/users", {
            "username": "unavailable", "display_name": "已停用人员", "role": "business",
            "password": "unavailablePassword123",
        }).json()
        self.assertEqual(self.call("PATCH", f"/api/users/{unavailable['id']}", {"active": False}).status_code, 200)
        target_path = f"/api/users/{departing['id']}"
        for payload, expected in (
            ({"replacement_user_id": 1, "reason": " "}, 422),
            ({"replacement_user_id": departing["id"], "reason": "无法接替自己"}, 422),
            ({"replacement_user_id": unavailable["id"], "reason": "接替人已停用"}, 422),
            ({"replacement_user_id": 999999, "reason": "接替人不存在"}, 404),
        ):
            response = self.call("DELETE", target_path, payload)
            self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self.call("DELETE", "/api/users/1", {
            "replacement_user_id": departing["id"], "reason": "管理员误操作",
        }).status_code, 409)
        self.assertIn(departing["id"], [item["id"] for item in self.call("GET", "/api/users?status=active").json()])
        with backend.reading() as conn:
            self.assertEqual(backend.must_get(conn, "users", departing["id"])["status"], "active")
            self.assertIsNone(backend.one(conn, "SELECT id FROM audit WHERE entity_type='user' AND entity_id=? AND action='delete'", (departing["id"],)))

    def test_delete_account_requires_admin(self):
        departing = self.call("POST", "/api/users", {
            "username": "otherworker", "display_name": "另一员工", "role": "business",
            "password": "otherworkerPassword123",
        }).json()
        worker = self.call("POST", "/api/users", {
            "username": "workeraccount", "display_name": "普通员工", "role": "business",
            "password": "workeraccountPassword123",
        }).json()
        worker_token = backend.login({"username": worker["username"], "password": "workeraccountPassword123"})["token"]
        headers = {"Authorization": f"Bearer {worker_token}"}
        self.assertEqual(self.call("GET", f"/api/users/{departing['id']}/delete-preview", headers=headers).status_code, 403)
        self.assertEqual(self.call("DELETE", f"/api/users/{departing['id']}", {
            "replacement_user_id": 1, "reason": "无权操作",
        }, headers=headers).status_code, 403)
        with backend.reading() as conn:
            self.assertEqual(backend.must_get(conn, "users", departing["id"])["status"], "active")

    def test_permission_switch_is_enforced_by_asgi_gate(self):
        new_user = self.call("POST", "/api/users", {
            "username": "purchaser", "display_name": "采购", "role": "purchasing", "password": "securePassword123",
        }).json()
        token = backend.login({"username": "purchaser", "password": "securePassword123"})["token"]
        denied = self.call("PATCH", f"/api/users/{new_user['id']}", {"permissions": {"orders_edit": False}})
        self.assertEqual(denied.status_code, 200, denied.text)
        with self.assertRaises(HTTPException):
            backend.require_user(f"Bearer {token}")
        token = backend.login({"username": "purchaser", "password": "securePassword123"})["token"]

        async def pass_through(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def try_permission(method):
            sent = []
            scope = {"type": "http", "method": method, "path": "/api/orders",
                     "headers": [(b"authorization", f"Bearer {token}".encode())]}
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                sent.append(message)
            await backend.PermissionMiddleware(pass_through)(scope, receive, send)
            return sent[0]["status"]

        self.assertEqual(asyncio.run(try_permission("GET")), 200)
        self.assertEqual(asyncio.run(try_permission("POST")), 403)
        self.assertEqual(backend.request_permission("POST", "/api/legacy/apply"), "imports_manage")
        self.assertEqual(backend.request_permission("GET", "/api/audit"), "audit_view")
        self.assertEqual(self.call("GET", "/api/users", headers={"Authorization": f"Bearer {token}"}).json()[0]["status"], "active")

    def test_version_reason_and_many_to_many_allocations(self):
        order, line = self.order()
        batch = self.batch(line, quantity="10")
        d1 = self.call("POST", "/api/delivery-batches", {"order_line_id": line["id"], "batch_no": "D1", "quantity": "4"}).json()
        d2 = self.call("POST", "/api/delivery-batches", {"order_line_id": line["id"], "batch_no": "D2", "quantity": "6"}).json()
        self.assertEqual(self.call("POST", "/api/allocations", {"production_batch_id": batch["id"], "delivery_batch_id": d1["id"], "quantity": "4"}).status_code, 200)
        self.assertEqual(self.call("POST", "/api/allocations", {"production_batch_id": batch["id"], "delivery_batch_id": d2["id"], "quantity": "6"}).status_code, 200)
        self.assertEqual(self.call("POST", "/api/allocations", {"production_batch_id": batch["id"], "delivery_batch_id": d2["id"], "quantity": "1"}).status_code, 409)
        self.assertEqual(self.call("PATCH", f"/api/delivery-batches/{d1['id']}", {"version": d1["version"], "changes": {"due_date": "2026-12-01"}}).status_code, 422)
        changed = self.call("PATCH", f"/api/delivery-batches/{d1['id']}", {"version": d1["version"], "changes": {"due_date": "2026-12-01"}, "reason": "客户邮件"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.call("PATCH", f"/api/delivery-batches/{d1['id']}", {"version": d1["version"], "changes": {"promised_date": "2026-12-02"}, "reason": "确认"}).status_code, 409)
        audit = self.call("GET", f"/api/audit?entity_type=delivery_batches&entity_id={d1['id']}").json()
        self.assertTrue(any(row["reason"] == "客户邮件" for row in audit))
        self.assertEqual(self.call("GET", "/api/overview").status_code, 200)

    def test_stock_reservation_iqc_reclassification_and_issue(self):
        _, line = self.order()
        first = self.batch(line, "P1", "5")
        second = self.batch(line, "P2", "5")
        snap = self.call("POST", "/api/inventory/snapshots", {"as_of": "2026-01-01", "basis": "available",
                    "items": [{"material_code": "M1", "quantity": "8"}]})
        self.assertEqual(snap.status_code, 200, snap.text)
        self.assertEqual(self.call("PUT", "/api/reservations", {"production_batch_id": first["id"], "material_code": "M1", "quantity": "5"}).status_code, 200)
        self.assertEqual(self.call("PUT", "/api/reservations", {"production_batch_id": second["id"], "material_code": "M1", "quantity": "5"}).status_code, 409)
        receipt = self.call("POST", "/api/receipts", {"material_code": "M1", "quantity": "2", "received_at": "2026-01-02", "production_batch_id": second["id"]}).json()
        passed = self.call("PATCH", f"/api/receipts/{receipt['id']}/iqc", {"version": receipt["version"], "status": "pass", "accepted_quantity": "2"})
        self.assertEqual(passed.status_code, 200, passed.text)
        self.assertEqual(self.call("PUT", "/api/reservations", {"production_batch_id": second["id"], "material_code": "M1", "quantity": "5"}).status_code, 200)
        failed = self.call("PATCH", f"/api/receipts/{receipt['id']}/iqc", {"version": passed.json()["version"], "status": "fail", "reason": "复检不合格"})
        self.assertEqual(failed.status_code, 200, failed.text)
        materials = self.call("GET", f"/api/production-batches/{second['id']}/materials").json()
        self.assertEqual(materials["items"][0]["shortage"], "2")
        task_response = self.call("GET", "/api/tasks?status=open")
        self.assertEqual(task_response.status_code, 200, task_response.text)
        tasks = task_response.json()
        self.assertTrue(any(task["kind"] == "iqc_failed" for task in tasks))
        issued = self.call("POST", f"/api/production-batches/{first['id']}/events", {"kind": "material_sent", "event_date": "2026-01-03", "material_code": "M1", "quantity": "3"})
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(self.call("GET", f"/api/production-batches/{first['id']}/materials").json()["items"][0]["shortage"], "0")

    def test_on_hand_snapshot_needs_available_confirmation_before_reservation(self):
        _, line = self.order("SO-STOCK-BASIS", quantity="5")
        batch = self.batch(line, quantity="5")
        day = backend.today()
        source = self.call("POST", "/api/inventory/snapshots", {
            "as_of": day, "basis": "on_hand", "items": [{"material_code": "M1", "quantity": "5"}]})
        self.assertEqual(source.status_code, 200, source.text)
        stock = self.call("GET", "/api/inventory").json()[0]
        self.assertEqual((stock["snapshot_quantity"], stock["usable_snapshot_quantity"], stock["capacity"]),
                         ("5", "0", "0"))
        self.assertTrue(stock["confirmation_required"])
        denied = self.call("PUT", "/api/reservations", {
            "production_batch_id": batch["id"], "material_code": "M1", "quantity": "5"})
        self.assertEqual(denied.status_code, 409)
        self.assertIn("现存量未经仓库核实", denied.json()["detail"])
        self.assertEqual(self.call("GET", f"/api/production-batches/{batch['id']}/materials").json()["items"][0]["shortage"], "5")
        self.assertTrue(any(task["kind"] == "stock_confirmation" and task["condition_active"]
                            for task in self.call("GET", "/api/tasks?status=open").json()))
        confirmed = self.call("POST", "/api/inventory/snapshots", {
            "as_of": day, "basis": "available", "source_name": "仓库核实可用量",
            "items": [{"material_code": "M1", "quantity": "5"}]})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        reserved = self.call("PUT", "/api/reservations", {
            "production_batch_id": batch["id"], "material_code": "M1", "quantity": "5"})
        self.assertEqual(reserved.status_code, 200, reserved.text)
        self.assertEqual(self.call("GET", f"/api/production-batches/{batch['id']}/materials").json()["status"], "ready")
        self.assertFalse(any(task["kind"] == "stock_confirmation" and task["condition_active"]
                             for task in self.call("GET", "/api/tasks?status=open").json()))

    def test_delivery_allocation_gap_is_task_and_unknown_coverage(self):
        day = date.fromisoformat(backend.today())
        order = self.call("POST", "/api/orders", {
            "order_no": "SO-ALLOC-GAP", "customer": "客户 A", "owner_id": 1,
            "lines": [{"line_no": "1", "product_code": "PCB-A", "product_name": "控制板", "quantity": "10"}]}).json()
        line = order["lines"][0]
        batch = self.batch(line, quantity="10", vendor="外协厂 A", commit_start=day.isoformat(),
                           commit_finish=(day + timedelta(days=30)).isoformat())
        self.call("POST", "/api/inventory/snapshots", {"as_of": day.isoformat(), "basis": "available",
                    "items": [{"material_code": "M1", "quantity": "10"}]})
        self.assertEqual(self.call("PUT", "/api/reservations", {
            "production_batch_id": batch["id"], "material_code": "M1", "quantity": "10"}).status_code, 200)
        delivery = self.call("POST", "/api/delivery-batches", {
            "order_line_id": line["id"], "batch_no": "D1", "quantity": "10",
            "due_date": (day + timedelta(days=45)).isoformat(),
            "promised_date": (day + timedelta(days=46)).isoformat()}).json()
        before = self.call("GET", "/api/overview").json()
        self.assertEqual(before["summary"]["red"], 0)
        self.assertEqual(before["summary"]["yellow"], 0)
        self.assertEqual(before["summary"]["unassessable"], 1)
        self.assertEqual(before["summary"]["coverage_percent"], 0)
        self.assertTrue(any(item["code"] == "delivery_unallocated" for item in before["assessment"][0]["missing_reasons"]))
        self.assertTrue(any(task["kind"] == "delivery_unallocated" and "尚有 10" in task["title"]
                            for task in before["tasks"] if task["condition_active"]))
        linked = self.call("POST", "/api/allocations", {
            "production_batch_id": batch["id"], "delivery_batch_id": delivery["id"], "quantity": "10"})
        self.assertEqual(linked.status_code, 200, linked.text)
        after = self.call("GET", "/api/overview").json()
        self.assertEqual(after["summary"]["assessable"], 1)
        self.assertEqual(after["summary"]["coverage_percent"], 100)
        self.assertFalse(any(task["kind"] == "delivery_unallocated" and task["condition_active"] for task in after["tasks"]))

    def test_overdue_unfinished_stage_requires_reconfirmation_and_owner_task(self):
        day = date.fromisoformat(backend.today())
        order = self.call("POST", "/api/orders", {
            "order_no": "SO-STAGE-LATE", "customer": "客户 A", "owner_id": 1,
            "lines": [{"line_no": "1", "product_code": "PCB-A", "product_name": "控制板", "quantity": "5"}]}).json()
        batch = self.batch(order["lines"][0], quantity="5", vendor="外协厂 A",
                           commit_start=day.isoformat(), commit_finish=(day + timedelta(days=30)).isoformat(),
                           schedule_start=(day - timedelta(days=5)).isoformat())
        self.call("POST", "/api/inventory/snapshots", {"as_of": day.isoformat(), "basis": "available",
                    "items": [{"material_code": "M1", "quantity": "5"}]})
        self.call("PUT", "/api/reservations", {
            "production_batch_id": batch["id"], "material_code": "M1", "quantity": "5"})
        overdue = self.call("GET", f"/api/production-batches/{batch['id']}/stages").json()[0]
        self.assertLess(overdue["planned_finish"], day.isoformat())
        assigned = self.call("PATCH", f"/api/stages/{overdue['id']}", {
            "version": overdue["version"], "changes": {"owner_id": 1}})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        forecast = self.call("GET", f"/api/production-batches/{batch['id']}").json()["forecast"]
        self.assertEqual(forecast["status"], "unknown")
        self.assertIsNone(forecast["formal_date"])
        self.assertGreaterEqual(forecast["shift_workdays"], 1)
        self.assertGreaterEqual(forecast["overdue_stage_count"], 1)
        self.assertIsNotNone(forecast["provisional_date"])
        tasks = self.call("GET", "/api/tasks?status=open").json()
        owned = [task for task in tasks if task["kind"] == "stage_overdue" and task["condition_active"]]
        self.assertEqual(len(owned), 1)
        self.assertEqual((owned[0]["entity_id"], owned[0]["assignee_id"]), (overdue["id"], 1))
        self.assertEqual(owned[0]["stage_id"], overdue["id"])
        self.assertEqual(owned[0]["production_batch_id"], batch["id"])
        self.assertEqual(owned[0]["order_id"], order["id"])

    def test_task_navigation_keeps_material_codes_distinct(self):
        order, line = self.order("SO-TASK-LINK")
        batch = self.batch(line, quantity="5")
        with backend.writing() as conn:
            for key, kind, entity_type, entity_id in (
                ("stock:MAT-A", "stock_shortage", "inventory", 0),
                ("stock:MAT-B", "stock_shortage", "inventory", 0),
                (f"eta:{batch['id']}:MAT-A", "missing_eta", "production_batches", batch["id"]),
                (f"eta:{batch['id']}:MAT-B", "missing_eta", "production_batches", batch["id"]),
            ):
                conn.execute("""INSERT INTO tasks(task_key,kind,entity_type,entity_id,title,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)""", (key, kind, entity_type, entity_id, key, backend.utcnow(), backend.utcnow()))
        tasks = {task["task_key"]: task for task in self.call("GET", "/api/tasks?status=open").json()}
        self.assertEqual(tasks["stock:MAT-A"]["material_code"], "MAT-A")
        self.assertEqual(tasks["stock:MAT-B"]["material_code"], "MAT-B")
        for code in ("MAT-A", "MAT-B"):
            task = tasks[f"eta:{batch['id']}:{code}"]
            self.assertEqual((task["material_code"], task["production_batch_id"], task["order_id"]),
                             (code, batch["id"], order["id"]))

    def test_existing_unassigned_task_fills_owner_without_changing_manual_assignment(self):
        order, _ = self.order("SO-TASK-ASSIGN")
        self.call("GET", "/api/overview")
        with backend.writing() as conn:
            first = backend.one(conn, "SELECT * FROM tasks WHERE kind='missing_delivery_batch'")
            second = backend.one(conn, "SELECT * FROM tasks WHERE kind='missing_batch'")
            conn.execute("UPDATE tasks SET assignee_id=NULL WHERE id=?", (first["id"],))
            self.assertEqual(second["assignee_id"], 1)
        updated = self.call("PATCH", f"/api/orders/{order['id']}", {
            "version": order["version"], "changes": {"owner_id": 1}})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.call("GET", "/api/overview")
        with backend.reading() as conn:
            filled = backend.must_get(conn, "tasks", first["id"])
            unchanged = backend.must_get(conn, "tasks", second["id"])
            assignment = backend.one(conn, "SELECT action FROM audit WHERE entity_type='tasks' AND entity_id=? ORDER BY id DESC LIMIT 1", (first["id"],))
        self.assertEqual(filled["assignee_id"], 1)
        self.assertEqual(unchanged["assignee_id"], 1)
        self.assertEqual(assignment["action"], "auto_assign")

    def test_auto_fallback_tasks_follow_new_owner_but_manual_transfer_stays(self):
        order, _ = self.order("SO-LEGACY-OWNER")
        initial = self.call("GET", "/api/overview").json()["tasks"]
        automatic = next(task for task in initial if task["kind"] == "missing_delivery_batch")
        manual = next(task for task in initial if task["kind"] == "missing_batch")
        self.assertEqual((automatic["assignee_id"], manual["assignee_id"]), (1, 1))
        owner = self.call("POST", "/api/users", {
            "username": "newowner", "display_name": "历史负责人", "role": "business",
            "password": "businesspass123"}).json()
        delegate = self.call("POST", "/api/users", {
            "username": "delegate", "display_name": "被转派人", "role": "warehouse",
            "password": "delegatepass123"}).json()
        noted = self.call("PATCH", f"/api/tasks/{automatic['id']}", {
            "version": automatic["version"], "changes": {"note": "业务已开始核对"}})
        self.assertEqual(noted.status_code, 200, noted.text)
        moved = self.call("PATCH", f"/api/tasks/{manual['id']}", {
            "version": manual["version"], "changes": {"assignee_id": delegate["id"]},
            "reason": "由仓库补录"})
        self.assertEqual(moved.status_code, 200, moved.text)
        bound = self.call("PATCH", f"/api/orders/{order['id']}", {
            "version": order["version"], "changes": {"owner_id": owner["id"]}})
        self.assertEqual(bound.status_code, 200, bound.text)
        self.call("GET", "/api/overview")
        with backend.reading() as conn:
            automatic_after = backend.must_get(conn, "tasks", automatic["id"])
            manual_after = backend.must_get(conn, "tasks", manual["id"])
            automatic_audit = backend.one(conn, "SELECT action,actor_id FROM audit WHERE entity_type='tasks' AND entity_id=? ORDER BY id DESC LIMIT 1",
                                          (automatic["id"],))
        self.assertEqual(automatic_after["assignee_id"], owner["id"])
        self.assertEqual(automatic_after["note"], "业务已开始核对")
        self.assertEqual((automatic_audit["action"], automatic_audit["actor_id"]), ("auto_reassign", None))
        self.assertEqual(manual_after["assignee_id"], delegate["id"])
        self.call("GET", "/api/overview")
        with backend.reading() as conn:
            self.assertEqual(backend.must_get(conn, "tasks", automatic["id"])["version"], automatic_after["version"])

    def test_active_problem_cannot_be_confirmed_until_condition_recovers(self):
        _, line = self.order("SO-TASK-CLOSE")
        tasks = self.call("GET", "/api/tasks").json()
        task = next(item for item in tasks if item["kind"] == "missing_delivery_batch")
        self.assertTrue(task["condition_active"])
        rejected = self.call("PATCH", f"/api/tasks/{task['id']}", {
            "version": task["version"], "changes": {"status": "done", "note": "误点完成"}})
        self.assertEqual(rejected.status_code, 409)
        self.assertIn("问题条件仍存在", rejected.json()["detail"])
        self.assertEqual(next(item for item in self.call("GET", "/api/tasks").json()
                              if item["id"] == task["id"])["status"], "open")

        # Older clients could leave an active problem marked done. Sync must surface it again.
        with backend.writing() as conn:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task["id"],))
        reopened = next(item for item in self.call("GET", "/api/tasks").json()
                        if item["id"] == task["id"])
        self.assertEqual(reopened["status"], "open")
        with backend.reading() as conn:
            last_audit = backend.one(conn, "SELECT action FROM audit WHERE entity_type='tasks' AND entity_id=? ORDER BY id DESC LIMIT 1", (task["id"],))
        self.assertEqual(last_audit["action"], "auto_reopen_active")

        created = self.call("POST", "/api/delivery-batches", {
            "order_line_id": line["id"], "batch_no": "D1", "quantity": "10"})
        self.assertEqual(created.status_code, 200, created.text)
        recovered = next(item for item in self.call("GET", "/api/tasks").json()
                         if item["id"] == task["id"])
        self.assertFalse(recovered["condition_active"])
        self.assertEqual(recovered["status"], "open")
        confirmed = self.call("PATCH", f"/api/tasks/{task['id']}", {
            "version": recovered["version"], "changes": {"status": "done", "note": "交付批次已建立"}})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "done")
        with backend.writing() as conn:
            conn.execute("DELETE FROM delivery_batches WHERE id=?", (created.json()["id"],))
        stale_close = self.call("PATCH", f"/api/tasks/{task['id']}", {
            "version": confirmed.json()["version"], "changes": {"status": "done"}})
        self.assertEqual(stale_close.status_code, 409)
        self.assertEqual(next(item for item in self.call("GET", "/api/tasks").json()
                              if item["id"] == task["id"])["status"], "open")

    def test_task_transfer_requires_active_approved_account(self):
        self.order("SO-TASK-TRANSFER")
        task = next(item for item in self.call("GET", "/api/tasks").json()
                    if item["kind"] == "missing_batch")
        active = self.call("POST", "/api/users", {
            "username": "activeworker", "display_name": "在职人员", "role": "business",
            "password": "securePassword123"}).json()
        disabled = self.call("POST", "/api/users", {
            "username": "disabledworker", "display_name": "停用人员", "role": "business",
            "password": "securePassword123"}).json()
        self.assertEqual(self.call("PATCH", f"/api/users/{disabled['id']}", {"active": False}).status_code, 200)
        salt = secrets.token_hex(16)
        with backend.writing() as conn:
            pending_id = conn.execute("""INSERT INTO users(username,display_name,role,password_salt,password_hash,active,created_at,status)
                VALUES(?,?,?,?,?,?,?,?)""", ("pendingworker", "待审批人员", "business", salt,
                backend.password_digest("securePassword123", salt), 0, backend.utcnow(), "pending")).lastrowid
        for candidate in (None, disabled["id"], pending_id):
            rejected = self.call("PATCH", f"/api/tasks/{task['id']}", {
                "version": task["version"], "changes": {"assignee_id": candidate}, "reason": "错误转派"})
            self.assertEqual(rejected.status_code, 422, rejected.text)
        transferred = self.call("PATCH", f"/api/tasks/{task['id']}", {
            "version": task["version"], "changes": {"assignee_id": active["id"]}, "reason": "业务交接"})
        self.assertEqual(transferred.status_code, 200, transferred.text)
        self.assertEqual(transferred.json()["assignee_id"], active["id"])

    def test_rd_coverage_uses_sample_target_without_customer_delivery_batch(self):
        day = date.fromisoformat(backend.today())
        order = self.call("POST", "/api/orders", {
            "project_mode": "internal", "project_name": "打样项目", "owner_id": 1,
            "lines": [{"line_no": "1", "product_code": "SAMPLE-A", "product_name": "样板", "quantity": "10"}]}).json()
        batch = self.call("POST", "/api/production-batches", {
            "order_line_id": order["lines"][0]["id"], "batch_no": "RD1", "quantity": "10", "phase": "rd",
            "schedule_start": day.isoformat(), "target_kind": "internal",
            "target_date": (day + timedelta(days=60)).isoformat(),
            "board_eta": (day + timedelta(days=5)).isoformat()})
        self.assertEqual(batch.status_code, 200, batch.text)
        overview = self.call("GET", "/api/overview").json()
        self.assertEqual(overview["summary"]["assessable"], 1)
        self.assertEqual(overview["summary"]["unassessable"], 0)
        self.assertEqual(overview["assessment"][0]["phase"], "rd")
        self.assertFalse(any(task["kind"] == "missing_delivery_batch" and task["condition_active"]
                             for task in overview["tasks"]))

    def test_engineering_substitute_and_late_forecast(self):
        _, line = self.order()
        today = date.fromisoformat(backend.today())
        start = today + timedelta(days=1)
        finish = start + timedelta(days=7)
        eta = finish + timedelta(days=2)
        batch = self.batch(line, quantity="10", material="M1", vendor="外协厂 A",
                           commit_start=start.isoformat(), commit_finish=finish.isoformat())
        delivery = self.call("POST", "/api/delivery-batches", {"order_line_id": line["id"], "batch_no": "D1", "quantity": "10",
                             "due_date": (finish + timedelta(days=3)).isoformat(), "promised_date": (finish + timedelta(days=4)).isoformat()}).json()
        self.assertEqual(self.call("POST", "/api/allocations", {"production_batch_id": batch["id"], "delivery_batch_id": delivery["id"], "quantity": "10"}).status_code, 200)
        purchase = self.call("POST", "/api/purchases", {"production_batch_id": batch["id"], "material_code": "M1", "quantity": "10", "eta": eta.isoformat()})
        self.assertEqual(purchase.status_code, 200, purchase.text)
        forecast = self.call("GET", f"/api/production-batches/{batch['id']}").json()["forecast"]
        self.assertEqual(forecast["status"], "unknown")
        self.assertIsNone(forecast["formal_date"])
        self.assertIsNotNone(forecast["provisional_date"])
        self.assertTrue(any(task["kind"] == "vendor_reconfirm" for task in self.call("GET", "/api/tasks?status=open").json()))
        engineering = self.call("POST", "/api/users", {"username": "engineer", "display_name": "工程", "role": "engineering", "password": "engineerpass123"})
        self.assertEqual(engineering.status_code, 200, engineering.text)
        requested = self.call("POST", "/api/substitutes", {"production_batch_id": batch["id"], "original_material_code": "M1", "substitute_material_code": "S1", "quantity": "10", "reason": "工程评估"})
        self.assertEqual(requested.status_code, 200, requested.text)
        sub = requested.json()
        self.assertEqual(self.call("PUT", "/api/reservations", {"production_batch_id": batch["id"], "for_material_code": "M1", "material_code": "S1", "quantity": "10"}).status_code, 409)
        self.assertEqual(self.call("POST", f"/api/substitutes/{sub['id']}/approve", {"version": sub["version"], "approve": True}).status_code, 403)
        user = self.call("POST", "/api/users", {"username": "buyer", "display_name": "采购", "role": "purchasing", "password": "buyerpass123"}).json()
        buyer_login = backend.login({"username": "buyer", "password": "buyerpass123"})
        buyer_headers = {"Authorization": f"Bearer {buyer_login['token']}"}
        self.assertEqual(self.call("POST", f"/api/substitutes/{sub['id']}/approve", {"version": sub["version"], "approve": True}, headers=buyer_headers).status_code, 403)
        engineer_login = backend.login({"username": "engineer", "password": "engineerpass123"})
        engineer_headers = {"Authorization": f"Bearer {engineer_login['token']}"}
        self.assertEqual(self.call("POST", f"/api/substitutes/{sub['id']}/approve", {"version": sub["version"], "approve": True}, headers=engineer_headers).status_code, 200)
        self.assertEqual(self.call("POST", "/api/inventory/snapshots", {"as_of": "2026-01-01", "basis": "available", "items": [{"material_code": "S1", "quantity": "10"}]}).status_code, 200)
        reserved = self.call("PUT", "/api/reservations", {"production_batch_id": batch["id"], "for_material_code": "M1", "material_code": "S1", "quantity": "10"})
        self.assertEqual(reserved.status_code, 200, reserved.text)
        self.assertEqual(self.call("GET", f"/api/production-batches/{batch['id']}/materials").json()["items"][0]["shortage"], "0")

    def test_partial_finished_event_does_not_complete_whole_batch(self):
        _, line = self.order()
        batch = self.batch(line, quantity="10")
        first = self.call("POST", f"/api/production-batches/{batch['id']}/events",
                          {"kind": "finished", "event_date": backend.today(), "quantity": "4"})
        self.assertEqual(first.status_code, 200, first.text)
        forecast = self.call("GET", f"/api/production-batches/{batch['id']}").json()["forecast"]
        self.assertNotEqual(forecast["status"], "complete")
        second = self.call("POST", f"/api/production-batches/{batch['id']}/events",
                           {"kind": "finished", "event_date": backend.today(), "quantity": "6"})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.call("GET", f"/api/production-batches/{batch['id']}").json()["forecast"]["status"], "complete")
        current = self.call("GET", f"/api/production-batches/{batch['id']}").json()
        self.assertEqual(self.call("PATCH", f"/api/production-batches/{batch['id']}",
                                   {"version": current["version"], "changes": {"quantity": "9"}, "reason": "复核"}).status_code, 409)
        self.assertEqual(self.call("POST", f"/api/production-batches/{batch['id']}/events",
                                   {"kind": "finished", "event_date": backend.today(), "quantity": "1"}).status_code, 409)

    def test_backup_restore_uses_consistent_snapshot_and_invalidates_sessions(self):
        self.order("SO1")
        user = backend.require_user(self.headers["Authorization"])
        backup = backend.create_backup(user=user)
        self.order("SO2")
        restored = backend.restore_backup(filename=backup["filename"], user=user)
        self.assertTrue(restored["relogin_required"])
        with backend.reading() as conn:
            self.assertEqual([row["order_no"] for row in backend.rows(conn, "SELECT order_no FROM orders ORDER BY order_no")], ["SO1"])
        with self.assertRaises(HTTPException) as caught:
            backend.require_user(self.headers["Authorization"])
        self.assertEqual(caught.exception.status_code, 401)

    def test_order_completion_uses_each_delivery_batches_selected_basis(self):
        order, line = self.order(quantity="10")
        first = self.call("POST", "/api/delivery-batches", {"order_line_id": line["id"], "batch_no": "D1",
                         "quantity": "4", "completion_basis": "shipment"}).json()
        second = self.call("POST", "/api/delivery-batches", {"order_line_id": line["id"], "batch_no": "D2",
                          "quantity": "6", "completion_basis": "receipt"}).json()
        self.assertEqual(self.call("POST", f"/api/delivery-batches/{first['id']}/events",
                                   {"kind": "shipped", "event_date": backend.today(), "quantity": "4"}).status_code, 200)
        self.assertEqual(self.call("GET", f"/api/orders/{order['id']}").json()["status"], "open")
        self.assertEqual(self.call("POST", f"/api/delivery-batches/{second['id']}/events",
                                   {"kind": "shipped", "event_date": backend.today(), "quantity": "6"}).status_code, 200)
        self.assertEqual(self.call("GET", f"/api/orders/{order['id']}").json()["status"], "open")
        self.assertEqual(self.call("POST", f"/api/delivery-batches/{second['id']}/events",
                                   {"kind": "customer_received", "event_date": backend.today(), "quantity": "6"}).status_code, 200)
        finished = self.call("GET", f"/api/orders/{order['id']}").json()
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["completed_quantity"], "10")
        self.assertEqual(self.call("GET", "/api/overview").json()["summary"]["orders_open"], 0)
        self.assertEqual(self.call("POST", f"/api/orders/{order['id']}/lines",
                                   {"line_no": "2", "product_code": "PCB-B", "product_name": "副板", "quantity": "1"}).status_code, 200)
        self.assertEqual(self.call("GET", f"/api/orders/{order['id']}").json()["status"], "open")

    def test_order_line_bom_version_follows_first_locked_batch_and_cannot_diverge(self):
        order, line = self.order(quantity="10")
        first = self.batch(line, "P1", "5")
        current_line = self.call("GET", f"/api/orders/{order['id']}").json()["lines"][0]
        self.assertEqual(current_line["bom_version"], "V1")
        conflicting = self.call("POST", "/api/production-batches", {"order_line_id": line["id"], "batch_no": "P2",
                           "quantity": "5", "bom_version": "V2", "bom_items": [{"material_code": "M1", "quantity_per": "1"}]})
        self.assertEqual(conflicting.status_code, 409)
        draft = self.call("POST", "/api/production-batches", {"order_line_id": line["id"], "batch_no": "P2", "quantity": "5"}).json()
        self.assertEqual(self.call("POST", f"/api/production-batches/{draft['id']}/bom-lock",
                                   {"version": draft["version"], "bom_version": "V2", "items": [{"material_code": "M1", "quantity_per": "1"}]}).status_code, 409)
        locked = self.call("POST", f"/api/production-batches/{draft['id']}/bom-lock",
                           {"version": draft["version"], "bom_version": "V1", "items": [{"material_code": "M1", "quantity_per": "1"}]})
        self.assertEqual(locked.status_code, 200, locked.text)
        self.assertEqual(self.call("PATCH", f"/api/order-lines/{line['id']}",
                                   {"version": current_line["version"], "changes": {"bom_version": "V2"}, "reason": "尝试改版"}).status_code, 409)
        self.assertEqual(self.call("GET", f"/api/production-batches/{first['id']}").json()["bom_items"][0]["material_code"], "M1")

    def test_estimated_eta_only_yields_provisional_forecast_until_confirmed(self):
        _, line = self.order(quantity="5")
        with backend.reading() as conn:
            start = backend.add_workdays(conn, backend.today(), 5)
            finish = backend.add_workdays(conn, start, 5)
            eta = backend.add_workdays(conn, backend.today(), 2)
        batch = self.batch(line, quantity="5", vendor="外协厂 A", commit_start=start, commit_finish=finish)
        purchase = self.call("POST", "/api/purchases", {"production_batch_id": batch["id"], "material_code": "M1",
                             "quantity": "5", "eta": eta}).json()
        self.assertEqual(purchase["eta_is_estimate"], 1)
        self.assertEqual(purchase["eta_source"], "manual")
        forecast = self.call("GET", f"/api/production-batches/{batch['id']}").json()["forecast"]
        self.assertEqual(forecast["status"], "unknown")
        self.assertIsNone(forecast["formal_date"])
        self.assertEqual(forecast["provisional_date"], finish)
        delivery = self.call("POST", "/api/delivery-batches", {"order_line_id": line["id"], "batch_no": "D1",
                             "quantity": "5", "due_date": finish}).json()
        self.assertEqual(self.call("POST", "/api/allocations", {"production_batch_id": batch["id"],
                         "delivery_batch_id": delivery["id"], "quantity": "5"}).status_code, 200)
        risk = next(item for item in self.call("GET", "/api/overview").json()["risks"]
                    if item["delivery_batch_id"] == delivery["id"])
        self.assertTrue(risk["is_provisional"])
        self.assertEqual(risk["provisional_date"], risk["forecast_date"])
        self.assertTrue(any(task["kind"] == "eta_confirm" for task in self.call("GET", "/api/tasks?status=open").json()))
        self.assertEqual(self.call("PATCH", f"/api/purchases/{purchase['id']}",
                                   {"version": purchase["version"], "changes": {"eta_is_estimate": False}}).status_code, 422)
        confirmed = self.call("PATCH", f"/api/purchases/{purchase['id']}",
                              {"version": purchase["version"], "changes": {"eta_is_estimate": False}, "reason": "供应商邮件确认"})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(self.call("GET", f"/api/production-batches/{batch['id']}").json()["forecast"]["formal_date"], finish)
        with backend.writing() as conn:
            imported_id = backend.import_purchase_row(conn,
                {"reference": "KIS-PO", "material_code": "M1", "quantity": "1", "eta": eta},
                {"reference", "material_code", "quantity", "eta"}, '["KIS-PO","M1"]', 1, None)
            imported = backend.must_get(conn, "purchases", imported_id)
            self.assertEqual((imported["eta_source"], imported["eta_is_estimate"]), ("KIS", 1))

    def test_quick_order_dates_create_audited_provisional_delivery(self):
        today = date.fromisoformat(backend.today())
        due = (today + timedelta(days=1)).isoformat()
        estimate = (today + timedelta(days=10)).isoformat()
        created = self.call("POST", "/api/orders", {
            "order_no": "SO-DATES", "customer": "客户 A",
            "lines": [{"line_no": "1", "product_code": "PCB-A", "product_name": "控制板", "quantity": "8"}],
            "initial_delivery": {"due_date": due, "promised_date": due,
                                 "manual_estimate_date": estimate, "manual_estimate_reason": "电话确认的临时估计"},
        })
        self.assertEqual(created.status_code, 200, created.text)
        order = created.json()
        delivery = order["lines"][0]["delivery_batches"][0]
        self.assertEqual(delivery["quantity"], "8")
        self.assertEqual(delivery["due_date"], due)
        self.assertEqual(delivery["promised_date"], due)
        self.assertEqual(delivery["manual_estimate_date"], estimate)
        self.assertEqual(delivery["forecast"]["status"], "unknown")
        self.assertIsNone(delivery["forecast"]["formal_date"])
        self.assertEqual(delivery["forecast"]["provisional_date"], estimate)
        risks = [risk for risk in self.call("GET", "/api/overview").json()["risks"]
                 if risk["delivery_batch_id"] == delivery["id"]]
        self.assertEqual({risk["basis"] for risk in risks}, {"due_date", "promised_date"})
        self.assertTrue(all(risk["is_provisional"] for risk in risks))
        audit_rows = self.call("GET", f"/api/audit?entity_type=delivery_batches&entity_id={delivery['id']}").json()
        self.assertEqual(audit_rows[0]["reason"], "电话确认的临时估计")

        no_reason = self.call("PATCH", f"/api/delivery-batches/{delivery['id']}", {
            "version": delivery["version"], "changes": {"manual_estimate_date": (today + timedelta(days=12)).isoformat()},
            "reason": "重新估算"})
        self.assertEqual(no_reason.status_code, 422)
        changed = self.call("PATCH", f"/api/delivery-batches/{delivery['id']}", {
            "version": delivery["version"], "changes": {"manual_estimate_date": (today + timedelta(days=12)).isoformat(),
                                                        "manual_estimate_reason": "内部会议重估"}, "reason": "内部会议重估"})
        self.assertEqual(changed.status_code, 200, changed.text)
        cleared = self.call("PATCH", f"/api/delivery-batches/{delivery['id']}", {
            "version": changed.json()["version"], "changes": {"manual_estimate_date": None}, "reason": "撤销无依据估算"})
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(cleared.json()["manual_estimate_reason"])
        detail = self.call("GET", f"/api/orders/{order['id']}").json()
        self.assertIsNone(detail["lines"][0]["delivery_batches"][0]["forecast"]["provisional_date"])

    def test_quick_order_actual_date_is_atomic_and_completes_selected_basis(self):
        actual_date = (date.fromisoformat(backend.today()) - timedelta(days=1)).isoformat()
        payload = {"order_no": "SO-ACTUAL", "customer": "客户 A",
                   "lines": [{"line_no": "1", "product_code": "PCB-A", "product_name": "控制板", "quantity": "7"}],
                   "initial_delivery": {"completion_basis": "receipt", "actual_delivery_date": actual_date}}
        created = self.call("POST", "/api/orders", payload)
        self.assertEqual(created.status_code, 200, created.text)
        order = created.json()
        delivery = order["lines"][0]["delivery_batches"][0]
        self.assertEqual((order["status"], order["completed_quantity"]), ("complete", "7"))
        self.assertEqual(delivery["forecast"]["status"], "complete")
        self.assertEqual(delivery["forecast"]["formal_date"], actual_date)
        self.assertEqual((delivery["events"][0]["kind"], delivery["events"][0]["quantity"]), ("customer_received", "7"))

        invalid = {**payload, "order_no": "SO-ROLLBACK", "initial_delivery": {"actual_delivery_date": "2026-99-99"}}
        self.assertEqual(self.call("POST", "/api/orders", invalid).status_code, 422)
        self.assertFalse(any(row["order_no"] == "SO-ROLLBACK" for row in self.call("GET", "/api/orders").json()))
        future = {**payload, "order_no": "SO-FUTURE", "initial_delivery": {
            "actual_delivery_date": (date.fromisoformat(backend.today()) + timedelta(days=1)).isoformat()}}
        self.assertEqual(self.call("POST", "/api/orders", future).status_code, 422)
        partial = {**payload, "order_no": "SO-PARTIAL", "initial_delivery": {
            "quantity": "3", "actual_delivery_date": actual_date}}
        self.assertEqual(self.call("POST", "/api/orders", partial).status_code, 422)
        self.assertFalse(any(row["order_no"] in {"SO-FUTURE", "SO-PARTIAL"}
                             for row in self.call("GET", "/api/orders").json()))
        many_lines = {**payload, "order_no": "SO-TWO", "lines": payload["lines"] * 2}
        self.assertEqual(self.call("POST", "/api/orders", many_lines).status_code, 422)

    def test_delivery_date_columns_migrate_existing_database(self):
        db_path = backend.DB_PATH
        with tempfile.TemporaryDirectory() as temp:
            backend.DB_PATH = Path(temp) / "old.db"
            try:
                with closing(sqlite3.connect(backend.DB_PATH)) as conn, conn:
                    conn.execute("CREATE TABLE delivery_batches (id INTEGER PRIMARY KEY, order_line_id INTEGER, batch_no TEXT, quantity TEXT, due_date TEXT, promised_date TEXT, completion_basis TEXT, shipping_lead_days INTEGER, receipt_lead_days INTEGER, version INTEGER, created_at TEXT, updated_at TEXT)")
                backend.init_db()
                with backend.reading() as conn:
                    columns = {row["name"] for row in conn.execute("PRAGMA table_info(delivery_batches)")}
                self.assertTrue({"manual_estimate_date", "manual_estimate_reason"} <= columns)
            finally:
                backend.DB_PATH = db_path

    def test_missing_delivery_batch_and_dates_generate_completion_tasks(self):
        order, line = self.order()
        tasks = self.call("GET", "/api/tasks?status=open").json()
        self.assertTrue(any(task["kind"] == "missing_delivery_batch" and task["entity_id"] == line["id"]
                            and task["condition_active"] for task in tasks))
        batch = self.call("POST", "/api/delivery-batches", {
            "order_line_id": line["id"], "batch_no": "D1", "quantity": "4"}).json()
        tasks = self.call("GET", "/api/tasks?status=open").json()
        self.assertEqual(len([task for task in tasks if task["kind"] == "missing_delivery_date"
                              and task["entity_id"] == batch["id"] and task["condition_active"]]), 2)
        self.assertTrue(any(task["kind"] == "missing_delivery_batch" and task["entity_id"] == line["id"]
                            and task["condition_active"] and "尚有 6 未排" in task["title"] for task in tasks))
        self.assertEqual(self.call("POST", "/api/delivery-batches", {
            "order_line_id": line["id"], "batch_no": "D2", "quantity": "6"}).status_code, 200)
        tasks = self.call("GET", "/api/tasks?status=open").json()
        self.assertTrue(any(task["kind"] == "missing_delivery_batch" and task["entity_id"] == line["id"]
                            and not task["condition_active"] for task in tasks))
        filled = self.call("PATCH", f"/api/delivery-batches/{batch['id']}", {
            "version": batch["version"], "changes": {"due_date": "2026-12-01", "promised_date": "2026-12-02"},
            "reason": "客户和业务确认"})
        self.assertEqual(filled.status_code, 200, filled.text)
        tasks = self.call("GET", "/api/tasks?status=open").json()
        self.assertFalse(any(task["kind"] == "missing_delivery_date" and task["entity_id"] == batch["id"]
                             and task["condition_active"] for task in tasks))


    def test_one_product_has_separate_rd_pp_mp_runs_and_editable_nodes(self):
        _, line = self.order(quantity="1200")
        made = []
        for phase, quantity, number in (("rd", "20", "RD-1"), ("pp", "100", "PP-1"), ("mp", "1200", "MP-1")):
            payload = {
                "order_line_id": line["id"], "batch_no": number, "quantity": quantity,
                "phase": phase, "phase_source": "quantity_rule", "phase_order_no": number,
                "schedule_start": "2026-09-25", "schedule_mode": "workday",
            }
            if phase == "rd":
                payload.update(target_kind="internal", target_date="2026-12-01", board_eta="2026-10-05")
            response = self.call("POST", "/api/production-batches", payload)
            self.assertEqual(response.status_code, 200, response.text)
            made.append(response.json())
        rd, pp, mp = made
        self.assertEqual([item["phase"] for item in made], ["rd", "pp", "mp"])
        self.assertEqual([item["phase_order_no"] for item in made], ["RD-1", "PP-1", "MP-1"])
        rd_stages = self.call("GET", f"/api/production-batches/{rd['id']}/stages").json()
        pp_stages = self.call("GET", f"/api/production-batches/{pp['id']}/stages").json()
        self.assertIn("嘉立创打样下单", [stage["name"] for stage in rd_stages])
        self.assertNotIn("板厂签收", [stage["name"] for stage in rd_stages])
        self.assertIn("板厂签收", [stage["name"] for stage in pp_stages])
        self.assertFalse(any(task["kind"] == "missing_vendor_date" and task["entity_id"] == rd["id"]
                             for task in self.call("GET", "/api/tasks?status=open").json()))
        before = rd_stages[-1]["planned_finish"]
        changed = self.call("PATCH", f"/api/production-batches/{rd['id']}", {
            "version": rd["version"], "changes": {"board_received_at": "2026-11-15"}, "reason": "嘉立创实际到板"})
        self.assertEqual(changed.status_code, 200, changed.text)
        after = self.call("GET", f"/api/production-batches/{rd['id']}/stages").json()[-1]["planned_finish"]
        self.assertGreater(after, before)
        edited = self.call("PATCH", f"/api/stages/{pp_stages[0]['id']}", {
            "version": pp_stages[0]["version"],
            "changes": {"owner_name": "工程甲", "duration_days": "1.5", "note": "已确认"}})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["owner_name"], "工程甲")
        moved = self.call("PUT", f"/api/production-batches/{pp['id']}/stages/reorder", {
            "stage_ids": list(reversed([stage["id"] for stage in pp_stages]))})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()[0]["id"], pp_stages[-1]["id"])

    def test_stage_completion_requires_bound_owner_and_records_actor(self):
        _, line = self.order(quantity="10")
        batch = self.batch(line)
        stage = self.call("GET", f"/api/production-batches/{batch['id']}/stages").json()[0]
        stage_id = stage["id"]
        named = self.call("PATCH", f"/api/stages/{stage_id}", {
            "version": stage["version"], "changes": {"owner_name": "唐林林"}})
        self.assertEqual(named.status_code, 200, named.text)
        self.assertEqual(named.json()["owner_display_name"], "唐林林")
        self.assertEqual(named.json()["owner_binding_status"], "unbound")
        unbound = self.call("POST", f"/api/stages/{stage_id}/completion", {
            "version": named.json()["version"], "completed": True})
        self.assertEqual(unbound.status_code, 422)

        assigned = self.call("PATCH", f"/api/stages/{stage_id}", {
            "version": named.json()["version"], "changes": {"owner_id": 1}})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(assigned.json()["owner_binding_status"], "bound")
        self.assertEqual(assigned.json()["owner_display_name"], "管理员")
        salt = secrets.token_hex(16)
        with backend.writing() as conn:
            worker_id = conn.execute(
                "INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                ("worker", "代录人员", "business", salt,
                 backend.password_digest("workerpass123", salt), backend.utcnow())
            ).lastrowid
        worker_headers = {"Authorization": f"Bearer {backend.login({'username': 'worker', 'password': 'workerpass123'})['token']}"}
        self.assertEqual(backend.request_permission("POST", f"/api/stages/{stage_id}/completion"), "production_edit")
        completed = self.call("POST", f"/api/stages/{stage_id}/completion", {
            "version": assigned.json()["version"], "completed": True}, headers=worker_headers)
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertTrue(completed.json()["is_complete"])
        self.assertEqual(completed.json()["actual_finish"], backend.today())
        self.assertEqual(completed.json()["completed_by"], worker_id)
        self.assertEqual(completed.json()["completed_by_name"], "代录人员")
        self.assertTrue(completed.json()["completed_at"])
        self.assertEqual(self.call("GET", f"/api/production-batches/{batch['id']}").json()["stages"][0]["is_complete"], True)
        with backend.reading() as conn:
            entry = backend.one(conn, "SELECT action,actor_id,before_json,after_json FROM audit WHERE entity_type='stages' AND entity_id=? ORDER BY id DESC LIMIT 1", (stage_id,))
        self.assertEqual((entry["action"], entry["actor_id"]), ("complete", worker_id))
        self.assertEqual(json.loads(entry["after_json"])["status"], "done")

        stale = self.call("POST", f"/api/stages/{stage_id}/completion", {
            "version": assigned.json()["version"], "completed": False, "reason": "复核"})
        self.assertEqual(stale.status_code, 409)
        no_reason = self.call("POST", f"/api/stages/{stage_id}/completion", {
            "version": completed.json()["version"], "completed": False})
        self.assertEqual(no_reason.status_code, 422)
        reopened = self.call("POST", f"/api/stages/{stage_id}/completion", {
            "version": completed.json()["version"], "completed": False, "reason": "验收未通过"})
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertFalse(reopened.json()["is_complete"])
        self.assertIsNone(reopened.json()["actual_finish"])
        self.assertIsNone(reopened.json()["completed_by"])
        with backend.reading() as conn:
            entry = backend.one(conn, "SELECT action,reason FROM audit WHERE entity_type='stages' AND entity_id=? ORDER BY id DESC LIMIT 1", (stage_id,))
        self.assertEqual((entry["action"], entry["reason"]), ("reopen", "验收未通过"))

    def test_ordinary_stage_patch_cannot_bypass_completion_rules(self):
        _, line = self.order(quantity="10")
        batch = self.batch(line)
        stage = self.call("GET", f"/api/production-batches/{batch['id']}/stages").json()[0]
        path = f"/api/stages/{stage['id']}"
        self.assertEqual(self.call("PATCH", path, {
            "version": stage["version"], "changes": {"status": "done"}}).status_code, 422)
        self.assertEqual(self.call("PATCH", path, {
            "version": stage["version"], "changes": {"actual_finish": backend.today()}}).status_code, 422)
        self.assertEqual(self.call("POST", f"/api/production-batches/{batch['id']}/stages", {
            "name": "临时工序", "status": "done"}).status_code, 422)
        created_done = self.call("POST", f"/api/production-batches/{batch['id']}/stages", {
            "name": "验收复核", "owner_id": 1, "status": "done"})
        self.assertEqual(created_done.status_code, 200, created_done.text)
        self.assertTrue(created_done.json()["is_complete"])
        assigned = self.call("PATCH", path, {"version": stage["version"], "changes": {"owner_id": 1}})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        done = self.call("PATCH", path, {"version": assigned.json()["version"], "changes": {"status": "done"}})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["is_complete"])
        self.assertEqual(done.json()["completed_by"], 1)
        self.assertEqual(done.json()["actual_finish"], backend.today())
        self.assertEqual(self.call("PATCH", path, {
            "version": done.json()["version"], "changes": {"status": "pending"}}).status_code, 422)
        self.assertEqual(self.call("PATCH", path, {
            "version": done.json()["version"], "changes": {"owner_id": None}, "reason": "换人"}).status_code, 422)
        reopened = self.call("PATCH", path, {
            "version": done.json()["version"], "changes": {"status": "pending"}, "reason": "复核未通过"})
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertFalse(reopened.json()["is_complete"])
        self.assertIsNone(reopened.json()["actual_finish"])
        tomorrow = (date.fromisoformat(backend.today()) + timedelta(days=1)).isoformat()
        self.assertEqual(self.call("PATCH", path, {
            "version": reopened.json()["version"], "changes": {"actual_finish": tomorrow}}).status_code, 422)

    def test_stage_preview_uses_product_phase_template_and_company_calendar(self):
        _, line = self.order(quantity="10")
        template = self.call("PUT", "/api/products/PCB-A/phase-template?phase=rd", {
            "version": 0, "steps": [{"name": "需求评审", "duration_days": "0.5"},
                                    {"name": "样板验收", "duration_days": "0.5"}]})
        self.assertEqual(template.status_code, 200, template.text)
        holiday = self.call("PUT", "/api/holidays/2026-09-29", {"is_workday": False})
        self.assertEqual(holiday.status_code, 200, holiday.text)
        query = "/api/schedule-preview?product_code=PCB-A&phase=rd&schedule_start=2026-09-28&schedule_mode=workday"
        preview = self.call("GET", query)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["source"], "phase_template")
        self.assertEqual(preview.json()["earliest_finish"], "2026-09-30")
        self.assertEqual(preview.json()["nodes"][-1]["name"], "样板验收")
        natural = self.call("GET", query.replace("workday", "natural"))
        self.assertEqual(natural.json()["earliest_finish"], "2026-09-29")

        created = self.call("POST", "/api/production-batches", {
            "order_line_id": line["id"], "batch_no": "RD-PREVIEW", "quantity": "10",
            "phase": "rd", "schedule_start": "2026-09-28", "schedule_mode": "workday"})
        self.assertEqual(created.status_code, 200, created.text)
        batch = created.json()
        stages = self.call("GET", f"/api/production-batches/{batch['id']}/stages").json()
        self.assertEqual(stages[-1]["planned_finish"], preview.json()["earliest_finish"])
        edit_query = query.replace("2026-09-28", "2026-09-30") + f"&batch_id={batch['id']}"
        edited_preview = self.call("GET", edit_query)
        self.assertEqual(edited_preview.status_code, 200, edited_preview.text)
        self.assertEqual(edited_preview.json()["source"], "current_stages")
        reclassified_preview = self.call("GET", edit_query.replace("phase=rd", "phase=pp"))
        self.assertEqual(reclassified_preview.json()["source"], "current_stages")
        self.assertTrue(reclassified_preview.json()["phase_change_pending"])
        self.assertEqual(len(reclassified_preview.json()["nodes"]), len(stages))
        changed = self.call("PATCH", f"/api/production-batches/{batch['id']}", {
            "version": batch["version"], "changes": {"schedule_start": "2026-09-30"}})
        self.assertEqual(changed.status_code, 200, changed.text)
        stages = self.call("GET", f"/api/production-batches/{batch['id']}/stages").json()
        self.assertEqual(stages[-1]["planned_finish"], edited_preview.json()["earliest_finish"])

    def test_each_phase_keeps_its_locked_bom_version(self):
        _, line = self.order(quantity="1200")
        locked = {}
        for phase, quantity, bom in (("rd", "20", "V1"), ("pp", "100", "V2"), ("mp", "1200", "V3")):
            result = self.call("POST", "/api/production-batches", {
                "order_line_id": line["id"], "batch_no": phase.upper(), "quantity": quantity,
                "phase": phase, "bom_version": bom,
                "bom_items": [{"material_code": "M1", "quantity_per": "1"}],
            })
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["bom_version"], bom)
            locked[phase] = result.json()
        conflict = self.call("POST", "/api/production-batches", {
            "order_line_id": line["id"], "batch_no": "PP-2", "quantity": "50", "phase": "pp",
            "bom_version": "V4", "bom_items": [{"material_code": "M1", "quantity_per": "1"}],
        })
        self.assertEqual(conflict.status_code, 409)
        moved = self.call("PATCH", f"/api/production-batches/{locked['rd']['id']}", {
            "version": locked["rd"]["version"], "changes": {"phase": "pp"},
            "reason": "核对阶段分类",
        })
        self.assertEqual(moved.status_code, 409)


if __name__ == "__main__":
    unittest.main()

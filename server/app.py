"""Small shared PCBA workbench: SQLite is the source of truth for LAN users."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .backup import DEFAULT_DB, backup_database, backup_runtime_status, backup_status, mirror_directory
from . import deepseek_import
from .export_workbook import build_workbook
from .kis_import import MAX_BYTES, SCHEMAS, inspect_upload, preview_import
from .legacy_import import MAX_BYTES as LEGACY_MAX_BYTES, preview_legacy_json, project_legacy_schedule, suggest_phase
from .phase_schedule import PHASE_LABELS, draft_dates, phase_template, plan_nodes


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("PCBA_DB_PATH", str(DEFAULT_DB))).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
APP_LOCK = threading.RLock()
LOCAL_TZ = timezone(timedelta(hours=8))
ROLES = {"admin", "engineering", "business", "purchasing", "warehouse"}
PERMISSION_LABELS = {
    "orders_edit": "编辑订单与交付",
    "production_edit": "编辑生产排期",
    "materials_edit": "编辑物料与采购",
    "tasks_manage": "处理和转派待办",
    "imports_manage": "导入数据",
    "audit_view": "查看修改记录",
}
DEFAULT_PERMISSIONS = {
    "orders_edit": True, "production_edit": True, "materials_edit": True,
    "tasks_manage": True, "imports_manage": False, "audit_view": False,
}
STAGE_NAMES = [
    "订单确认", "BOM锁定", "采购/到料", "IQC", "我方备料发料", "厂商签收",
    "SMT", "DIP", "烧录", "测试", "FQC", "厂商完工", "回厂/直发", "客户签收",
]
DEFAULT_TEMPLATE = [
    {"name": name, "duration_days": (1 if name in {"SMT", "DIP", "烧录", "测试", "FQC"} else 0),
     "enabled": name not in {"DIP", "烧录"}}
    for name in STAGE_NAMES
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def dec(value: Any, *, allow_zero: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(422, "数量必须是十进制数")
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise HTTPException(422, "数量必须大于零" if not allow_zero else "数量不能为负数")
    return result


def ds(value: Decimal | Any) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def unit_price(value: Any) -> str | None:
    """Store CNY prices exactly; an absent price means the BOM is not fully priced."""
    if value in (None, ""):
        return None
    price = dec(value)
    if price >= Decimal("1000000000000") or price.normalize().as_tuple().exponent < -8:
        raise HTTPException(422, "物料单价须小于 1 万亿元，且最多保留 8 位小数")
    return ds(price)


def bom_row_required(item: dict, batch_quantity: Any, mode: str = "shared") -> Decimal:
    if mode == "independent" and item.get("batch_board_id"):
        board_quantity = dec(item.get("board_planned_quantity"), allow_zero=False)
        catalog_per = (dec(item["catalog_quantity_per"], allow_zero=False)
                       if item.get("catalog_quantity_per") else
                       dec(item["quantity_per"], allow_zero=False) / dec(item["board_quantity_per_product"], allow_zero=False))
        raw = board_quantity * catalog_per * (Decimal(1) + dec(item["loss_rate"]))
    else:
        raw = dec(batch_quantity, allow_zero=False) * dec(item["quantity_per"], allow_zero=False) * (Decimal(1) + dec(item["loss_rate"]))
    return raw.to_integral_value(rounding=ROUND_CEILING)


def bom_pricing(items: list[dict], batch_quantity: Any, mode: str = "shared") -> dict:
    """Price the same whole-piece demand used by material shortage calculations."""
    quantity = dec(batch_quantity, allow_zero=False)
    lines = []
    subtotal = Decimal(0)
    missing = 0
    with localcontext() as context:
        context.prec = 80
        for item in items:
            required = bom_row_required(item, quantity, mode)
            price = item.get("unit_price")
            line_total = None
            if price is None:
                missing += 1
            else:
                line_total = (required * dec(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                subtotal += line_total
            lines.append({"bom_item_id": item.get("id"), "source_line_no": item.get("source_line_no"),
                          "catalog_source_line_no": item.get("catalog_source_line_no"),
                          "batch_board_id": item.get("batch_board_id"),
                          "board_ref": item.get("board_ref"), "board_name": item.get("board_name"),
                          "material_code": item["material_code"], "material_name": item.get("material_name"),
                          "required_quantity": ds(required), "unit_price": price,
                          "line_total": ds(line_total) if line_total is not None else None})
    complete = bool(items) and missing == 0
    return {"currency": "CNY", "batch_quantity": ds(quantity), "priced_subtotal": ds(subtotal),
            "total": ds(subtotal) if complete else None, "missing_price_count": missing,
            "complete": complete, "lines": lines}


def valid_date(value: Any, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise HTTPException(422, "日期不能为空")
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise HTTPException(422, "日期格式应为 YYYY-MM-DD")


def valid_datetime(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        raise HTTPException(422, "时间格式应为 ISO 8601")


def conn_open() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def reading():
    with APP_LOCK:
        conn = conn_open()
        try:
            yield conn
        finally:
            conn.close()


@contextmanager
def writing():
    with APP_LOCK:
        conn = conn_open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise HTTPException(409, f"唯一键或关联约束冲突：{exc}")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def must_get(conn: sqlite3.Connection, table: str, row_id: int) -> dict:
    assert table in {"users", "orders", "order_lines", "order_line_boards", "production_batches", "batch_boards", "board_stages", "delivery_batches", "allocations", "bom_items", "bom_catalog", "reservations", "receipts", "purchases", "purchase_allocations", "substitutes", "stages", "tasks", "legacy_records"}
    record = one(conn, f"SELECT * FROM {table} WHERE id=?", (row_id,))
    if not record:
        raise HTTPException(404, "记录不存在")
    return record


def need(data: dict, key: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise HTTPException(422, f"缺少 {key}")
    return value


def audit(conn: sqlite3.Connection, entity_type: str, entity_id: int, action: str,
          actor_id: int | None, before: Any, after: Any, reason: str | None = None) -> None:
    conn.execute(
        "INSERT INTO audit(entity_type,entity_id,action,actor_id,before_json,after_json,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (entity_type, entity_id, action, actor_id, json.dumps(before, ensure_ascii=False, default=str),
         json.dumps(after, ensure_ascii=False, default=str), reason, utcnow()),
    )


def insert_legacy_stages(conn: sqlite3.Connection, batch_id: int, schedule: dict) -> None:
    for node in schedule["nodes"]:
        conn.execute("""
            INSERT INTO stages(production_batch_id,sequence,name,planned_start,planned_finish,
                               actual_finish,status,owner_name,stage_type,duration_days,
                               actual_material_at,note,legacy_source_index,planned_start_at,
                               planned_finish_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (batch_id, node["sequence"], node["name"], node["planned_start"],
                  node["estimated_finish"], node["actual_finish_date"],
                  "done" if node["actual_finish_date"] else None, node["owner"],
                  node["type"] or "normal", node["duration_days"] or "0", node["actual_arrival_date"],
                  node["remark"], node["sequence"] - 1, node["planned_start_at"],
                  node["estimated_finish_at"], utcnow(), utcnow()))


def backfill_legacy_phases(conn: sqlite3.Connection, *, classify_existing: bool) -> None:
    """Repair only untouched default nodes from the earlier legacy importer.

    Original JSON stays in legacy_records.  A human-edited stage (version > 1)
    is never deleted by this migration.
    """
    for record in rows(conn, "SELECT * FROM legacy_records WHERE production_batch_id IS NOT NULL"):
        batch = must_get(conn, "production_batches", record["production_batch_id"])
        source = json.loads(record["source_json"])
        schedule = project_legacy_schedule(source)
        if classify_existing:
            phase = suggest_phase(source.get("qty")) or "unclassified"
            conn.execute(
                "UPDATE production_batches SET phase=?,phase_source=?,schedule_start=?,schedule_mode=? WHERE id=?",
                (phase, "quantity_rule" if phase != "unclassified" else "manual",
                 schedule["start_date"], schedule["calendar_mode"] or "workday", batch["id"]),
            )
        current = rows(conn, "SELECT * FROM stages WHERE production_batch_id=? ORDER BY sequence", (batch["id"],))
        source_names = [node["name"] for node in schedule["nodes"]]
        untouched_default = (current and [stage["name"] for stage in current] != source_names
                             and all(step["version"] == 1 and step["name"] in STAGE_NAMES
                                     and not step["actual_start"] and not step["actual_finish"]
                                     and step["legacy_source_index"] is None for step in current))
        if not untouched_default:
            continue
        node_names = source_names
        if len(set(node_names)) != len(node_names):
            continue  # existing UNIQUE(batch,name) requires human resolution
        conn.execute("DELETE FROM stages WHERE production_batch_id=?", (batch["id"],))
        insert_legacy_stages(conn, batch["id"], schedule)
        audit(conn, "production_batches", batch["id"], "legacy_stage_repair", None,
              {"default_stages": [step["name"] for step in current]},
              {"restored_stages": node_names}, "按旧 JSON 恢复原始节点；旧预计日仅作历史估算")


def init_db() -> None:
    with closing(conn_open()) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
          role TEXT NOT NULL, password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active', permissions_json TEXT,
          approved_at TEXT, approved_by INTEGER REFERENCES users(id), reject_reason TEXT,
          password_changed_at TEXT);
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY, order_no TEXT NOT NULL UNIQUE, customer TEXT NOT NULL,
          project_name TEXT, project_mode TEXT NOT NULL DEFAULT 'customer',
          owner_id INTEGER REFERENCES users(id), status TEXT NOT NULL DEFAULT 'open',
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS order_lines (
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), line_no TEXT NOT NULL,
          product_code TEXT NOT NULL, product_name TEXT NOT NULL, quantity TEXT NOT NULL,
          bom_product_code TEXT, bom_version TEXT, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(order_id,line_no));
        CREATE TABLE IF NOT EXISTS order_line_boards (
          id INTEGER PRIMARY KEY, order_line_id INTEGER NOT NULL REFERENCES order_lines(id),
          board_ref TEXT NOT NULL, board_name TEXT NOT NULL, bom_product_code TEXT NOT NULL,
          quantity_per_product TEXT NOT NULL, default_bom_version TEXT, active INTEGER NOT NULL DEFAULT 1,
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(order_line_id,board_ref));
        CREATE TABLE IF NOT EXISTS production_batches (
          id INTEGER PRIMARY KEY, order_line_id INTEGER NOT NULL REFERENCES order_lines(id), batch_no TEXT NOT NULL,
          quantity TEXT NOT NULL, vendor TEXT, commit_start TEXT, commit_finish TEXT,
          phase TEXT NOT NULL DEFAULT 'unclassified', phase_source TEXT NOT NULL DEFAULT 'manual',
          phase_order_no TEXT, schedule_start TEXT, schedule_mode TEXT NOT NULL DEFAULT 'workday',
          target_kind TEXT, target_date TEXT, sampling_order_date TEXT,
          board_eta TEXT, board_received_at TEXT, debug_accepted_at TEXT,
          vendor_confirmed_at TEXT, bom_version TEXT, bom_locked INTEGER NOT NULL DEFAULT 0,
          material_ready_at TEXT,
          conflict_ack_reason TEXT, conflict_ack_by INTEGER REFERENCES users(id), conflict_ack_at TEXT,
          conflict_ack_signature TEXT,
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(order_line_id,batch_no));
        CREATE TABLE IF NOT EXISTS batch_boards (
          id INTEGER PRIMARY KEY, production_batch_id INTEGER NOT NULL REFERENCES production_batches(id),
          order_line_board_id INTEGER NOT NULL REFERENCES order_line_boards(id),
          board_ref TEXT NOT NULL, board_name TEXT NOT NULL, bom_product_code TEXT NOT NULL,
          quantity_per_product TEXT NOT NULL, planned_quantity TEXT, vendor TEXT,
          commit_start TEXT, commit_finish TEXT, bom_version TEXT, bom_locked INTEGER NOT NULL DEFAULT 0,
          conflict_ack_reason TEXT, conflict_ack_by INTEGER REFERENCES users(id), conflict_ack_at TEXT,
          conflict_ack_signature TEXT,
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(production_batch_id,order_line_board_id));
        CREATE TABLE IF NOT EXISTS board_stages (
          id INTEGER PRIMARY KEY, batch_board_id INTEGER NOT NULL REFERENCES batch_boards(id),
          sequence INTEGER NOT NULL, name TEXT NOT NULL, planned_start TEXT, planned_finish TEXT,
          actual_start TEXT, actual_finish TEXT, status TEXT, version INTEGER NOT NULL DEFAULT 1,
          owner_name TEXT, owner_id INTEGER REFERENCES users(id),
          completed_by INTEGER REFERENCES users(id), completed_at TEXT,
          stage_type TEXT NOT NULL DEFAULT 'normal', duration_days TEXT NOT NULL DEFAULT '0',
          note TEXT, plan_overridden INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(batch_board_id,name));
        CREATE TABLE IF NOT EXISTS bom_items (
          id INTEGER PRIMARY KEY, production_batch_id INTEGER NOT NULL REFERENCES production_batches(id),
          source_line_no TEXT NOT NULL, material_code TEXT NOT NULL, material_name TEXT, quantity_per TEXT NOT NULL,
          loss_rate TEXT NOT NULL DEFAULT '0', unit_price TEXT, version INTEGER NOT NULL DEFAULT 1,
          UNIQUE(production_batch_id,source_line_no));
        CREATE TABLE IF NOT EXISTS material_reviews (
          id INTEGER PRIMARY KEY, production_batch_id INTEGER NOT NULL REFERENCES production_batches(id),
          material_code TEXT NOT NULL, state_hash TEXT NOT NULL, state_parts_json TEXT NOT NULL,
          reviewed_by INTEGER NOT NULL REFERENCES users(id), reviewed_at TEXT NOT NULL,
          note TEXT, version INTEGER NOT NULL DEFAULT 1,
          UNIQUE(production_batch_id,material_code));
        CREATE TABLE IF NOT EXISTS delivery_batches (
          id INTEGER PRIMARY KEY, order_line_id INTEGER NOT NULL REFERENCES order_lines(id), batch_no TEXT NOT NULL,
          quantity TEXT NOT NULL, due_date TEXT, promised_date TEXT,
          manual_estimate_date TEXT, manual_estimate_reason TEXT,
          completion_basis TEXT NOT NULL DEFAULT 'shipment', shipping_lead_days INTEGER NOT NULL DEFAULT 1,
          receipt_lead_days INTEGER NOT NULL DEFAULT 2, version INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(order_line_id,batch_no));
        CREATE TABLE IF NOT EXISTS allocations (
          id INTEGER PRIMARY KEY, production_batch_id INTEGER NOT NULL REFERENCES production_batches(id),
          delivery_batch_id INTEGER NOT NULL REFERENCES delivery_batches(id), quantity TEXT NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(production_batch_id,delivery_batch_id));
        CREATE TABLE IF NOT EXISTS inventory_snapshots (
          id INTEGER PRIMARY KEY, as_of TEXT NOT NULL, as_of_precision TEXT NOT NULL DEFAULT 'timestamp', basis TEXT NOT NULL,
          source_name TEXT, created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inventory_rows (
          id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES inventory_snapshots(id),
          material_code TEXT NOT NULL, quantity TEXT NOT NULL, UNIQUE(snapshot_id,material_code));
        CREATE TABLE IF NOT EXISTS reservations (
          id INTEGER PRIMARY KEY, production_batch_id INTEGER NOT NULL REFERENCES production_batches(id),
          for_material_code TEXT NOT NULL, stock_material_code TEXT NOT NULL,
          quantity TEXT NOT NULL, issued_quantity TEXT NOT NULL DEFAULT '0',
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(production_batch_id,for_material_code,stock_material_code));
        CREATE TABLE IF NOT EXISTS receipts (
          id INTEGER PRIMARY KEY, material_code TEXT NOT NULL, quantity TEXT NOT NULL, received_at TEXT NOT NULL,
          source_ref TEXT, source_line_no TEXT, purchase_id INTEGER REFERENCES purchases(id),
          production_batch_id INTEGER REFERENCES production_batches(id),
          iqc_status TEXT NOT NULL DEFAULT 'pending', accepted_quantity TEXT NOT NULL DEFAULT '0',
          version INTEGER NOT NULL DEFAULT 1, created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS purchases (
          id INTEGER PRIMARY KEY, reference TEXT, source_line_no TEXT, material_code TEXT NOT NULL, quantity TEXT NOT NULL, eta TEXT,
          eta_is_estimate INTEGER NOT NULL DEFAULT 1, eta_source TEXT NOT NULL DEFAULT 'manual',
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS purchase_allocations (
          id INTEGER PRIMARY KEY, purchase_id INTEGER NOT NULL REFERENCES purchases(id),
          production_batch_id INTEGER NOT NULL REFERENCES production_batches(id), material_code TEXT NOT NULL,
          quantity TEXT NOT NULL, UNIQUE(purchase_id,production_batch_id));
        CREATE TABLE IF NOT EXISTS substitutes (
          id INTEGER PRIMARY KEY, production_batch_id INTEGER NOT NULL REFERENCES production_batches(id),
          original_material_code TEXT NOT NULL, substitute_material_code TEXT NOT NULL,
          quantity TEXT NOT NULL, request_reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          approved_by INTEGER REFERENCES users(id), approved_at TEXT,
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS process_templates (
          product_code TEXT PRIMARY KEY, steps_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
          updated_by INTEGER NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS phase_templates (
          product_code TEXT NOT NULL, phase TEXT NOT NULL, steps_json TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, updated_by INTEGER NOT NULL REFERENCES users(id),
          updated_at TEXT NOT NULL, PRIMARY KEY(product_code,phase));
        CREATE TABLE IF NOT EXISTS stages (
          id INTEGER PRIMARY KEY, production_batch_id INTEGER NOT NULL REFERENCES production_batches(id),
          sequence INTEGER NOT NULL, name TEXT NOT NULL, planned_start TEXT, planned_finish TEXT,
          actual_start TEXT, actual_finish TEXT, status TEXT, version INTEGER NOT NULL DEFAULT 1,
          owner_name TEXT, owner_id INTEGER REFERENCES users(id),
          completed_by INTEGER REFERENCES users(id), completed_at TEXT,
          stage_type TEXT NOT NULL DEFAULT 'normal', duration_days TEXT NOT NULL DEFAULT '0',
          actual_material_at TEXT, note TEXT, legacy_source_index INTEGER,
          planned_start_at TEXT, planned_finish_at TEXT, plan_overridden INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(production_batch_id,name));
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, kind TEXT NOT NULL,
          event_date TEXT NOT NULL, event_at TEXT, quantity TEXT, material_code TEXT, for_material_code TEXT,
          note TEXT, actor_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY, task_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, title TEXT NOT NULL,
          assignee_id INTEGER REFERENCES users(id), status TEXT NOT NULL DEFAULT 'open', note TEXT,
          condition_active INTEGER NOT NULL DEFAULT 1,
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS holidays (
          day TEXT PRIMARY KEY, is_workday INTEGER NOT NULL, description TEXT,
          updated_by INTEGER NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, action TEXT NOT NULL,
          actor_id INTEGER REFERENCES users(id), before_json TEXT, after_json TEXT,
          reason TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_attempts (
          id INTEGER PRIMARY KEY, username TEXT NOT NULL, remote_ip TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_auth_attempts_user_time ON auth_attempts(username,created_at);
        CREATE INDEX IF NOT EXISTS idx_auth_attempts_ip_time ON auth_attempts(remote_ip,created_at);
        CREATE TABLE IF NOT EXISTS import_previews (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, filename TEXT NOT NULL, preview_json TEXT NOT NULL,
          source_blob BLOB NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_extractions (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, filename TEXT NOT NULL,
          source_sha256 TEXT NOT NULL, source_blob BLOB NOT NULL, csv_blob BLOB NOT NULL,
          metadata_json TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
          created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
          applied_import_batch_id INTEGER REFERENCES import_batches(id),
          UNIQUE(created_by,kind,source_sha256,model));
        CREATE TABLE IF NOT EXISTS import_batches (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, filename TEXT NOT NULL, source_blob BLOB NOT NULL,
          selection_json TEXT NOT NULL, rows_applied INTEGER NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id),
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bom_catalog (
          id INTEGER PRIMARY KEY, product_code TEXT NOT NULL, bom_version TEXT NOT NULL,
          source_line_no TEXT, material_code TEXT NOT NULL, material_name TEXT,
          quantity_per TEXT NOT NULL, loss_rate TEXT NOT NULL DEFAULT '0', unit_price TEXT,
          source_key TEXT NOT NULL UNIQUE, version INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS source_rows (
          kind TEXT NOT NULL, source_key TEXT NOT NULL, record_id INTEGER, values_json TEXT NOT NULL,
          last_import_batch_id INTEGER REFERENCES import_batches(id),
          PRIMARY KEY(kind,source_key));
        CREATE TABLE IF NOT EXISTS legacy_records (
          id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, source_hash TEXT NOT NULL,
          filename TEXT NOT NULL, source_index INTEGER NOT NULL, source_json TEXT NOT NULL,
          order_id INTEGER NOT NULL REFERENCES orders(id),
          order_line_id INTEGER REFERENCES order_lines(id),
          production_batch_id INTEGER REFERENCES production_batches(id),
          import_batch_id INTEGER NOT NULL REFERENCES import_batches(id),
          missing_fields_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          version INTEGER NOT NULL DEFAULT 1,
          created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_prod_line ON production_batches(order_line_id);
        CREATE INDEX IF NOT EXISTS idx_board_line ON order_line_boards(order_line_id);
        CREATE INDEX IF NOT EXISTS idx_batch_board_batch ON batch_boards(production_batch_id);
        CREATE INDEX IF NOT EXISTS idx_board_stage_board ON board_stages(batch_board_id,sequence);
        CREATE INDEX IF NOT EXISTS idx_delivery_line ON delivery_batches(order_line_id);
        CREATE INDEX IF NOT EXISTS idx_bom_catalog_product_version ON bom_catalog(product_code,bom_version);
        CREATE INDEX IF NOT EXISTS idx_res_stock ON reservations(stock_material_code);
        CREATE INDEX IF NOT EXISTS idx_inventory_material ON inventory_rows(material_code,snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_receipts_material_received ON receipts(material_code,received_at);
        CREATE INDEX IF NOT EXISTS idx_purchase_alloc_batch_material ON purchase_allocations(production_batch_id,material_code);
        CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit(entity_type,entity_id);
        CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type,entity_id);
        CREATE INDEX IF NOT EXISTS idx_events_kind_material ON events(kind,material_code);
        CREATE INDEX IF NOT EXISTS idx_legacy_order ON legacy_records(order_id);
        CREATE INDEX IF NOT EXISTS idx_ai_extractions_created ON ai_extractions(created_at);
        """)
        # Additive migration for databases created before ETA provenance was tracked.
        purchase_columns = {row["name"] for row in conn.execute("PRAGMA table_info(purchases)")}
        if "eta_is_estimate" not in purchase_columns:
            conn.execute("ALTER TABLE purchases ADD COLUMN eta_is_estimate INTEGER NOT NULL DEFAULT 1")
        if "eta_source" not in purchase_columns:
            conn.execute("ALTER TABLE purchases ADD COLUMN eta_source TEXT NOT NULL DEFAULT 'manual'")
        for table in ("bom_items", "bom_catalog"):
            if "unit_price" not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN unit_price TEXT")
        if "bom_product_code" not in {row["name"] for row in conn.execute("PRAGMA table_info(order_lines)")}:
            conn.execute("ALTER TABLE order_lines ADD COLUMN bom_product_code TEXT")
        line_columns = {row["name"] for row in conn.execute("PRAGMA table_info(order_lines)")}
        for column, declaration in {
            "board_set_confirmed_at": "TEXT", "board_set_confirmed_by": "INTEGER REFERENCES users(id)",
            "board_set_revision": "INTEGER NOT NULL DEFAULT 0",
            "board_schedule_mode": "TEXT NOT NULL DEFAULT 'shared'",
        }.items():
            if column not in line_columns:
                conn.execute(f"ALTER TABLE order_lines ADD COLUMN {column} {declaration}")
        if "active" not in {row["name"] for row in conn.execute("PRAGMA table_info(order_line_boards)")}:
            conn.execute("ALTER TABLE order_line_boards ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        batch_board_columns = {row["name"] for row in conn.execute("PRAGMA table_info(batch_boards)")}
        for column, declaration in {
            "planned_quantity": "TEXT", "vendor": "TEXT", "commit_start": "TEXT", "commit_finish": "TEXT",
            "conflict_ack_reason": "TEXT", "conflict_ack_by": "INTEGER REFERENCES users(id)",
            "conflict_ack_at": "TEXT", "conflict_ack_signature": "TEXT",
        }.items():
            if column not in batch_board_columns:
                conn.execute(f"ALTER TABLE batch_boards ADD COLUMN {column} {declaration}")
        item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(bom_items)")}
        for column, declaration in {
            "batch_board_id": "INTEGER REFERENCES batch_boards(id)",
            "catalog_source_line_no": "TEXT",
            "catalog_quantity_per": "TEXT",
        }.items():
            if column not in item_columns:
                conn.execute(f"ALTER TABLE bom_items ADD COLUMN {column} {declaration}")
        if "applied_import_batch_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(ai_extractions)")}:
            conn.execute("ALTER TABLE ai_extractions ADD COLUMN applied_import_batch_id INTEGER REFERENCES import_batches(id)")
        delivery_columns = {row["name"] for row in conn.execute("PRAGMA table_info(delivery_batches)")}
        if "manual_estimate_date" not in delivery_columns:
            conn.execute("ALTER TABLE delivery_batches ADD COLUMN manual_estimate_date TEXT")
        if "manual_estimate_reason" not in delivery_columns:
            conn.execute("ALTER TABLE delivery_batches ADD COLUMN manual_estimate_reason TEXT")
        order_columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
        if "project_name" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN project_name TEXT")
        if "project_mode" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN project_mode TEXT NOT NULL DEFAULT 'customer'")
        # Existing accounts retain their active/disabled state. Registered accounts
        # start pending and never become active until an administrator approves them.
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "status" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            conn.execute("UPDATE users SET status='disabled' WHERE active=0")
        for column, declaration in {
            "permissions_json": "TEXT", "approved_at": "TEXT", "approved_by": "INTEGER",
            "reject_reason": "TEXT", "password_changed_at": "TEXT",
        }.items():
            if column not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
        batch_columns = {row["name"] for row in conn.execute("PRAGMA table_info(production_batches)")}
        classify_existing = "phase" not in batch_columns
        for column, declaration in {
            "phase": "TEXT NOT NULL DEFAULT 'unclassified'", "phase_source": "TEXT NOT NULL DEFAULT 'manual'",
            "phase_order_no": "TEXT", "schedule_start": "TEXT", "schedule_mode": "TEXT NOT NULL DEFAULT 'workday'",
            "target_kind": "TEXT", "target_date": "TEXT", "sampling_order_date": "TEXT",
            "board_eta": "TEXT", "board_received_at": "TEXT", "debug_accepted_at": "TEXT",
            "bom_scope": "TEXT NOT NULL DEFAULT 'single'",
            "board_set_revision": "INTEGER", "board_set_confirmed_at": "TEXT",
            "board_schedule_mode": "TEXT NOT NULL DEFAULT 'shared'",
            "conflict_ack_signature": "TEXT",
        }.items():
            if column not in batch_columns:
                conn.execute(f"ALTER TABLE production_batches ADD COLUMN {column} {declaration}")
        for board in rows(conn, """SELECT b.id,b.quantity_per_product,p.quantity FROM batch_boards b
                             JOIN production_batches p ON p.id=b.production_batch_id
                             WHERE b.planned_quantity IS NULL"""):
            conn.execute("UPDATE batch_boards SET planned_quantity=? WHERE id=?",
                         (ds(dec(board["quantity_per_product"]) * dec(board["quantity"])), board["id"]))
        stage_columns = {row["name"] for row in conn.execute("PRAGMA table_info(stages)")}
        for column, declaration in {
            "owner_name": "TEXT", "owner_id": "INTEGER REFERENCES users(id)",
            "completed_by": "INTEGER REFERENCES users(id)", "completed_at": "TEXT",
            "stage_type": "TEXT NOT NULL DEFAULT 'normal'",
            "duration_days": "TEXT NOT NULL DEFAULT '0'", "actual_material_at": "TEXT",
            "note": "TEXT", "legacy_source_index": "INTEGER", "planned_start_at": "TEXT",
            "planned_finish_at": "TEXT", "plan_overridden": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in stage_columns:
                conn.execute(f"ALTER TABLE stages ADD COLUMN {column} {declaration}")
        backfill_legacy_phases(conn, classify_existing=classify_existing)


init_db()
app = FastAPI(title="PCBA 排期工作台", version="0.1.0")
if (ROOT / "web" / "assets").exists():
    app.mount("/assets", StaticFiles(directory=ROOT / "web" / "assets"), name="assets")


@app.get("/")
def index():
    page = ROOT / "web" / "index.html"
    if not page.exists():
        raise HTTPException(404, "前端尚未安装")
    return FileResponse(page)


@app.get("/api/health")
def health():
    try:
        with reading() as conn:
            conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    except sqlite3.Error:
        raise HTTPException(503, "数据库不可用")
    backups = backup_status(DB_PATH)
    mirror_configured = mirror_directory() is not None
    local = backup_runtime_status(DB_PATH)
    return {"ok": True, "database": "ok", "backup": {
        **local,
        "last_local_failure_at": backups.get("last_local_failure_at"),
        "mirror_configured": mirror_configured,
        "mirror_ok": backups.get("mirror_ok") if mirror_configured else None,
        "last_mirror_success_at": backups.get("last_mirror_success_at") if mirror_configured else None,
    }}


def password_digest(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()


def checked_username(value: Any) -> str:
    username = str(value or "").strip()
    if not 2 <= len(username) <= 50 or not all(char.isascii() and (char.isalnum() or char in "._-") for char in username):
        raise HTTPException(422, "登录账号须为 2–50 位英文字母、数字、点、下划线或短横线")
    return username


def checked_display_name(value: Any) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 100:
        raise HTTPException(422, "姓名须为 1–100 个字符")
    return name


def checked_password(value: Any) -> str:
    password = str(value or "")
    if not 10 <= len(password) <= 256:
        raise HTTPException(422, "密码须为 10–256 位")
    return password


def checked_permissions(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict) or any(key not in PERMISSION_LABELS or type(enabled) is not bool for key, enabled in value.items()):
        raise HTTPException(422, "权限内容无效")
    return value


def permissions_for_user(row: dict) -> dict[str, bool]:
    if row["role"] == "admin":
        return {key: True for key in PERMISSION_LABELS}
    permissions = DEFAULT_PERMISSIONS.copy()
    try:
        stored = json.loads(row.get("permissions_json") or "{}")
        if isinstance(stored, dict):
            permissions.update({key: enabled for key, enabled in stored.items()
                                if key in PERMISSION_LABELS and type(enabled) is bool})
    except (TypeError, ValueError):
        pass
    return permissions


def user_public(row: dict) -> dict:
    safe = {key: row[key] for key in ("id", "username", "display_name", "role", "active",
                                     "status", "created_at", "approved_at", "approved_by", "reject_reason") if key in row}
    safe["permissions"] = permissions_for_user(row)
    return safe


def require_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录")
    token_hash = hashlib.sha256(authorization[7:].encode()).hexdigest()
    with reading() as conn:
        user = one(conn, "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1 AND u.status='active'", (token_hash, utcnow()))
    if not user:
        raise HTTPException(401, "登录已失效")
    return user


def admin(user: dict) -> None:
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员账号")


def require_permission(user: dict, permission: str) -> None:
    if not permissions_for_user(user)[permission]:
        raise HTTPException(403, f"没有{PERMISSION_LABELS[permission]}权限")


def request_permission(method: str, path: str) -> str | None:
    if path == "/api/audit" and method == "GET":
        return "audit_view"
    if path.startswith("/api/imports") or path.startswith("/api/legacy"):
        return "imports_manage" if method != "GET" or path == "/api/imports" else None
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        for prefixes, permission in (
            (("/api/orders", "/api/order-lines", "/api/order-line-boards", "/api/delivery-batches", "/api/allocations"), "orders_edit"),
            (("/api/products", "/api/phase-templates", "/api/production-batches", "/api/batch-boards", "/api/board-stages", "/api/stages", "/api/vendors"), "production_edit"),
            (("/api/bom-items", "/api/bom-catalog", "/api/material-reviews", "/api/inventory", "/api/reservations", "/api/receipts",
              "/api/purchases", "/api/purchase-allocations", "/api/substitutes"), "materials_edit"),
            (("/api/tasks",), "tasks_manage"),
        ):
            if path.startswith(prefixes):
                return permission
    return None


class PermissionMiddleware:
    """Apply account switches to every HTTP route, including future write routes."""

    def __init__(self, app):
        self.inner_app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            permission = request_permission(scope["method"], scope["path"])
            if permission:
                headers = {key.lower(): value for key, value in scope["headers"]}
                try:
                    user = require_user(headers.get(b"authorization", b"").decode("utf-8"))
                    require_permission(user, permission)
                except HTTPException as exc:
                    response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                    await response(scope, receive, send)
                    return
        await self.inner_app(scope, receive, send)


app.add_middleware(PermissionMiddleware)


@app.post("/api/auth/bootstrap")
def bootstrap(data: dict, request: Request):
    setup_token = os.getenv("PCBA_SETUP_TOKEN")
    from_localhost = request.client and request.client.host in {"127.0.0.1", "::1", "localhost", "testclient"}
    if not from_localhost and (not setup_token or request.headers.get("X-Setup-Token") != setup_token):
        raise HTTPException(403, "首次管理员只能在主机本机设置")
    password = checked_password(need(data, "password"))
    with writing() as conn:
        if one(conn, "SELECT id FROM users LIMIT 1"):
            raise HTTPException(409, "管理员已创建")
        salt = secrets.token_hex(16)
        cur = conn.execute("INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at,status,approved_at) VALUES(?,?,?,?,?,?,?,?)",
                           (checked_username(need(data, "username")), checked_display_name(need(data, "display_name")),
                            "admin", salt, password_digest(password, salt), utcnow(), "active", utcnow()))
        return user_public(must_get(conn, "users", cur.lastrowid))


@app.get("/api/auth/status")
def auth_status():
    with reading() as conn:
        initialized = bool(one(conn, "SELECT id FROM users WHERE role='admin' AND active=1 AND status='active' LIMIT 1"))
    return {"initialized": initialized, "registration_open": initialized}


@app.post("/api/auth/register")
def register(data: dict):
    username = checked_username(need(data, "username"))
    display_name = checked_display_name(need(data, "display_name"))
    password = checked_password(need(data, "password"))
    with writing() as conn:
        if not one(conn, "SELECT id FROM users WHERE role='admin' AND active=1 AND status='active' LIMIT 1"):
            raise HTTPException(409, "请先在主机上创建管理员")
        if one(conn, "SELECT id FROM users WHERE username=?", (username,)):
            raise HTTPException(409, "登录账号已存在")
        salt = secrets.token_hex(16)
        cur = conn.execute("INSERT INTO users(username,display_name,role,password_salt,password_hash,active,created_at,status) VALUES(?,?,?,?,?,?,?,?)",
                           (username, display_name, "business", salt, password_digest(password, salt), 0, utcnow(), "pending"))
        audit(conn, "user", cur.lastrowid, "register", None, None, {"username": username, "status": "pending"})
    return {"message": "注册申请已提交，请等待管理员审批", "status": "pending"}


@app.post("/api/auth/login")
def login(data: dict, request: Request = None):
    username = str(data.get("username", "")).strip()[:50]
    remote_ip = (request.client.host if request and request.client else "direct")[:100]
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds")
    error = None
    result = None
    with writing() as conn:
        conn.execute("DELETE FROM auth_attempts WHERE created_at<?", (cutoff,))
        failed_ip = one(conn, "SELECT COUNT(*) AS n FROM auth_attempts WHERE remote_ip=?", (remote_ip,))["n"]
        failed_user = one(conn, "SELECT COUNT(*) AS n FROM auth_attempts WHERE username=?", (username,))["n"]
        if failed_ip >= 10 or failed_user >= 25:
            error = HTTPException(429, "登录尝试过多，请 15 分钟后重试", headers={"Retry-After": "900"})
        else:
            user = one(conn, "SELECT * FROM users WHERE username=?", (username,))
            password_matches = bool(user) and hmac.compare_digest(
                password_digest(str(data.get("password", "")), user["password_salt"]), user["password_hash"])
            if not password_matches:
                conn.execute("INSERT INTO auth_attempts(username,remote_ip,created_at) VALUES(?,?,?)",
                             (username, remote_ip, utcnow()))
                error = HTTPException(401, "账号或密码错误")
            elif user["status"] == "pending":
                error = HTTPException(403, "账号待管理员审批")
            elif user["status"] == "rejected":
                error = HTTPException(403, "账号申请已被拒绝" + (f"：{user['reject_reason']}" if user.get("reject_reason") else ""))
            elif not user["active"] or user["status"] != "active":
                error = HTTPException(403, "账号已停用，请联系管理员")
            else:
                token = secrets.token_urlsafe(32)
                conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                             (hashlib.sha256(token.encode()).hexdigest(), user["id"],
                              (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds")))
                conn.execute("DELETE FROM auth_attempts WHERE username=? AND remote_ip=?", (username, remote_ip))
                result = {"token": token, "user": user_public(user)}
    if error:
        raise error
    return result


@app.get("/api/auth/me")
def me(user: dict = Depends(require_user)):
    return user_public(user)


@app.post("/api/auth/change-password")
def change_password(data: dict, user: dict = Depends(require_user)):
    current = str(need(data, "current_password"))
    new_password = checked_password(need(data, "new_password"))
    if current == new_password:
        raise HTTPException(422, "新密码不能与原密码相同")
    if not hmac.compare_digest(password_digest(current, user["password_salt"]), user["password_hash"]):
        raise HTTPException(403, "原密码错误")
    with writing() as conn:
        salt = secrets.token_hex(16)
        conn.execute("UPDATE users SET password_salt=?,password_hash=?,password_changed_at=? WHERE id=?",
                     (salt, password_digest(new_password, salt), utcnow(), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        audit(conn, "user", user["id"], "change_password", user["id"], None, {"sessions_revoked": True})
    return {"ok": True, "relogin_required": True}


@app.get("/api/users")
def users(status: str | None = None, user: dict = Depends(require_user)):
    if status and status not in {"active", "pending", "rejected", "disabled", "deleted"}:
        raise HTTPException(422, "账号状态无效")
    with reading() as conn:
        if user["role"] == "admin":
            found = rows(conn, "SELECT * FROM users WHERE (? IS NULL AND status<>'deleted') OR status=? ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,created_at DESC", (status, status))
        else:
            found = rows(conn, "SELECT * FROM users WHERE active=1 AND status='active' ORDER BY display_name")
        return [user_public(row) for row in found]


@app.post("/api/users")
def create_user(data: dict, user: dict = Depends(require_user)):
    admin(user)
    role = need(data, "role")
    if role not in ROLES:
        raise HTTPException(422, "无效角色")
    password = checked_password(need(data, "password"))
    username = checked_username(need(data, "username"))
    display_name = checked_display_name(need(data, "display_name"))
    permissions = checked_permissions(data["permissions"]) if "permissions" in data else {}
    with writing() as conn:
        salt = secrets.token_hex(16)
        cur = conn.execute("INSERT INTO users(username,display_name,role,password_salt,password_hash,created_at,status,approved_at,approved_by,permissions_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (username, display_name, role, salt, password_digest(password, salt), utcnow(), "active",
                            utcnow(), user["id"], json.dumps(permissions)))
        result = must_get(conn, "users", cur.lastrowid)
        audit(conn, "user", cur.lastrowid, "create", user["id"], None, user_public(result))
        return user_public(result)


@app.post("/api/users/{user_id}/approve")
def approve_user(user_id: int, data: dict | None = None, user: dict = Depends(require_user)):
    admin(user)
    changes = data or {}
    if set(changes) - {"role", "permissions"}:
        raise HTTPException(422, "审批字段无效")
    role = changes.get("role", "business")
    if role not in ROLES:
        raise HTTPException(422, "无效角色")
    permissions = checked_permissions(changes["permissions"]) if "permissions" in changes else {}
    with writing() as conn:
        before = must_get(conn, "users", user_id)
        if before["status"] not in {"pending", "rejected"}:
            raise HTTPException(409, "只有待审批或被拒绝的账号可以审批")
        conn.execute("UPDATE users SET role=?,permissions_json=?,active=1,status='active',approved_at=?,approved_by=?,reject_reason=NULL WHERE id=?",
                     (role, json.dumps(permissions), utcnow(), user["id"], user_id))
        after = must_get(conn, "users", user_id)
        audit(conn, "user", user_id, "approve", user["id"], user_public(before), user_public(after))
        return user_public(after)


@app.post("/api/users/{user_id}/reject")
def reject_user(user_id: int, data: dict | None = None, user: dict = Depends(require_user)):
    admin(user)
    changes = data or {}
    if set(changes) - {"reason"}:
        raise HTTPException(422, "拒绝申请字段无效")
    reason = str(changes.get("reason") or "").strip()
    if len(reason) > 200:
        raise HTTPException(422, "原因不能超过 200 字")
    with writing() as conn:
        before = must_get(conn, "users", user_id)
        if before["status"] != "pending":
            raise HTTPException(409, "只能拒绝待审批的账号")
        conn.execute("UPDATE users SET active=0,status='rejected',reject_reason=? WHERE id=?", (reason or None, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        after = must_get(conn, "users", user_id)
        audit(conn, "user", user_id, "reject", user["id"], user_public(before), user_public(after), reason or None)
        return user_public(after)


@app.patch("/api/users/{user_id}")
def patch_user(user_id: int, data: dict, user: dict = Depends(require_user)):
    admin(user)
    if not isinstance(data, dict) or not data or set(data) - {"display_name", "role", "active", "permissions"}:
        raise HTTPException(422, "账号修改字段无效")
    display_name = checked_display_name(data["display_name"]) if "display_name" in data else None
    role = data.get("role")
    if role is not None and role not in ROLES:
        raise HTTPException(422, "无效角色")
    if "active" in data and type(data["active"]) is not bool:
        raise HTTPException(422, "启用状态应为布尔值")
    permissions = checked_permissions(data["permissions"]) if "permissions" in data else None
    with writing() as conn:
        before = must_get(conn, "users", user_id)
        if before["status"] == "deleted":
            raise HTTPException(409, "已删除的账号不能重新启用或修改")
        new_role = role or before["role"]
        new_active = int(data["active"]) if "active" in data else before["active"]
        if before["status"] in {"pending", "rejected"} and new_active:
            raise HTTPException(409, "请使用审批操作启用申请账号")
        if user_id == user["id"] and (new_role != "admin" or not new_active):
            raise HTTPException(409, "不能取消自己的管理员权限或停用自己的账号")
        if before["role"] == "admin" and before["active"] and (new_role != "admin" or not new_active):
            admins_left = one(conn, "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1 AND status='active'")["n"]
            if admins_left < 2:
                raise HTTPException(409, "至少需要保留一个启用的管理员")
        new_status = before["status"]
        if "active" in data and before["status"] not in {"pending", "rejected"}:
            new_status = "active" if new_active else "disabled"
        old_permissions = json.loads(before["permissions_json"] or "{}")
        if not isinstance(old_permissions, dict):
            old_permissions = {}
        if permissions is not None:
            old_permissions.update(permissions)
        conn.execute("UPDATE users SET display_name=?,role=?,active=?,status=?,permissions_json=? WHERE id=?",
                     (display_name if display_name is not None else before["display_name"], new_role,
                      new_active, new_status, json.dumps(old_permissions), user_id))
        if not new_active or new_role != before["role"] or permissions is not None:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        after = must_get(conn, "users", user_id)
        audit(conn, "user", user_id, "update", user["id"], user_public(before), user_public(after))
        return user_public(after)


@app.post("/api/users/{user_id}/reset-password")
def reset_user_password(user_id: int, data: dict, user: dict = Depends(require_user)):
    admin(user)
    password = checked_password(need(data, "password"))
    with writing() as conn:
        target = must_get(conn, "users", user_id)
        if target["status"] == "deleted":
            raise HTTPException(409, "已删除的账号不能重置密码")
        salt = secrets.token_hex(16)
        conn.execute("UPDATE users SET password_salt=?,password_hash=?,password_changed_at=? WHERE id=?",
                     (salt, password_digest(password, salt), utcnow(), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        audit(conn, "user", user_id, "reset_password", user["id"], None, {"sessions_revoked": True})
    return {"ok": True, "relogin_required": user_id == user["id"]}


OPEN_USER_STAGE_CONDITION = "actual_finish IS NULL AND COALESCE(status,'pending')<>'skipped'"


def user_open_assignments(conn: sqlite3.Connection, user_id: int) -> dict[str, int]:
    checks = {
        "orders": "SELECT COUNT(*) AS n FROM orders WHERE owner_id=? AND status='open'",
        "stages": f"SELECT COUNT(*) AS n FROM stages WHERE owner_id=? AND {OPEN_USER_STAGE_CONDITION}",
        "board_stages": f"SELECT COUNT(*) AS n FROM board_stages WHERE owner_id=? AND {OPEN_USER_STAGE_CONDITION}",
        "tasks": "SELECT COUNT(*) AS n FROM tasks WHERE assignee_id=? AND status='open'",
    }
    return {kind: one(conn, query, (user_id,))["n"] for kind, query in checks.items()}


@app.get("/api/users/{user_id}/delete-preview")
def preview_delete_user(user_id: int, user: dict = Depends(require_user)):
    admin(user)
    with reading() as conn:
        target = must_get(conn, "users", user_id)
        if target["status"] == "deleted":
            raise HTTPException(409, "账号已删除")
        return {"user": user_public(target), "assignments": user_open_assignments(conn, user_id)}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, data: dict, user: dict = Depends(require_user)):
    admin(user)
    if not isinstance(data, dict) or set(data) != {"replacement_user_id", "reason"}:
        raise HTTPException(422, "请指定接替账号并填写删除原因")
    reason = str(data["reason"] or "").strip()
    if not 1 <= len(reason) <= 200:
        raise HTTPException(422, "删除原因须为 1–200 个字符")
    if type(data["replacement_user_id"]) is not int:
        raise HTTPException(422, "接替账号无效")
    replacement_id = data["replacement_user_id"]
    with writing() as conn:
        target = must_get(conn, "users", user_id)
        if target["status"] == "deleted":
            raise HTTPException(409, "账号已删除")
        if user_id == user["id"]:
            raise HTTPException(409, "不能删除当前登录的管理员账号")
        if target["role"] == "admin" and target["active"]:
            admins_left = one(conn, "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1 AND status='active'")["n"]
            if admins_left < 2:
                raise HTTPException(409, "至少需要保留一个启用的管理员")
        replacement = must_get(conn, "users", replacement_id)
        if replacement_id == user_id or not replacement["active"] or replacement["status"] != "active":
            raise HTTPException(422, "接替人必须是其他已启用账号")

        assignments = user_open_assignments(conn, user_id)
        now = utcnow()
        transfers = (
            ("orders", "owner_id", "status='open'", {"owner_id": replacement_id}),
            ("stages", "owner_id", OPEN_USER_STAGE_CONDITION,
             {"owner_id": replacement_id, "owner_name": replacement["display_name"]}),
            ("board_stages", "owner_id", OPEN_USER_STAGE_CONDITION,
             {"owner_id": replacement_id, "owner_name": replacement["display_name"]}),
            ("tasks", "assignee_id", "status='open'", {"assignee_id": replacement_id}),
        )
        for table, owner_column, condition, changes in transfers:
            for before in rows(conn, f"SELECT * FROM {table} WHERE {owner_column}=? AND {condition}", (user_id,)):
                updates = {**changes, "version": before["version"] + 1, "updated_at": now}
                set_clause = ",".join(f"{column}=?" for column in updates)
                conn.execute(f"UPDATE {table} SET {set_clause} WHERE id=?", (*updates.values(), before["id"]))
                after = must_get(conn, table, before["id"])
                audit(conn, table, before["id"], "transfer_on_user_delete", user["id"], before, after, reason)

        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.execute("UPDATE users SET active=0,status='deleted',password_salt=?,password_hash=? WHERE id=?",
                     (secrets.token_hex(16), secrets.token_hex(64), user_id))
        after = must_get(conn, "users", user_id)
        audit(conn, "user", user_id, "delete", user["id"], user_public(target),
              {**user_public(after), "replacement_user_id": replacement_id, "transferred": assignments}, reason)
        return {"ok": True, "replacement_user_id": replacement_id, "transferred": assignments}


def ownership_groups(conn: sqlite3.Connection) -> dict:
    """Show unassigned historic orders separately from unassigned process roles."""
    order_counts: dict[str, int] = {}
    for order in rows(conn, "SELECT id FROM orders WHERE owner_id IS NULL ORDER BY id"):
        legacy = one(conn, "SELECT source_json FROM legacy_records WHERE order_id=?", (order["id"],))
        name = legacy_owner_name(legacy)
        if name:
            order_counts[name] = order_counts.get(name, 0) + 1
    stages = rows(conn, """SELECT b.phase,s.name,COUNT(*) AS count FROM stages s
                          JOIN production_batches b ON b.id=s.production_batch_id
                          WHERE s.owner_id IS NULL GROUP BY b.phase,s.name ORDER BY b.phase,s.name""")
    return {"users": rows(conn, "SELECT id,display_name,role FROM users WHERE active=1 AND status='active' ORDER BY display_name,id"),
            "order_groups": [{"legacy_name": name, "count": count} for name, count in sorted(order_counts.items())],
            "stage_groups": stages}


def ownership_candidates(conn: sqlite3.Connection, data: dict) -> dict:
    if not isinstance(data, dict):
        raise HTTPException(422, "责任人映射格式错误")
    order_bindings = data.get("order_bindings", [])
    stage_bindings = data.get("stage_bindings", [])
    if (not isinstance(order_bindings, list) or not isinstance(stage_bindings, list) or
            len(order_bindings) > 100 or len(stage_bindings) > 200):
        raise HTTPException(422, "责任人映射数量或格式错误")
    order_map: dict[str, int] = {}
    stage_map: dict[tuple[str, str], int] = {}
    owner_names: dict[int, str] = {}
    for binding in order_bindings:
        if not isinstance(binding, dict) or set(binding) != {"legacy_name", "user_id"}:
            raise HTTPException(422, "历史业务负责人映射格式错误")
        name = str(binding["legacy_name"] or "").strip()
        if not name or len(name) > 100 or name in order_map:
            raise HTTPException(422, "历史业务负责人名称为空、重复或过长")
        owner_id = active_owner_id(conn, binding["user_id"])
        order_map[name] = owner_id
        owner_names[owner_id] = must_get(conn, "users", owner_id)["display_name"]
    for binding in stage_bindings:
        if not isinstance(binding, dict) or set(binding) != {"phase", "name", "user_id"}:
            raise HTTPException(422, "生产节点责任人映射格式错误")
        phase = str(binding["phase"] or "").strip()
        name = str(binding["name"] or "").strip()
        key = (phase, name)
        if phase not in {"rd", "pp", "mp", "unclassified"} or not name or len(name) > 100 or key in stage_map:
            raise HTTPException(422, "生产节点阶段或名称无效、重复")
        owner_id = active_owner_id(conn, binding["user_id"])
        stage_map[key] = owner_id
        owner_names[owner_id] = must_get(conn, "users", owner_id)["display_name"]
    proposed_orders = []
    for order in rows(conn, "SELECT id,order_no,owner_id,version FROM orders WHERE owner_id IS NULL ORDER BY id"):
        legacy = one(conn, "SELECT source_json FROM legacy_records WHERE order_id=?", (order["id"],))
        legacy_name = legacy_owner_name(legacy)
        owner_id = order_map.get(legacy_name)
        if owner_id:
            proposed_orders.append({"id": order["id"], "order_no": order["order_no"],
                                    "legacy_name": legacy_name, "version": order["version"],
                                    "new_owner_id": owner_id, "new_owner_name": owner_names[owner_id]})
    proposed_stages = []
    for stage in rows(conn, """SELECT s.id,s.name,s.version,b.id AS batch_id,b.batch_no,b.phase,
                                      o.order_no FROM stages s JOIN production_batches b ON b.id=s.production_batch_id
                                      JOIN order_lines l ON l.id=b.order_line_id JOIN orders o ON o.id=l.order_id
                                      WHERE s.owner_id IS NULL ORDER BY o.id,b.id,s.sequence"""):
        owner_id = stage_map.get((stage["phase"], stage["name"]))
        if owner_id:
            proposed_stages.append({**stage, "new_owner_id": owner_id,
                                    "new_owner_name": owner_names[owner_id]})
    signed = {"orders": [(item["id"], item["version"], item["new_owner_id"]) for item in proposed_orders],
              "stages": [(item["id"], item["version"], item["new_owner_id"]) for item in proposed_stages]}
    signature = hashlib.sha256(json.dumps(signed, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"orders": proposed_orders, "stages": proposed_stages,
            "summary": {"orders": len(proposed_orders), "stages": len(proposed_stages)},
            "signature": signature}


@app.get("/api/setup/ownership")
def get_ownership_setup(user: dict = Depends(require_user)):
    admin(user)
    with reading() as conn:
        return ownership_groups(conn)


@app.post("/api/setup/ownership/preview")
def preview_ownership_setup(data: dict, user: dict = Depends(require_user)):
    admin(user)
    with reading() as conn:
        return ownership_candidates(conn, data)


@app.post("/api/setup/ownership/apply")
def apply_ownership_setup(data: dict, user: dict = Depends(require_user)):
    admin(user)
    reason = str(data.get("reason") or "").strip() if isinstance(data, dict) else ""
    if not reason or len(reason) > 500:
        raise HTTPException(422, "批量绑定责任人须填写不超过 500 字的原因")
    with writing() as conn:
        preview = ownership_candidates(conn, data)
        if preview["signature"] != data.get("signature"):
            raise HTTPException(409, "责任人预览已过期，请重新预览后应用")
        if not preview["orders"] and not preview["stages"]:
            raise HTTPException(422, "没有可绑定的订单或节点")
        for item in preview["orders"]:
            before = must_get(conn, "orders", item["id"])
            conn.execute("UPDATE orders SET owner_id=?,version=version+1,updated_at=? WHERE id=? AND version=? AND owner_id IS NULL",
                         (item["new_owner_id"], utcnow(), item["id"], item["version"]))
            audit(conn, "orders", item["id"], "bulk_bind_owner", user["id"], before,
                  must_get(conn, "orders", item["id"]), reason)
        for item in preview["stages"]:
            before = must_get(conn, "stages", item["id"])
            conn.execute("UPDATE stages SET owner_id=?,version=version+1,updated_at=? WHERE id=? AND version=? AND owner_id IS NULL",
                         (item["new_owner_id"], utcnow(), item["id"], item["version"]))
            audit(conn, "stages", item["id"], "bulk_bind_owner", user["id"], before,
                  must_get(conn, "stages", item["id"]), reason)
        sync_tasks(conn)
        return {"ok": True, "summary": preview["summary"]}


def normalize_changes(changes: dict, allowed: set[str], *, reason: str | None,
                      protected: set[str] = frozenset()) -> dict:
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise HTTPException(422, "修改字段无效")
    if set(changes) & protected and not str(reason or "").strip():
        raise HTTPException(422, "修改数量或关键日期必须填写原因")
    result = dict(changes)
    for key in set(result) & {"quantity", "quantity_per", "loss_rate"}:
        result[key] = ds(dec(result[key], allow_zero=key == "loss_rate"))
    for key in set(result) & {"commit_start", "commit_finish", "due_date", "promised_date", "manual_estimate_date", "eta", "planned_start", "planned_finish", "actual_start", "actual_finish"}:
        result[key] = valid_date(result[key])
    if "completion_basis" in result and result["completion_basis"] not in {"shipment", "receipt"}:
        raise HTTPException(422, "完成口径应为 shipment 或 receipt")
    return result


def patch_record(conn: sqlite3.Connection, table: str, row_id: int, version: int,
                 changes: dict, actor_id: int, reason: str | None = None) -> dict:
    before = must_get(conn, table, row_id)
    if before["version"] != version:
        raise HTTPException(409, "记录已被其他人修改，请刷新后重试")
    if all(before.get(key) == value for key, value in changes.items()):
        return before
    update = dict(changes)
    update["version"] = version + 1
    if "updated_at" in before:
        update["updated_at"] = utcnow()
    clause = ",".join(f"{key}=?" for key in update)
    conn.execute(f"UPDATE {table} SET {clause} WHERE id=? AND version=?", (*update.values(), row_id, version))
    after = must_get(conn, table, row_id)
    audit(conn, table, row_id, "update", actor_id, before, after, reason)
    return after


def workday(conn: sqlite3.Connection, day: date) -> bool:
    override = one(conn, "SELECT is_workday FROM holidays WHERE day=?", (day.isoformat(),))
    return bool(override["is_workday"]) if override else day.weekday() < 5


def add_workdays(conn: sqlite3.Connection, day_str: str, count: int) -> str:
    day = date.fromisoformat(day_str)
    step = 1 if count >= 0 else -1
    for _ in range(abs(count)):
        day += timedelta(days=step)
        while not workday(conn, day):
            day += timedelta(days=step)
    return day.isoformat()


def workdays_between(conn: sqlite3.Connection, start: str, end: str) -> int:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        return -workdays_between(conn, end, start)
    result = 0
    while first < last:
        first += timedelta(days=1)
        if workday(conn, first):
            result += 1
    return result


def template_for(conn: sqlite3.Connection, product_code: str) -> dict:
    current = one(conn, "SELECT * FROM process_templates WHERE product_code=?", (product_code,))
    return {"product_code": product_code, "version": current["version"] if current else 0,
            "steps": json.loads(current["steps_json"]) if current else DEFAULT_TEMPLATE}


def checked_phase(value: Any, *, allow_unclassified: bool = True) -> str:
    phase = str(value or "unclassified")
    if phase not in PHASE_LABELS or (phase == "unclassified" and not allow_unclassified):
        raise HTTPException(422, "阶段须为研发试产打样、PP 或 MP")
    return phase


def checked_schedule_mode(value: Any) -> str:
    mode = str(value or "workday")
    if mode not in {"workday", "natural"}:
        raise HTTPException(422, "日历规则须为工作日或自然日")
    return mode


def checked_stage_duration(value: Any) -> str:
    duration = dec(value)
    if duration > 365 or duration * 2 != (duration * 2).to_integral_value():
        raise HTTPException(422, "节点耗时须为 0 到 365 天，按半天填写")
    return ds(duration)


def phase_template_for(conn: sqlite3.Connection, product_code: str, phase: str) -> dict:
    checked_phase(phase, allow_unclassified=False)
    saved = one(conn, "SELECT * FROM phase_templates WHERE product_code=? AND phase=?", (product_code, phase))
    return {"product_code": product_code, "phase": phase,
            "phase_label": PHASE_LABELS[phase], "version": saved["version"] if saved else 0,
            "steps": json.loads(saved["steps_json"]) if saved else phase_template(phase)}


@app.get("/api/phase-templates")
def get_phase_templates(user: dict = Depends(require_user)):
    return [{"phase": phase, "label": label, "steps": phase_template(phase)}
            for phase, label in PHASE_LABELS.items() if phase != "unclassified"]


@app.get("/api/products/{product_code}/phase-template")
def get_phase_template(product_code: str, phase: str, user: dict = Depends(require_user)):
    with reading() as conn:
        return phase_template_for(conn, product_code, phase)


@app.get("/api/schedule-preview")
def preview_stage_schedule(product_code: str, phase: str, schedule_start: str,
                           schedule_mode: str = "workday", batch_id: int | None = None,
                           board_eta: str | None = None, board_received_at: str | None = None,
                           user: dict = Depends(require_user)):
    """Preview the same dated draft used when stages are generated or replanned."""
    phase = checked_phase(phase, allow_unclassified=False)
    start = valid_date(schedule_start, required=True)
    mode = checked_schedule_mode(schedule_mode)
    eta = valid_date(board_eta)
    received = valid_date(board_received_at)
    with reading() as conn:
        batch = must_get(conn, "production_batches", batch_id) if batch_id else None
        if batch:
            line = must_get(conn, "order_lines", batch["order_line_id"])
            if line["product_code"] != product_code:
                raise HTTPException(422, "预览产品与生产批次不一致")
        # Reclassifying an existing batch does not delete its historical or hand-edited
        # nodes. Preview those same nodes so the shown finish matches the saved plan.
        use_current = bool(batch)
        if use_current:
            steps = rows(conn, "SELECT * FROM stages WHERE production_batch_id=? ORDER BY sequence", (batch_id,))
        else:
            steps = phase_template_for(conn, product_code, phase)["steps"]
        nodes = []
        for step in steps:
            node = dict(step)
            node.setdefault("stage_type", "milestone" if node["name"] in
                            {"样板到料", "板厂签收", "板厂完工", "客户签收"} else "normal")
            if phase == "rd" and node["name"] == "样板到料" and not node.get("actual_material_at"):
                node["actual_material_at"] = (received or eta or
                                              (batch["board_received_at"] or batch["board_eta"] if batch else None))
            nodes.append(node)
        try:
            plans = plan_nodes(start, nodes, mode, lambda day: workday(conn, day))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        dated = [{"name": node["name"], "duration_days": node.get("duration_days", "0"),
                  "planned_start": plan["planned_start"], "planned_finish": plan["planned_finish"]}
                 for node, plan in zip(nodes, plans)]
        return {"status": "draft", "phase": phase, "schedule_start": start,
                "schedule_mode": mode, "source": "current_stages" if use_current else "phase_template",
                "phase_change_pending": bool(batch and batch["phase"] != phase),
                "earliest_finish": dated[-1]["planned_finish"] if dated else None,
                "nodes": dated,
                "note": "节点草案最早完工，需核对到料/厂商承诺；不是正式客户交期"}


@app.put("/api/products/{product_code}/phase-template")
def put_phase_template(product_code: str, phase: str, data: dict, user: dict = Depends(require_user)):
    checked_phase(phase, allow_unclassified=False)
    proposed = data.get("steps")
    if not isinstance(proposed, list) or not 1 <= len(proposed) <= 50:
        raise HTTPException(422, "阶段模板应包含 1 到 50 个节点")
    normalized = []
    names = set()
    for item in proposed:
        if not isinstance(item, dict):
            raise HTTPException(422, "节点应为对象")
        name = str(need(item, "name")).strip()
        if not name or len(name) > 100 or name in names:
            raise HTTPException(422, "节点名称不能为空、重复或超过 100 字")
        names.add(name)
        stage_type = item.get("stage_type", "normal")
        if stage_type not in {"normal", "milestone"}:
            raise HTTPException(422, "节点类型无效")
        normalized.append({"name": name,
                           "duration_days": checked_stage_duration(item.get("duration_days", "0")),
                           "stage_type": stage_type})
    with writing() as conn:
        old = one(conn, "SELECT * FROM phase_templates WHERE product_code=? AND phase=?", (product_code, phase))
        expected = int(data.get("version", 0))
        if expected != (old["version"] if old else 0):
            raise HTTPException(409, "模板已由其他人修改，请刷新")
        version = expected + 1
        conn.execute("INSERT INTO phase_templates(product_code,phase,steps_json,version,updated_by,updated_at) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(product_code,phase) DO UPDATE SET steps_json=excluded.steps_json,version=excluded.version,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                     (product_code, phase, json.dumps(normalized, ensure_ascii=False), version, user["id"], utcnow()))
        result = phase_template_for(conn, product_code, phase)
        audit(conn, "phase_template", 0, "update", user["id"], old, result, data.get("reason"))
        return result


def replan_batch_stages(conn: sqlite3.Connection, batch_id: int, actor_id: int,
                        reason: str) -> list[dict]:
    batch = must_get(conn, "production_batches", batch_id)
    current = rows(conn, "SELECT * FROM stages WHERE production_batch_id=? ORDER BY sequence", (batch_id,))
    schedule_nodes = []
    for stage in current:
        node = dict(stage)
        if batch["phase"] == "rd" and stage["name"] == "样板到料" and not node["actual_material_at"]:
            node["actual_material_at"] = batch["board_received_at"] or batch["board_eta"]
        schedule_nodes.append(node)
    try:
        plans = plan_nodes(batch["schedule_start"] or batch["commit_start"], schedule_nodes,
                           batch["schedule_mode"], lambda day: workday(conn, day))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    changes = []
    for stage, plan in zip(current, plans):
        fields = {key: plan[key] for key in ("planned_start", "planned_finish", "planned_start_at", "planned_finish_at")
                  if plan[key] != stage[key]}
        if not fields:
            continue
        conn.execute("""UPDATE stages SET planned_start=?,planned_finish=?,planned_start_at=?,planned_finish_at=?,
                      version=version+1,updated_at=? WHERE id=?""",
                     (plan["planned_start"], plan["planned_finish"], plan["planned_start_at"],
                      plan["planned_finish_at"], utcnow(), stage["id"]))
        changes.append({"stage_id": stage["id"], "before": {key: stage[key] for key in fields},
                        "after": fields})
    if changes:
        audit(conn, "production_batches", batch_id, "replan_stages", actor_id, None, changes, reason)
    return rows(conn, "SELECT * FROM stages WHERE production_batch_id=? ORDER BY sequence", (batch_id,))


def generate_stages(conn: sqlite3.Connection, batch_id: int, actor_id: int, replace: bool = False) -> list[dict]:
    batch = must_get(conn, "production_batches", batch_id)
    line = must_get(conn, "order_lines", batch["order_line_id"])
    phase = checked_phase(batch["phase"], allow_unclassified=False)
    if replace:
        existing = rows(conn, "SELECT * FROM stages WHERE production_batch_id=?", (batch_id,))
        if any(item["actual_start"] or item["actual_finish"] for item in existing):
            raise HTTPException(409, "已有实际工序日期，不能重新生成")
        conn.execute("DELETE FROM stages WHERE production_batch_id=?", (batch_id,))
    elif one(conn, "SELECT id FROM stages WHERE production_batch_id=?", (batch_id,)):
        return rows(conn, "SELECT * FROM stages WHERE production_batch_id=? ORDER BY sequence", (batch_id,))
    steps = phase_template_for(conn, line["product_code"], phase)["steps"]
    nodes = [{"duration_days": step.get("duration_days", "0"),
              "stage_type": step.get("stage_type", "milestone" if step["name"] in {"样板到料", "板厂签收", "板厂完工", "客户签收"} else "normal"),
              "actual_material_at": (batch["board_received_at"] or batch["board_eta"])
              if phase == "rd" and step["name"] == "样板到料" else None}
             for step in steps]
    try:
        plans = plan_nodes(batch["schedule_start"] or batch["commit_start"], nodes,
                           batch["schedule_mode"], lambda day: workday(conn, day))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = []
    for sequence, (step, plan) in enumerate(zip(steps, plans), 1):
        cur = conn.execute("""
            INSERT INTO stages(production_batch_id,sequence,name,planned_start,planned_finish,
                               stage_type,duration_days,actual_material_at,planned_start_at,planned_finish_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (batch_id, sequence, step["name"], plan["planned_start"], plan["planned_finish"],
                  nodes[sequence - 1]["stage_type"], nodes[sequence - 1]["duration_days"],
                  batch["board_received_at"] if phase == "rd" and step["name"] == "样板到料" else None,
                  plan["planned_start_at"], plan["planned_finish_at"], utcnow(), utcnow()))
        result.append(must_get(conn, "stages", cur.lastrowid))
    audit(conn, "production_batches", batch_id, "generate_stages", actor_id, None, result)
    return result


@app.get("/api/products/{product_code}/stage-template")
def get_template(product_code: str, user: dict = Depends(require_user)):
    with reading() as conn:
        return template_for(conn, product_code)


@app.put("/api/products/{product_code}/stage-template")
def put_template(product_code: str, data: dict, user: dict = Depends(require_user)):
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 30:
        raise HTTPException(422, "工序模板应有 1 到 30 个节点")
    names = []
    normalized = []
    for step in steps:
        name = str(need(step, "name")).strip()
        if name not in STAGE_NAMES or name in names:
            raise HTTPException(422, "工序名称必须来自已确认流程且不可重复")
        names.append(name)
        duration = dec(step.get("duration_days", 0))
        if duration != int(duration):
            raise HTTPException(422, "当前按整工作日计划，请填写整数天数")
        normalized.append({"name": name, "duration_days": int(duration), "enabled": bool(step.get("enabled", True))})
    if names != STAGE_NAMES:
        raise HTTPException(422, "模板须保留已确认的全部流程节点与顺序")
    if any(not step["enabled"] and step["name"] not in {"DIP", "烧录"} for step in normalized):
        raise HTTPException(422, "当前仅 DIP 与烧录可设为跳过")
    with writing() as conn:
        old = template_for(conn, product_code)
        if data.get("version", old["version"]) != old["version"]:
            raise HTTPException(409, "模板已被其他人修改")
        version = old["version"] + 1
        conn.execute("INSERT INTO process_templates(product_code,steps_json,version,updated_by,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(product_code) DO UPDATE SET steps_json=excluded.steps_json,version=excluded.version,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                     (product_code, json.dumps(normalized, ensure_ascii=False), version, user["id"], utcnow()))
        result = template_for(conn, product_code)
        audit(conn, "process_template", 0, "update", user["id"], old, result)
        return result


def legacy_owner_name(record: dict | None) -> str | None:
    """Keep the old free-text owner distinct from an assigned workbench user."""
    if not record:
        return None
    source = json.loads(record["source_json"])
    owner = source.get("owner")
    if isinstance(owner, str):
        return owner.strip() or None
    return None


def order_detail(conn: sqlite3.Connection, order_id: int) -> dict:
    order = must_get(conn, "orders", order_id)
    legacy = one(conn, "SELECT * FROM legacy_records WHERE order_id=?", (order_id,))
    old_owner = legacy_owner_name(legacy)
    assigned_owner = (one(conn, "SELECT display_name FROM users WHERE id=?", (order["owner_id"],))
                      if order["owner_id"] else None)
    order["legacy_owner_name"] = old_owner
    order["owner_name"] = assigned_owner["display_name"] if assigned_owner else old_owner
    order["owner_binding_status"] = "bound" if assigned_owner else "legacy_unbound" if old_owner else "missing"
    if legacy:
        missing = legacy_missing_fields(conn, legacy)
        order["legacy_record"] = {"id": legacy["id"], "status": "pending" if missing else "complete",
                                  "missing_fields": missing}
        order["legacy_schedule"] = project_legacy_schedule(json.loads(legacy["source_json"]))
    order["lines"] = rows(conn, "SELECT * FROM order_lines WHERE order_id=? ORDER BY line_no", (order_id,))
    if not order.get("project_name") and order["lines"]:
        order["project_name"] = order["lines"][0]["product_name"]
    order_total = Decimal(0)
    order_completed = Decimal(0)
    for line in order["lines"]:
        order_total += dec(line["quantity"])
        line["boards"] = board_definitions(conn, line["id"])
        line["board_set_confirmed"] = bool(line["board_set_confirmed_at"])
        line["production_batches"] = rows(conn, "SELECT * FROM production_batches WHERE order_line_id=? ORDER BY batch_no", (line["id"],))
        for production in line["production_batches"]:
            production["legacy_imported"] = is_imported_batch(conn, production["id"])
            if production["bom_scope"] == "multi":
                production["boards"] = batch_board_views(conn, production["id"])
        line["delivery_batches"] = rows(conn, "SELECT * FROM delivery_batches WHERE order_line_id=? ORDER BY batch_no", (line["id"],))
        line_completed = Decimal(0)
        for batch in line["delivery_batches"]:
            batch["forecast"] = delivery_forecast(conn, batch)
            batch["events"] = rows(conn, "SELECT * FROM events WHERE entity_type='delivery_batch' AND entity_id=? ORDER BY event_date,id", (batch["id"],))
            line_completed += min(dec(batch["quantity"]), dec(batch["forecast"]["completed_quantity"]))
        line["completed_quantity"] = ds(line_completed)
        order_completed += line_completed
    order["total_quantity"] = ds(order_total)
    order["completed_quantity"] = ds(order_completed)
    return order


@app.get("/api/orders")
def get_orders(user: dict = Depends(require_user)):
    with reading() as conn:
        return [order_detail(conn, row["id"]) for row in rows(conn, "SELECT id FROM orders ORDER BY updated_at DESC")]


@app.get("/api/orders/{order_id}")
def get_order(order_id: int, user: dict = Depends(require_user)):
    with reading() as conn:
        return order_detail(conn, order_id)


def export_xlsx(sheets: list[tuple], notes: list[tuple], filename: str) -> Response:
    content = build_workbook(sheets, notes)
    encoded = quote(filename)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=PCBA-export.xlsx; filename*=UTF-8''{encoded}",
                             "Cache-Control": "no-store"})


def export_notes(scope: str, count: int, meaning: str) -> list[tuple[str, str]]:
    return [("导出范围", scope), ("导出时间", datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")),
            ("记录数", str(count)), ("日期说明", meaning),
            ("数据来源", "PCBA 排期交付协同平台当前记录；导出时的快照，不会自动更新")]


@app.get("/api/exports/project-schedule.xlsx")
def export_project_schedule(order_line_id: int | None = None, phase: str = "all",
                            user: dict = Depends(require_user)):
    if phase not in {"all", *PHASE_LABELS}:
        raise HTTPException(422, "排期阶段无效")
    if order_line_id is not None and order_line_id <= 0:
        raise HTTPException(422, "项目编号无效")
    project_rows, batch_rows, board_rows, node_rows = [], [], [], []
    with reading() as conn:
        for order_row in rows(conn, "SELECT id FROM orders ORDER BY id"):
            order = order_detail(conn, order_row["id"])
            for line in order["lines"]:
                if order_line_id is not None and line["id"] != order_line_id:
                    continue
                batches = [batch for batch in line["production_batches"]
                           if phase == "all" or batch["phase"] == phase]
                if phase != "all" and not batches:
                    continue
                common = {"project_name": order["project_name"], "project_mode": "客户订单" if order["project_mode"] == "customer" else "内部研发",
                          "order_no": order["order_no"], "customer": order["customer"],
                          "owner": order["owner_name"], "line_no": line["line_no"],
                          "product_code": line["product_code"], "product_name": line["product_name"]}
                project_rows.append({**common, "order_quantity": line["quantity"],
                                     "delivered_quantity": line["completed_quantity"],
                                     "batch_count": len(batches), "board_mode": "逐板独立" if line["board_schedule_mode"] == "independent" else "批次共用"})
                for batch in batches:
                    batch_name = production_batch_label(conn, batch)
                    forecast = batch_finish_forecast(conn, batch["id"])
                    batch_common = {**common, "phase": PHASE_LABELS.get(batch["phase"], batch["phase"]),
                                    "batch_no": batch_name, "phase_order_no": batch["phase_order_no"]}
                    batch_rows.append({**batch_common, "quantity": batch["quantity"],
                                       "board_mode": "逐板独立" if batch["board_schedule_mode"] == "independent" else "批次共用",
                                       "schedule_start": batch["schedule_start"],
                                       "schedule_mode": "工作日" if batch["schedule_mode"] == "workday" else "自然日",
                                       "target_kind": "内部目标" if batch["target_kind"] == "internal" else "客户目标" if batch["target_kind"] == "customer" else "",
                                       "target_date": batch["target_date"], "vendor": batch["vendor"],
                                       "commit_start": batch["commit_start"], "commit_finish": batch["commit_finish"],
                                       "board_eta": batch["board_eta"], "board_received_at": batch["board_received_at"],
                                       "debug_accepted_at": batch["debug_accepted_at"],
                                       "bom_version": batch["bom_version"], "bom_status": "已锁定" if batch["bom_locked"] else "待锁定",
                                       "material_ready_at": forecast.get("material_ready_date"),
                                       "forecast_status": {"unknown": "无法确定", "forecast": "已预测", "complete": "已完成"}.get(forecast["status"], forecast["status"]),
                                       "formal_finish": forecast.get("formal_date"), "provisional_finish": forecast.get("provisional_date"),
                                       "forecast_reason": forecast.get("reason")})
                    boards = rows(conn, "SELECT * FROM batch_boards WHERE production_batch_id=? ORDER BY id", (batch["id"],))
                    for board in boards:
                        board_stages = board_stage_views(conn, board["id"]) if batch["board_schedule_mode"] == "independent" else []
                        planned_starts = [stage["planned_start"] for stage in board_stages if stage["planned_start"]]
                        planned_finishes = [stage["planned_finish"] for stage in board_stages if stage["planned_finish"]]
                        actual_finishes = [stage["actual_finish"] for stage in board_stages if stage["actual_finish"]]
                        board_rows.append({**batch_common, "board_ref": board["board_ref"],
                                           "board_name": board["board_name"], "board_product_code": board["bom_product_code"],
                                           "quantity_per_product": board["quantity_per_product"],
                                           "planned_quantity": board["planned_quantity"] or ds(dec(batch["quantity"]) * dec(board["quantity_per_product"])),
                                           "completed_quantity": ds(board_finished_quantity(conn, board["id"])),
                                           "board_mode": "逐板独立" if batch["board_schedule_mode"] == "independent" else "批次共用",
                                           "vendor": board["vendor"] if batch["board_schedule_mode"] == "independent" else board["vendor"] or batch["vendor"],
                                           "commit_start": board["commit_start"] if batch["board_schedule_mode"] == "independent" else board["commit_start"] or batch["commit_start"],
                                           "commit_finish": board["commit_finish"] if batch["board_schedule_mode"] == "independent" else board["commit_finish"] or batch["commit_finish"],
                                           "planned_start": min(planned_starts) if planned_starts else None,
                                           "planned_finish": max(planned_finishes) if planned_finishes else None,
                                           "actual_finish": max(actual_finishes) if actual_finishes and len(actual_finishes) == len(board_stages) else None,
                                           "bom_version": board["bom_version"],
                                           "bom_status": "已锁定" if board["bom_locked"] else "待锁定"})
                    if batch["board_schedule_mode"] == "independent":
                        scoped_stages = [(board["board_ref"], board["board_name"], stage)
                                         for board in boards for stage in board_stage_views(conn, board["id"])]
                    else:
                        scoped_stages = [(None, None, stage) for stage in stage_views(conn, batch["id"])]
                    for board_ref, board_name, stage in scoped_stages:
                        node_rows.append({**batch_common, "node_scope": "逐板独立" if board_ref else "批次共用",
                                          "board_ref": board_ref, "board_name": board_name,
                                          "sequence": stage["sequence"], "stage_name": stage["name"],
                                          "owner": stage["owner_display_name"],
                                          "planned_start": stage["planned_start"], "planned_finish": stage["planned_finish"],
                                          "actual_start": stage["actual_start"], "actual_finish": stage["actual_finish"],
                                          "stage_status": "已完成" if stage["is_complete"] else "已跳过" if stage["status"] == "skipped" else "进行中" if stage["status"] == "in_progress" else "待开始",
                                          "completed_by": stage["completed_by_name"], "note": stage["note"]})
    if not project_rows:
        raise HTTPException(404, "没有符合条件的项目排期可导出")
    identity = [("project_name", "项目名称", "text", 26), ("order_no", "项目 / 订单号", "text", 24),
                ("customer", "客户", "text", 20), ("product_code", "产品编码", "text", 19),
                ("product_name", "产品名称", "text", 24)]
    batch_identity = identity + [("phase", "阶段", "text", 17), ("batch_no", "生产批次", "text", 17),
                                 ("phase_order_no", "阶段单号", "text", 20)]
    project_cols = identity + [("project_mode", "项目类型", "text", 14), ("owner", "业务负责人", "text", 17),
                               ("line_no", "产品行号", "text", 13), ("order_quantity", "订单数量", "number", 15),
                               ("delivered_quantity", "已交付数量", "number", 15), ("batch_count", "筛选后批次数", "number", 15),
                               ("board_mode", "默认排期方式", "text", 15)]
    batch_cols = batch_identity + [("quantity", "生产数量", "number", 15), ("board_mode", "排期方式", "text", 15),
                                   ("schedule_start", "排期起点", "date", 15), ("schedule_mode", "日历规则", "text", 13),
                                   ("target_kind", "目标类型", "text", 14), ("target_date", "阶段目标日", "date", 16),
                                   ("vendor", "外协厂", "text", 21), ("commit_start", "厂商承诺开工", "date", 17),
                                   ("commit_finish", "厂商承诺完工", "date", 17), ("board_eta", "预计到板", "date", 15),
                                   ("board_received_at", "实际到板", "date", 15), ("debug_accepted_at", "调试验收", "date", 15),
                                   ("bom_version", "BOM 版本", "text", 16), ("bom_status", "BOM 状态", "text", 13),
                                   ("material_ready_at", "物料齐套日", "date", 16), ("forecast_status", "正式预测状态", "text", 17),
                                   ("formal_finish", "正式预测完工日", "date", 18),
                                   ("provisional_finish", "临时估算完工日", "date", 18),
                                   ("forecast_reason", "待确认原因", "text", 45)]
    board_cols = batch_identity + [("board_ref", "板卡标识", "text", 18), ("board_name", "板卡名称", "text", 22),
                                   ("board_product_code", "板卡编码", "text", 20),
                                   ("quantity_per_product", "单套板数", "number", 14),
                                   ("planned_quantity", "板卡计划数量", "number", 18),
                                   ("completed_quantity", "板卡完工数量", "number", 18), ("board_mode", "排期方式", "text", 15),
                                   ("vendor", "外协厂", "text", 20), ("commit_start", "承诺开工", "date", 15),
                                   ("commit_finish", "承诺完工", "date", 15),
                                   ("planned_start", "计划开始", "date", 15), ("planned_finish", "计划完成", "date", 15),
                                   ("actual_finish", "节点实际完成", "date", 17), ("bom_version", "BOM 版本", "text", 16),
                                   ("bom_status", "BOM 状态", "text", 13)]
    node_cols = batch_identity + [("node_scope", "节点范围", "text", 14), ("board_ref", "板卡标识", "text", 17),
                                  ("board_name", "板卡名称", "text", 22), ("sequence", "顺序", "number", 9),
                                  ("stage_name", "生产节点", "text", 22), ("owner", "节点责任人", "text", 19),
                                  ("planned_start", "计划开始", "date", 15), ("planned_finish", "计划完成", "date", 15),
                                  ("actual_start", "实际开始", "date", 15), ("actual_finish", "实际完成", "date", 15),
                                  ("stage_status", "完成状态", "text", 13), ("completed_by", "完成人", "text", 17),
                                  ("note", "备注", "text", 34)]
    scope = f"产品行 {order_line_id if order_line_id is not None else '全部'}；阶段 {PHASE_LABELS.get(phase, '全部')}"
    return export_xlsx([("项目清单", project_cols, project_rows), ("生产批次", batch_cols, batch_rows),
                        ("板件排期", board_cols, board_rows), ("生产节点", node_cols, node_rows)],
                       export_notes(scope, len(project_rows), "正式预测未确定时日期留空；临时估算另列显示。节点实际完成以勾选完成并登记实际完成日为准。"),
                       f"PCBA项目排期_{today()}.xlsx")


@app.get("/api/exports/customer-orders.xlsx")
def export_customer_orders(q: str = "", user: dict = Depends(require_user)):
    query = q.strip().casefold()
    if len(query) > 200:
        raise HTTPException(422, "搜索词过长")
    order_rows, delivery_rows, allocation_rows = [], [], []
    with reading() as conn:
        for order_row in rows(conn, "SELECT id FROM orders WHERE project_mode='customer' ORDER BY id"):
            order = order_detail(conn, order_row["id"])
            searchable = [order["project_name"], order["order_no"], order["customer"]]
            searchable.extend(value for line in order["lines"] for value in
                              (line["product_code"], line["product_name"]))
            if query and not any(query in str(value or "").casefold() for value in searchable):
                continue
            for line in order["lines"]:
                common = {"project_name": order["project_name"], "order_no": order["order_no"],
                          "customer": order["customer"], "owner": order["owner_name"],
                          "order_status": "已完成" if order["status"] == "complete" else "跟进中",
                          "line_no": line["line_no"], "product_code": line["product_code"],
                          "product_name": line["product_name"]}
                order_rows.append({**common, "order_quantity": line["quantity"],
                                   "delivered_quantity": line["completed_quantity"],
                                   "delivery_count": len(line["delivery_batches"])})
                for delivery in line["delivery_batches"]:
                    forecast = delivery["forecast"]
                    allocations = rows(conn, "SELECT * FROM allocations WHERE delivery_batch_id=? ORDER BY id", (delivery["id"],))
                    risks = delivery_risks(conn, delivery, forecast)
                    risk_by_basis = {risk["basis"]: risk["message"] for risk in risks}
                    delivery_common = {**common, "delivery_batch_no": delivery["batch_no"]}
                    delivery_rows.append({**delivery_common, "delivery_quantity": delivery["quantity"],
                                          "completion_basis": "客户签收" if delivery["completion_basis"] == "receipt" else "发货",
                                          "due_date": delivery["due_date"], "promised_date": delivery["promised_date"],
                                          "manual_estimate_date": delivery["manual_estimate_date"],
                                          "manual_estimate_reason": delivery["manual_estimate_reason"],
                                          "forecast_status": {"unknown": "无法确定", "forecast": "已预测", "complete": "已完成"}.get(forecast["status"], forecast["status"]),
                                          "formal_date": forecast["formal_date"] if forecast["status"] != "unknown" else None,
                                          "provisional_date": forecast["provisional_date"],
                                          "actual_delivery_date": forecast["formal_date"] if forecast["status"] == "complete" else None,
                                          "completed_quantity": forecast["completed_quantity"],
                                          "forecast_reason": forecast["reason"],
                                          "due_risk": risk_by_basis.get("due_date"),
                                          "promise_risk": risk_by_basis.get("promised_date")})
                    for allocation in allocations:
                        production = must_get(conn, "production_batches", allocation["production_batch_id"])
                        allocation_rows.append({**delivery_common,
                                                "production_batch_no": production_batch_label(conn, production),
                                                "production_phase": PHASE_LABELS.get(production["phase"], production["phase"]),
                                                "production_quantity": production["quantity"],
                                                "allocation_quantity": allocation["quantity"]})
    if not order_rows:
        raise HTTPException(404, "没有符合条件的客户订单可导出")
    identity = [("project_name", "项目名称", "text", 25), ("order_no", "客户订单号", "text", 23),
                ("customer", "客户", "text", 20), ("line_no", "产品行号", "text", 13),
                ("product_code", "产品编码", "text", 19), ("product_name", "产品名称", "text", 24)]
    order_cols = identity + [("owner", "业务负责人", "text", 17), ("order_status", "订单状态", "text", 14),
                             ("order_quantity", "产品行数量", "number", 16),
                             ("delivered_quantity", "已交付数量", "number", 16),
                             ("delivery_count", "交付批次数", "number", 14)]
    delivery_cols = identity + [("delivery_batch_no", "交付批次", "text", 18),
                                ("delivery_quantity", "本批交付数量", "number", 18),
                                ("completion_basis", "完成口径", "text", 15),
                                ("due_date", "客户要求日", "date", 16),
                                ("promised_date", "业务承诺日", "date", 16),
                                ("manual_estimate_date", "人工预计日（临时）", "date", 20),
                                ("manual_estimate_reason", "人工预计依据", "text", 32),
                                ("forecast_status", "正式预测状态", "text", 17),
                                ("formal_date", "正式预测交付日", "date", 20),
                                ("provisional_date", "临时估算交付日", "date", 20),
                                ("actual_delivery_date", "实际完成交付日", "date", 20),
                                ("completed_quantity", "本批已交付数量", "number", 20),
                                ("forecast_reason", "待确认原因", "text", 44),
                                ("due_risk", "客户要求日风险", "text", 26),
                                ("promise_risk", "业务承诺日风险", "text", 26)]
    allocation_cols = identity + [("delivery_batch_no", "交付批次", "text", 18),
                                  ("production_batch_no", "关联生产批次", "text", 19),
                                  ("production_phase", "生产阶段", "text", 16),
                                  ("production_quantity", "生产批次数量", "number", 18),
                                  ("allocation_quantity", "分配至本交付批次数量", "number", 25)]
    return export_xlsx([("客户订单", order_cols, order_rows), ("交付批次", delivery_cols, delivery_rows),
                        ("生产交付关联", allocation_cols, allocation_rows)],
                       export_notes(f"客户订单；搜索词 {q.strip() or '全部'}；内部研发项目不包含在内", len(order_rows),
                                    "客户要求日与业务承诺日分别保留；正式预测无法确定时日期留空并写明原因。实际完成交付日仅在本批按所选口径足额完成后填写。"),
                       f"PCBA客户订单_{today()}.xlsx")


def insert_line(conn: sqlite3.Connection, order_id: int, data: dict, actor_id: int) -> dict:
    quantity = ds(dec(need(data, "quantity"), allow_zero=False))
    cur = conn.execute("INSERT INTO order_lines(order_id,line_no,product_code,product_name,quantity,bom_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (order_id, str(need(data, "line_no")), str(need(data, "product_code")),
                        str(need(data, "product_name")), quantity, data.get("bom_version"), utcnow(), utcnow()))
    result = must_get(conn, "order_lines", cur.lastrowid)
    audit(conn, "order_lines", result["id"], "create", actor_id, None, result)
    return result


def active_owner_id(conn: sqlite3.Connection, value: Any) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        owner_id = int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "负责人账号无效")
    owner = must_get(conn, "users", owner_id)
    if not owner["active"] or owner.get("status", "active") != "active":
        raise HTTPException(422, "不能指定已停用的负责人账号")
    return owner_id


@app.post("/api/orders")
def create_order(data: dict, user: dict = Depends(require_user)):
    lines = data.get("lines", [])
    initial_delivery = data.get("initial_delivery")
    project_mode = data.get("project_mode", "customer")
    if project_mode not in {"customer", "internal"}:
        raise HTTPException(422, "项目类型应为客户订单或内部研发")
    if initial_delivery is not None and (not isinstance(initial_delivery, dict) or len(lines) != 1):
        raise HTTPException(422, "初始交付批次只适用于单产品行订单")
    with writing() as conn:
        owner_id = active_owner_id(conn, data["owner_id"]) if data.get("owner_id") is not None else None
        order_no = str(data.get("order_no") or "").strip()
        customer = str(data.get("customer") or "").strip()
        if project_mode == "customer":
            order_no = str(need(data, "order_no")).strip()
            customer = str(need(data, "customer")).strip()
        else:
            order_no = order_no or f"PCBA-PROJ-{secrets.token_hex(6).upper()}"
            customer = customer or "内部研发"
        project_name = str(data.get("project_name") or (lines[0].get("product_name") if lines else "") or "").strip() or None
        cur = conn.execute("INSERT INTO orders(order_no,customer,project_name,project_mode,owner_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                           (order_no, customer, project_name, project_mode, owner_id, utcnow(), utcnow()))
        created_lines = [insert_line(conn, cur.lastrowid, line, user["id"]) for line in lines]
        if initial_delivery is not None:
            batch_data = {**initial_delivery, "order_line_id": created_lines[0]["id"]}
            batch_data.setdefault("batch_no", "D1")
            batch_data.setdefault("quantity", created_lines[0]["quantity"])
            delivery = insert_delivery_batch(conn, batch_data, user["id"])
            actual_date = valid_date(initial_delivery.get("actual_delivery_date"))
            if actual_date:
                if actual_date > today():
                    raise HTTPException(422, "实际交付日期不能晚于今天")
                if dec(delivery["quantity"]) != dec(created_lines[0]["quantity"]):
                    raise HTTPException(422, "新建订单时录入实际交付日，交付批次须覆盖订单行全量")
                record_delivery_event(conn, delivery["id"], {
                    "kind": "shipped" if delivery["completion_basis"] == "shipment" else "customer_received",
                    "event_date": actual_date, "quantity": delivery["quantity"],
                    "note": "创建订单时录入的实际交付日期",
                }, user["id"])
        result = order_detail(conn, cur.lastrowid)
        audit(conn, "orders", cur.lastrowid, "create", user["id"], None, result)
        return result


@app.post("/api/orders/{order_id}/lines")
def create_order_line(order_id: int, data: dict, user: dict = Depends(require_user)):
    with writing() as conn:
        must_get(conn, "orders", order_id)
        result = insert_line(conn, order_id, data, user["id"])
        legacy = one(conn, "SELECT * FROM legacy_records WHERE order_id=? AND order_line_id IS NULL", (order_id,))
        if legacy:
            conn.execute("UPDATE legacy_records SET order_line_id=?,version=version+1 WHERE id=?", (result["id"], legacy["id"]))
            audit(conn, "legacy_records", legacy["id"], "link_line", user["id"],
                  {"order_line_id": None}, {"order_line_id": result["id"]})
        sync_order_status(conn, order_id)
        return result


@app.patch("/api/orders/{order_id}")
def patch_order(order_id: int, data: dict, user: dict = Depends(require_user)):
    changes = normalize_changes(data.get("changes", {}),
                                {"customer", "owner_id", "order_no", "project_name", "project_mode"},
                                protected={"project_mode"}, reason=data.get("reason"))
    with writing() as conn:
        old = must_get(conn, "orders", order_id)
        if "project_mode" in changes and changes["project_mode"] not in {"customer", "internal"}:
            raise HTTPException(422, "项目类型应为客户订单或内部研发")
        if "project_name" in changes:
            changes["project_name"] = str(changes["project_name"] or "").strip() or None
        if "order_no" in changes and not str(changes["order_no"] or "").strip():
            raise HTTPException(422, "项目编号不能为空")
        if "owner_id" in changes and changes["owner_id"] is not None:
            changes["owner_id"] = active_owner_id(conn, changes["owner_id"])
        if changes.get("project_mode", old["project_mode"]) == "customer":
            effective_no = str(changes.get("order_no", old["order_no"]) or "").strip()
            effective_customer = str(changes.get("customer", old["customer"]) or "").strip()
            if effective_no.startswith("PCBA-PROJ-") or not effective_customer or effective_customer == "内部研发":
                raise HTTPException(422, "客户订单需填写正式客户订单号和客户名称")
        return patch_record(conn, "orders", order_id, int(need(data, "version")), changes, user["id"], data.get("reason"))


@app.patch("/api/order-lines/{line_id}")
def patch_order_line(line_id: int, data: dict, user: dict = Depends(require_user)):
    changes = normalize_changes(data.get("changes", {}), {"product_code", "product_name", "quantity", "bom_version"},
                                protected={"quantity", "bom_version"}, reason=data.get("reason"))
    with writing() as conn:
        line = must_get(conn, "order_lines", line_id)
        if "quantity" in changes:
            cap = dec(changes["quantity"])
            for table in ("production_batches", "delivery_batches"):
                total = sum((dec(r["quantity"]) for r in rows(conn, f"SELECT quantity FROM {table} WHERE order_line_id=?", (line_id,))), Decimal(0))
                if total > cap:
                    raise HTTPException(409, "订单行数量不能小于已有批次数量")
        if "product_code" in changes:
            if not str(changes["product_code"] or "").strip():
                raise HTTPException(422, "产品编码不能为空")
            batches = rows(conn, "SELECT * FROM production_batches WHERE order_line_id=?", (line_id,))
            if batches:
                legacy = one(conn, "SELECT id FROM legacy_records WHERE order_line_id=?", (line_id,))
                if not legacy or not line["product_code"].startswith("PCBA-TEMP-"):
                    raise HTTPException(409, "已有生产批次，不能修改产品编码")
                if any(batch["bom_locked"] or one(conn, "SELECT id FROM events WHERE entity_type='production_batch' AND entity_id=? LIMIT 1", (batch["id"],))
                       or one(conn, "SELECT id FROM stages WHERE production_batch_id=? AND (actual_start IS NOT NULL OR actual_finish IS NOT NULL) LIMIT 1", (batch["id"],))
                       for batch in batches):
                    raise HTTPException(409, "该生产批次已有 BOM 或实际进度，不能更正临时产品编码")
        if "bom_version" in changes:
            if one(conn, "SELECT id FROM order_line_boards WHERE order_line_id=? LIMIT 1", (line_id,)):
                raise HTTPException(409, "多板项目请分别设置各板 BOM 版本")
            locked_versions = {row["bom_version"] for row in rows(conn, "SELECT DISTINCT bom_version FROM production_batches WHERE order_line_id=? AND bom_locked=1", (line_id,))}
            if locked_versions and locked_versions != {changes["bom_version"]}:
                raise HTTPException(409, "订单行 BOM 版本必须与已锁定的生产批次一致")
        result = patch_record(conn, "order_lines", line_id, int(need(data, "version")), changes, user["id"], data.get("reason"))
        sync_order_status(conn, line["order_id"])
        return result


def checked_board_fields(data: dict) -> dict:
    board_ref = str(need(data, "board_ref")).strip()
    board_name = str(need(data, "board_name")).strip()
    code = str(need(data, "bom_product_code")).strip()
    ratio = dec(need(data, "quantity_per_product"), allow_zero=False)
    version = str(data.get("default_bom_version") or "").strip() or None
    if not board_ref or len(board_ref) > 60 or not board_name or len(board_name) > 100 or not code or len(code) > 100:
        raise HTTPException(422, "板位、板名或 BOM 母件编码无效")
    if ratio > 10000 or len(ratio.as_tuple().digits) > 12:
        raise HTTPException(422, "每套用板数过大")
    if ratio != ratio.to_integral_value():
        raise HTTPException(422, "每套用板数须为正整数；拼板和半片板请先按实际 PCBA 片数拆分")
    if version and len(version) > 100:
        raise HTTPException(422, "默认 BOM 版本不能超过 100 字")
    return {"board_ref": board_ref, "board_name": board_name,
            "bom_product_code": code, "quantity_per_product": ds(ratio),
            "default_bom_version": version}


def board_definitions(conn: sqlite3.Connection, line_id: int) -> list[dict]:
    return rows(conn, """SELECT b.*, (SELECT COUNT(*) FROM bom_catalog c WHERE c.product_code=b.bom_product_code
                          AND c.bom_version=b.default_bom_version) AS catalog_item_count
                          FROM order_line_boards b WHERE b.order_line_id=? AND b.active=1 ORDER BY b.id""", (line_id,))


def batch_board_views(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    batch = must_get(conn, "production_batches", batch_id)
    materials = batch_materials(conn, batch_id) if batch["board_schedule_mode"] == "independent" else None
    result = rows(conn, """SELECT b.*, (SELECT COUNT(*) FROM bom_items i WHERE i.batch_board_id=b.id) AS bom_item_count,
                     (SELECT COUNT(*) FROM bom_items i WHERE i.batch_board_id=b.id AND i.unit_price IS NULL) AS missing_price_count
                     FROM batch_boards b WHERE b.production_batch_id=? ORDER BY b.id""", (batch_id,))
    for board in result:
        board["required_quantity"] = ds(dec(batch["quantity"]) * dec(board["quantity_per_product"]))
        board["completed_quantity"] = ds(board_finished_quantity(conn, board["id"]))
        if batch["board_schedule_mode"] == "independent":
            stages = board_stage_views(conn, board["id"])
            board["stage_progress"] = {"total": len(stages),
                                       "completed": sum(stage["is_complete"] for stage in stages)}
            board["forecast"] = board_finish_forecast(conn, board["id"], materials)
            board["conflicts"] = board_vendor_conflicts(conn, board)
            board["conflict_acknowledged"] = board_conflicts_acknowledged(board, board["conflicts"])
    return result


def batch_bom_gap(conn: sqlite3.Connection, batch: dict, line: dict | None = None) -> str:
    if batch["bom_scope"] == "multi":
        boards = batch_board_views(conn, batch["id"])
        if not batch["board_set_confirmed_at"]:
            return "本批板卡组成尚未确认，请按当前项目组成重新启用"
        if not boards:
            return "本批尚无板卡快照"
        pending = [board["board_name"] for board in boards if not board["bom_locked"]]
        return f"{len(boards) - len(pending)}/{len(boards)} 块板已锁定；待锁：{'、'.join(pending)}" if pending else "板卡已锁定"
    line = line or must_get(conn, "order_lines", batch["order_line_id"])
    if board_definitions(conn, line["id"]):
        return "旧单 BOM 批次尚未启用项目多板组成"
    return "尚未锁定 BOM"


def board_stage_views(conn: sqlite3.Connection, board_id: int) -> list[dict]:
    return [stage_view(conn, stage) for stage in rows(
        conn, "SELECT * FROM board_stages WHERE batch_board_id=? ORDER BY sequence,id", (board_id,))]


def replan_board_stages(conn: sqlite3.Connection, board_id: int, actor_id: int | None = None,
                        reason: str | None = None) -> list[dict]:
    board = must_get(conn, "batch_boards", board_id)
    batch = must_get(conn, "production_batches", board["production_batch_id"])
    stages = rows(conn, "SELECT * FROM board_stages WHERE batch_board_id=? ORDER BY sequence,id", (board_id,))
    try:
        plans = plan_nodes(board["commit_start"], stages, batch["schedule_mode"], lambda day: workday(conn, day))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    changed = []
    for stage, plan in zip(stages, plans):
        if stage["planned_start"] == plan["planned_start"] and stage["planned_finish"] == plan["planned_finish"]:
            continue
        conn.execute("""UPDATE board_stages SET planned_start=?,planned_finish=?,version=version+1,updated_at=?
                     WHERE id=?""", (plan["planned_start"], plan["planned_finish"], utcnow(), stage["id"]))
        changed.append({"stage_id": stage["id"], "planned_start": plan["planned_start"],
                        "planned_finish": plan["planned_finish"]})
    if changed:
        audit(conn, "batch_boards", board_id, "replan_board_stages", actor_id, None, changed, reason)
    return board_stage_views(conn, board_id)


def generate_board_stages(conn: sqlite3.Connection, board_id: int) -> list[dict]:
    board = must_get(conn, "batch_boards", board_id)
    batch = must_get(conn, "production_batches", board["production_batch_id"])
    if batch["phase"] == "unclassified":
        return []
    steps = phase_template_for(conn, board["bom_product_code"], batch["phase"])["steps"]
    for sequence, step in enumerate(steps, 1):
        conn.execute("""INSERT INTO board_stages(batch_board_id,sequence,name,stage_type,duration_days,
                     created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
                     (board_id, sequence, step["name"], step.get("stage_type", "normal"),
                      checked_stage_duration(step.get("duration_days", "0")), utcnow(), utcnow()))
    return replan_board_stages(conn, board_id)


def clone_batch_boards(conn: sqlite3.Connection, batch_id: int, line_id: int) -> None:
    batch = must_get(conn, "production_batches", batch_id)
    for board in rows(conn, "SELECT * FROM order_line_boards WHERE order_line_id=? AND active=1 ORDER BY id", (line_id,)):
        board_id = conn.execute("""INSERT INTO batch_boards(production_batch_id,order_line_board_id,board_ref,board_name,
                     bom_product_code,quantity_per_product,planned_quantity,bom_version,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""", (batch_id, board["id"], board["board_ref"], board["board_name"],
                     board["bom_product_code"], board["quantity_per_product"],
                     ds(dec(batch["quantity"]) * dec(board["quantity_per_product"])), board["default_bom_version"],
                     utcnow(), utcnow())).lastrowid
        if batch["board_schedule_mode"] == "independent" and batch["phase"] != "unclassified":
            generate_board_stages(conn, board_id)


def finish_board_definition_change(conn: sqlite3.Connection, line: dict, actor_id: int,
                                   reason: str) -> None:
    # Batch board plans are snapshots. Editing the project composition affects
    # future batches only; an existing unlocked batch must be rebased explicitly.
    patch_record(conn, "order_lines", line["id"], line["version"],
                 {"board_set_confirmed_at": None, "board_set_confirmed_by": None,
                  "board_set_revision": line["board_set_revision"] + 1}, actor_id, reason)


@app.get("/api/order-lines/{line_id}/boards")
def get_order_line_boards(line_id: int, user: dict = Depends(require_user)):
    with reading() as conn:
        line = must_get(conn, "order_lines", line_id)
        return {"order_line": line, "confirmed": bool(line["board_set_confirmed_at"]),
                "boards": board_definitions(conn, line_id)}


@app.post("/api/order-lines/{line_id}/boards")
def add_order_line_board(line_id: int, data: dict, user: dict = Depends(require_user)):
    fields = checked_board_fields(data)
    reason = str(need(data, "reason")).strip()
    if not reason:
        raise HTTPException(422, "请填写板卡配置原因")
    with writing() as conn:
        line = must_get(conn, "order_lines", line_id)
        if line["version"] != int(need(data, "version")):
            raise HTTPException(409, "订单产品行已被修改，请刷新后重试")
        cur = conn.execute("""INSERT INTO order_line_boards(order_line_id,board_ref,board_name,bom_product_code,
                           quantity_per_product,default_bom_version,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?)""", (line_id, fields["board_ref"], fields["board_name"],
                           fields["bom_product_code"], fields["quantity_per_product"], fields["default_bom_version"],
                           utcnow(), utcnow()))
        board = must_get(conn, "order_line_boards", cur.lastrowid)
        audit(conn, "order_line_boards", board["id"], "create", user["id"], None, board, reason)
        finish_board_definition_change(conn, line, user["id"], reason)
        return get_order_line_boards_from_conn(conn, line_id)


def get_order_line_boards_from_conn(conn: sqlite3.Connection, line_id: int) -> dict:
    line = must_get(conn, "order_lines", line_id)
    return {"order_line": line, "confirmed": bool(line["board_set_confirmed_at"]),
            "boards": board_definitions(conn, line_id)}


@app.get("/api/board-overview")
def board_overview(user: dict = Depends(require_user)):
    with reading() as conn:
        line_summaries = {}
        for line in rows(conn, "SELECT * FROM order_lines ORDER BY id"):
            boards = board_definitions(conn, line["id"])
            line_summaries[str(line["id"])] = {
                "order_line_id": line["id"], "confirmed": bool(line["board_set_confirmed_at"]),
                "board_schedule_mode": line["board_schedule_mode"],
                "board_set_revision": line["board_set_revision"], "board_count": len(boards), "boards": boards,
            }
        batch_summaries = {}
        for batch in rows(conn, "SELECT * FROM production_batches ORDER BY id"):
            boards = batch_board_views(conn, batch["id"]) if batch["bom_scope"] == "multi" else []
            batch_summaries[str(batch["id"])] = {
                "production_batch_id": batch["id"], "order_line_id": batch["order_line_id"],
                "bom_scope": batch["bom_scope"], "bom_locked": bool(batch["bom_locked"]),
                "board_schedule_mode": batch["board_schedule_mode"],
                "board_set_revision": batch["board_set_revision"],
                "board_set_confirmed": bool(batch["board_set_confirmed_at"]),
                "board_count": len(boards),
                "locked_board_count": sum(bool(board["bom_locked"]) for board in boards),
                "boards": boards,
            }
        return {"order_lines": line_summaries, "production_batches": batch_summaries}


@app.patch("/api/order-line-boards/{board_id}")
def patch_order_line_board(board_id: int, data: dict, user: dict = Depends(require_user)):
    fields = checked_board_fields(data)
    reason = str(need(data, "reason")).strip()
    if not reason:
        raise HTTPException(422, "请填写板卡修改原因")
    with writing() as conn:
        board = must_get(conn, "order_line_boards", board_id)
        if board["version"] != int(need(data, "version")):
            raise HTTPException(409, "板卡信息已被修改，请刷新后重试")
        line = must_get(conn, "order_lines", board["order_line_id"])
        if not board["active"]:
            raise HTTPException(409, "板位已停用，不能修改")
        patch_record(conn, "order_line_boards", board_id, board["version"], fields, user["id"], reason)
        finish_board_definition_change(conn, line, user["id"], reason)
        return get_order_line_boards_from_conn(conn, line["id"])


@app.delete("/api/order-line-boards/{board_id}")
def delete_order_line_board(board_id: int, data: dict | None = None, user: dict = Depends(require_user)):
    payload = data or {}
    reason = str(need(payload, "reason")).strip()
    if not reason:
        raise HTTPException(422, "请填写删除板卡原因")
    with writing() as conn:
        board = must_get(conn, "order_line_boards", board_id)
        if board["version"] != int(need(payload, "version")):
            raise HTTPException(409, "板卡信息已被修改，请刷新后重试")
        line = must_get(conn, "order_lines", board["order_line_id"])
        if not board["active"]:
            raise HTTPException(409, "板位已停用")
        patch_record(conn, "order_line_boards", board_id, board["version"], {"active": 0}, user["id"], reason)
        finish_board_definition_change(conn, line, user["id"], reason)
        return get_order_line_boards_from_conn(conn, line["id"])


@app.post("/api/order-lines/{line_id}/boards/confirm")
def confirm_order_line_boards(line_id: int, data: dict, user: dict = Depends(require_user)):
    reason = str(need(data, "reason")).strip()
    if not reason:
        raise HTTPException(422, "请填写板卡组成确认依据")
    with writing() as conn:
        line = must_get(conn, "order_lines", line_id)
        if not board_definitions(conn, line_id):
            raise HTTPException(409, "请先添加项目所需板卡")
        patch_record(conn, "order_lines", line_id, int(need(data, "version")),
                     {"board_set_confirmed_at": utcnow(), "board_set_confirmed_by": user["id"]},
                     user["id"], reason)
        return get_order_line_boards_from_conn(conn, line_id)


@app.patch("/api/order-lines/{line_id}/board-schedule-mode")
def patch_board_schedule_mode(line_id: int, data: dict, user: dict = Depends(require_user)):
    mode = str(need(data, "mode"))
    reason = str(need(data, "reason")).strip()
    if mode not in {"shared", "independent"} or not reason:
        raise HTTPException(422, "板卡排期方式须为共用或逐板，且需填写原因")
    with writing() as conn:
        line = patch_record(conn, "order_lines", line_id, int(need(data, "version")),
                            {"board_schedule_mode": mode}, user["id"], reason)
        return {"order_line": line, "mode": line["board_schedule_mode"],
                "existing_batches_unchanged": True}


def bom_linkage_summary(conn: sqlite3.Connection, line: dict) -> dict:
    board_count = len(board_definitions(conn, line["id"]))
    linked_code = line.get("bom_product_code") or line["product_code"]
    catalogs = rows(conn, """SELECT product_code,bom_version,COUNT(*) AS item_count,
                          SUM(CASE WHEN unit_price IS NULL THEN 1 ELSE 0 END) AS missing_price_count
                          FROM bom_catalog WHERE product_code=? GROUP BY product_code,bom_version
                          ORDER BY bom_version""", (linked_code,))
    batches = rows(conn, "SELECT id,batch_no,phase,bom_version,bom_locked FROM production_batches WHERE order_line_id=? ORDER BY id", (line["id"],))
    locked_count = sum(bool(batch["bom_locked"]) for batch in batches)
    locked_version_mismatch_count = sum(bool(batch["bom_locked"]) and batch["bom_version"] != line["bom_version"]
                                        for batch in batches)
    selected = any(item["bom_version"] == line["bom_version"] for item in catalogs)
    if board_count:
        status, reason = "multi_board", f"已配置 {board_count} 块板卡；按板卡分别锁定 BOM"
    elif batches and locked_count == len(batches):
        status, reason = "locked", "全部生产批次已锁定 BOM 快照"
    elif locked_count:
        status, reason = "partially_locked", "仍有生产批次尚未锁定 BOM"
    elif selected:
        status, reason = "catalog_ready", "已找到对应目录，待生产批次锁定"
    elif not line["bom_version"]:
        status, reason = "unlinked", "请选择导入的 BOM 版本"
    else:
        status, reason = "unlinked", "BOM 产品编码和版本未匹配到导入目录"
    order = must_get(conn, "orders", line["order_id"])
    return {"order_line_id": line["id"], "order_id": line["order_id"],
            "order_no": order["order_no"], "project_name": order["project_name"] or line["product_name"],
            "product_code": line["product_code"], "product_name": line["product_name"],
            "linked_product_code": linked_code, "bom_product_code": line.get("bom_product_code"),
            "bom_version": line["bom_version"], "status": status, "reason": reason,
            "catalog_match": selected, "linked_explicitly": bool(line.get("bom_product_code")),
            "candidate_count": len(catalogs), "locked_batch_count": locked_count,
            "locked_version_mismatch_count": locked_version_mismatch_count,
            "production_batch_count": len(batches), "board_count": board_count,
            "board_set_confirmed": bool(line["board_set_confirmed_at"])}


@app.get("/api/bom-linkages")
def list_bom_linkages(user: dict = Depends(require_user)):
    with reading() as conn:
        return [bom_linkage_summary(conn, line) for line in rows(conn, "SELECT * FROM order_lines ORDER BY id")]


def bom_linkage_detail(conn: sqlite3.Connection, line_id: int) -> dict:
    line = must_get(conn, "order_lines", line_id)
    summary = bom_linkage_summary(conn, line)
    candidates = rows(conn, """SELECT product_code,bom_version,COUNT(*) AS item_count,
                           SUM(CASE WHEN unit_price IS NULL THEN 1 ELSE 0 END) AS missing_price_count
                           FROM bom_catalog GROUP BY product_code,bom_version
                           ORDER BY CASE WHEN product_code=? THEN 0 ELSE 1 END,product_code,bom_version""",
                      (summary["linked_product_code"],))
    for candidate in candidates:
        candidate["match"] = ("exact" if candidate["product_code"] == summary["linked_product_code"]
                              and candidate["bom_version"] == line["bom_version"] else
                              "product_code" if candidate["product_code"] == summary["linked_product_code"] else "other")
    batches = rows(conn, """SELECT p.id,p.batch_no,p.phase,p.bom_version,p.bom_locked,
                       (SELECT COUNT(*) FROM bom_items i WHERE i.production_batch_id=p.id) AS bom_item_count
                       FROM production_batches p WHERE p.order_line_id=? ORDER BY p.id""", (line_id,))
    return {**summary, "order_line": line, "catalogs": candidates, "batches": batches}


@app.get("/api/order-lines/{line_id}/bom-linkage")
def get_bom_linkage(line_id: int, user: dict = Depends(require_user)):
    with reading() as conn:
        return bom_linkage_detail(conn, line_id)


@app.post("/api/order-lines/{line_id}/bom-linkage")
def set_bom_linkage(line_id: int, data: dict, user: dict = Depends(require_user)):
    code = str(need(data, "bom_product_code")).strip()
    version = str(need(data, "bom_version")).strip()
    reason = str(need(data, "reason")).strip()
    if not code or not version or not reason:
        raise HTTPException(422, "BOM 产品编码、版本和关联原因不能为空")
    try:
        expected_version = int(need(data, "version"))
    except (TypeError, ValueError):
        raise HTTPException(422, "订单产品行版本无效")
    with writing() as conn:
        line = must_get(conn, "order_lines", line_id)
        if board_definitions(conn, line_id):
            raise HTTPException(409, "多板项目请按板卡分别关联 BOM")
        if line["version"] != expected_version:
            raise HTTPException(409, "订单产品行已被修改，请刷新后重试")
        catalog = one(conn, "SELECT id FROM bom_catalog WHERE product_code=? AND bom_version=? LIMIT 1", (code, version))
        if not catalog:
            raise HTTPException(422, "所选 BOM 产品编码和版本在导入目录中没有物料")
        old_code = line.get("bom_product_code") or line["product_code"]
        changed = old_code != code or line["bom_version"] != version
        locked = rows(conn, "SELECT id,bom_version FROM production_batches WHERE order_line_id=? AND bom_locked=1", (line_id,))
        first_legacy_mapping = (changed and locked and not line.get("bom_product_code")
                                and line["product_code"].startswith("PCBA-TEMP-")
                                and code != line["product_code"])
        if changed and locked and not first_legacy_mapping:
            raise HTTPException(409, "已有批次锁定 BOM，不能更改映射；改版请建立新的订单产品行及生产批次")
        if changed:
            updated = patch_record(conn, "order_lines", line_id, line["version"],
                                   {"bom_product_code": code, "bom_version": version}, user["id"], reason)
            if first_legacy_mapping:
                audit(conn, "order_lines", line_id, "bom_link_legacy", user["id"],
                      {"bom_product_code": line.get("bom_product_code"), "bom_version": line["bom_version"]},
                      {"bom_product_code": code, "bom_version": version,
                       "existing_locked_batches_unchanged": [batch["id"] for batch in locked],
                       "line_version": updated["version"]}, reason)
        return bom_linkage_detail(conn, line_id)


def board_vendor_conflicts(conn: sqlite3.Connection, board: dict) -> list[dict]:
    if not board.get("vendor") or not board.get("commit_start") or not board.get("commit_finish"):
        return []
    others = rows(conn, """SELECT b.id AS board_id,b.board_name,p.id AS batch_id,p.batch_no,
                         b.commit_start,b.commit_finish,b.updated_at,b.version FROM batch_boards b
                         JOIN production_batches p ON p.id=b.production_batch_id
                         WHERE p.board_schedule_mode='independent' AND b.id<>? AND b.vendor=?
                         AND b.commit_start<=? AND b.commit_finish>=?""",
                  (board["id"], board["vendor"], board["commit_finish"], board["commit_start"]))
    shared = rows(conn, """SELECT id AS batch_id,batch_no,commit_start,commit_finish,updated_at,version FROM production_batches
                         WHERE board_schedule_mode='shared' AND vendor=? AND commit_start<=? AND commit_finish>=?""",
                  (board["vendor"], board["commit_finish"], board["commit_start"]))
    return [{"kind": "board", **item} for item in others] + [{"kind": "batch", **item} for item in shared]


def vendor_conflict_signature(conflicts: list[dict]) -> str:
    evidence = [{key: item.get(key) for key in ("kind", "board_id", "batch_id", "version",
                                                "commit_start", "commit_finish")}
                for item in conflicts]
    evidence.sort(key=lambda item: (str(item["kind"]), str(item["batch_id"]), str(item["board_id"])))
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def board_conflicts_acknowledged(board: dict, conflicts: list[dict]) -> bool:
    if not conflicts or not board["conflict_ack_at"]:
        return False
    if board.get("conflict_ack_signature"):
        return board["conflict_ack_signature"] == vendor_conflict_signature(conflicts)
    return all(not item.get("updated_at") or item["updated_at"] <= board["conflict_ack_at"]
               for item in conflicts)


def batch_conflicts_acknowledged(batch: dict, conflicts: list[dict]) -> bool:
    if not conflicts or not batch["conflict_ack_at"]:
        return False
    if batch.get("conflict_ack_signature"):
        return batch["conflict_ack_signature"] == vendor_conflict_signature(conflicts)
    return all(not item.get("updated_at") or item["updated_at"] <= batch["conflict_ack_at"]
               for item in conflicts)


def vendor_conflicts(conn: sqlite3.Connection, batch: dict) -> list[dict]:
    if batch.get("board_schedule_mode") == "independent":
        return [{"source_board_id": board["id"], "source_board_name": board["board_name"],
                 "source_acknowledged": board_conflicts_acknowledged(board, conflicts), **conflict}
                for board in rows(conn, "SELECT * FROM batch_boards WHERE production_batch_id=?", (batch["id"],))
                for conflicts in [board_vendor_conflicts(conn, board)] for conflict in conflicts]
    if not batch.get("vendor") or not batch.get("commit_start") or not batch.get("commit_finish"):
        return []
    shared = rows(conn, """SELECT id,batch_no,commit_start,commit_finish,updated_at,version FROM production_batches
                         WHERE board_schedule_mode='shared' AND vendor=? AND id<>?
                         AND commit_start<=? AND commit_finish>=?""",
                  (batch["vendor"], batch["id"], batch["commit_finish"], batch["commit_start"]))
    boards = rows(conn, """SELECT b.id AS board_id,b.board_name,p.id AS batch_id,p.batch_no,
                        b.commit_start,b.commit_finish,b.updated_at,b.version FROM batch_boards b
                        JOIN production_batches p ON p.id=b.production_batch_id
                        WHERE p.board_schedule_mode='independent' AND b.vendor=?
                        AND b.commit_start<=? AND b.commit_finish>=?""",
                  (batch["vendor"], batch["commit_finish"], batch["commit_start"]))
    return [{"kind": "batch", **item} for item in shared] + [{"kind": "board", **item} for item in boards]


def insert_bom(conn: sqlite3.Connection, batch_id: int, version: str, items: list, actor_id: int) -> None:
    if not items:
        raise HTTPException(422, "锁定 BOM 必须包含全部物料行")
    batch = must_get(conn, "production_batches", batch_id)
    if batch["bom_scope"] == "multi":
        raise HTTPException(409, "多板批次请逐板锁定 BOM")
    line = must_get(conn, "order_lines", batch["order_line_id"])
    if board_definitions(conn, line["id"]):
        raise HTTPException(409, "项目已配置多块板，请先为该批次启用多板组成")
    other_versions = {row["bom_version"] for row in rows(
        conn, "SELECT DISTINCT bom_version FROM production_batches WHERE order_line_id=? AND phase=? AND bom_locked=1 AND id<>?",
        (line["id"], batch["phase"], batch_id))}
    if other_versions and other_versions != {version}:
        raise HTTPException(409, "同一阶段已有不同 BOM 版本的锁定批次")
    seen = set()
    for index, item in enumerate(items, 1):
        code = str(need(item, "material_code"))
        source_line_no = str(item.get("source_line_no") or index)
        if source_line_no in seen:
            raise HTTPException(422, "BOM 行号重复")
        seen.add(source_line_no)
        loss_rate = dec(item.get("loss_rate", 0))
        if loss_rate > 1:
            raise HTTPException(422, "损耗率按小数填写，例如 0.05 表示 5%")
        conn.execute("INSERT INTO bom_items(production_batch_id,source_line_no,material_code,material_name,quantity_per,loss_rate,unit_price) VALUES(?,?,?,?,?,?,?)",
                     (batch_id, source_line_no, code, item.get("material_name"), ds(dec(need(item, "quantity_per"), allow_zero=False)),
                      ds(loss_rate), unit_price(item.get("unit_price"))))
    if not line["bom_version"]:
        patch_record(conn, "order_lines", line["id"], line["version"], {"bom_version": version}, actor_id,
                     "首个生产批次锁定 BOM 版本")
    conn.execute("UPDATE production_batches SET bom_version=?,bom_locked=1,version=version+1,updated_at=? WHERE id=?",
                 (version, utcnow(), batch_id))
    audit(conn, "production_batches", batch_id, "bom_lock", actor_id, None,
          {"bom_version": version, "items": rows(conn, "SELECT * FROM bom_items WHERE production_batch_id=?", (batch_id,))})


def validate_batch_quantity(conn: sqlite3.Connection, line_id: int, quantity: Decimal,
                            table: str, exclude_id: int = 0, phase: str | None = None) -> None:
    line = must_get(conn, "order_lines", line_id)
    if table == "production_batches" and phase:
        existing = rows(conn, "SELECT quantity FROM production_batches WHERE order_line_id=? AND id<>? AND phase=?",
                        (line_id, exclude_id, phase))
    else:
        existing = rows(conn, f"SELECT quantity FROM {table} WHERE order_line_id=? AND id<>?", (line_id, exclude_id))
    total = sum((dec(row["quantity"]) for row in existing), Decimal(0))
    if total + quantity > dec(line["quantity"]):
        raise HTTPException(409, "同产品行的批次数量超过订单行数量")


@app.post("/api/production-batches")
def create_production_batch(data: dict, user: dict = Depends(require_user)):
    line_id = int(need(data, "order_line_id"))
    quantity = dec(need(data, "quantity"), allow_zero=False)
    phase = checked_phase(data.get("phase", "mp"), allow_unclassified=False)
    phase_source = data.get("phase_source", "manual" if data.get("phase") else "default")
    if phase_source not in {"manual", "quantity_rule", "default"}:
        raise HTTPException(422, "阶段来源无效")
    if phase_source == "quantity_rule" and phase != suggest_phase(quantity):
        raise HTTPException(422, "按数量建议的阶段与当前数量不符")
    mode = checked_schedule_mode(data.get("schedule_mode"))
    schedule_start = valid_date(data.get("schedule_start"))
    target_kind = data.get("target_kind")
    if target_kind not in {None, "internal", "customer"}:
        raise HTTPException(422, "阶段目标须为内部样机或客户要求")
    phase_order_no = str(data.get("phase_order_no") or "").strip() or None
    if phase_order_no and len(phase_order_no) > 100:
        raise HTTPException(422, "阶段订单号不能超过 100 字")
    start, finish = valid_date(data.get("commit_start")), valid_date(data.get("commit_finish"))
    if start and finish and finish < start:
        raise HTTPException(422, "厂商承诺完工日不能早于开工日")
    with writing() as conn:
        validate_batch_quantity(conn, line_id, quantity, "production_batches", phase=phase)
        line = must_get(conn, "order_lines", line_id)
        multi_boards = board_definitions(conn, line_id)
        if multi_boards and quantity != quantity.to_integral_value():
            raise HTTPException(422, "多板生产批次数量须为整数件")
        if multi_boards and data.get("bom_items"):
            raise HTTPException(409, "多板项目请在建立批次后按板卡分别锁定 BOM")
        requested_version = None if multi_boards else data.get("bom_version") or line["bom_version"]
        bom_items = data.get("bom_items")
        if not bom_items and requested_version:
            bom_items = rows(conn, "SELECT source_line_no,material_code,material_name,quantity_per,loss_rate,unit_price FROM bom_catalog WHERE product_code=? AND bom_version=? ORDER BY source_line_no,id",
                             (line.get("bom_product_code") or line["product_code"], str(requested_version)))
        cur = conn.execute("""
            INSERT INTO production_batches(order_line_id,batch_no,quantity,vendor,commit_start,commit_finish,
               phase,phase_source,phase_order_no,schedule_start,schedule_mode,target_kind,target_date,
               sampling_order_date,board_eta,board_received_at,debug_accepted_at,
               vendor_confirmed_at,bom_version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (line_id, str(need(data, "batch_no")), ds(quantity), data.get("vendor"), start, finish,
                  phase, phase_source, phase_order_no, schedule_start,
                  mode, target_kind, valid_date(data.get("target_date")),
                  valid_date(data.get("sampling_order_date")), valid_date(data.get("board_eta")),
                  valid_date(data.get("board_received_at")), valid_date(data.get("debug_accepted_at")),
                  utcnow() if start and finish else None, requested_version, utcnow(), utcnow()))
        batch_id = cur.lastrowid
        conn.execute("UPDATE production_batches SET board_schedule_mode=? WHERE id=?",
                     (line["board_schedule_mode"], batch_id))
        if multi_boards:
            conn.execute("""UPDATE production_batches SET bom_scope='multi',board_set_revision=?,
                         board_set_confirmed_at=? WHERE id=?""",
                         (line["board_set_revision"], line["board_set_confirmed_at"], batch_id))
            clone_batch_boards(conn, batch_id, line_id)
        if bom_items:
            insert_bom(conn, batch_id, str(requested_version or need(data, "bom_version")), bom_items, user["id"])
        generate_stages(conn, batch_id, user["id"])
        legacy = one(conn, "SELECT * FROM legacy_records WHERE order_line_id=? AND production_batch_id IS NULL", (line_id,))
        if legacy:
            conn.execute("UPDATE legacy_records SET production_batch_id=?,version=version+1 WHERE id=?", (batch_id, legacy["id"]))
            audit(conn, "legacy_records", legacy["id"], "link_batch", user["id"],
                  {"production_batch_id": None}, {"production_batch_id": batch_id})
        result = must_get(conn, "production_batches", batch_id)
        result["conflicts"] = vendor_conflicts(conn, result)
        audit(conn, "production_batches", batch_id, "create", user["id"], None, result)
        return result


@app.get("/api/production-batches/{batch_id}")
def get_production_batch(batch_id: int, user: dict = Depends(require_user)):
    with reading() as conn:
        result = must_get(conn, "production_batches", batch_id)
        result["legacy_imported"] = is_imported_batch(conn, batch_id)
        line = must_get(conn, "order_lines", result["order_line_id"])
        result["product_code"] = line["product_code"]
        result["product_name"] = line["product_name"]
        result["order_line"] = line
        result["order_no"] = must_get(conn, "orders", line["order_id"])["order_no"]
        result["bom_items"] = rows(conn, """SELECT i.*,b.board_ref,b.board_name,b.bom_product_code AS board_product_code,
                              b.bom_version AS board_bom_version,b.planned_quantity AS board_planned_quantity,
                              b.quantity_per_product AS board_quantity_per_product FROM bom_items i
                              LEFT JOIN batch_boards b ON b.id=i.batch_board_id
                              WHERE i.production_batch_id=? ORDER BY i.id""", (batch_id,))
        result["bom_pricing"] = bom_pricing(result["bom_items"], result["quantity"], result["board_schedule_mode"])
        result["boards"] = batch_board_views(conn, batch_id) if result["bom_scope"] == "multi" else []
        if result["bom_scope"] == "multi" and not result["bom_locked"]:
            result["bom_pricing"].update({"complete": False, "total": None,
                                           "missing_board_count": sum(not board["bom_locked"] for board in result["boards"])})
        result["stages"] = stage_views(conn, batch_id)
        result["materials"] = batch_materials(conn, batch_id)
        result["forecast"] = batch_finish_forecast(conn, batch_id)
        result["conflicts"] = vendor_conflicts(conn, result)
        result["allocations"] = rows(conn, "SELECT * FROM allocations WHERE production_batch_id=?", (batch_id,))
        result["events"] = rows(conn, "SELECT * FROM events WHERE entity_type='production_batch' AND entity_id=? ORDER BY event_date,id", (batch_id,))
        return result


@app.get("/api/production-batches/{batch_id}/boards")
def get_batch_boards(batch_id: int, user: dict = Depends(require_user)):
    with reading() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        batch["legacy_imported"] = is_imported_batch(conn, batch_id)
        return {"batch": batch, "boards": batch_board_views(conn, batch_id) if batch["bom_scope"] == "multi" else []}


def independent_board(conn: sqlite3.Connection, board_id: int) -> tuple[dict, dict]:
    board = must_get(conn, "batch_boards", board_id)
    batch = must_get(conn, "production_batches", board["production_batch_id"])
    if batch["board_schedule_mode"] != "independent" or batch["bom_scope"] != "multi":
        raise HTTPException(409, "本批次使用共用排期；请在项目设置逐板排期并为新批次生成板卡计划")
    return board, batch


def board_detail(conn: sqlite3.Connection, board_id: int) -> dict:
    board, batch = independent_board(conn, board_id)
    view = next(item for item in batch_board_views(conn, batch["id"]) if item["id"] == board_id)
    return {"board": view, "stages": board_stage_views(conn, board_id),
            "forecast": view["forecast"]}


@app.get("/api/batch-boards/{board_id}")
def get_batch_board(board_id: int, user: dict = Depends(require_user)):
    with reading() as conn:
        return board_detail(conn, board_id)


@app.patch("/api/batch-boards/{board_id}")
def patch_batch_board(board_id: int, data: dict, user: dict = Depends(require_user)):
    reason = str(need(data, "reason")).strip()
    changes = data.get("changes")
    if not reason or not isinstance(changes, dict) or not changes or set(changes) - {
            "planned_quantity", "vendor", "commit_start", "commit_finish"}:
        raise HTTPException(422, "请填写逐板数量、外协厂或承诺日期修改内容及原因")
    with writing() as conn:
        board, batch = independent_board(conn, board_id)
        if "planned_quantity" in changes:
            planned = dec(changes["planned_quantity"], allow_zero=False)
            if planned != planned.to_integral_value():
                raise HTTPException(422, "板卡计划生产数量须为正整数片")
            if planned < board_finished_quantity(conn, board_id):
                raise HTTPException(409, "板卡计划生产数量不能小于累计实际完工数量")
            changes["planned_quantity"] = ds(planned)
        if "vendor" in changes:
            changes["vendor"] = str(changes["vendor"] or "").strip() or None
            if changes["vendor"] and len(changes["vendor"]) > 100:
                raise HTTPException(422, "外协厂名称不能超过 100 字")
        for field in ("commit_start", "commit_finish"):
            if field in changes:
                changes[field] = valid_date(changes[field])
        start, finish = changes.get("commit_start", board["commit_start"]), changes.get("commit_finish", board["commit_finish"])
        if start and finish and finish < start:
            raise HTTPException(422, "本板承诺完工日不能早于开工日")
        if set(changes) & {"vendor", "commit_start", "commit_finish"}:
            changes.update({"conflict_ack_reason": None, "conflict_ack_by": None,
                            "conflict_ack_at": None, "conflict_ack_signature": None})
        patch_record(conn, "batch_boards", board_id, int(need(data, "version")), changes, user["id"], reason)
        if "commit_start" in changes:
            replan_board_stages(conn, board_id, user["id"], "本板开工日变化后更新节点草案")
        if "planned_quantity" in changes:
            sync_material_ready(conn, batch["id"])
        return board_detail(conn, board_id)


@app.post("/api/batch-boards/{board_id}/conflicts/acknowledge")
def acknowledge_board_conflicts(board_id: int, data: dict, user: dict = Depends(require_user)):
    reason = str(need(data, "reason")).strip()
    if not reason:
        raise HTTPException(422, "请填写外协厂能够并行生产的确认依据")
    with writing() as conn:
        board, _ = independent_board(conn, board_id)
        if not board_vendor_conflicts(conn, board):
            raise HTTPException(409, "当前没有板卡档期重叠")
        patch_record(conn, "batch_boards", board_id, int(need(data, "version")),
                     {"conflict_ack_reason": reason, "conflict_ack_by": user["id"],
                      "conflict_ack_at": utcnow(),
                      "conflict_ack_signature": vendor_conflict_signature(board_vendor_conflicts(conn, board))},
                     user["id"], reason)
        return board_detail(conn, board_id)


@app.post("/api/batch-boards/{board_id}/stages")
def add_board_stage(board_id: int, data: dict, user: dict = Depends(require_user)):
    name = str(need(data, "name")).strip()
    reason = str(need(data, "reason")).strip()
    if not name or len(name) > 100 or not reason:
        raise HTTPException(422, "请填写节点名称和新增原因")
    stage_type = data.get("stage_type", "normal")
    if stage_type not in {"normal", "milestone"}:
        raise HTTPException(422, "节点类型无效")
    duration = checked_stage_duration(data.get("duration_days", "0"))
    with writing() as conn:
        independent_board(conn, board_id)
        if one(conn, "SELECT id FROM board_stages WHERE batch_board_id=? AND name=?", (board_id, name)):
            raise HTTPException(409, "本板已有同名节点")
        owner_id = active_owner_id(conn, data["owner_id"]) if data.get("owner_id") not in (None, "") else None
        sequence = (one(conn, "SELECT MAX(sequence) AS maximum FROM board_stages WHERE batch_board_id=?", (board_id,))["maximum"] or 0) + 1
        cur = conn.execute("""INSERT INTO board_stages(batch_board_id,sequence,name,stage_type,duration_days,
                           owner_id,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                           (board_id, sequence, name, stage_type, duration, owner_id,
                            str(data.get("note") or "").strip() or None, utcnow(), utcnow()))
        stage = must_get(conn, "board_stages", cur.lastrowid)
        audit(conn, "board_stages", stage["id"], "create", user["id"], None, stage, reason)
        replan_board_stages(conn, board_id, user["id"], reason)
        return stage_view(conn, must_get(conn, "board_stages", stage["id"]))


@app.patch("/api/board-stages/{stage_id}")
def patch_board_stage(stage_id: int, data: dict, user: dict = Depends(require_user)):
    reason = str(need(data, "reason")).strip()
    changes = data.get("changes")
    allowed = {"name", "owner_id", "duration_days", "stage_type", "planned_start", "planned_finish",
               "actual_start", "note"}
    if not reason or not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise HTTPException(422, "请填写节点修改内容及原因；完工日请使用节点完成操作")
    with writing() as conn:
        stage = must_get(conn, "board_stages", stage_id)
        independent_board(conn, stage["batch_board_id"])
        if "name" in changes:
            changes["name"] = str(changes["name"] or "").strip()
            if not changes["name"] or len(changes["name"]) > 100:
                raise HTTPException(422, "节点名称不能为空或超过 100 字")
            duplicate = one(conn, "SELECT id FROM board_stages WHERE batch_board_id=? AND name=? AND id<>?",
                            (stage["batch_board_id"], changes["name"], stage_id))
            if duplicate:
                raise HTTPException(409, "本板已有同名节点")
        if "owner_id" in changes:
            changes["owner_id"] = active_owner_id(conn, changes["owner_id"]) if changes["owner_id"] not in (None, "") else None
            if stage["status"] == "done" and changes["owner_id"] is None:
                raise HTTPException(422, "已完成节点须保留责任人账号")
        if "duration_days" in changes:
            changes["duration_days"] = checked_stage_duration(changes["duration_days"])
        if "stage_type" in changes and changes["stage_type"] not in {"normal", "milestone"}:
            raise HTTPException(422, "节点类型无效")
        for field in ("planned_start", "planned_finish", "actual_start"):
            if field in changes:
                changes[field] = valid_date(changes[field])
        if "actual_start" in changes and changes["actual_start"]:
            if changes["actual_start"] > today():
                raise HTTPException(422, "实际开始日不能晚于今天")
            if stage["actual_finish"] and changes["actual_start"] > stage["actual_finish"]:
                raise HTTPException(422, "实际开始日不能晚于实际完工日")
        if "note" in changes:
            changes["note"] = str(changes["note"] or "").strip() or None
            if changes["note"] and len(changes["note"]) > 1000:
                raise HTTPException(422, "节点备注不能超过 1000 字")
        if changes.get("planned_start", stage["planned_start"]) and changes.get("planned_finish", stage["planned_finish"]) and changes.get("planned_finish", stage["planned_finish"]) < changes.get("planned_start", stage["planned_start"]):
            raise HTTPException(422, "计划完工不能早于计划开始")
        if "planned_start" in changes or "planned_finish" in changes:
            changes["plan_overridden"] = 1
        result = patch_record(conn, "board_stages", stage_id, int(need(data, "version")), changes, user["id"], reason)
        if set(changes) & {"duration_days", "stage_type", "planned_start", "planned_finish"}:
            replan_board_stages(conn, stage["batch_board_id"], user["id"], reason)
            result = must_get(conn, "board_stages", stage_id)
        return stage_view(conn, result)


@app.post("/api/board-stages/{stage_id}/completion")
def complete_board_stage(stage_id: int, data: dict, user: dict = Depends(require_user)):
    completed = data.get("completed", data.get("complete"))
    if type(completed) is not bool:
        raise HTTPException(422, "completed 须为布尔值")
    reason = str(data.get("reason") or "").strip() or None
    with writing() as conn:
        stage = must_get(conn, "board_stages", stage_id)
        independent_board(conn, stage["batch_board_id"])
        if stage["version"] != int(need(data, "version")):
            raise HTTPException(409, "记录已被其他人修改，请刷新后重试")
        was_complete = stage["status"] == "done" and bool(stage["actual_finish"])
        if completed:
            if not stage["owner_id"]:
                raise HTTPException(422, "请先绑定节点责任人账号，再勾选完成")
            active_owner_id(conn, stage["owner_id"])
            finish = valid_date(data.get("actual_finish")) or (stage["actual_finish"] if was_complete else today())
            if finish > today() or stage["actual_start"] and finish < stage["actual_start"]:
                raise HTTPException(422, "实际完成日无效")
            if was_complete and finish == stage["actual_finish"]:
                return stage_view(conn, stage)
            if was_complete and not reason:
                raise HTTPException(422, "修改实际完成日须填写原因")
            changes = {"status": "done", "actual_finish": finish,
                       "completed_by": stage["completed_by"] if was_complete else user["id"],
                       "completed_at": stage["completed_at"] if was_complete else utcnow()}
        else:
            if not was_complete:
                return stage_view(conn, stage)
            if not reason:
                raise HTTPException(422, "撤销节点完成状态须填写原因")
            changes = {"status": "in_progress" if stage["actual_start"] else "pending",
                       "actual_finish": None, "completed_by": None, "completed_at": None}
        result = patch_record(conn, "board_stages", stage_id, stage["version"], changes, user["id"], reason)
        return stage_view(conn, result)


@app.post("/api/batch-boards/{board_id}/completion")
def complete_batch_board(board_id: int, data: dict, user: dict = Depends(require_user)):
    quantity = dec(need(data, "quantity"), allow_zero=False)
    if quantity != quantity.to_integral_value():
        raise HTTPException(422, "实际完工数量须为正整数片")
    reason = str(need(data, "reason")).strip()
    event_date = valid_date(need(data, "event_date"), required=True)
    if not reason or event_date > today():
        raise HTTPException(422, "请填写实际完工依据，完工日不能晚于今天")
    with writing() as conn:
        board, batch = independent_board(conn, board_id)
        if board["version"] != int(need(data, "version")):
            raise HTTPException(409, "板卡信息已更新，请刷新后重试")
        if not board["bom_locked"]:
            raise HTTPException(409, "本板 BOM 未锁定，不能登记完工")
        if board_finished_quantity(conn, board_id) + quantity > dec(board["planned_quantity"]):
            raise HTTPException(409, "累计实际完工数量不能超过本板计划生产数量")
        cur = conn.execute("""INSERT INTO events(entity_type,entity_id,kind,event_date,quantity,note,actor_id,created_at)
                           VALUES('batch_board',?,'finished',?,?,?,?,?)""",
                           (board_id, event_date, ds(quantity), reason, user["id"], utcnow()))
        event = one(conn, "SELECT * FROM events WHERE id=?", (cur.lastrowid,))
        audit(conn, "events", event["id"], "create", user["id"], None, event, reason)
        sync_order_status(conn, must_get(conn, "order_lines", batch["order_line_id"])["order_id"])
        return {"board": board_detail(conn, board_id)["board"], "event": event,
                "completed_quantity": ds(board_finished_quantity(conn, board_id)),
                "required_quantity": ds(dec(batch["quantity"]) * dec(board["quantity_per_product"])),
                "forecast": board_finish_forecast(conn, board_id)}


@app.post("/api/production-batches/{batch_id}/boards/activate")
def activate_batch_boards(batch_id: int, data: dict, user: dict = Depends(require_user)):
    reason = str(need(data, "reason")).strip()
    if not reason:
        raise HTTPException(422, "请填写启用或更新板卡计划的原因")
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        if batch["version"] != int(need(data, "version")):
            raise HTTPException(409, "生产批次已被修改，请刷新后重试")
        line = must_get(conn, "order_lines", batch["order_line_id"])
        if not line["board_set_confirmed_at"] or not board_definitions(conn, line["id"]):
            raise HTTPException(409, "请先配置并确认项目完整板卡组成")
        if dec(batch["quantity"]) != dec(batch["quantity"]).to_integral_value():
            raise HTTPException(422, "多板生产批次数量须为整数件")
        if (batch["bom_locked"] or one(conn, "SELECT id FROM bom_items WHERE production_batch_id=? LIMIT 1", (batch_id,))
                or one(conn, "SELECT id FROM batch_boards WHERE production_batch_id=? AND bom_locked=1 LIMIT 1", (batch_id,))):
            raise HTTPException(409, "已有 BOM 快照，不能转换或更新此生产批次的板卡计划")
        if (one(conn, "SELECT id FROM events WHERE entity_type='production_batch' AND entity_id=? LIMIT 1", (batch_id,))
                or one(conn, "SELECT id FROM stages WHERE production_batch_id=? AND (actual_start IS NOT NULL OR actual_finish IS NOT NULL) LIMIT 1", (batch_id,))
                or one(conn, """SELECT s.id FROM board_stages s JOIN batch_boards b ON b.id=s.batch_board_id
                        WHERE b.production_batch_id=? AND (s.actual_start IS NOT NULL OR s.actual_finish IS NOT NULL) LIMIT 1""", (batch_id,))
                or one(conn, """SELECT e.id FROM events e JOIN batch_boards b ON b.id=e.entity_id
                        WHERE e.entity_type='batch_board' AND b.production_batch_id=? LIMIT 1""", (batch_id,))):
            raise HTTPException(409, "已有实际进度，不能更换板卡计划")
        previous = batch_board_views(conn, batch_id) if batch["bom_scope"] == "multi" else []
        conn.execute("DELETE FROM board_stages WHERE batch_board_id IN (SELECT id FROM batch_boards WHERE production_batch_id=?)", (batch_id,))
        conn.execute("DELETE FROM batch_boards WHERE production_batch_id=?", (batch_id,))
        conn.execute("""UPDATE production_batches SET bom_scope='multi',bom_locked=0,bom_version=NULL,
                     board_schedule_mode=?,board_set_revision=?,board_set_confirmed_at=?,version=version+1,updated_at=? WHERE id=?""",
                     (line["board_schedule_mode"], line["board_set_revision"], line["board_set_confirmed_at"], utcnow(), batch_id))
        clone_batch_boards(conn, batch_id, line["id"])
        result = must_get(conn, "production_batches", batch_id)
        boards = batch_board_views(conn, batch_id)
        audit(conn, "production_batches", batch_id, "activate_boards", user["id"],
              {"batch": batch, "boards": previous}, {"batch": result, "boards": boards}, reason)
        return {"batch": result, "boards": boards}


@app.post("/api/production-batches/{batch_id}/boards/{board_id}/lock-from-catalog")
def lock_batch_board_from_catalog(batch_id: int, board_id: int, data: dict,
                                  user: dict = Depends(require_user)):
    reason = str(need(data, "reason")).strip()
    version = str(need(data, "bom_version")).strip()
    if not reason or not version:
        raise HTTPException(422, "请填写本板 BOM 版本及锁定依据")
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        board = must_get(conn, "batch_boards", board_id)
        if board["production_batch_id"] != batch_id:
            raise HTTPException(422, "板卡不属于此生产批次")
        if batch["bom_scope"] != "multi" or not batch["board_set_confirmed_at"]:
            raise HTTPException(409, "请先确认并启用多板批次的完整板卡组成")
        if batch["version"] != int(need(data, "batch_version")) or board["version"] != int(need(data, "version")):
            raise HTTPException(409, "生产批次或板卡信息已更新，请刷新后重试")
        if board["bom_locked"]:
            raise HTTPException(409, "本板 BOM 已锁定；改版请建立新生产批次")
        catalog = rows(conn, """SELECT source_line_no,material_code,material_name,quantity_per,loss_rate,unit_price
                       FROM bom_catalog WHERE product_code=? AND bom_version=? ORDER BY source_line_no,id""",
                       (board["bom_product_code"], version))
        if not catalog:
            raise HTTPException(409, "所选板卡母件编码和 BOM 版本在导入目录中没有物料")
        seen = set()
        for index, item in enumerate(catalog, 1):
            source_no = str(item["source_line_no"] or index)
            if source_no in seen:
                raise HTTPException(409, "目录内同一板卡 BOM 行号重复，请先修正导入数据")
            seen.add(source_no)
            effective_per_product = dec(board["quantity_per_product"]) * dec(item["quantity_per"], allow_zero=False)
            conn.execute("""INSERT INTO bom_items(production_batch_id,source_line_no,material_code,material_name,
                         quantity_per,loss_rate,unit_price,batch_board_id,catalog_source_line_no,catalog_quantity_per)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         (batch_id, f"B{board_id}:{source_no}", item["material_code"], item["material_name"],
                          ds(effective_per_product), item["loss_rate"], item["unit_price"], board_id, source_no,
                          item["quantity_per"]))
        patch_record(conn, "batch_boards", board_id, board["version"],
                     {"bom_version": version, "bom_locked": 1}, user["id"], reason)
        boards = batch_board_views(conn, batch_id)
        all_locked = bool(boards) and all(item["bom_locked"] for item in boards)
        conn.execute("UPDATE production_batches SET bom_locked=?,version=version+1,updated_at=? WHERE id=?",
                     (int(all_locked), utcnow(), batch_id))
        result = must_get(conn, "production_batches", batch_id)
        audit(conn, "production_batches", batch_id, "board_bom_snapshot", user["id"], batch,
              {"board_id": board_id, "board_ref": board["board_ref"], "bom_product_code": board["bom_product_code"],
               "bom_version": version, "item_count": len(catalog), "all_boards_locked": all_locked}, reason)
        sync_material_ready(conn, batch_id)
        return {"batch": result, "boards": boards, "bom_item_count": len(catalog)}


@app.patch("/api/production-batches/{batch_id}")
def patch_production_batch(batch_id: int, data: dict, user: dict = Depends(require_user)):
    changes = normalize_changes(data.get("changes", {}),
                                {"quantity", "vendor", "commit_start", "commit_finish", "phase",
                                 "phase_order_no", "schedule_start", "schedule_mode", "target_kind", "target_date",
                                 "sampling_order_date", "board_eta", "board_received_at", "debug_accepted_at"},
                                protected={"quantity", "commit_start", "commit_finish", "vendor", "phase", "phase_order_no",
                                           "target_kind", "target_date", "board_eta", "board_received_at", "debug_accepted_at"},
                                reason=data.get("reason"))
    with writing() as conn:
        old = must_get(conn, "production_batches", batch_id)
        if "phase" in changes:
            changes["phase"] = checked_phase(changes["phase"], allow_unclassified=False)
            if (old["board_schedule_mode"] == "independent" and changes["phase"] != old["phase"]
                    and one(conn, "SELECT id FROM batch_boards WHERE production_batch_id=? LIMIT 1", (batch_id,))):
                raise HTTPException(409, "逐板节点已按原阶段生成，请新建目标阶段生产批次")
            changes["phase_source"] = "manual"
            if old["bom_locked"] and old["bom_scope"] != "multi":
                competing_versions = {row["bom_version"] for row in rows(
                    conn,
                    "SELECT DISTINCT bom_version FROM production_batches WHERE order_line_id=? AND phase=? AND bom_locked=1 AND id<>?",
                    (old["order_line_id"], changes["phase"], batch_id),
                )}
                if competing_versions and competing_versions != {old["bom_version"]}:
                    raise HTTPException(409, "目标阶段已有不同 BOM 版本的锁定批次")
        if "phase_order_no" in changes:
            changes["phase_order_no"] = str(changes["phase_order_no"] or "").strip() or None
            if changes["phase_order_no"] and len(changes["phase_order_no"]) > 100:
                raise HTTPException(422, "阶段订单号不能超过 100 字")
        if "schedule_mode" in changes:
            changes["schedule_mode"] = checked_schedule_mode(changes["schedule_mode"])
        if "target_kind" in changes and changes["target_kind"] not in {None, "internal", "customer"}:
            raise HTTPException(422, "阶段目标须为内部样机或客户要求")
        for key in ("schedule_start", "target_date", "sampling_order_date", "board_eta", "board_received_at", "debug_accepted_at"):
            if key in changes:
                changes[key] = valid_date(changes[key])
        new_start = changes.get("commit_start", old["commit_start"])
        new_finish = changes.get("commit_finish", old["commit_finish"])
        if new_start and new_finish and new_finish < new_start:
            raise HTTPException(422, "厂商承诺完工日不能早于开工日")
        if "quantity" in changes or "phase" in changes:
            validate_batch_quantity(conn, old["order_line_id"], dec(changes.get("quantity", old["quantity"])),
                                    "production_batches", batch_id, changes.get("phase", old["phase"]))
        if "quantity" in changes:
            if old["bom_scope"] == "multi" and dec(changes["quantity"]) != dec(changes["quantity"]).to_integral_value():
                raise HTTPException(422, "多板生产批次数量须为整数件")
            allocated = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM allocations WHERE production_batch_id=?", (batch_id,))), Decimal(0))
            if dec(changes["quantity"]) < allocated:
                raise HTTPException(409, "生产数量不能小于已分配交付数量")
            finished = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM events WHERE entity_type='production_batch' AND entity_id=? AND kind='finished'", (batch_id,))), Decimal(0))
            if dec(changes["quantity"]) < finished:
                raise HTTPException(409, "生产数量不能小于累计实际完工数量")
        if set(changes) & {"commit_start", "commit_finish", "vendor"}:
            changes.update({"vendor_confirmed_at": utcnow() if new_start and new_finish else None,
                            "conflict_ack_reason": None, "conflict_ack_by": None,
                            "conflict_ack_at": None, "conflict_ack_signature": None})
        result = patch_record(conn, "production_batches", batch_id, int(need(data, "version")), changes, user["id"], data.get("reason"))
        if "quantity" in changes:
            if old["board_schedule_mode"] == "shared" and old["bom_scope"] == "multi":
                for board in rows(conn, "SELECT * FROM batch_boards WHERE production_batch_id=?", (batch_id,)):
                    patch_record(conn, "batch_boards", board["id"], board["version"],
                                 {"planned_quantity": ds(dec(changes["quantity"]) * dec(board["quantity_per_product"]))},
                                 user["id"], data.get("reason"))
            sync_material_ready(conn, batch_id)
        if "schedule_start" in changes or "schedule_mode" in changes or "board_received_at" in changes:
            if "board_received_at" in changes:
                milestone = one(conn, "SELECT * FROM stages WHERE production_batch_id=? AND name='样板到料'", (batch_id,))
                if milestone:
                    conn.execute("UPDATE stages SET actual_material_at=?,version=version+1,updated_at=? WHERE id=?",
                                 (changes["board_received_at"], utcnow(), milestone["id"]))
            replan_batch_stages(conn, batch_id, user["id"], "阶段日期变化后顺延节点")
        result["conflicts"] = vendor_conflicts(conn, result)
        return result


@app.post("/api/production-batches/{batch_id}/bom-lock")
def lock_bom(batch_id: int, data: dict, user: dict = Depends(require_user)):
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        if batch["version"] != int(need(data, "version")):
            raise HTTPException(409, "记录已被其他人修改")
        if batch["bom_locked"]:
            raise HTTPException(409, "BOM 已锁定，改版须新建生产批次")
        insert_bom(conn, batch_id, str(need(data, "bom_version")), data.get("items", []), user["id"])
        sync_material_ready(conn, batch_id)
        return must_get(conn, "production_batches", batch_id)


@app.post("/api/production-batches/{batch_id}/bom-lock-from-catalog")
def lock_bom_from_catalog(batch_id: int, data: dict, user: dict = Depends(require_user)):
    reason = str(need(data, "reason")).strip()
    version = str(need(data, "bom_version")).strip()
    if not reason or not version:
        raise HTTPException(422, "锁定原因和 BOM 版本不能为空")
    try:
        expected_version = int(need(data, "version"))
    except (TypeError, ValueError):
        raise HTTPException(422, "生产批次版本无效")
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        if batch["version"] != expected_version:
            raise HTTPException(409, "生产批次已被修改，请刷新后重试")
        if batch["bom_locked"]:
            raise HTTPException(409, "BOM 已锁定，改版须新建生产批次")
        line = must_get(conn, "order_lines", batch["order_line_id"])
        if not line["bom_version"]:
            raise HTTPException(409, "订单产品行尚未关联 BOM，请先选择目录母件和默认版本")
        linked_code = line.get("bom_product_code") or line["product_code"]
        items = rows(conn, """SELECT source_line_no,material_code,material_name,quantity_per,loss_rate,unit_price
                         FROM bom_catalog WHERE product_code=? AND bom_version=? ORDER BY source_line_no,id""",
                     (linked_code, version))
        if not items:
            raise HTTPException(409, "对应的 BOM 目录没有物料，请先导入并关联")
        insert_bom(conn, batch_id, version, items, user["id"])
        sync_material_ready(conn, batch_id)
        audit(conn, "production_batches", batch_id, "bom_catalog_snapshot", user["id"], None,
              {"bom_product_code": linked_code, "bom_version": version, "item_count": len(items)}, reason)
        return {"batch": must_get(conn, "production_batches", batch_id), "bom_item_count": len(items)}


@app.post("/api/delivery-batches")
def create_delivery_batch(data: dict, user: dict = Depends(require_user)):
    with writing() as conn:
        return insert_delivery_batch(conn, data, user["id"])


def insert_delivery_batch(conn: sqlite3.Connection, data: dict, actor_id: int) -> dict:
    line_id = int(need(data, "order_line_id"))
    quantity = dec(need(data, "quantity"), allow_zero=False)
    basis = data.get("completion_basis", "shipment")
    if basis not in {"shipment", "receipt"}:
        raise HTTPException(422, "完成口径无效")
    manual_date = valid_date(data.get("manual_estimate_date"))
    manual_reason = str(data.get("manual_estimate_reason") or "").strip() or None
    if bool(manual_date) != bool(manual_reason):
        raise HTTPException(422, "人工预计交付日及估算依据须同时填写")
    validate_batch_quantity(conn, line_id, quantity, "delivery_batches")
    shipping_lead = int(data.get("shipping_lead_days", 1))
    receipt_lead = int(data.get("receipt_lead_days", 2))
    if not 0 <= shipping_lead <= 30 or not 0 <= receipt_lead <= 30:
        raise HTTPException(422, "物流提前期应为 0 到 30 个工作日")
    cur = conn.execute("INSERT INTO delivery_batches(order_line_id,batch_no,quantity,due_date,promised_date,manual_estimate_date,manual_estimate_reason,completion_basis,shipping_lead_days,receipt_lead_days,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (line_id, str(need(data, "batch_no")), ds(quantity), valid_date(data.get("due_date")),
                        valid_date(data.get("promised_date")), manual_date, manual_reason, basis,
                        shipping_lead, receipt_lead, utcnow(), utcnow()))
    result = must_get(conn, "delivery_batches", cur.lastrowid)
    audit(conn, "delivery_batches", result["id"], "create", actor_id, None, result, manual_reason)
    sync_order_status(conn, must_get(conn, "order_lines", line_id)["order_id"])
    return result


@app.patch("/api/delivery-batches/{batch_id}")
def patch_delivery_batch(batch_id: int, data: dict, user: dict = Depends(require_user)):
    changes = normalize_changes(data.get("changes", {}), {"quantity", "due_date", "promised_date", "manual_estimate_date", "manual_estimate_reason", "completion_basis", "shipping_lead_days", "receipt_lead_days"},
                                protected={"quantity", "due_date", "promised_date", "manual_estimate_date", "manual_estimate_reason", "shipping_lead_days", "receipt_lead_days"}, reason=data.get("reason"))
    with writing() as conn:
        for key in ("shipping_lead_days", "receipt_lead_days"):
            if key in changes:
                changes[key] = int(changes[key])
                if not 0 <= changes[key] <= 30:
                    raise HTTPException(422, "物流提前期应为 0 到 30 个工作日")
        old = must_get(conn, "delivery_batches", batch_id)
        if "manual_estimate_date" in changes and changes["manual_estimate_date"] is None and "manual_estimate_reason" not in changes:
            changes["manual_estimate_reason"] = None
        manual_date = changes.get("manual_estimate_date", old["manual_estimate_date"])
        manual_reason = str(changes.get("manual_estimate_reason", old["manual_estimate_reason"]) or "").strip() or None
        if bool(manual_date) != bool(manual_reason):
            raise HTTPException(422, "人工预计交付日及估算依据须同时填写")
        if "manual_estimate_date" in changes and changes["manual_estimate_date"] and "manual_estimate_reason" not in changes:
            raise HTTPException(422, "修改人工预计交付日须填写本次估算依据")
        if "manual_estimate_reason" in changes:
            changes["manual_estimate_reason"] = manual_reason
        if "quantity" in changes:
            validate_batch_quantity(conn, old["order_line_id"], dec(changes["quantity"]), "delivery_batches", batch_id)
            allocated = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM allocations WHERE delivery_batch_id=?", (batch_id,))), Decimal(0))
            if dec(changes["quantity"]) < allocated:
                raise HTTPException(409, "交付数量不能小于已分配生产数量")
            delivered = max((sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM events WHERE entity_type='delivery_batch' AND entity_id=? AND kind=?", (batch_id, kind))), Decimal(0))
                             for kind in ("shipped", "customer_received")), default=Decimal(0))
            if dec(changes["quantity"]) < delivered:
                raise HTTPException(409, "交付数量不能小于累计实际发货或签收数量")
        result = patch_record(conn, "delivery_batches", batch_id, int(need(data, "version")), changes, user["id"], data.get("reason"))
        sync_order_status(conn, must_get(conn, "order_lines", old["order_line_id"])["order_id"])
        return result


@app.post("/api/allocations")
def create_allocation(data: dict, user: dict = Depends(require_user)):
    p_id, d_id = int(need(data, "production_batch_id")), int(need(data, "delivery_batch_id"))
    quantity = dec(need(data, "quantity"), allow_zero=False)
    with writing() as conn:
        prod, delivery = must_get(conn, "production_batches", p_id), must_get(conn, "delivery_batches", d_id)
        if prod["order_line_id"] != delivery["order_line_id"]:
            raise HTTPException(409, "生产批次与交付批次必须属于同一产品行")
        used_prod = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM allocations WHERE production_batch_id=?", (p_id,))), Decimal(0))
        used_delivery = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM allocations WHERE delivery_batch_id=?", (d_id,))), Decimal(0))
        if used_prod + quantity > dec(prod["quantity"]) or used_delivery + quantity > dec(delivery["quantity"]):
            raise HTTPException(409, "分配数量超过生产或交付批次剩余量")
        cur = conn.execute("INSERT INTO allocations(production_batch_id,delivery_batch_id,quantity,created_at) VALUES(?,?,?,?)",
                           (p_id, d_id, ds(quantity), utcnow()))
        result = must_get(conn, "allocations", cur.lastrowid)
        audit(conn, "allocations", result["id"], "create", user["id"], None, result)
        return result


@app.delete("/api/allocations/{allocation_id}")
def delete_allocation(allocation_id: int, data: dict | None = None, user: dict = Depends(require_user)):
    with writing() as conn:
        before = must_get(conn, "allocations", allocation_id)
        conn.execute("DELETE FROM allocations WHERE id=?", (allocation_id,))
        audit(conn, "allocations", allocation_id, "delete", user["id"], before, None, (data or {}).get("reason"))
        return {"ok": True}


def latest_stock(conn: sqlite3.Connection, material_code: str) -> dict | None:
    return one(conn, "SELECT s.id AS snapshot_id,s.as_of,s.as_of_precision,s.basis,r.quantity FROM inventory_rows r JOIN inventory_snapshots s ON s.id=r.snapshot_id WHERE r.material_code=? ORDER BY s.as_of DESC,s.id DESC LIMIT 1", (material_code,))


def stock_capacity(conn: sqlite3.Connection, material_code: str) -> dict:
    latest = latest_stock(conn, material_code)
    as_of = latest["as_of"] if latest else "0001-01-01T00:00:00+00:00"
    accepted_after = sum((dec(r["accepted_quantity"]) for r in rows(conn, "SELECT accepted_quantity FROM receipts WHERE material_code=? AND iqc_status='pass' AND received_at>?", (material_code, as_of))), Decimal(0))
    issued_after = Decimal(0)
    ambiguous = Decimal(0)
    snapshot_day = datetime.fromisoformat(as_of).astimezone(LOCAL_TZ).date().isoformat()
    for event in rows(conn, "SELECT quantity,event_date,event_at FROM events WHERE kind='material_sent' AND material_code=?", (material_code,)):
        if event["event_at"]:
            after = event["event_at"] > as_of
        elif not latest or event["event_date"] > snapshot_day:
            after = True
        elif event["event_date"] < snapshot_day or latest["as_of_precision"] == "day_end":
            after = False
        else:
            ambiguous += dec(event["quantity"])
            continue
        if after:
            issued_after += dec(event["quantity"])
    # KIS "on hand" can include stock reserved elsewhere or awaiting IQC. Keep
    # its original quantity visible, but only a warehouse-confirmed available
    # snapshot may support a new internal reservation or a ready BOM line.
    snapshot_quantity = dec(latest["quantity"]) if latest else Decimal(0)
    confirmation_required = bool(latest and latest["basis"] == "on_hand")
    usable_base = Decimal(0) if confirmation_required else snapshot_quantity
    reserved = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM reservations WHERE stock_material_code=?", (material_code,))), Decimal(0))
    capacity = usable_base + accepted_after - issued_after
    return {"material_code": material_code, "snapshot_as_of": latest["as_of"] if latest else None,
            "basis": latest["basis"] if latest else None, "snapshot_quantity": ds(snapshot_quantity),
            "usable_snapshot_quantity": ds(usable_base), "confirmation_required": confirmation_required,
            "qualified_after_snapshot": ds(accepted_after), "issued_after_snapshot": ds(issued_after),
            "same_day_issue_unresolved": ds(ambiguous),
            "capacity": ds(capacity), "reserved": ds(reserved), "free": ds(capacity - reserved)}


def effective_held(conn: sqlite3.Connection, reservation: dict) -> Decimal:
    capacity = max(Decimal(0), Decimal(stock_capacity(conn, reservation["stock_material_code"])["capacity"]))
    earlier = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM reservations WHERE stock_material_code=? AND id<?", (reservation["stock_material_code"], reservation["id"]))), Decimal(0))
    return min(dec(reservation["quantity"]), max(Decimal(0), capacity - earlier))


def required_materials(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    batch = must_get(conn, "production_batches", batch_id)
    grouped: dict[str, dict] = {}
    for item in rows(conn, """SELECT i.*,b.board_ref,b.board_name,b.bom_product_code AS board_product_code,
                       b.bom_version AS board_bom_version,b.planned_quantity AS board_planned_quantity,
                       b.quantity_per_product AS board_quantity_per_product FROM bom_items i
                       LEFT JOIN batch_boards b ON b.id=i.batch_board_id
                       WHERE i.production_batch_id=? ORDER BY i.id""", (batch_id,)):
        # Unknown stock unit precision: round each BOM row up to whole pieces, never understate shortage.
        required = bom_row_required(item, batch["quantity"], batch["board_schedule_mode"])
        bucket = grouped.setdefault(item["material_code"], {"material_code": item["material_code"],
                           "material_name": item["material_name"], "required": Decimal(0), "bom_lines": []})
        bucket["required"] += required
        bucket["bom_lines"].append({**item, "required": ds(required)})
    return list(grouped.values())


def approved_substitute_limit(conn: sqlite3.Connection, batch_id: int, original: str, actual: str) -> Decimal:
    return sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM substitutes WHERE production_batch_id=? AND original_material_code=? AND substitute_material_code=? AND status='approved'", (batch_id, original, actual))), Decimal(0))


def purchase_eta_for(conn: sqlite3.Connection, batch_id: int, material_code: str, shortage: Decimal) -> tuple[str | None, bool, bool]:
    purchases = rows(conn, "SELECT p.id,p.reference,p.eta,p.eta_is_estimate,a.quantity FROM purchase_allocations a JOIN purchases p ON p.id=a.purchase_id WHERE a.production_batch_id=? AND a.material_code=?", (batch_id, material_code))
    confirmed: list[tuple[str, Decimal]] = []
    estimated: list[tuple[str, Decimal]] = []
    for purchase in purchases:
        arrived = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM receipts WHERE purchase_id=? AND production_batch_id=?", (purchase["id"], batch_id))), Decimal(0))
        pending = max(Decimal(0), dec(purchase["quantity"]) - arrived)
        if pending <= 0:
            continue
        if purchase["eta"] and purchase["eta"] >= today():
            (estimated if purchase["eta_is_estimate"] else confirmed).append((purchase["eta"], pending))
    remaining = shortage
    chosen_dates = []
    for eta, quantity in sorted(confirmed):
        remaining -= quantity
        chosen_dates.append(eta)
        if remaining <= 0:
            return max(chosen_dates), False, False
    used_estimate = False
    for eta, quantity in sorted(estimated):
        remaining -= quantity
        chosen_dates.append(eta)
        used_estimate = True
        if remaining <= 0:
            return max(chosen_dates), False, used_estimate
    return None, True, False


MATERIAL_REVIEW_REASONS = {
    "bom": "BOM 或生产数量已变化",
    "stock": "库存口径或可用量已变化",
    "reservations": "预留或发料数量已变化",
    "purchases": "采购、到料或 IQC 信息已变化",
    "substitutes": "替代料审批已变化",
    "material": "需求缺口或预计齐套日已变化",
}


def material_review_state(conn: sqlite3.Connection, batch: dict, item: dict) -> tuple[str, dict]:
    """Fingerprint the evidence reviewed by a person, not the review record itself."""
    batch_id, code = batch["id"], item["material_code"]
    reservations = rows(conn, "SELECT * FROM reservations WHERE production_batch_id=? AND for_material_code=? ORDER BY id", (batch_id, code))
    stock_codes = {code, *(reservation["stock_material_code"] for reservation in reservations)}
    parts = {
        "bom": {"batch_quantity": batch["quantity"], "bom_version": batch["bom_version"],
                "board_planned_quantities": [
                    {"board_id": board["id"], "planned_quantity": board["planned_quantity"]}
                    for board in rows(conn, "SELECT id,planned_quantity FROM batch_boards WHERE production_batch_id=? ORDER BY id",
                                      (batch_id,))],
                "bom_lines": item["bom_lines"]},
        "stock": [stock_capacity(conn, stock_code) for stock_code in sorted(stock_codes)],
        "reservations": reservations,
        "purchases": {
            "allocations": rows(conn, """SELECT a.id,a.quantity,p.id AS purchase_id,p.version,p.reference,
                                 p.quantity AS purchase_quantity,p.eta,p.eta_is_estimate
                                 FROM purchase_allocations a JOIN purchases p ON p.id=a.purchase_id
                                 WHERE a.production_batch_id=? AND a.material_code=? ORDER BY a.id""", (batch_id, code)),
            "receipts": rows(conn, """SELECT id,quantity,received_at,purchase_id,production_batch_id,
                             iqc_status,accepted_quantity,version FROM receipts WHERE material_code=? ORDER BY id""", (code,)),
        },
        "substitutes": rows(conn, "SELECT * FROM substitutes WHERE production_batch_id=? AND original_material_code=? ORDER BY id", (batch_id, code)),
        "material": {key: item[key] for key in ("required", "reserved", "shortage", "eta", "missing_eta", "eta_is_estimate")},
    }
    digests = {name: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
               for name, value in parts.items()}
    state_hash = hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()
    return state_hash, digests


def material_review_view(conn: sqlite3.Connection, batch: dict, item: dict) -> dict:
    state_hash, parts = material_review_state(conn, batch, item)
    review = one(conn, """SELECT r.*,u.display_name AS reviewed_by_name FROM material_reviews r
                   JOIN users u ON u.id=r.reviewed_by
                   WHERE r.production_batch_id=? AND r.material_code=?""", (batch["id"], item["material_code"]))
    stale_reasons = []
    if review and review["state_hash"] != state_hash:
        previous = json.loads(review["state_parts_json"])
        stale_reasons = [label for name, label in MATERIAL_REVIEW_REASONS.items()
                         if previous.get(name) != parts[name]]
        if not stale_reasons:
            stale_reasons = ["核对依据已变化"]
    return {"status": "unreviewed" if not review else "stale" if review["state_hash"] != state_hash else "current",
            "state_hash": state_hash, "stale_reasons": stale_reasons,
            "reviewed_at": review["reviewed_at"] if review else None,
            "reviewed_by_name": review["reviewed_by_name"] if review else None,
            "note": review["note"] if review else None,
            "version": review["version"] if review else None}


def batch_materials(conn: sqlite3.Connection, batch_id: int) -> dict:
    batch = must_get(conn, "production_batches", batch_id)
    partially_locked_multi = batch["bom_scope"] == "multi" and not batch["bom_locked"]
    if not batch["bom_locked"] and not partially_locked_multi:
        return {"items": [], "ready_date": None, "status": "bom_unlocked",
                "review_summary": {"current": 0, "stale": 0, "unreviewed": 0}}
    items = []
    all_current = True
    unknown = False
    has_estimate = False
    ready_dates = []
    for item in required_materials(conn, batch_id):
        direct = Decimal(0)
        substitutes = Decimal(0)
        direct_reservation = None
        substitute_reservations = []
        for reservation in rows(conn, "SELECT * FROM reservations WHERE production_batch_id=? AND for_material_code=?", (batch_id, item["material_code"])):
            credited = effective_held(conn, reservation) + dec(reservation["issued_quantity"])
            if reservation["stock_material_code"] == item["material_code"]:
                direct += credited
                direct_reservation = reservation
            else:
                approved = approved_substitute_limit(conn, batch_id, item["material_code"], reservation["stock_material_code"])
                substitutes += min(credited, approved)
                substitute_reservations.append({**reservation, "status": "approved" if approved > 0 else "unapproved"})
        available = direct + substitutes
        shortage = max(Decimal(0), item["required"] - available)
        if shortage:
            all_current = False
            eta, missing, eta_is_estimate = purchase_eta_for(conn, batch_id, item["material_code"], shortage)
            if missing or not eta:
                unknown = True
            elif eta_is_estimate:
                has_estimate = True
        else:
            eta, missing, eta_is_estimate = None, False, False
        if eta:
            ready_dates.append(eta)
        material = {"material_code": item["material_code"], "material_name": item["material_name"],
                      "required": ds(item["required"]), "reserved": ds(available), "shortage": ds(shortage),
                      "eta": eta, "ready_date": eta if shortage else today(),
                      "missing_eta": missing, "eta_is_estimate": eta_is_estimate,
                      "bom_lines": item["bom_lines"],
                      "direct_reservation_quantity": direct_reservation["quantity"] if direct_reservation else "0",
                      "direct_reservation_version": direct_reservation["version"] if direct_reservation else None,
                      "issued_quantity": direct_reservation["issued_quantity"] if direct_reservation else "0",
                      "substitute_reservations": substitute_reservations}
        material["review"] = material_review_view(conn, batch, material)
        items.append(material)
    if partially_locked_multi:
        status, ready_date = "bom_unlocked", None
    elif all_current:
        status, ready_date = "ready", (batch["material_ready_at"] or utcnow())[:10]
    elif unknown:
        status, ready_date = "unknown", None
    elif has_estimate:
        status, ready_date = "estimated", max(ready_dates) if ready_dates else None
    else:
        status, ready_date = "forecast", max(ready_dates) if ready_dates else None
    review_summary = {state: sum(item["review"]["status"] == state for item in items)
                      for state in ("current", "stale", "unreviewed")}
    return {"items": items, "ready_date": ready_date, "status": status, "review_summary": review_summary}


def sync_material_ready(conn: sqlite3.Connection, batch_id: int) -> None:
    batch = must_get(conn, "production_batches", batch_id)
    status = batch_materials(conn, batch_id)["status"]
    if status == "ready" and not batch["material_ready_at"]:
        conn.execute("UPDATE production_batches SET material_ready_at=? WHERE id=?", (utcnow(), batch_id))
        audit(conn, "production_batches", batch_id, "material_ready", None, {"material_ready_at": None}, {"material_ready_at": utcnow()})
    elif status != "ready" and batch["material_ready_at"]:
        conn.execute("UPDATE production_batches SET material_ready_at=NULL WHERE id=?", (batch_id,))
        audit(conn, "production_batches", batch_id, "material_not_ready", None,
              {"material_ready_at": batch["material_ready_at"]}, {"material_ready_at": None})


def sync_ready_for_material(conn: sqlite3.Connection, material_code: str) -> None:
    batch_ids = {r["production_batch_id"] for r in rows(conn, "SELECT DISTINCT production_batch_id FROM reservations WHERE stock_material_code=?", (material_code,))}
    for batch_id in batch_ids:
        sync_material_ready(conn, batch_id)


@app.get("/api/production-batches/{batch_id}/materials")
def get_batch_materials(batch_id: int, user: dict = Depends(require_user)):
    with reading() as conn:
        return batch_materials(conn, batch_id)


@app.get("/api/materials")
def all_materials(user: dict = Depends(require_user), production_batch_id: int | None = None,
                  order_id: int | None = None):
    with reading() as conn:
        result = []
        for batch in rows(conn, """SELECT p.id,p.batch_no,p.phase,p.bom_version,p.bom_locked,p.order_line_id,
                           l.product_code,l.product_name,l.bom_product_code,o.id AS order_id,o.order_no,o.project_name
                           FROM production_batches p JOIN order_lines l ON l.id=p.order_line_id
                           JOIN orders o ON o.id=l.order_id
                           WHERE (? IS NULL OR p.id=?) AND (? IS NULL OR o.id=?)
                           ORDER BY o.order_no,p.batch_no""",
                          (production_batch_id, production_batch_id, order_id, order_id)):
            batch["legacy_imported"] = is_imported_batch(conn, batch["id"])
            result.append({**batch, **batch_materials(conn, batch["id"])})
        return result


@app.put("/api/material-reviews")
def put_material_review(data: dict, user: dict = Depends(require_user)):
    try:
        batch_id = int(need(data, "production_batch_id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "生产批次 ID 无效")
    code = str(need(data, "material_code"))
    confirmed = data.get("confirmed")
    if not isinstance(confirmed, bool):
        raise HTTPException(422, "核对状态须为 true 或 false")
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        material = next((item for item in batch_materials(conn, batch_id)["items"]
                         if item["material_code"] == code), None)
        if material is None:
            raise HTTPException(409, "物料不在此批次已锁定的 BOM 中")
        review = one(conn, "SELECT * FROM material_reviews WHERE production_batch_id=? AND material_code=?", (batch_id, code))
        if material["review"]["state_hash"] != str(need(data, "state_hash")):
            raise HTTPException(409, "物料状态已变化，请刷新后重新核对")
        if review and data.get("version") != review["version"]:
            raise HTTPException(409, "核对记录已被其他人修改，请刷新后重试")
        if not confirmed:
            if review:
                conn.execute("DELETE FROM material_reviews WHERE id=?", (review["id"],))
                audit(conn, "material_reviews", review["id"], "uncheck", user["id"], review, None,
                      str(data.get("note") or "") or None)
        else:
            state_hash, parts = material_review_state(conn, batch, material)
            note = str(data.get("note") or "").strip() or None
            if review:
                conn.execute("""UPDATE material_reviews SET state_hash=?,state_parts_json=?,reviewed_by=?,
                              reviewed_at=?,note=?,version=version+1 WHERE id=?""",
                             (state_hash, json.dumps(parts, sort_keys=True), user["id"], utcnow(), note, review["id"]))
                review_id = review["id"]
            else:
                review_id = conn.execute("""INSERT INTO material_reviews(production_batch_id,material_code,
                           state_hash,state_parts_json,reviewed_by,reviewed_at,note) VALUES(?,?,?,?,?,?,?)""",
                          (batch_id, code, state_hash, json.dumps(parts, sort_keys=True), user["id"], utcnow(), note)).lastrowid
            audit(conn, "material_reviews", review_id, "check", user["id"], review,
                  one(conn, "SELECT * FROM material_reviews WHERE id=?", (review_id,)), note)
        material["review"] = material_review_view(conn, batch, material)
        return material


@app.patch("/api/bom-items/prices")
def patch_batch_bom_prices(data: dict, user: dict = Depends(require_user)):
    try:
        batch_id = int(need(data, "production_batch_id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "生产批次 ID 无效")
    updates = data.get("items")
    if not isinstance(updates, list) or not 0 < len(updates) <= 2000:
        raise HTTPException(422, "请选择 1 至 2000 条 BOM 价格行")
    reason = str(data.get("reason") or "").strip()
    if not reason:
        raise HTTPException(422, "修改价格必须填写原因")
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        if not batch["bom_locked"]:
            raise HTTPException(409, "须先锁定本批 BOM")
        seen = set()
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("id"), int) or not isinstance(update.get("version"), int):
                raise HTTPException(422, "价格行须包含整数 id 和 version")
            row_id = update["id"]
            if row_id in seen or "unit_price" not in update:
                raise HTTPException(422, "价格行重复或缺少单价")
            seen.add(row_id)
            item = must_get(conn, "bom_items", row_id)
            if item["production_batch_id"] != batch_id:
                raise HTTPException(422, "价格行不属于此生产批次")
            patch_record(conn, "bom_items", row_id, update["version"],
                         {"unit_price": unit_price(update["unit_price"])}, user["id"], reason)
        items = rows(conn, """SELECT i.*,b.board_ref,b.board_name,b.planned_quantity AS board_planned_quantity,
                      b.quantity_per_product AS board_quantity_per_product FROM bom_items i
                      LEFT JOIN batch_boards b ON b.id=i.batch_board_id
                      WHERE i.production_batch_id=? ORDER BY i.id""", (batch_id,))
        return {"items": items, "bom_pricing": bom_pricing(items, batch["quantity"], batch["board_schedule_mode"])}


@app.patch("/api/bom-items/{item_id}")
def patch_bom_item(item_id: int, data: dict, user: dict = Depends(require_user)):
    changes = normalize_changes(data.get("changes", {}), {"loss_rate", "unit_price"},
                                protected={"loss_rate", "unit_price"}, reason=data.get("reason"))
    if "unit_price" in changes:
        changes["unit_price"] = unit_price(changes["unit_price"])
    with writing() as conn:
        item = must_get(conn, "bom_items", item_id)
        if "loss_rate" in changes and dec(changes["loss_rate"]) > Decimal(1):
            raise HTTPException(422, "损耗率按小数填写，例如 0.05 表示 5%")
        result = patch_record(conn, "bom_items", item_id, int(need(data, "version")), changes, user["id"], data.get("reason"))
        sync_material_ready(conn, item["production_batch_id"])
        return result


@app.post("/api/inventory/snapshots")
def create_inventory_snapshot(data: dict, user: dict = Depends(require_user)):
    raw_as_of = str(need(data, "as_of"))
    day_end = len(raw_as_of) == 10
    as_of = valid_datetime(raw_as_of + "T23:59:59" if day_end else raw_as_of)
    basis = need(data, "basis")
    if basis not in {"on_hand", "available"}:
        raise HTTPException(422, "库存基数需明确为 on_hand 或 available")
    stock_rows = data.get("items")
    if not isinstance(stock_rows, list) or not stock_rows:
        raise HTTPException(422, "库存快照不能为空")
    with writing() as conn:
        cur = conn.execute("INSERT INTO inventory_snapshots(as_of,as_of_precision,basis,source_name,created_by,created_at) VALUES(?,?,?,?,?,?)",
                           (as_of, "day_end" if day_end else "timestamp", basis, data.get("source_name"), user["id"], utcnow()))
        snapshot_id = cur.lastrowid
        for item in stock_rows:
            conn.execute("INSERT INTO inventory_rows(snapshot_id,material_code,quantity) VALUES(?,?,?)",
                         (snapshot_id, str(need(item, "material_code")), ds(dec(need(item, "quantity")))))
        result = {"id": snapshot_id, "as_of": as_of, "as_of_precision": "day_end" if day_end else "timestamp", "basis": basis, "items": stock_rows,
                  "reconciliation": [stock_capacity(conn, str(i["material_code"])) for i in stock_rows]}
        for item in stock_rows:
            sync_ready_for_material(conn, str(item["material_code"]))
        audit(conn, "inventory_snapshot", snapshot_id, "create", user["id"], None, result)
        return result


@app.get("/api/inventory")
def inventory(user: dict = Depends(require_user)):
    with reading() as conn:
        codes = {r["material_code"] for r in rows(conn, "SELECT material_code FROM inventory_rows UNION SELECT material_code FROM receipts UNION SELECT stock_material_code AS material_code FROM reservations")}
        return [stock_capacity(conn, code) for code in sorted(codes)]


@app.put("/api/reservations")
def put_reservation(data: dict, user: dict = Depends(require_user)):
    batch_id = int(need(data, "production_batch_id"))
    original = str(data.get("for_material_code") or need(data, "material_code"))
    actual = str(need(data, "material_code"))
    quantity = dec(need(data, "quantity"))
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        if not batch["bom_locked"]:
            raise HTTPException(409, "须先锁定 BOM")
        required = {i["material_code"]: i["required"] for i in required_materials(conn, batch_id)}
        if original not in required:
            raise HTTPException(422, "物料不在此批次锁定 BOM 中")
        old = one(conn, "SELECT * FROM reservations WHERE production_batch_id=? AND for_material_code=? AND stock_material_code=?", (batch_id, original, actual))
        if old and data.get("version") != old["version"]:
            raise HTTPException(409, "预留已被其他人修改")
        if actual != original:
            limit = approved_substitute_limit(conn, batch_id, original, actual)
            if quantity + (dec(old["issued_quantity"]) if old else Decimal(0)) > limit:
                raise HTTPException(409, "替代料超过工程批准数量")
        same_need = sum((dec(r["quantity"]) + dec(r["issued_quantity"]) for r in rows(conn, "SELECT quantity,issued_quantity FROM reservations WHERE production_batch_id=? AND for_material_code=? AND stock_material_code<>?", (batch_id, original, actual))), Decimal(0))
        if same_need + quantity + (dec(old["issued_quantity"]) if old else Decimal(0)) > required[original]:
            raise HTTPException(409, "预留数量超过该 BOM 物料需求")
        capacity = stock_capacity(conn, actual)
        if dec(capacity["same_day_issue_unresolved"]) > 0:
            raise HTTPException(409, "快照当日有时间不明的发料，请补录准确时间或导入下一次库存快照后再预留")
        other_reserved = dec(capacity["reserved"]) - (dec(old["quantity"]) if old else Decimal(0))
        if other_reserved + quantity > Decimal(capacity["capacity"]):
            if capacity["confirmation_required"]:
                raise HTTPException(409, "现存量未经仓库核实，不能作为可预留量；请提交 basis=available 的可用量快照")
            raise HTTPException(409, "库存基数扣除其他订单预留后不足；工作台预留不等于 KIS 锁库")
        if old:
            result = patch_record(conn, "reservations", old["id"], old["version"], {"quantity": ds(quantity)}, user["id"], data.get("reason"))
        else:
            cur = conn.execute("INSERT INTO reservations(production_batch_id,for_material_code,stock_material_code,quantity,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                               (batch_id, original, actual, ds(quantity), utcnow(), utcnow()))
            result = must_get(conn, "reservations", cur.lastrowid)
            audit(conn, "reservations", result["id"], "create", user["id"], None, result)
        result["stock"] = stock_capacity(conn, actual)
        sync_ready_for_material(conn, actual)
        sync_material_ready(conn, batch_id)
        return result


@app.post("/api/receipts")
def create_receipt(data: dict, user: dict = Depends(require_user)):
    quantity = dec(need(data, "quantity"), allow_zero=False)
    received_at = valid_datetime(need(data, "received_at"))
    with writing() as conn:
        batch_id = data.get("production_batch_id")
        purchase_id = data.get("purchase_id")
        if batch_id is not None:
            must_get(conn, "production_batches", int(batch_id))
        if purchase_id is not None:
            purchase = must_get(conn, "purchases", int(purchase_id))
            if purchase["material_code"] != str(need(data, "material_code")):
                raise HTTPException(422, "到料物料与采购行不符")
        cur = conn.execute("INSERT INTO receipts(material_code,quantity,received_at,source_ref,source_line_no,purchase_id,production_batch_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (str(need(data, "material_code")), ds(quantity), received_at, data.get("source_ref"),
                            data.get("source_line_no"), purchase_id, batch_id, user["id"], utcnow()))
        result = must_get(conn, "receipts", cur.lastrowid)
        audit(conn, "receipts", result["id"], "create", user["id"], None, result)
        return result


@app.get("/api/receipts")
def get_receipts(user: dict = Depends(require_user)):
    with reading() as conn:
        return rows(conn, "SELECT * FROM receipts ORDER BY received_at DESC,id DESC")


@app.patch("/api/receipts/{receipt_id}/iqc")
def patch_iqc(receipt_id: int, data: dict, user: dict = Depends(require_user)):
    status = need(data, "status")
    if status not in {"pass", "fail"}:
        raise HTTPException(422, "IQC 应为 pass 或 fail")
    with writing() as conn:
        receipt = must_get(conn, "receipts", receipt_id)
        accepted = dec(data.get("accepted_quantity", receipt["quantity"] if status == "pass" else 0))
        if accepted > dec(receipt["quantity"]) or (status == "fail" and accepted != 0):
            raise HTTPException(422, "合格数量不能超过实收量；不合格时应为 0")
        result = patch_record(conn, "receipts", receipt_id, int(need(data, "version")),
                              {"iqc_status": status, "accepted_quantity": ds(accepted)}, user["id"], data.get("reason"))
        sync_ready_for_material(conn, receipt["material_code"])
        return result


@app.post("/api/purchases")
def create_purchase(data: dict, user: dict = Depends(require_user)):
    quantity = dec(need(data, "quantity"), allow_zero=False)
    material_code = str(need(data, "material_code"))
    eta = valid_date(data.get("eta"))
    eta_is_estimate = data.get("eta_is_estimate", True)
    if not isinstance(eta_is_estimate, bool) or (not eta_is_estimate and not eta):
        raise HTTPException(422, "确认 ETA 需填写日期；估算标记应为布尔值")
    with writing() as conn:
        cur = conn.execute("INSERT INTO purchases(reference,source_line_no,material_code,quantity,eta,eta_is_estimate,eta_source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (data.get("reference"), data.get("source_line_no"), material_code, ds(quantity),
                            eta, int(eta_is_estimate), "manual", utcnow(), utcnow()))
        result = must_get(conn, "purchases", cur.lastrowid)
        if data.get("production_batch_id"):
            insert_purchase_allocation(conn, result, int(data["production_batch_id"]), quantity, user["id"])
        audit(conn, "purchases", result["id"], "create", user["id"], None, result)
        result["allocations"] = rows(conn, "SELECT * FROM purchase_allocations WHERE purchase_id=?", (result["id"],))
        return result


def insert_purchase_allocation(conn: sqlite3.Connection, purchase: dict, batch_id: int,
                               quantity: Decimal, actor_id: int) -> dict:
    batch = must_get(conn, "production_batches", batch_id)
    if purchase["material_code"] not in {r["material_code"] for r in required_materials(conn, batch_id)}:
        raise HTTPException(422, "采购物料不在生产批次锁定 BOM 中")
    allocated = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM purchase_allocations WHERE purchase_id=?", (purchase["id"],))), Decimal(0))
    if allocated + quantity > dec(purchase["quantity"]):
        raise HTTPException(409, "采购行已分配数量超过采购数量")
    cur = conn.execute("INSERT INTO purchase_allocations(purchase_id,production_batch_id,material_code,quantity) VALUES(?,?,?,?)",
                       (purchase["id"], batch_id, purchase["material_code"], ds(quantity)))
    result = must_get(conn, "purchase_allocations", cur.lastrowid)
    audit(conn, "purchase_allocations", result["id"], "create", actor_id, None, result)
    return result


@app.post("/api/purchase-allocations")
def create_purchase_allocation(data: dict, user: dict = Depends(require_user)):
    with writing() as conn:
        purchase = must_get(conn, "purchases", int(need(data, "purchase_id")))
        return insert_purchase_allocation(conn, purchase, int(need(data, "production_batch_id")),
                                          dec(need(data, "quantity"), allow_zero=False), user["id"])


@app.get("/api/purchases")
def get_purchases(user: dict = Depends(require_user)):
    with reading() as conn:
        result = rows(conn, "SELECT * FROM purchases ORDER BY id DESC")
        for item in result:
            item["allocations"] = rows(conn, "SELECT * FROM purchase_allocations WHERE purchase_id=?", (item["id"],))
        return result


@app.patch("/api/purchases/{purchase_id}")
def patch_purchase(purchase_id: int, data: dict, user: dict = Depends(require_user)):
    changes = normalize_changes(data.get("changes", {}), {"reference", "quantity", "eta", "eta_is_estimate"},
                                protected={"quantity", "eta", "eta_is_estimate"}, reason=data.get("reason"))
    with writing() as conn:
        purchase = must_get(conn, "purchases", purchase_id)
        if "eta_is_estimate" in changes:
            if not isinstance(changes["eta_is_estimate"], bool):
                raise HTTPException(422, "估算标记应为布尔值")
            changes["eta_is_estimate"] = int(changes["eta_is_estimate"])
        elif "eta" in changes:
            changes["eta_is_estimate"] = 1
        if "eta" in changes:
            changes["eta_source"] = "manual"
        if not changes.get("eta_is_estimate", purchase["eta_is_estimate"]) and not changes.get("eta", purchase["eta"]):
            raise HTTPException(422, "确认 ETA 需填写日期")
        if "quantity" in changes:
            allocated = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM purchase_allocations WHERE purchase_id=?", (purchase_id,))), Decimal(0))
            if dec(changes["quantity"]) < allocated:
                raise HTTPException(409, "采购数量不能小于已分配数量")
        return patch_record(conn, "purchases", purchase_id, int(need(data, "version")), changes, user["id"], data.get("reason"))


@app.post("/api/substitutes")
def request_substitute(data: dict, user: dict = Depends(require_user)):
    batch_id = int(need(data, "production_batch_id"))
    original = str(need(data, "original_material_code"))
    substitute = str(need(data, "substitute_material_code"))
    if original == substitute:
        raise HTTPException(422, "替代料编码应不同于原料")
    reason = str(need(data, "reason")).strip()
    with writing() as conn:
        required = {r["material_code"]: r["required"] for r in required_materials(conn, batch_id)}
        if original not in required:
            raise HTTPException(422, "原料不在锁定 BOM 中")
        quantity = dec(need(data, "quantity"), allow_zero=False)
        if quantity > required[original]:
            raise HTTPException(422, "替代数量超过该物料需求")
        cur = conn.execute("INSERT INTO substitutes(production_batch_id,original_material_code,substitute_material_code,quantity,request_reason,created_at) VALUES(?,?,?,?,?,?)",
                           (batch_id, original, substitute, ds(quantity), reason, utcnow()))
        result = must_get(conn, "substitutes", cur.lastrowid)
        audit(conn, "substitutes", result["id"], "request", user["id"], None, result, reason)
        return result


@app.get("/api/substitutes")
def get_substitutes(user: dict = Depends(require_user)):
    with reading() as conn:
        return rows(conn, "SELECT * FROM substitutes ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,id DESC")


@app.post("/api/substitutes/{substitute_id}/approve")
def approve_substitute(substitute_id: int, data: dict, user: dict = Depends(require_user)):
    if user["role"] != "engineering":
        raise HTTPException(403, "仅工程账号可批准替代料")
    if not isinstance(data.get("approve"), bool):
        raise HTTPException(422, "approve 应为布尔值")
    with writing() as conn:
        item = must_get(conn, "substitutes", substitute_id)
        if item["status"] != "pending":
            raise HTTPException(409, "替代料已审批，不可覆盖历史")
        result = patch_record(conn, "substitutes", substitute_id, int(need(data, "version")),
                              {"status": "approved" if data["approve"] else "rejected",
                               "approved_by": user["id"], "approved_at": utcnow()}, user["id"], data.get("reason"))
        sync_material_ready(conn, item["production_batch_id"])
        return result


@app.get("/api/vendors/conflicts")
def get_vendor_conflicts(user: dict = Depends(require_user)):
    with reading() as conn:
        return [{"batch": batch, "conflicts": conflicts,
                 "acknowledged": (all(item["source_acknowledged"] for item in conflicts)
                                  if batch["board_schedule_mode"] == "independent" else
                                  batch_conflicts_acknowledged(batch, conflicts))}
                for batch in rows(conn, "SELECT * FROM production_batches")
                if (conflicts := vendor_conflicts(conn, batch))]


@app.post("/api/vendors/conflicts/{batch_id}/acknowledge")
def acknowledge_conflict(batch_id: int, data: dict, user: dict = Depends(require_user)):
    reason = str(need(data, "reason")).strip()
    if not reason:
        raise HTTPException(422, "请填写外协厂能够并行生产的确认依据")
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        if batch["board_schedule_mode"] == "independent":
            raise HTTPException(409, "逐板排期请分别确认各板外协厂档期重叠")
        if not vendor_conflicts(conn, batch):
            raise HTTPException(409, "当前没有重叠档期")
        return patch_record(conn, "production_batches", batch_id, batch["version"],
                            {"conflict_ack_reason": reason, "conflict_ack_by": user["id"],
                             "conflict_ack_at": utcnow(),
                             "conflict_ack_signature": vendor_conflict_signature(vendor_conflicts(conn, batch))},
                            user["id"], reason)


def stage_view(conn: sqlite3.Connection, stage: dict) -> dict:
    owner = (one(conn, "SELECT display_name,active,status FROM users WHERE id=?", (stage["owner_id"],))
             if stage.get("owner_id") else None)
    completer = (one(conn, "SELECT display_name FROM users WHERE id=?", (stage["completed_by"],))
                 if stage.get("completed_by") else None)
    result = dict(stage)
    result["owner_display_name"] = owner["display_name"] if owner else stage.get("owner_name")
    result["owner_binding_status"] = (
        "bound" if owner and owner["active"] and owner["status"] == "active" else
        "inactive" if owner else "legacy_unbound" if stage.get("owner_name") and stage.get("legacy_source_index") is not None else
        "unbound" if stage.get("owner_name") else "missing"
    )
    result["completed_by_name"] = completer["display_name"] if completer else None
    result["is_complete"] = stage.get("status") == "done" and bool(stage.get("actual_finish"))
    return result


def stage_views(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    return [stage_view(conn, stage) for stage in rows(
        conn, "SELECT * FROM stages WHERE production_batch_id=? ORDER BY sequence", (batch_id,))]


@app.get("/api/production-batches/{batch_id}/stages")
def get_stages(batch_id: int, user: dict = Depends(require_user)):
    with reading() as conn:
        must_get(conn, "production_batches", batch_id)
        return stage_views(conn, batch_id)


@app.get("/api/stage-progress")
def get_stage_progress(user: dict = Depends(require_user)):
    """Summarize all batch stage chains with two queries for project lists."""
    with reading() as conn:
        progress = {row["id"]: {"production_batch_id": row["id"], "total": 0,
                                "completed": 0, "current_name": None, "current_status": None}
                    for row in rows(conn, "SELECT id FROM production_batches ORDER BY id")}
        for stage in rows(conn, """SELECT production_batch_id,name,status,actual_finish FROM stages
                           ORDER BY production_batch_id,sequence,id"""):
            if stage["status"] == "skipped":
                continue
            item = progress[stage["production_batch_id"]]
            item["total"] += 1
            if stage["status"] == "done" and stage["actual_finish"]:
                item["completed"] += 1
            elif item["current_name"] is None:
                item["current_name"] = stage["name"]
                item["current_status"] = stage["status"] or "pending"
        return list(progress.values())


@app.post("/api/production-batches/{batch_id}/generate-stages")
def regenerate_stages(batch_id: int, data: dict, user: dict = Depends(require_user)):
    if not str(data.get("reason", "")).strip():
        raise HTTPException(422, "重新生成工序计划需填写原因")
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        if batch["version"] != int(need(data, "version")):
            raise HTTPException(409, "生产批次已被修改")
        result = generate_stages(conn, batch_id, user["id"], replace=True)
        conn.execute("UPDATE production_batches SET version=version+1,updated_at=? WHERE id=?", (utcnow(), batch_id))
        audit(conn, "production_batches", batch_id, "regenerate_stages", user["id"], None, result, data["reason"])
        return result


@app.post("/api/production-batches/{batch_id}/stages")
def create_stage(batch_id: int, data: dict, user: dict = Depends(require_user)):
    name = str(need(data, "name")).strip()
    if len(name) > 100:
        raise HTTPException(422, "节点名称不能超过 100 字")
    stage_type = data.get("stage_type", "normal")
    if stage_type not in {"normal", "milestone"}:
        raise HTTPException(422, "节点类型无效")
    duration = checked_stage_duration(data.get("duration_days", "0"))
    owner_name = str(data.get("owner_name") or "").strip() or None
    note = str(data.get("note") or "").strip() or None
    if owner_name and len(owner_name) > 100 or note and len(note) > 1000:
        raise HTTPException(422, "负责人或备注过长")
    status = data.get("status")
    if status not in {None, "pending", "in_progress", "done", "skipped"}:
        raise HTTPException(422, "节点状态无效")
    actual_finish = valid_date(data.get("actual_finish"))
    if actual_finish:
        status = "done"
    if status == "done":
        actual_finish = actual_finish or today()
        if actual_finish > today():
            raise HTTPException(422, "实际完成日不能晚于今天")
    with writing() as conn:
        must_get(conn, "production_batches", batch_id)
        owner_id = active_owner_id(conn, data["owner_id"]) if data.get("owner_id") not in (None, "") else None
        if status == "done" and owner_id is None:
            raise HTTPException(422, "请先绑定节点责任人账号，再勾选完成")
        sequence = (one(conn, "SELECT MAX(sequence) AS maximum FROM stages WHERE production_batch_id=?", (batch_id,))["maximum"] or 0) + 1
        cur = conn.execute("""
            INSERT INTO stages(production_batch_id,sequence,name,planned_start,planned_finish,
                               actual_start,actual_finish,status,owner_name,owner_id,completed_by,completed_at,
                               stage_type,duration_days,
                               actual_material_at,note,plan_overridden,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (batch_id, sequence, name, valid_date(data.get("planned_start")), valid_date(data.get("planned_finish")),
                  valid_date(data.get("actual_start")), actual_finish, status,
                  owner_name, owner_id, user["id"] if status == "done" else None,
                  utcnow() if status == "done" else None,
                  stage_type, duration, valid_date(data.get("actual_material_at")), note,
                  int(bool(data.get("planned_start") or data.get("planned_finish"))), utcnow(), utcnow()))
        result = must_get(conn, "stages", cur.lastrowid)
        audit(conn, "stages", result["id"], "create", user["id"], None, result)
        replan_batch_stages(conn, batch_id, user["id"], "新增节点后更新排期")
        return stage_view(conn, must_get(conn, "stages", result["id"]))


@app.patch("/api/stages/{stage_id}")
def patch_stage(stage_id: int, data: dict, user: dict = Depends(require_user)):
    changes = normalize_changes(data.get("changes", {}),
                                {"name", "owner_name", "owner_id", "stage_type", "duration_days", "actual_material_at", "note",
                                 "planned_start", "planned_finish", "actual_start", "actual_finish", "status", "plan_overridden"},
                                reason=data.get("reason"))
    with writing() as conn:
        stage = must_get(conn, "stages", stage_id)
        version = int(need(data, "version"))
        if stage["version"] != version:
            raise HTTPException(409, "记录已被其他人修改，请刷新后重试")
        if "name" in changes:
            changes["name"] = str(changes["name"] or "").strip()
            if not changes["name"] or len(changes["name"]) > 100:
                raise HTTPException(422, "节点名称不能为空或超过 100 字")
        if "owner_name" in changes:
            changes["owner_name"] = str(changes["owner_name"] or "").strip() or None
            if changes["owner_name"] and len(changes["owner_name"]) > 100:
                raise HTTPException(422, "负责人不能超过 100 字")
            if "owner_id" not in changes:
                changes["owner_id"] = None  # A newly typed name is not an approved account binding.
        if "owner_id" in changes:
            changes["owner_id"] = (active_owner_id(conn, changes["owner_id"])
                                   if changes["owner_id"] not in (None, "") else None)
        if stage["status"] == "done" and "owner_id" in changes and changes["owner_id"] != stage["owner_id"]:
            if not str(data.get("reason") or "").strip():
                raise HTTPException(422, "更换已完成节点的责任人须填写原因")
        if "note" in changes:
            changes["note"] = str(changes["note"] or "").strip() or None
            if changes["note"] and len(changes["note"]) > 1000:
                raise HTTPException(422, "备注不能超过 1000 字")
        if "stage_type" in changes and changes["stage_type"] not in {"normal", "milestone"}:
            raise HTTPException(422, "节点类型无效")
        if "duration_days" in changes:
            changes["duration_days"] = checked_stage_duration(changes["duration_days"])
        for key in ("actual_material_at", "planned_start", "planned_finish", "actual_start", "actual_finish"):
            if key in changes:
                changes[key] = valid_date(changes[key])
        if "status" in changes and changes["status"] not in {None, "pending", "in_progress", "done", "skipped"}:
            raise HTTPException(422, "节点状态无效")
        if "actual_finish" in changes and changes["actual_finish"] and changes.get("status") not in {None, "done"}:
            raise HTTPException(422, "填写实际完成日时，节点状态必须为已完成")
        if "actual_finish" in changes and changes["actual_finish"]:
            changes["status"] = "done"
        elif "actual_finish" in changes and changes["actual_finish"] is None and stage["status"] == "done" and "status" not in changes:
            changes["status"] = "in_progress" if stage["actual_start"] else "pending"
        resulting_status = changes.get("status", stage["status"])
        if (resulting_status == "done" and stage["owner_id"] and
                "owner_id" in changes and changes["owner_id"] is None):
            raise HTTPException(422, "已完成节点须保留责任人账号；请先撤销完成状态")
        if resulting_status == "done" and ({"status", "actual_finish"} & set(changes)):
            if not changes.get("owner_id", stage["owner_id"]):
                raise HTTPException(422, "请先绑定节点责任人账号，再勾选完成")
            active_owner_id(conn, changes.get("owner_id", stage["owner_id"]))
            finish_date = changes.get("actual_finish", stage["actual_finish"]) or today()
            if finish_date > today():
                raise HTTPException(422, "实际完成日不能晚于今天")
            if changes.get("actual_start", stage["actual_start"]) and finish_date < changes.get("actual_start", stage["actual_start"]):
                raise HTTPException(422, "实际完成日不能早于实际开始日")
            changes["actual_finish"] = finish_date
            if stage["status"] != "done" or not stage["actual_finish"]:
                changes["completed_by"] = user["id"]
                changes["completed_at"] = utcnow()
            elif finish_date != stage["actual_finish"] and not str(data.get("reason") or "").strip():
                raise HTTPException(422, "修改实际完成日须填写原因")
        elif resulting_status != "done" and stage["status"] == "done" and ({"status", "actual_finish"} & set(changes)):
            if not str(data.get("reason") or "").strip():
                raise HTTPException(422, "撤销节点完成状态须填写原因")
            changes.update(actual_finish=None, completed_by=None, completed_at=None)
        if "plan_overridden" in changes and not isinstance(changes["plan_overridden"], bool):
            raise HTTPException(422, "手工计划标记应为布尔值")
        if "planned_start" in changes or "planned_finish" in changes:
            changes["plan_overridden"] = 1
        elif "plan_overridden" in changes:
            changes["plan_overridden"] = int(changes["plan_overridden"])
        start = changes.get("planned_start", stage["planned_start"])
        finish = changes.get("planned_finish", stage["planned_finish"])
        if start and finish and finish < start:
            raise HTTPException(422, "计划完工不能早于计划开始")
        result = patch_record(conn, "stages", stage_id, version, changes, user["id"], data.get("reason"))
        if set(changes) & {"duration_days", "stage_type", "actual_material_at", "planned_start", "planned_finish", "plan_overridden"}:
            replan_batch_stages(conn, stage["production_batch_id"], user["id"], "节点变更后更新后续排期")
            result = must_get(conn, "stages", stage_id)
        return stage_view(conn, result)


@app.post("/api/stages/{stage_id}/completion")
def set_stage_completion(stage_id: int, data: dict, user: dict = Depends(require_user)):
    """Record a checkbox action with an actual finish date and the acting user."""
    if type(data.get("completed")) is not bool:
        raise HTTPException(422, "completed 须为布尔值")
    completed = data["completed"]
    reason = str(data.get("reason") or "").strip() or None
    with writing() as conn:
        stage = must_get(conn, "stages", stage_id)
        version = int(need(data, "version"))
        if stage["version"] != version:
            raise HTTPException(409, "记录已被其他人修改，请刷新后重试")
        was_complete = stage["status"] == "done" and bool(stage["actual_finish"])
        if completed:
            if not stage["owner_id"]:
                raise HTTPException(422, "请先绑定节点责任人账号，再勾选完成")
            active_owner_id(conn, stage["owner_id"])
            finish_date = valid_date(data.get("actual_finish")) or (stage["actual_finish"] if was_complete else today())
            if finish_date > today():
                raise HTTPException(422, "实际完成日不能晚于今天")
            if stage["actual_start"] and finish_date < stage["actual_start"]:
                raise HTTPException(422, "实际完成日不能早于实际开始日")
            if was_complete and finish_date == stage["actual_finish"]:
                return stage_view(conn, stage)
            if was_complete and not reason:
                raise HTTPException(422, "修改实际完成日须填写原因")
            changes = {"status": "done", "actual_finish": finish_date}
            if not was_complete:
                changes.update(completed_by=user["id"], completed_at=utcnow())
            action = "complete" if not was_complete else "correct_completion"
        else:
            if not was_complete and stage["status"] != "done":
                return stage_view(conn, stage)
            if not reason:
                raise HTTPException(422, "撤销节点完成状态须填写原因")
            changes = {"status": "in_progress" if stage["actual_start"] else "pending",
                       "actual_finish": None, "completed_by": None, "completed_at": None}
            action = "reopen"
        update = dict(changes, version=version + 1, updated_at=utcnow())
        clause = ",".join(f"{key}=?" for key in update)
        conn.execute(f"UPDATE stages SET {clause} WHERE id=? AND version=?",
                     (*update.values(), stage_id, version))
        result = must_get(conn, "stages", stage_id)
        audit(conn, "stages", stage_id, action, user["id"], stage, result, reason)
        return stage_view(conn, result)


@app.delete("/api/stages/{stage_id}")
def delete_stage(stage_id: int, data: dict | None = None, user: dict = Depends(require_user)):
    with writing() as conn:
        stage = must_get(conn, "stages", stage_id)
        reason = str((data or {}).get("reason") or "").strip()
        if (stage["actual_start"] or stage["actual_finish"] or stage["actual_material_at"]) and not reason:
            raise HTTPException(422, "删除已有实际记录的节点须填写原因")
        conn.execute("DELETE FROM stages WHERE id=?", (stage_id,))
        audit(conn, "stages", stage_id, "delete", user["id"], stage, None, reason)
        replan_batch_stages(conn, stage["production_batch_id"], user["id"], "删除节点后更新排期")
        return {"deleted": stage_id}


@app.put("/api/production-batches/{batch_id}/stages/reorder")
def reorder_stages(batch_id: int, data: dict, user: dict = Depends(require_user)):
    wanted = data.get("stage_ids")
    with writing() as conn:
        must_get(conn, "production_batches", batch_id)
        old = rows(conn, "SELECT * FROM stages WHERE production_batch_id=? ORDER BY sequence", (batch_id,))
        current_ids = [stage["id"] for stage in old]
        if not isinstance(wanted, list) or any(type(item) is not int for item in wanted) or sorted(wanted) != sorted(current_ids):
            raise HTTPException(422, "排序须包含该批次全部节点且不能重复")
        for sequence, stage_id in enumerate(wanted, 1):
            conn.execute("UPDATE stages SET sequence=?,version=version+1,updated_at=? WHERE id=?",
                         (sequence, utcnow(), stage_id))
        audit(conn, "production_batches", batch_id, "reorder_stages", user["id"], current_ids, wanted,
              data.get("reason"))
        return replan_batch_stages(conn, batch_id, user["id"], "节点排序变化后更新排期")


@app.post("/api/production-batches/{batch_id}/events")
def production_event(batch_id: int, data: dict, user: dict = Depends(require_user)):
    kind = need(data, "kind")
    if kind not in {"material_sent", "vendor_received", "finished", "returned", "direct_shipped"}:
        raise HTTPException(422, "无效生产物流事件")
    event_date = valid_date(need(data, "event_date"), required=True)
    event_at = valid_datetime(data["event_at"]) if data.get("event_at") else None
    quantity = dec(data["quantity"], allow_zero=False) if data.get("quantity") is not None else None
    with writing() as conn:
        batch = must_get(conn, "production_batches", batch_id)
        if kind == "finished":
            already = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM events WHERE entity_type='production_batch' AND entity_id=? AND kind='finished'", (batch_id,))), Decimal(0))
            quantity = quantity if quantity is not None else dec(batch["quantity"]) - already
            if quantity <= 0 or already + quantity > dec(batch["quantity"]):
                raise HTTPException(409, "累计完工数量不能超过生产批次数量")
            if batch["board_schedule_mode"] == "independent":
                boards = rows(conn, "SELECT * FROM batch_boards WHERE production_batch_id=?", (batch_id,))
                if not boards or not batch["board_set_confirmed_at"] or not batch["bom_locked"]:
                    raise HTTPException(409, "逐板排期须先确认完整板卡组成并锁定全部板卡 BOM")
                missing = [board["board_name"] for board in boards
                           if board_finished_quantity(conn, board["id"]) < dec(batch["quantity"]) * dec(board["quantity_per_product"])]
                if missing:
                    raise HTTPException(409, f"必要板卡尚未足额完工：{'、'.join(missing)}")
                board_finish_dates = [board_finish_forecast(conn, board["id"])["formal_date"] for board in boards]
                if event_date < max(board_finish_dates):
                    raise HTTPException(409, "整批实际完工日不能早于最晚必要板卡实际完工日")
        actual = data.get("material_code")
        original = data.get("for_material_code") or actual
        if kind == "material_sent":
            if not actual or quantity is None:
                raise HTTPException(422, "发料须写明物料和数量")
            reservation = one(conn, "SELECT * FROM reservations WHERE production_batch_id=? AND for_material_code=? AND stock_material_code=?",
                              (batch_id, original, actual))
            if not reservation or dec(reservation["quantity"]) < quantity:
                raise HTTPException(409, "发料不能超过当前在库预留量")
            patch_record(conn, "reservations", reservation["id"], reservation["version"],
                         {"quantity": ds(dec(reservation["quantity"]) - quantity),
                          "issued_quantity": ds(dec(reservation["issued_quantity"]) + quantity)},
                         user["id"], data.get("note"))
        cur = conn.execute("INSERT INTO events(entity_type,entity_id,kind,event_date,event_at,quantity,material_code,for_material_code,note,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           ("production_batch", batch_id, kind, event_date, event_at, ds(quantity) if quantity is not None else None,
                            actual, original, data.get("note"), user["id"], utcnow()))
        result = one(conn, "SELECT * FROM events WHERE id=?", (cur.lastrowid,))
        audit(conn, "events", result["id"], "create", user["id"], None, result)
        sync_order_status(conn, must_get(conn, "order_lines", batch["order_line_id"])["order_id"])
        return result


@app.post("/api/delivery-batches/{batch_id}/events")
def delivery_event(batch_id: int, data: dict, user: dict = Depends(require_user)):
    with writing() as conn:
        return record_delivery_event(conn, batch_id, data, user["id"])


def record_delivery_event(conn: sqlite3.Connection, batch_id: int, data: dict, actor_id: int) -> dict:
    kind = need(data, "kind")
    if kind not in {"shipped", "customer_received"}:
        raise HTTPException(422, "无效交付事件")
    quantity = dec(need(data, "quantity"), allow_zero=False)
    batch = must_get(conn, "delivery_batches", batch_id)
    actual = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM events WHERE entity_type='delivery_batch' AND entity_id=? AND kind=?", (batch_id, kind))), Decimal(0))
    if actual + quantity > dec(batch["quantity"]):
        raise HTTPException(409, "累计交付数量超过交付批次数量")
    cur = conn.execute("INSERT INTO events(entity_type,entity_id,kind,event_date,quantity,note,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       ("delivery_batch", batch_id, kind, valid_date(need(data, "event_date"), required=True),
                        ds(quantity), data.get("note"), actor_id, utcnow()))
    result = one(conn, "SELECT * FROM events WHERE id=?", (cur.lastrowid,))
    audit(conn, "events", result["id"], "create", actor_id, None, result)
    sync_order_status(conn, must_get(conn, "order_lines", batch["order_line_id"])["order_id"])
    return result


def sync_order_status(conn: sqlite3.Connection, order_id: int) -> None:
    order = must_get(conn, "orders", order_id)
    lines = rows(conn, "SELECT * FROM order_lines WHERE order_id=?", (order_id,))
    complete = bool(lines)
    for line in lines:
        phase_batches = rows(conn, "SELECT * FROM production_batches WHERE order_line_id=?", (line["id"],))
        if phase_batches and all(batch["phase"] == "rd" for batch in phase_batches):
            if any(not batch["debug_accepted_at"] or
                   (batch["board_schedule_mode"] == "independent" and
                    batch_finish_forecast(conn, batch["id"])["status"] != "complete")
                   for batch in phase_batches):
                complete = False
            continue
        deliveries = rows(conn, "SELECT * FROM delivery_batches WHERE order_line_id=?", (line["id"],))
        if not deliveries or sum((dec(d["quantity"]) for d in deliveries), Decimal(0)) != dec(line["quantity"]):
            complete = False
            break
        if any(delivery_forecast(conn, delivery)["status"] != "complete" for delivery in deliveries):
            complete = False
            break
    status = "complete" if complete else "open"
    if order["status"] != status:
        conn.execute("UPDATE orders SET status=?,version=version+1,updated_at=? WHERE id=?", (status, utcnow(), order_id))
        audit(conn, "orders", order_id, "auto_status", None, order, must_get(conn, "orders", order_id),
              "全部交付批次按选定口径足额完成" if complete else "交付批次数量或完成状态发生变化")


def board_finished_quantity(conn: sqlite3.Connection, board_id: int) -> Decimal:
    return sum((dec(event["quantity"]) for event in rows(
        conn, "SELECT quantity FROM events WHERE entity_type='batch_board' AND entity_id=? AND kind='finished'",
        (board_id,))), Decimal(0))


def board_finish_forecast(conn: sqlite3.Connection, board_id: int,
                          materials: dict | None = None) -> dict:
    board = must_get(conn, "batch_boards", board_id)
    batch = must_get(conn, "production_batches", board["production_batch_id"])
    required = dec(batch["quantity"]) * dec(board["quantity_per_product"])
    base = {"formal_date": None, "provisional_date": None, "material_ready_date": None}
    if not board["bom_locked"]:
        return {**base, "status": "unknown", "reason": "BOM 未锁定"}
    if not board["planned_quantity"] or dec(board["planned_quantity"]) < required:
        return {**base, "status": "unknown",
                "reason": f"计划生产 {board['planned_quantity'] or '0'} 片，低于本批交付所需 {ds(required)} 片"}
    events = rows(conn, """SELECT event_date,quantity FROM events WHERE entity_type='batch_board'
                     AND entity_id=? AND kind='finished' ORDER BY event_date,id""", (board_id,))
    completed = Decimal(0)
    for event in events:
        completed += dec(event["quantity"])
        if completed >= required:
            return {**base, "status": "complete", "formal_date": event["event_date"], "reason": None,
                    "completed_quantity": ds(completed)}
    if materials is None:
        materials = batch_materials(conn, batch["id"])
    base["material_ready_date"] = materials["ready_date"]
    if not board["vendor"] or not board["commit_start"] or not board["commit_finish"]:
        return {**base, "status": "unknown", "reason": "缺少本板外协厂或承诺开工、完工日"}
    if materials["status"] == "bom_unlocked":
        return {**base, "status": "unknown", "reason": "整批板卡 BOM 尚未全部锁定"}
    if materials["status"] == "unknown":
        return {**base, "status": "unknown", "reason": "物料缺少足额到货 ETA"}
    ready = materials["ready_date"]
    if ready:
        while not workday(conn, date.fromisoformat(ready)):
            ready = (date.fromisoformat(ready) + timedelta(days=1)).isoformat()
    late_material = workdays_between(conn, board["commit_start"], ready) if ready and ready > board["commit_start"] else 0
    stages = rows(conn, "SELECT * FROM board_stages WHERE batch_board_id=? ORDER BY sequence,id", (board_id,))
    late_stage = max((workdays_between(conn, stage["planned_finish"], stage["actual_finish"])
                      for stage in stages if stage["planned_finish"] and stage["actual_finish"]
                      and stage["actual_finish"] > stage["planned_finish"]), default=0)
    overdue = [stage for stage in stages if stage["planned_finish"] and stage["planned_finish"] < today()
               and not stage["actual_finish"] and stage["status"] != "skipped"]
    late_incomplete = max((max(1, workdays_between(conn, stage["planned_finish"], today()))
                           for stage in overdue), default=0)
    last_planned = stages[-1]["planned_finish"] if stages else None
    late_template = (workdays_between(conn, board["commit_finish"], last_planned)
                     if last_planned and last_planned > board["commit_finish"] else 0)
    shift = max(late_material, late_stage, late_incomplete, late_template)
    if shift or materials["status"] == "estimated":
        reasons = []
        if materials["status"] == "estimated":
            reasons.append("采购 ETA 为人工估算，待采购确认")
        if late_material:
            reasons.append("物料晚到，待外协厂重新确认")
        if late_incomplete:
            reasons.append(f"{len(overdue)} 个工序逾期未确认")
        if late_stage:
            reasons.append("工序实际完成晚于计划")
        if late_template:
            reasons.append("节点计划晚于外协厂承诺")
        return {**base, "status": "unknown", "reason": "；".join(reasons),
                "provisional_date": add_workdays(conn, board["commit_finish"], shift),
                "shift_workdays": shift, "needs_vendor_reconfirm": bool(shift),
                "overdue_stage_count": len(overdue)}
    return {**base, "status": "forecast", "formal_date": board["commit_finish"], "reason": None}


def batch_finish_forecast(conn: sqlite3.Connection, batch_id: int) -> dict:
    batch = must_get(conn, "production_batches", batch_id)
    if batch["board_schedule_mode"] == "independent":
        boards = rows(conn, "SELECT * FROM batch_boards WHERE production_batch_id=? ORDER BY id", (batch_id,))
        if batch["bom_scope"] != "multi" or not batch["board_set_confirmed_at"] or not boards:
            return {"status": "unknown", "formal_date": None, "provisional_date": None,
                    "reason": "逐板排期尚未确认完整板卡组成", "material_ready_date": None}
        materials = batch_materials(conn, batch_id)
        board_results = [(board, board_finish_forecast(conn, board["id"], materials)) for board in boards]
        unknown = [f"{board['board_name']}：{result['reason']}" for board, result in board_results
                   if result["status"] == "unknown"]
        provisional = [result["provisional_date"] for _, result in board_results if result["provisional_date"]]
        formal = [result["formal_date"] for _, result in board_results if result["formal_date"]]
        if unknown:
            return {"status": "unknown", "formal_date": None,
                    "provisional_date": max(provisional + formal) if provisional or formal else None,
                    "reason": "；".join(unknown), "material_ready_date": materials["ready_date"],
                    "needs_vendor_reconfirm": any(result.get("needs_vendor_reconfirm") for _, result in board_results),
                    "board_forecasts": [{"board_ref": board["board_ref"], "board_name": board["board_name"],
                                         **result} for board, result in board_results]}
        return {"status": "complete" if all(result["status"] == "complete" for _, result in board_results) else "forecast",
                "formal_date": max(formal) if formal else None, "provisional_date": None,
                "reason": None, "material_ready_date": materials["ready_date"],
                "board_forecasts": [{"board_ref": board["board_ref"], "board_name": board["board_name"],
                                     **result} for board, result in board_results]}
    if batch["phase"] == "rd":
        last = one(conn, "SELECT planned_finish FROM stages WHERE production_batch_id=? ORDER BY sequence DESC LIMIT 1", (batch_id,))
        if batch["debug_accepted_at"]:
            return {"status": "complete", "formal_date": batch["debug_accepted_at"],
                    "provisional_date": None, "reason": "研发调试验收完成", "material_ready_date": None}
        return {"status": "unknown", "formal_date": None,
                "provisional_date": last["planned_finish"] if last else None,
                "reason": "研发打样节点草案，待实际到板和调试验收", "material_ready_date": None}
    finished = rows(conn, "SELECT event_date,quantity FROM events WHERE entity_type='production_batch' AND entity_id=? AND kind='finished' ORDER BY event_date", (batch_id,))
    finished_quantity = sum((dec(item["quantity"]) for item in finished), Decimal(0))
    if finished_quantity >= dec(batch["quantity"]):
        return {"status": "complete", "formal_date": finished[-1]["event_date"],
                "provisional_date": None, "reason": None, "material_ready_date": None}
    materials = batch_materials(conn, batch_id)
    if not batch["commit_start"] or not batch["commit_finish"]:
        return {"status": "unknown", "formal_date": None, "provisional_date": None,
                "reason": "缺少厂商承诺开工或完工日", "material_ready_date": materials["ready_date"]}
    if materials["status"] in {"bom_unlocked", "unknown"}:
        return {"status": "unknown", "formal_date": None, "provisional_date": None,
                "reason": "BOM 未锁定或物料缺少足额到货 ETA", "material_ready_date": materials["ready_date"]}
    effective_ready = materials["ready_date"]
    if effective_ready:
        while not workday(conn, date.fromisoformat(effective_ready)):
            effective_ready = (date.fromisoformat(effective_ready) + timedelta(days=1)).isoformat()
    late_material = (workdays_between(conn, batch["commit_start"], effective_ready)
                     if effective_ready and effective_ready > batch["commit_start"] else 0)
    late_stage = 0
    for stage in rows(conn, "SELECT planned_finish,actual_finish FROM stages WHERE production_batch_id=? AND planned_finish IS NOT NULL AND actual_finish IS NOT NULL", (batch_id,)):
        if stage["actual_finish"] > stage["planned_finish"]:
            late_stage = max(late_stage, workdays_between(conn, stage["planned_finish"], stage["actual_finish"]))
    overdue = overdue_incomplete_stages(conn, batch_id)
    late_incomplete = max((max(1, workdays_between(conn, stage["planned_finish"], today()))
                           for stage in overdue), default=0)
    finish_stage = one(conn, "SELECT planned_finish FROM stages WHERE production_batch_id=? AND name='厂商完工'", (batch_id,))
    late_template = (workdays_between(conn, batch["commit_finish"], finish_stage["planned_finish"])
                     if finish_stage and finish_stage["planned_finish"] and finish_stage["planned_finish"] > batch["commit_finish"] else 0)
    shift = max(late_material, late_stage, late_template, late_incomplete)
    if shift or materials["status"] == "estimated":
        reasons = []
        if materials["status"] == "estimated":
            reasons.append("采购 ETA 为人工估算，待采购确认")
        if shift:
            reasons.append(f"{len(overdue)} 个工序已过计划日仍未完成，待更新计划并与厂商复核" if late_incomplete == shift else
                           "物料晚到，待厂商重新确认" if late_material == shift else
                           "模板计划晚于厂商承诺，待核对" if late_template == shift else
                           "工序晚于计划，待厂商重新确认")
        return {"status": "unknown", "formal_date": None,
                "provisional_date": add_workdays(conn, batch["commit_finish"], shift),
                "reason": "；".join(reasons), "material_ready_date": materials["ready_date"],
                "shift_workdays": shift, "needs_vendor_reconfirm": bool(shift),
                "overdue_stage_count": len(overdue)}
    return {"status": "forecast", "formal_date": batch["commit_finish"],
            "provisional_date": None, "reason": None, "material_ready_date": materials["ready_date"]}


def delivery_forecast(conn: sqlite3.Connection, batch: dict) -> dict:
    basis = batch["completion_basis"]
    event_kind = "shipped" if basis == "shipment" else "customer_received"
    events = rows(conn, "SELECT event_date,quantity FROM events WHERE entity_type='delivery_batch' AND entity_id=? AND kind=? ORDER BY event_date", (batch["id"], event_kind))
    completed = sum((dec(event["quantity"]) for event in events), Decimal(0))
    base = {"status": "unknown", "formal_date": None, "provisional_date": None,
            "reason": None, "completed_quantity": ds(completed), "completion_basis": basis,
            "manual_estimate_date": batch.get("manual_estimate_date")}

    def unknown(reason: str | None, provisional_date: str | None = None) -> dict:
        manual_date = batch.get("manual_estimate_date")
        if manual_date:
            provisional_date = max(filter(None, (provisional_date, manual_date)))
            manual_note = f"人工估算交付日 {manual_date}（{batch['manual_estimate_reason']}）"
            reason = "；".join(filter(None, (reason, manual_note)))
        return {**base, "provisional_date": provisional_date, "reason": reason}

    if completed >= dec(batch["quantity"]):
        return {**base, "status": "complete", "formal_date": events[-1]["event_date"]}
    allocations = rows(conn, "SELECT * FROM allocations WHERE delivery_batch_id=?", (batch["id"],))
    allocated = sum((dec(item["quantity"]) for item in allocations), Decimal(0))
    if allocated < dec(batch["quantity"]):
        return unknown("生产批次尚未足额分配")
    formal_dates, provisional_dates, reasons, provisional_reasons = [], [], [], []
    lead = batch["shipping_lead_days"] if basis == "shipment" else batch["receipt_lead_days"]
    for allocation in allocations:
        predicted = batch_finish_forecast(conn, allocation["production_batch_id"])
        if predicted["formal_date"]:
            formal_dates.append(add_workdays(conn, predicted["formal_date"], lead))
        elif predicted["provisional_date"]:
            provisional_dates.append(add_workdays(conn, predicted["provisional_date"], lead))
            if predicted["reason"]:
                provisional_reasons.append(predicted["reason"])
        else:
            reasons.append(predicted["reason"])
    if reasons:
        return unknown("；".join(sorted(set(reasons))))
    if provisional_dates:
        reason = "；".join(filter(None, ("；".join(sorted(set(provisional_reasons))), "日期为临时估算")))
        return unknown(reason, max(provisional_dates + formal_dates))
    return {**base, "status": "forecast", "formal_date": max(formal_dates), "reason": None}


def delivery_risks(conn: sqlite3.Connection, batch: dict, forecast: dict) -> list[dict]:
    if forecast["status"] == "complete":
        return []
    line = must_get(conn, "order_lines", batch["order_line_id"])
    order = must_get(conn, "orders", line["order_id"])
    chosen = forecast["formal_date"] or forecast["provisional_date"]
    result = []
    for field, label in (("due_date", "客户要求日"), ("promised_date", "业务承诺日")):
        baseline = batch[field]
        if not baseline:
            continue
        workdays_left = workdays_between(conn, today(), baseline)
        late = chosen and chosen > baseline
        overdue = baseline < today()
        if late or overdue:
            level = "red"
            message = f"{label}已逾期" if overdue else f"预计晚于{label}"
        elif 0 <= workdays_left <= 5:
            level = "yellow"
            message = f"{label}还有 {workdays_left} 个工作日"
        else:
            continue
        result.append({"level": level, "basis": field, "basis_label": label,
                       "order_id": order["id"], "order_no": order["order_no"],
                       "delivery_batch_id": batch["id"], "baseline_date": baseline,
                       "forecast_date": chosen, "forecast_status": forecast["status"],
                       "provisional_date": forecast["provisional_date"],
                       "is_provisional": bool(forecast["provisional_date"]), "message": message,
                       "workdays_left": workdays_left})
    return result


def phase_target_risks(conn: sqlite3.Connection, batch: dict) -> list[dict]:
    """Warn about an R&D sample goal without calling a draft a formal promise."""
    if batch["phase"] != "rd" or not batch["target_date"]:
        return []
    if batch["board_schedule_mode"] == "independent":
        prediction = batch_finish_forecast(conn, batch["id"])
        if batch["debug_accepted_at"] and prediction["status"] == "complete":
            return []
        draft = prediction["formal_date"] or prediction["provisional_date"]
    else:
        if batch["debug_accepted_at"]:
            return []
        last = one(conn, "SELECT planned_finish FROM stages WHERE production_batch_id=? ORDER BY sequence DESC LIMIT 1", (batch["id"],))
        draft = last["planned_finish"] if last else None
    line = must_get(conn, "order_lines", batch["order_line_id"])
    order = must_get(conn, "orders", line["order_id"])
    target = batch["target_date"]
    target_label = "客户样机要求日" if batch["target_kind"] == "customer" else "内部样机目标日"
    remaining = workdays_between(conn, today(), target)
    overdue = target < today()
    if overdue or draft and draft > target:
        level = "red"
        message = (f"{target_label}已逾期" if overdue else
                   f"逐板计划晚于{target_label}" if batch["board_schedule_mode"] == "independent" else
                   f"节点草案晚于{target_label}")
    elif 0 <= remaining <= 5:
        level = "yellow"
        message = f"{target_label}还有 {remaining} 个工作日"
    else:
        return []
    return [{"level": level, "basis": "phase_target", "basis_label": target_label,
             "order_id": order["id"], "order_no": order["order_no"],
             "production_batch_id": batch["id"], "baseline_date": target,
             "forecast_date": None, "forecast_status": "draft", "provisional_date": draft,
             "is_provisional": True, "message": message, "workdays_left": remaining}]


def overdue_incomplete_stages(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    """Return late, unfinished nodes in production order, excluding skipped nodes."""
    return rows(conn, "SELECT * FROM stages WHERE production_batch_id=? AND planned_finish<? "
                "AND actual_finish IS NULL AND COALESCE(status,'pending')<>'skipped' ORDER BY sequence,id",
                (batch_id, today()))


def role_assignee(conn: sqlite3.Connection, role: str) -> int | None:
    found = one(conn, "SELECT id FROM users WHERE role=? AND active=1 ORDER BY id LIMIT 1", (role,))
    if not found:
        found = one(conn, "SELECT id FROM users WHERE role='admin' AND active=1 ORDER BY id LIMIT 1")
    return found["id"] if found else None


def order_task_ref(conn: sqlite3.Connection, order_id: int, order_no: str) -> str:
    """Lead imported task titles with the product people recognize."""
    if not order_no.startswith("PCBA-TEMP-"):
        return f"订单 {order_no}"
    line = one(conn, "SELECT product_name FROM order_lines WHERE order_id=? ORDER BY line_no,id LIMIT 1", (order_id,))
    name = line["product_name"] if line else None
    if not name:
        legacy = one(conn, "SELECT source_json FROM legacy_records WHERE order_id=?", (order_id,))
        if legacy:
            source = json.loads(legacy["source_json"])
            name = source.get("name") or source.get("product")
    return f"项目 {name}（{order_no}）" if name else f"项目 {order_no}"


def is_imported_batch(conn: sqlite3.Connection, batch_id: int) -> bool:
    return bool(one(conn, """SELECT a.id FROM legacy_records r JOIN audit a
                     ON a.entity_type='production_batches' AND a.entity_id=r.production_batch_id
                     AND a.action='legacy_create' WHERE r.production_batch_id=? LIMIT 1""", (batch_id,)))


def production_batch_label(conn: sqlite3.Connection, batch: dict) -> str:
    """Hide the importer placeholder only for an importer-created batch."""
    raw = batch["batch_no"]
    suffix = raw.removeprefix("旧排期-")
    if raw.startswith("旧排期-") and suffix.isdigit() and is_imported_batch(conn, batch["id"]):
        return f"第 {suffix} 批"
    return raw


def task_allows_automatic_reassignment(conn: sqlite3.Connection, task_id: int,
                                       current_assignee_id: int | None) -> bool:
    """Keep a person's transfer, including a deliberate unassignment, authoritative."""
    system_assigned = False
    for change in rows(conn, "SELECT action,actor_id,before_json,after_json FROM audit "
                       "WHERE entity_type='tasks' AND entity_id=? ORDER BY id", (task_id,)):
        before = json.loads(change["before_json"]) if change["before_json"] else None
        after = json.loads(change["after_json"]) if change["after_json"] else None
        if change["actor_id"] is not None and isinstance(before, dict) and isinstance(after, dict):
            if before.get("assignee_id") != after.get("assignee_id"):
                return False
        elif change["actor_id"] is None and change["action"] in {"auto_create", "auto_assign", "auto_reassign"}:
            system_assigned = True
    return current_assignee_id is None or system_assigned


def sync_tasks(conn: sqlite3.Connection) -> None:
    for order in rows(conn, "SELECT id FROM orders"):
        sync_order_status(conn, order["id"])
    active: dict[str, tuple[str, str, int, str, str, int | None]] = {}

    def add(key: str, kind: str, entity_type: str, entity_id: int, title: str, role: str,
            preferred_id: int | None = None):
        active[key] = (kind, entity_type, entity_id, title, role, preferred_id)

    for order in rows(conn, "SELECT * FROM orders WHERE status='open'"):
        order_ref = order_task_ref(conn, order["id"], order["order_no"])
        legacy_order = order["order_no"].startswith("PCBA-TEMP-")
        if not order["owner_id"]:
            legacy = one(conn, "SELECT source_json FROM legacy_records WHERE order_id=?", (order["id"],))
            old_owner = legacy_owner_name(legacy)
            title = (f"{order_ref} 原负责人 {old_owner} 尚未绑定工作台账号" if old_owner
                     else f"{order_ref} 尚未指定负责人")
            add(f"owner:{order['id']}", "missing_owner", "orders", order["id"], title, "business")
        for line in rows(conn, "SELECT * FROM order_lines WHERE order_id=?", (order["id"],)):
            product_ref = "" if legacy_order else f" 产品 {line['product_code']}"
            configured_boards = board_definitions(conn, line["id"])
            if configured_boards and not line["board_set_confirmed_at"]:
                add(f"board_confirm:{line['id']}", "board_composition_unconfirmed", "order_lines", line["id"],
                    f"{order_ref}{product_ref} 已配置 {len(configured_boards)} 块板，完整板卡组成待确认",
                    "engineering", order["owner_id"])
            deliveries = rows(conn, "SELECT * FROM delivery_batches WHERE order_line_id=?", (line["id"],))
            batches = rows(conn, "SELECT * FROM production_batches WHERE order_line_id=?", (line["id"],))
            rd_only = bool(batches) and all(batch["phase"] == "rd" for batch in batches)
            unscheduled = dec(line["quantity"]) - sum((dec(item["quantity"]) for item in deliveries), Decimal(0))
            if unscheduled > 0 and not rd_only:
                add(f"delivery_batch:{line['id']}", "missing_delivery_batch", "order_lines", line["id"],
                    f"{order_ref}{product_ref} 尚有 {ds(unscheduled)} 未排交付批次", "business", order["owner_id"])
            if not batches:
                add(f"batch:{line['id']}", "missing_batch", "order_lines", line["id"],
                    f"{order_ref}{product_ref} 尚未建立生产批次", "business", order["owner_id"])
            for batch in batches:
                visible_batch_no = production_batch_label(conn, batch)
                batch_ref = (f"产品 {line['product_name']} {PHASE_LABELS.get(batch['phase'], '阶段')} {visible_batch_no}" if legacy_order
                             else f"{PHASE_LABELS.get(batch['phase'], '阶段')} 批次 {visible_batch_no}")
                if not batch["bom_locked"]:
                    title = (f"产品 {line['product_name']} 生产批次 {visible_batch_no}：" if legacy_order
                             else f"生产批次 {visible_batch_no}：") + batch_bom_gap(conn, batch, line)
                    add(f"bom:{batch['id']}", "bom_unlocked", "production_batches", batch["id"], title, "business", order["owner_id"])
                materials = batch_materials(conn, batch["id"])
                for material in materials["items"]:
                    stock = stock_capacity(conn, material["material_code"])
                    if dec(material["shortage"]) > 0 and stock["confirmation_required"] and dec(stock["snapshot_quantity"]) > 0:
                        add(f"stock_confirm:{material['material_code']}", "stock_confirmation", "inventory", 0,
                            f"物料 {material['material_code']} 的现存量未经仓库确认可用，不能计入齐套", "warehouse")
                    if dec(material["shortage"]) > 0 and material["missing_eta"]:
                        add(f"eta:{batch['id']}:{material['material_code']}", "missing_eta", "production_batches", batch["id"],
                            f"{batch_ref} 物料 {material['material_code']} 缺口需补足采购 ETA", "purchasing")
                    elif dec(material["shortage"]) > 0 and material["eta_is_estimate"]:
                        add(f"eta_confirm:{batch['id']}:{material['material_code']}", "eta_confirm", "production_batches", batch["id"],
                            f"{batch_ref} 物料 {material['material_code']} 的人工估算 ETA 待确认", "purchasing")
                if batch["board_schedule_mode"] == "independent":
                    if batch["phase"] == "rd" and (not batch["target_kind"] or not batch["target_date"]):
                        add(f"rd_target:{batch['id']}", "missing_phase_target", "production_batches", batch["id"],
                            f"{batch_ref} 需选择内部/客户目标并填写目标日", "business", order["owner_id"])
                    for board in rows(conn, "SELECT * FROM batch_boards WHERE production_batch_id=?", (batch["id"],)):
                        board_ref = f"{batch_ref} 板卡 {board['board_name']}"
                        required = dec(batch["quantity"]) * dec(board["quantity_per_product"])
                        if not board["planned_quantity"] or dec(board["planned_quantity"]) < required:
                            add(f"board_quantity:{board['id']}", "board_quantity_short", "batch_boards", board["id"],
                                f"{board_ref} 计划数量不足 {ds(required)} 片交付所需", "business", order["owner_id"])
                        if not board["vendor"] or not board["commit_start"] or not board["commit_finish"]:
                            add(f"board_vendor_date:{board['id']}", "missing_vendor_date", "batch_boards", board["id"],
                                f"{board_ref} 缺少外协厂或承诺开工、完工日", "business", order["owner_id"])
                        overdue_board = rows(conn, """SELECT * FROM board_stages WHERE batch_board_id=? AND planned_finish<?
                                   AND actual_finish IS NULL AND COALESCE(status,'pending')<>'skipped' ORDER BY sequence,id""",
                                             (board["id"], today()))
                        for stage in overdue_board:
                            add(f"board_stage_overdue:{stage['id']}", "stage_overdue", "board_stages", stage["id"],
                                f"{board_ref} 节点 {stage['name']} 原计划 {stage['planned_finish']} 完成，至今未确认",
                                "business", stage["owner_id"] or order["owner_id"])
                        conflicts = board_vendor_conflicts(conn, board)
                        if conflicts and not board_conflicts_acknowledged(board, conflicts):
                            add(f"board_conflict:{board['id']}", "vendor_conflict", "batch_boards", board["id"],
                                f"{board_ref} 外协厂档期重叠，需确认能否并行", "business", order["owner_id"])
                        forecast = board_finish_forecast(conn, board["id"])
                        if forecast.get("needs_vendor_reconfirm"):
                            add(f"board_reconfirm:{board['id']}", "vendor_reconfirm", "batch_boards", board["id"],
                                f"{board_ref}：{forecast['reason']}", "business", order["owner_id"])
                    if batch["phase"] == "rd":
                        for risk in phase_target_risks(conn, batch):
                            add(f"phase_target:{batch['id']}", "phase_target_risk", "production_batches", batch["id"],
                                f"{batch_ref}：{risk['message']}", "business", order["owner_id"])
                    continue
                overdue = overdue_incomplete_stages(conn, batch["id"])
                if overdue:
                    first = overdue[0]
                    stage_owner = first["owner_id"] and one(
                        conn, "SELECT id FROM users WHERE id=? AND active=1 AND status='active'", (first["owner_id"],))
                    extra = f"，另有 {len(overdue) - 1} 个节点计划已过" if len(overdue) > 1 else ""
                    add(f"stage_overdue:{first['id']}", "stage_overdue", "stages", first["id"],
                        f"{batch_ref} 节点 {first['name']} 原计划 {first['planned_finish']} 完成，至今未确认{extra}",
                        "business", first["owner_id"] if stage_owner else order["owner_id"])
                if batch["phase"] == "rd":
                    if not batch["target_kind"] or not batch["target_date"]:
                        add(f"rd_target:{batch['id']}", "missing_phase_target", "production_batches", batch["id"],
                            f"{batch_ref} 需选择内部/客户目标并填写目标日", "business", order["owner_id"])
                    if not batch["board_eta"] and not batch["board_received_at"]:
                        add(f"board_eta:{batch['id']}", "missing_board_eta", "production_batches", batch["id"],
                            f"{batch_ref} 缺少预计到板日", "engineering", order["owner_id"])
                    for risk in phase_target_risks(conn, batch):
                        add(f"phase_target:{batch['id']}", "phase_target_risk", "production_batches", batch["id"],
                            f"{batch_ref}：{risk['message']}", "business", order["owner_id"])
                else:
                    estimate = batch_finish_forecast(conn, batch["id"])
                    if not batch["commit_start"] or not batch["commit_finish"]:
                        add(f"vendor_date:{batch['id']}", "missing_vendor_date", "production_batches", batch["id"],
                            f"{batch_ref} 缺少板厂承诺日期", "business", order["owner_id"])
                    elif estimate.get("needs_vendor_reconfirm"):
                        add(f"reconfirm:{batch['id']}", "vendor_reconfirm", "production_batches", batch["id"],
                            f"{batch_ref} 需板厂重新确认完工日期", "business", order["owner_id"])
                    conflicts = vendor_conflicts(conn, batch)
                    if conflicts and not batch_conflicts_acknowledged(batch, conflicts):
                        add(f"conflict:{batch['id']}", "vendor_conflict", "production_batches", batch["id"],
                            f"{batch_ref} 板厂档期重叠待核对", "business", order["owner_id"])
            for delivery in deliveries:
                forecast = delivery_forecast(conn, delivery)
                if forecast["status"] != "complete":
                    allocated = sum((dec(item["quantity"]) for item in rows(
                        conn, "SELECT quantity FROM allocations WHERE delivery_batch_id=?", (delivery["id"],))), Decimal(0))
                    unallocated = dec(delivery["quantity"]) - allocated
                    if unallocated > 0:
                        add(f"delivery_unallocated:{delivery['id']}", "delivery_unallocated", "delivery_batches", delivery["id"],
                            f"{order_ref} 交付批次 {delivery['batch_no']} 尚有 {ds(unallocated)} 未关联生产批次",
                            "business", order["owner_id"])
                for risk in delivery_risks(conn, delivery, forecast):
                    add(f"risk:{delivery['id']}:{risk['basis']}", "due_risk", "delivery_batches", delivery["id"],
                        f"{order_ref} 交付批次 {delivery['batch_no']}：{risk['message']}", "business", order["owner_id"])
    # Date omissions remain actionable even when a historical batch is marked complete.
    for delivery in rows(conn, "SELECT d.*,l.product_code,o.id AS order_id,o.order_no,o.owner_id FROM delivery_batches d JOIN order_lines l ON l.id=d.order_line_id JOIN orders o ON o.id=l.order_id"):
        for field, label in (("due_date", "客户要求日"), ("promised_date", "业务承诺日")):
            if not delivery[field]:
                add(f"date:{delivery['id']}:{field}", "missing_delivery_date", "delivery_batches", delivery["id"],
                    f"{order_task_ref(conn, delivery['order_id'], delivery['order_no'])} 交付批次 {delivery['batch_no']} 缺少{label}", "business", delivery["owner_id"])
    for legacy in rows(conn, "SELECT r.*,o.order_no,o.owner_id FROM legacy_records r JOIN orders o ON o.id=r.order_id"):
        missing = legacy_missing_fields(conn, legacy)
        if missing:
            legacy_ref = order_task_ref(conn, legacy["order_id"], legacy["order_no"])
            add(f"legacy:{legacy['id']}", "legacy_pending", "legacy_records", legacy["id"],
                f"{legacy_ref} 仍有 {len(missing)} 项待补录", "business", legacy["owner_id"])
    for receipt in rows(conn, "SELECT * FROM receipts WHERE iqc_status='fail'"):
        add(f"iqc:{receipt['id']}", "iqc_failed", "receipts", receipt["id"],
            f"物料 {receipt['material_code']} IQC 不合格，需补购或退换", "purchasing")
    for item in rows(conn, "SELECT * FROM substitutes WHERE status='pending'"):
        add(f"substitute:{item['id']}", "substitute_approval", "substitutes", item["id"],
            f"替代料 {item['substitute_material_code']} 待工程审批", "engineering")
    for stock in rows(conn, "SELECT DISTINCT stock_material_code AS material_code FROM reservations"):
        capacity = stock_capacity(conn, stock["material_code"])
        if Decimal(capacity["free"]) < 0:
            add(f"stock:{stock['material_code']}", "stock_shortage", "inventory", 0,
                f"物料 {stock['material_code']} 新库存快照不足以覆盖工作台预留", "warehouse")
        if dec(capacity["same_day_issue_unresolved"]) > 0:
            add(f"stock_time:{stock['material_code']}", "stock_time_check", "inventory", 0,
                f"物料 {stock['material_code']} 快照当日发料时点待核对", "warehouse")
    old_tasks = {r["task_key"]: r for r in rows(conn, "SELECT * FROM tasks")}
    for key, (kind, entity_type, entity_id, title, role, preferred_id) in active.items():
        old = old_tasks.get(key)
        preferred = (one(conn, "SELECT id FROM users WHERE id=? AND active=1 AND status='active'", (preferred_id,))
                     if preferred_id else None)
        assignee_id = preferred["id"] if preferred else role_assignee(conn, role)
        if not old:
            cur = conn.execute("INSERT INTO tasks(task_key,kind,entity_type,entity_id,title,assignee_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                               (key, kind, entity_type, entity_id, title, assignee_id, utcnow(), utcnow()))
            audit(conn, "tasks", cur.lastrowid, "auto_create", None, None, must_get(conn, "tasks", cur.lastrowid))
        else:
            if not old["condition_active"] or old["title"] != title:
                conn.execute("UPDATE tasks SET title=?,condition_active=1,status=CASE WHEN condition_active=0 THEN 'open' ELSE status END,version=version+1,updated_at=? WHERE id=?",
                             (title, utcnow(), old["id"]))
                current = must_get(conn, "tasks", old["id"])
                audit(conn, "tasks", old["id"], "condition_active", None, old, current)
                old = current
            if old["status"] == "done" and old["condition_active"]:
                conn.execute("UPDATE tasks SET status='open',version=version+1,updated_at=? WHERE id=?", (utcnow(), old["id"]))
                current = must_get(conn, "tasks", old["id"])
                audit(conn, "tasks", old["id"], "auto_reopen_active", None, old, current)
                old = current
            if (old["status"] == "open" and assignee_id is not None and old["assignee_id"] != assignee_id
                    and task_allows_automatic_reassignment(conn, old["id"], old["assignee_id"])):
                conn.execute("UPDATE tasks SET assignee_id=?,version=version+1,updated_at=? WHERE id=?",
                             (assignee_id, utcnow(), old["id"]))
                action = "auto_reassign" if old["assignee_id"] is not None else "auto_assign"
                audit(conn, "tasks", old["id"], action, None, old, must_get(conn, "tasks", old["id"]))
    for key, old in old_tasks.items():
        if key not in active and old["condition_active"]:
            conn.execute("UPDATE tasks SET condition_active=0,version=version+1,updated_at=? WHERE id=?", (utcnow(), old["id"]))
            audit(conn, "tasks", old["id"], "condition_cleared", None, old, must_get(conn, "tasks", old["id"]))


def order_assessment(conn: sqlite3.Connection, order: dict) -> dict:
    """Explain why an open project's delivery or R&D target cannot yet be judged."""
    missing: dict[tuple[str, str, int], dict] = {}
    phases: set[str] = set()

    def add(code: str, label: str, entity_type: str, entity_id: int) -> None:
        missing[(code, entity_type, entity_id)] = {
            "code": code, "label": label, "entity_type": entity_type, "entity_id": entity_id}

    for line in rows(conn, "SELECT * FROM order_lines WHERE order_id=? ORDER BY line_no,id", (order["id"],)):
        boards = board_definitions(conn, line["id"])
        if boards and not line["board_set_confirmed_at"]:
            add("board_composition_unconfirmed", f"{line['product_name']} 已配置 {len(boards)} 块板，完整板卡组成待确认",
                "order_lines", line["id"])
        batches = rows(conn, "SELECT * FROM production_batches WHERE order_line_id=?", (line["id"],))
        phases.update(batch["phase"] for batch in batches)
        if not batches:
            add("missing_batch", f"{line['product_name']} 尚未建立生产批次", "order_lines", line["id"])
        if batches and all(batch["phase"] == "rd" for batch in batches):
            for batch in batches:
                if batch["board_schedule_mode"] == "independent":
                    forecast = batch_finish_forecast(conn, batch["id"])
                    if forecast["status"] == "unknown":
                        add("board_forecast_unknown", f"{line['product_name']}：{forecast['reason']}",
                            "production_batches", batch["id"])
                    if not batch["target_kind"] or not batch["target_date"]:
                        add("missing_phase_target", f"{line['product_name']} 研发目标类型或日期未填写",
                            "production_batches", batch["id"])
                    continue
                if not batch["target_kind"] or not batch["target_date"]:
                    add("missing_phase_target", f"{line['product_name']} 研发目标类型或日期未填写", "production_batches", batch["id"])
                if not batch["board_eta"] and not batch["board_received_at"]:
                    add("missing_board_eta", f"{line['product_name']} 预计或实际到板日未填写", "production_batches", batch["id"])
                if not one(conn, "SELECT id FROM stages WHERE production_batch_id=?", (batch["id"],)):
                    add("missing_stages", f"{line['product_name']} 尚无研发节点计划", "production_batches", batch["id"])
                if overdue_incomplete_stages(conn, batch["id"]):
                    add("stage_overdue", f"{line['product_name']} 有研发节点过计划日未完成", "production_batches", batch["id"])
            continue

        if any(batch["phase"] == "unclassified" for batch in batches):
            add("phase_unclassified", f"{line['product_name']} 的生产批次尚未确认阶段", "order_lines", line["id"])
        deliveries = rows(conn, "SELECT * FROM delivery_batches WHERE order_line_id=?", (line["id"],))
        delivery_total = sum((dec(delivery["quantity"]) for delivery in deliveries), Decimal(0))
        if delivery_total < dec(line["quantity"]):
            add("missing_delivery_batch", f"{line['product_name']} 尚有 {ds(dec(line['quantity']) - delivery_total)} 未排交付批次",
                "order_lines", line["id"])
        incomplete_delivery = False
        for delivery in deliveries:
            if not delivery["due_date"]:
                add("missing_due_date", f"交付批次 {delivery['batch_no']} 缺少客户要求日", "delivery_batches", delivery["id"])
            if not delivery["promised_date"]:
                add("missing_promised_date", f"交付批次 {delivery['batch_no']} 缺少业务承诺日", "delivery_batches", delivery["id"])
            forecast = delivery_forecast(conn, delivery)
            if forecast["status"] == "complete":
                continue
            incomplete_delivery = True
            allocated = sum((dec(item["quantity"]) for item in rows(
                conn, "SELECT quantity FROM allocations WHERE delivery_batch_id=?", (delivery["id"],))), Decimal(0))
            if allocated < dec(delivery["quantity"]):
                add("delivery_unallocated", f"交付批次 {delivery['batch_no']} 尚有 {ds(dec(delivery['quantity']) - allocated)} 未分配生产",
                    "delivery_batches", delivery["id"])
            elif forecast["status"] == "unknown":
                add("forecast_unconfirmed", f"交付批次 {delivery['batch_no']}：{forecast['reason'] or '正式预测尚未确认'}",
                    "delivery_batches", delivery["id"])
        if not deliveries:
            incomplete_delivery = True
        if not incomplete_delivery:
            continue
        for batch in batches:
            if batch["phase"] == "rd":
                continue
            visible_batch_no = production_batch_label(conn, batch)
            if not batch["bom_locked"]:
                add("bom_unlocked", f"生产批次 {visible_batch_no}：{batch_bom_gap(conn, batch, line)}",
                    "production_batches", batch["id"])
                continue
            if batch["board_schedule_mode"] == "independent":
                forecast = batch_finish_forecast(conn, batch["id"])
                if forecast["status"] == "unknown":
                    add("board_forecast_unknown", f"生产批次 {visible_batch_no}：{forecast['reason']}",
                        "production_batches", batch["id"])
            elif not batch["commit_start"] or not batch["commit_finish"]:
                add("missing_vendor_date", f"生产批次 {visible_batch_no} 缺少厂商承诺日期", "production_batches", batch["id"])
            materials = batch_materials(conn, batch["id"])
            if materials["status"] == "unknown":
                add("material_eta_unknown", f"生产批次 {visible_batch_no} 物料缺口无足额到货日期", "production_batches", batch["id"])
                if any(dec(item["shortage"]) > 0 and stock_capacity(conn, item["material_code"])["confirmation_required"]
                       for item in materials["items"]):
                    add("stock_unconfirmed", f"生产批次 {visible_batch_no} 的现存量未经仓库确认可用", "production_batches", batch["id"])
            elif materials["status"] == "estimated":
                add("eta_unconfirmed", f"生产批次 {visible_batch_no} 的到货日期仅为人工估算", "production_batches", batch["id"])
            if batch["board_schedule_mode"] == "independent":
                for board in rows(conn, "SELECT * FROM batch_boards WHERE production_batch_id=?", (batch["id"],)):
                    late = rows(conn, """SELECT id FROM board_stages WHERE batch_board_id=? AND planned_finish<?
                                   AND actual_finish IS NULL AND COALESCE(status,'pending')<>'skipped' LIMIT 1""",
                                (board["id"], today()))
                    if late:
                        add("stage_overdue", f"生产批次 {visible_batch_no} 板卡 {board['board_name']} 有节点过计划日未完成",
                            "batch_boards", board["id"])
            else:
                if overdue_incomplete_stages(conn, batch["id"]):
                    add("stage_overdue", f"生产批次 {visible_batch_no} 有节点过计划日未完成", "production_batches", batch["id"])
                if batch_finish_forecast(conn, batch["id"]).get("needs_vendor_reconfirm"):
                    add("vendor_reconfirm", f"生产批次 {visible_batch_no} 需厂商重新确认承诺日期", "production_batches", batch["id"])

    if not phases:
        phase = None
    else:
        phase = next(iter(phases)) if len(phases) == 1 else "mixed"
    return {"order_id": order["id"], "order_no": order["order_no"],
            "project_name": order["project_name"], "phase": phase,
            "assessable": not missing, "missing_reasons": list(missing.values())}


@app.get("/api/overview")
def overview(user: dict = Depends(require_user)):
    with writing() as conn:
        sync_tasks(conn)
        open_rows = rows(conn, "SELECT * FROM orders WHERE status='open' ORDER BY updated_at DESC")
        orders = [order_detail(conn, row["id"]) for row in open_rows]
        assessment = [order_assessment(conn, row) for row in open_rows]
        assessable = sum(item["assessable"] for item in assessment)
        risks = []
        for delivery in rows(conn, "SELECT * FROM delivery_batches"):
            risks.extend(delivery_risks(conn, delivery, delivery_forecast(conn, delivery)))
        for batch in rows(conn, "SELECT * FROM production_batches WHERE phase='rd'"):
            risks.extend(phase_target_risks(conn, batch))
        tasks = rows(conn, "SELECT * FROM tasks WHERE status='open' ORDER BY condition_active DESC,updated_at DESC")
        imports = rows(conn, "SELECT kind,MAX(created_at) AS last_import_at FROM import_batches GROUP BY kind")
        return {"summary": {"orders_open": len(orders), "red": sum(r["level"] == "red" for r in risks),
                            "yellow": sum(r["level"] == "yellow" for r in risks), "tasks_open": len(tasks),
                            "assessable": assessable, "unassessable": len(assessment) - assessable,
                            "coverage_percent": round(100 * assessable / len(assessment)) if assessment else 0},
                "risks": risks, "tasks": tasks, "orders": orders, "assessment": assessment,
                "sources": {item["kind"]: item["last_import_at"] for item in imports}}


@app.get("/api/tasks")
def get_tasks(status: str | None = None, user: dict = Depends(require_user)):
    with writing() as conn:
        sync_tasks(conn)
        if status and status not in {"open", "done"}:
            raise HTTPException(422, "状态无效")
        tasks = rows(conn, "SELECT * FROM tasks WHERE (? IS NULL OR status=?) ORDER BY condition_active DESC,updated_at DESC", (status, status))
        context_cache: dict[tuple[str, int], dict] = {}
        for task in tasks:
            key = (task["entity_type"], task["entity_id"])
            if key not in context_cache:
                context_cache[key] = task_navigation_context(conn, task)
            task.update(context_cache[key])
            if task["entity_type"] == "inventory":
                task["material_code"] = task["task_key"].split(":", 1)[-1]
            elif task["kind"] in {"missing_eta", "eta_confirm"}:
                task["material_code"] = task["task_key"].split(":", 2)[-1]
        return tasks


def task_navigation_context(conn: sqlite3.Connection, task: dict) -> dict:
    """Resolve a task's source record so the worklist can open the right page."""
    entity_type, entity_id = task["entity_type"], task["entity_id"]
    if entity_type == "orders":
        return {"order_id": entity_id}
    if entity_type == "inventory":
        return {}
    queries = {
        "order_lines": "SELECT l.id AS order_line_id,l.order_id FROM order_lines l WHERE l.id=?",
        "production_batches": """SELECT p.id AS production_batch_id,p.order_line_id,l.order_id
            FROM production_batches p JOIN order_lines l ON l.id=p.order_line_id WHERE p.id=?""",
        "batch_boards": """SELECT b.id AS batch_board_id,p.id AS production_batch_id,p.order_line_id,l.order_id
            FROM batch_boards b JOIN production_batches p ON p.id=b.production_batch_id
            JOIN order_lines l ON l.id=p.order_line_id WHERE b.id=?""",
        "stages": """SELECT s.id AS stage_id,p.id AS production_batch_id,p.order_line_id,l.order_id
            FROM stages s JOIN production_batches p ON p.id=s.production_batch_id
            JOIN order_lines l ON l.id=p.order_line_id WHERE s.id=?""",
        "board_stages": """SELECT s.id AS board_stage_id,b.id AS batch_board_id,p.id AS production_batch_id,
            p.order_line_id,l.order_id FROM board_stages s JOIN batch_boards b ON b.id=s.batch_board_id
            JOIN production_batches p ON p.id=b.production_batch_id
            JOIN order_lines l ON l.id=p.order_line_id WHERE s.id=?""",
        "delivery_batches": """SELECT d.id AS delivery_batch_id,d.order_line_id,l.order_id
            FROM delivery_batches d JOIN order_lines l ON l.id=d.order_line_id WHERE d.id=?""",
        "legacy_records": """SELECT r.order_id,r.production_batch_id,p.order_line_id
            FROM legacy_records r LEFT JOIN production_batches p ON p.id=r.production_batch_id WHERE r.id=?""",
        "receipts": """SELECT r.production_batch_id,r.material_code,p.order_line_id,l.order_id
            FROM receipts r LEFT JOIN production_batches p ON p.id=r.production_batch_id
            LEFT JOIN order_lines l ON l.id=p.order_line_id WHERE r.id=?""",
        "substitutes": """SELECT s.production_batch_id,s.original_material_code AS material_code,p.order_line_id,l.order_id
            FROM substitutes s JOIN production_batches p ON p.id=s.production_batch_id
            JOIN order_lines l ON l.id=p.order_line_id WHERE s.id=?""",
    }
    query = queries.get(entity_type)
    if not query:
        return {}
    return one(conn, query, (entity_id,)) or {}


@app.patch("/api/tasks/{task_id}")
def patch_task(task_id: int, data: dict, user: dict = Depends(require_user)):
    changes = normalize_changes(data.get("changes", {}), {"assignee_id", "status", "note"}, reason=data.get("reason"))
    with writing() as conn:
        if changes.get("status") == "done":
            sync_tasks(conn)
        old = must_get(conn, "tasks", task_id)
        if "status" in changes:
            if changes["status"] not in {"open", "done"}:
                raise HTTPException(422, "状态无效")
            if changes["status"] == "done" and user["id"] != old["assignee_id"] and user["role"] != "admin":
                raise HTTPException(403, "只有当前责任人或管理员可确认完成待办")
            if changes["status"] == "done" and old["condition_active"]:
                raise HTTPException(409, "问题条件仍存在，请先处理源记录；条件恢复后再确认待办完成")
        if "assignee_id" in changes:
            if changes["assignee_id"] is None:
                raise HTTPException(422, "转派必须选择已启用且已获批的账号")
            assignee = must_get(conn, "users", int(changes["assignee_id"]))
            if not assignee["active"] or assignee["status"] != "active":
                raise HTTPException(422, "接收人账号未启用或未获批，请选择其他账号")
        return patch_record(conn, "tasks", task_id, int(need(data, "version")), changes, user["id"], data.get("reason"))


@app.get("/api/audit")
def get_audit(entity_type: str | None = None, entity_id: int | None = None,
              limit: int = 200, user: dict = Depends(require_user)):
    if not 1 <= limit <= 1000:
        raise HTTPException(422, "limit 应为 1 到 1000")
    with reading() as conn:
        return rows(conn, "SELECT a.*,u.display_name AS actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor_id WHERE (? IS NULL OR a.entity_type=?) AND (? IS NULL OR a.entity_id=?) ORDER BY a.id DESC LIMIT ?",
                    (entity_type, entity_type, entity_id, entity_id, limit))


@app.get("/api/holidays")
def get_holidays(user: dict = Depends(require_user)):
    with reading() as conn:
        return rows(conn, "SELECT * FROM holidays ORDER BY day")


@app.put("/api/holidays/{day}")
def put_holiday(day: str, data: dict, user: dict = Depends(require_user)):
    admin(user)
    valid_date(day, required=True)
    if not isinstance(data.get("is_workday"), bool):
        raise HTTPException(422, "is_workday 应为布尔值")
    with writing() as conn:
        old = one(conn, "SELECT * FROM holidays WHERE day=?", (day,))
        conn.execute("INSERT INTO holidays(day,is_workday,description,updated_by,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET is_workday=excluded.is_workday,description=excluded.description,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                     (day, int(data["is_workday"]), data.get("description"), user["id"], utcnow()))
        result = one(conn, "SELECT * FROM holidays WHERE day=?", (day,))
        audit(conn, "holidays", 0, "update", user["id"], old, result)
        return result


@app.get("/api/backups")
def get_backups(user: dict = Depends(require_user)):
    admin(user)
    folder = DB_PATH.parent / "backups"
    if not folder.exists():
        return []
    return [{"filename": file.name, "size": file.stat().st_size,
             "modified_at": datetime.fromtimestamp(file.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
            for file in sorted(folder.glob("pcba_*.db"), reverse=True)]


@app.post("/api/backups")
def create_backup(user: dict = Depends(require_user)):
    admin(user)
    with APP_LOCK:
        path = backup_database(DB_PATH)
    return {"filename": path.name, "size": path.stat().st_size}


@app.post("/api/backups/{filename}/restore")
def restore_backup(filename: str, user: dict = Depends(require_user)):
    admin(user)
    if not filename.startswith("pcba_") or not filename.endswith(".db") or Path(filename).name != filename:
        raise HTTPException(422, "备份文件名无效")
    source_path = DB_PATH.parent / "backups" / filename
    if not source_path.is_file():
        raise HTTPException(404, "备份不存在")
    with APP_LOCK:
        with closing(sqlite3.connect(source_path)) as source, source:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise HTTPException(409, "备份完整性检查未通过")
            safety = backup_database(DB_PATH)
            with closing(sqlite3.connect(DB_PATH, timeout=15)) as destination, destination:
                source.backup(destination)
                destination.execute("DELETE FROM sessions")
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise HTTPException(500, "恢复后数据库完整性检查未通过；请使用自动生成的安全备份")
    return {"ok": True, "safety_backup": safety.name, "relogin_required": True}


def daily_backup_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            folder = DB_PATH.parent / "backups"
            local_now = datetime.now(LOCAL_TZ)
            today_prefix = "pcba_" + local_now.strftime("%Y%m%d")
            if local_now.hour >= 2 and not any(folder.glob(today_prefix + "_*.db")):
                with APP_LOCK:
                    backup_database(DB_PATH)
        except Exception as exc:
            # Startup and service logs remain the operator-visible record of backup failures.
            print(f"PCBA backup failed: {exc}", flush=True)
        stop.wait(60)


_backup_stop = threading.Event()


@app.on_event("startup")
def start_daily_backup():
    _backup_stop.clear()
    threading.Thread(target=daily_backup_loop, args=(_backup_stop,), daemon=True, name="pcba-backup").start()


@app.on_event("shutdown")
def stop_daily_backup():
    _backup_stop.set()


def existing_import_rows(conn: sqlite3.Connection, kind: str) -> list[dict]:
    if kind == "orders":
        result = []
        for row in rows(conn, "SELECT l.id,l.line_no,l.product_code,l.product_name,l.quantity,o.order_no,o.customer FROM order_lines l JOIN orders o ON o.id=l.order_id"):
            delivery = one(conn, "SELECT due_date,promised_date FROM delivery_batches WHERE order_line_id=? AND batch_no=?", (row["id"], f"KIS-{row['line_no']}"))
            result.append({key: value for key, value in row.items() if key != "id"} | (delivery or {}))
        return result
    if kind == "bom":
        return rows(conn, "SELECT product_code,bom_version,source_line_no,material_code,material_name,quantity_per,loss_rate,unit_price FROM bom_catalog")
    if kind == "inventory":
        codes = [r["material_code"] for r in rows(conn, "SELECT DISTINCT material_code FROM inventory_rows")]
        return [{"material_code": code, "quantity": latest_stock(conn, code)["quantity"],
                 "as_of": datetime.fromisoformat(latest_stock(conn, code)["as_of"]).astimezone(LOCAL_TZ).date().isoformat(),
                 "basis": latest_stock(conn, code)["basis"]} for code in codes]
    if kind == "purchases":
        return rows(conn, "SELECT reference,source_line_no,material_code,quantity,eta FROM purchases")
    if kind == "receipts":
        return [{"reference": r["source_ref"], "source_line_no": r["source_line_no"], "material_code": r["material_code"],
                 "quantity": r["quantity"], "received_at": datetime.fromisoformat(r["received_at"]).astimezone(LOCAL_TZ).date().isoformat(),
                 "iqc_status": r["iqc_status"], "accepted_quantity": r["accepted_quantity"]}
                for r in rows(conn, "SELECT * FROM receipts WHERE source_ref IS NOT NULL")]
    return []


@app.post("/api/imports/inspect")
def inspect_import(file: UploadFile = File(...), header_row: int = Form(1), kind: str | None = Form(None),
                   user: dict = Depends(require_user)):
    content = file.file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "文件超过 20 MB，请按单号或日期分批导出")
    return inspect_upload(file.filename or "", content, header_row=header_row, kind=kind)


@app.get("/api/imports/ai-status")
def ai_import_status(user: dict = Depends(require_user)):
    return {"configured": bool(deepseek_import.api_key()), "model": deepseek_import.MODEL}


def prune_unapplied_ai_extractions(conn: sqlite3.Connection) -> None:
    """Keep recent candidates for retries; applied candidates remain for audit."""
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    safe = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    active = []
    for row in rows(conn, "SELECT preview_json FROM import_previews WHERE created_at>=?", (safe,)):
        extraction_id = json.loads(row["preview_json"]).get("ai_extraction_id")
        if extraction_id:
            active.append(extraction_id)
    protected = f" AND id NOT IN ({','.join('?' for _ in active)})" if active else ""
    conn.execute("DELETE FROM ai_extractions WHERE applied_import_batch_id IS NULL AND created_at<?" + protected,
                 (old, *active))
    conn.execute("DELETE FROM ai_extractions WHERE applied_import_batch_id IS NULL AND created_at<? "
                 "AND id NOT IN (SELECT id FROM ai_extractions WHERE applied_import_batch_id IS NULL "
                 "ORDER BY created_at DESC,id DESC LIMIT 50)" + protected, (safe, *active))


@app.post("/api/imports/ai-extract")
def ai_extract_import(file: UploadFile = File(...), kind: str = Form(...),
                      user: dict = Depends(require_user)):
    if kind not in SCHEMAS:
        raise HTTPException(422, "导入类型无效")
    filename = file.filename or ""
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(422, "DeepSeek 智能解析仅支持 PDF")
    content = file.file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "文件超过 20 MB，请按单号或日期拆分 PDF")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(422, "文件不是有效的 PDF")
    source_hash = hashlib.sha256(content).hexdigest()
    with writing() as conn:
        prune_unapplied_ai_extractions(conn)
        cached = one(conn, "SELECT id,metadata_json FROM ai_extractions WHERE created_by=? AND kind=? AND source_sha256=? AND model=?",
                     (user["id"], kind, source_hash, deepseek_import.MODEL))
    if cached:
        return {**json.loads(cached["metadata_json"]), "ai_extraction_id": cached["id"], "cached": True}
    key = deepseek_import.api_key()
    if not key:
        raise HTTPException(503, "主机尚未配置 DeepSeek API Key；请由管理员在 server/.env 设置 DEEPSEEK_API_KEY")
    try:
        candidate = deepseek_import.extract_pdf_candidate(content, kind, key)
    except deepseek_import.AiExtractionError as exc:
        raise HTTPException(422, str(exc)) from exc
    csv_blob = deepseek_import.candidate_csv(candidate)
    inspected = inspect_upload("AI.csv", csv_blob, kind=kind)
    if inspected["errors"] or not inspected["sheets"]:
        raise HTTPException(422, "AI 候选表格无法进入导入预览；请重新解析或手工整理为 Excel")
    result = {**inspected, "ai": True, "ocr": False,
              "sheets": [{**sheet, "ai": True, "ocr": False} for sheet in inspected["sheets"]],
              "suggested_fixed_fields": candidate["suggested_fixed_fields"],
              "warnings": [{"code": "ai_review", "message": "AI 仅生成候选表格；请核对原 PDF 的单号、数量、日期、价格及每行内容后再应用"}],
              "page_count": candidate["page_count"]}
    extraction_id = secrets.token_urlsafe(20)
    with writing() as conn:
        existing = one(conn, "SELECT id,metadata_json FROM ai_extractions WHERE created_by=? AND kind=? AND source_sha256=? AND model=?",
                       (user["id"], kind, source_hash, deepseek_import.MODEL))
        if existing:
            return {**json.loads(existing["metadata_json"]), "ai_extraction_id": existing["id"], "cached": True}
        conn.execute("INSERT INTO ai_extractions(id,kind,filename,source_sha256,source_blob,csv_blob,metadata_json,provider,model,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (extraction_id, kind, filename, source_hash, content, csv_blob,
                      json.dumps(result, ensure_ascii=False), "DeepSeek", deepseek_import.MODEL, user["id"], utcnow()))
    return {**result, "ai_extraction_id": extraction_id, "cached": False}


def ai_candidate_for(conn: sqlite3.Connection, extraction_id: str, kind: str,
                     original: bytes, user_id: int) -> sqlite3.Row:
    extraction = one(conn, "SELECT * FROM ai_extractions WHERE id=? AND created_by=?",
                     (extraction_id, user_id))
    if (not extraction or extraction["kind"] != kind or
            extraction["source_sha256"] != hashlib.sha256(original).hexdigest() or
            extraction["source_blob"] != original):
        raise HTTPException(422, "AI 解析结果与当前用户、单据类型或原 PDF 不匹配；请重新解析")
    return extraction


@app.post("/api/imports/preview")
def preview_import_route(file: UploadFile = File(...), kind: str = Form(...), column_map: str = Form("{}"),
                         sheet_name: str | None = Form(None), header_row: int = Form(1),
                         key_fields: str | None = Form(None), snapshot_as_of: str | None = Form(None),
                         snapshot_basis: str | None = Form(None), fixed_fields: str = Form("{}"),
                         bom_parent_column: str | None = Form(None),
                         ai_extraction_id: str | None = Form(None),
                         user: dict = Depends(require_user)):
    if kind not in SCHEMAS:
        raise HTTPException(422, "导入类型无效")
    try:
        mapping = json.loads(column_map)
        keys = json.loads(key_fields) if key_fields else None
        fixed = json.loads(fixed_fields)
    except json.JSONDecodeError:
        raise HTTPException(422, "列映射、固定字段或唯一键不是合法 JSON")
    if not isinstance(mapping, dict) or not isinstance(fixed, dict) or (keys is not None and not isinstance(keys, list)):
        raise HTTPException(422, "列映射和固定字段应为对象，唯一键应为数组")
    parent_column = bom_parent_column.strip() if isinstance(bom_parent_column, str) and bom_parent_column.strip() else None
    if kind == "inventory":
        if snapshot_as_of is not None:
            valid_datetime(snapshot_as_of + "T23:59:59" if len(snapshot_as_of) == 10 else snapshot_as_of)
        if snapshot_basis is not None and snapshot_basis not in {"on_hand", "available"}:
            raise HTTPException(422, "库存口径无效")
    content = file.file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "文件超过 20 MB，请按单号或日期分批导出")
    ai_id = ai_extraction_id.strip() if isinstance(ai_extraction_id, str) and ai_extraction_id.strip() else None
    parse_name, parse_content, ai_model = file.filename or "", content, None
    with reading() as conn:
        if ai_id:
            if Path(parse_name).suffix.lower() != ".pdf":
                raise HTTPException(422, "AI 预览必须附上原始 PDF")
            extracted = ai_candidate_for(conn, ai_id, kind, content, user["id"])
            parse_name, parse_content, ai_model = "AI.csv", extracted["csv_blob"], extracted["model"]
        existing = existing_import_rows(conn, kind)
    outcome = preview_import(kind, parse_name, parse_content, mapping, existing_rows=existing,
                             key_fields=keys, sheet_name=sheet_name, header_row=header_row,
                             fixed_fields=fixed, bom_parent_column=parent_column)
    if ai_id:
        outcome["warnings"].append({"row_number": None, "field": None, "code": "ai_review",
                                    "message": "AI 候选结果须与原 PDF 逐行核对；确认列、日期、数量和价格后再选择应用"})
        outcome["summary"]["ai"] = True
    if kind == "orders":
        with reading() as conn:
            for entry in outcome["rows"]:
                values = entry["values"]
                if values.get("order_no") and values.get("line_no") and (values.get("due_date") or values.get("promised_date")):
                    manual = one(conn, "SELECT d.id FROM delivery_batches d JOIN order_lines l ON l.id=d.order_line_id JOIN orders o ON o.id=l.order_id WHERE o.order_no=? AND l.line_no=? AND d.batch_no<>? LIMIT 1",
                                 (values["order_no"], values["line_no"], f"KIS-{values['line_no']}"))
                    if manual:
                        outcome["warnings"].append({"row_number": entry["row_number"], "field": "due_date",
                                                    "code": "manual_delivery_batches",
                                                    "message": "该订单行已有手工交付批次；导入日期只会更新 KIS 默认批次，请核对分批日期和数量"})
    if kind == "inventory":
        present_as_of = {r["values"].get("as_of") for r in outcome["rows"] if r["values"].get("as_of")}
        present_basis = {r["values"].get("basis") for r in outcome["rows"] if r["values"].get("basis")}
        if snapshot_as_of and present_as_of and present_as_of != {snapshot_as_of[:10]}:
            outcome["errors"].append({"row_number": None, "field": "as_of", "message": "文件日期与人工指定快照日冲突"})
        if snapshot_basis and present_basis and present_basis != {snapshot_basis}:
            outcome["errors"].append({"row_number": None, "field": "basis", "message": "文件库存口径与人工指定口径冲突"})
        if not (snapshot_as_of or present_as_of) or not (snapshot_basis or present_basis):
            outcome["errors"].append({"row_number": None, "field": "snapshot", "message": "应用库存前必须明确快照日期和口径"})
    preview_id = secrets.token_urlsafe(20)
    metadata = {"column_map": mapping, "fixed_fields": fixed, "bom_parent_column": parent_column,
                "key_fields": keys,
                "sheet_name": sheet_name, "header_row": header_row,
                "ai_extraction_id": ai_id, "ai_provider": "DeepSeek" if ai_id else None,
                "ai_model": ai_model,
                "snapshot_as_of": snapshot_as_of, "snapshot_basis": snapshot_basis, "outcome": outcome}
    with writing() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM import_previews WHERE created_at<?", (cutoff,))
        conn.execute("INSERT INTO import_previews(id,kind,filename,preview_json,source_blob,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                     (preview_id, kind, file.filename or "", json.dumps(metadata, ensure_ascii=False), content, user["id"], utcnow()))
        conn.execute("DELETE FROM import_previews WHERE id IN (SELECT id FROM import_previews ORDER BY created_at DESC LIMIT -1 OFFSET 20)")
    return {"preview_id": preview_id, **outcome}


def source_record(conn: sqlite3.Connection, kind: str, key: str) -> int | None:
    found = one(conn, "SELECT record_id FROM source_rows WHERE kind=? AND source_key=?", (kind, key))
    return found["record_id"] if found else None


def import_order_row(conn: sqlite3.Connection, values: dict, selected: set[str], key: str,
                     actor_id: int, reason: str | None) -> int:
    order_no, line_no = values["order_no"], values["line_no"]
    order = one(conn, "SELECT * FROM orders WHERE order_no=?", (order_no,))
    if not order:
        cur = conn.execute("INSERT INTO orders(order_no,customer,created_at,updated_at) VALUES(?,?,?,?)",
                           (order_no, values.get("customer") or "待确认客户", utcnow(), utcnow()))
        order = must_get(conn, "orders", cur.lastrowid)
        audit(conn, "orders", order["id"], "import_create", actor_id, None, order)
    elif "customer" in selected and values.get("customer") is not None and order["customer"] != values["customer"]:
        order = patch_record(conn, "orders", order["id"], order["version"], {"customer": values["customer"]}, actor_id, reason)
    line = one(conn, "SELECT * FROM order_lines WHERE order_id=? AND line_no=?", (order["id"], line_no))
    if not line:
        line = insert_line(conn, order["id"], {"line_no": line_no, "product_code": values["product_code"],
                           "product_name": values.get("product_name") or values["product_code"],
                           "quantity": values["quantity"]}, actor_id)
    else:
        changes = {k: values[k] for k in selected & {"product_code", "product_name", "quantity"}
                   if values.get(k) is not None and values[k] != line[k]}
        if changes:
            if "product_code" in changes and one(conn, "SELECT id FROM production_batches WHERE order_line_id=?", (line["id"],)):
                raise HTTPException(409, "已有生产批次，KIS 产品编码变化需人工处理")
            if "quantity" in changes:
                new_quantity = dec(changes["quantity"])
                production_total = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM production_batches WHERE order_line_id=?", (line["id"],))), Decimal(0))
                if production_total > new_quantity:
                    raise HTTPException(409, "KIS 数量小于已建生产批次，需先人工处理")
                deliveries = rows(conn, "SELECT * FROM delivery_batches WHERE order_line_id=?", (line["id"],))
                default_only = len(deliveries) == 1 and deliveries[0]["batch_no"] == f"KIS-{line_no}"
                if default_only:
                    default = deliveries[0]
                    if one(conn, "SELECT id FROM allocations WHERE delivery_batch_id=?", (default["id"],)) or one(conn, "SELECT id FROM events WHERE entity_type='delivery_batch' AND entity_id=?", (default["id"],)):
                        default_only = False
                delivery_total = sum((dec(r["quantity"]) for r in deliveries), Decimal(0))
                if delivery_total > new_quantity and not default_only:
                    raise HTTPException(409, "KIS 数量小于已拆分交付批次，需先人工处理")
                if default_only and dec(default["quantity"]) != new_quantity:
                    patch_record(conn, "delivery_batches", default["id"], default["version"],
                                 {"quantity": ds(new_quantity)}, actor_id, reason)
            line = patch_record(conn, "order_lines", line["id"], line["version"], changes, actor_id, reason)
    if selected & {"due_date", "promised_date"}:
        batch_no = f"KIS-{line_no}"
        delivery = one(conn, "SELECT * FROM delivery_batches WHERE order_line_id=? AND batch_no=?", (line["id"], batch_no))
        dates = {k: values.get(k) for k in selected & {"due_date", "promised_date"}}
        if delivery:
            changes = {k: v for k, v in dates.items() if delivery[k] != v}
            if changes:
                patch_record(conn, "delivery_batches", delivery["id"], delivery["version"], changes, actor_id, reason)
        else:
            current_qty = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM delivery_batches WHERE order_line_id=?", (line["id"],))), Decimal(0))
            remaining = dec(line["quantity"]) - current_qty
            if remaining <= 0:
                raise HTTPException(409, "订单已拆分交付批次，KIS 日期无法自动落到默认批次")
            cur = conn.execute("INSERT INTO delivery_batches(order_line_id,batch_no,quantity,due_date,promised_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                               (line["id"], batch_no, ds(remaining), dates.get("due_date"), dates.get("promised_date"), utcnow(), utcnow()))
            audit(conn, "delivery_batches", cur.lastrowid, "import_create", actor_id, None, must_get(conn, "delivery_batches", cur.lastrowid))
    sync_order_status(conn, order["id"])
    return line["id"]


def import_bom_row(conn: sqlite3.Connection, values: dict, selected: set[str], key: str,
                   actor_id: int, reason: str | None) -> int:
    row_id = source_record(conn, "bom", key)
    current = one(conn, "SELECT * FROM bom_catalog WHERE id=?", (row_id,)) if row_id else one(conn, "SELECT * FROM bom_catalog WHERE source_key=?", (key,))
    if current:
        changes = {k: values[k] for k in selected & {"material_code", "material_name", "quantity_per", "loss_rate", "unit_price"}
                   if values.get(k) is not None and values[k] != current[k]}
        if changes:
            patch_record(conn, "bom_catalog", current["id"], current["version"], changes, actor_id, reason)
        return current["id"]
    cur = conn.execute("INSERT INTO bom_catalog(product_code,bom_version,source_line_no,material_code,material_name,quantity_per,loss_rate,unit_price,source_key) VALUES(?,?,?,?,?,?,?,?,?)",
                       (values["product_code"], values["bom_version"], values.get("source_line_no"), values["material_code"],
                        values.get("material_name"), values["quantity_per"], values.get("loss_rate") or "0",
                        unit_price(values.get("unit_price")), key))
    audit(conn, "bom_catalog", cur.lastrowid, "import_create", actor_id, None, one(conn, "SELECT * FROM bom_catalog WHERE id=?", (cur.lastrowid,)))
    return cur.lastrowid


def import_purchase_row(conn: sqlite3.Connection, values: dict, selected: set[str], key: str,
                        actor_id: int, reason: str | None) -> int:
    row_id = source_record(conn, "purchases", key)
    current = one(conn, "SELECT * FROM purchases WHERE id=?", (row_id,)) if row_id else None
    if not current:
        if values.get("source_line_no"):
            current = one(conn, "SELECT * FROM purchases WHERE reference=? AND source_line_no=?", (values["reference"], values["source_line_no"]))
        else:
            candidates = rows(conn, "SELECT * FROM purchases WHERE reference=? AND material_code=? AND source_line_no IS NULL", (values["reference"], values["material_code"]))
            if len(candidates) > 1:
                raise HTTPException(409, "采购匹配键重复，请映射源行号")
            current = candidates[0] if candidates else None
    if current:
        changes = {k: values[k] for k in selected & {"quantity", "eta"} if values.get(k) != current[k]}
        if changes:
            if "eta" in changes:
                changes.update({"eta_is_estimate": 1, "eta_source": "KIS"})
            if "quantity" in changes:
                allocated = sum((dec(r["quantity"]) for r in rows(conn, "SELECT quantity FROM purchase_allocations WHERE purchase_id=?", (current["id"],))), Decimal(0))
                if dec(changes["quantity"]) < allocated:
                    raise HTTPException(409, "KIS 采购数量小于工作台已分配量")
            patch_record(conn, "purchases", current["id"], current["version"], changes, actor_id, reason)
        return current["id"]
    cur = conn.execute("INSERT INTO purchases(reference,source_line_no,material_code,quantity,eta,eta_is_estimate,eta_source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (values["reference"], values.get("source_line_no"), values["material_code"], values["quantity"],
                        values.get("eta"), 1, "KIS", utcnow(), utcnow()))
    audit(conn, "purchases", cur.lastrowid, "import_create", actor_id, None, must_get(conn, "purchases", cur.lastrowid))
    return cur.lastrowid


def import_receipt_row(conn: sqlite3.Connection, values: dict, selected: set[str], key: str,
                       actor_id: int, reason: str | None) -> int:
    row_id = source_record(conn, "receipts", key)
    current = one(conn, "SELECT * FROM receipts WHERE id=?", (row_id,)) if row_id else None
    if not current:
        if values.get("source_line_no"):
            current = one(conn, "SELECT * FROM receipts WHERE source_ref=? AND source_line_no=?", (values["reference"], values["source_line_no"]))
        else:
            candidates = rows(conn, "SELECT * FROM receipts WHERE source_ref=? AND material_code=? AND source_line_no IS NULL", (values["reference"], values["material_code"]))
            if len(candidates) > 1:
                raise HTTPException(409, "入库匹配键重复，请映射源行号")
            current = candidates[0] if candidates else None
    status = values.get("iqc_status") or "pending"
    accepted = values.get("accepted_quantity") or "0"
    if status == "pass" and not values.get("accepted_quantity"):
        raise HTTPException(422, "IQC 合格记录必须有明确合格数量")
    if current:
        mapping = {"quantity": "quantity", "received_at": "received_at", "iqc_status": "iqc_status", "accepted_quantity": "accepted_quantity"}
        changes = {target: (valid_datetime(values[field]) if field == "received_at" and values.get(field) else values.get(field))
                   for field, target in mapping.items() if field in selected and values.get(field) is not None and
                   (valid_datetime(values[field]) if field == "received_at" else values[field]) != current[target]}
        if changes:
            patch_record(conn, "receipts", current["id"], current["version"], changes, actor_id, reason)
            sync_ready_for_material(conn, current["material_code"])
        return current["id"]
    cur = conn.execute("INSERT INTO receipts(material_code,quantity,received_at,source_ref,source_line_no,iqc_status,accepted_quantity,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (values["material_code"], values["quantity"], valid_datetime(need(values, "received_at")),
                        values["reference"], values.get("source_line_no"), status, accepted, actor_id, utcnow()))
    audit(conn, "receipts", cur.lastrowid, "import_create", actor_id, None, must_get(conn, "receipts", cur.lastrowid))
    sync_ready_for_material(conn, values["material_code"])
    return cur.lastrowid


@app.post("/api/imports/apply")
def apply_import(data: dict, user: dict = Depends(require_user)):
    preview_id = str(need(data, "preview_id"))
    selections = data.get("selections")
    if not isinstance(selections, list) or not selections:
        raise HTTPException(422, "请逐项选择要应用的行和字段")
    if len({s.get("row_index") for s in selections if isinstance(s, dict)}) != len(selections):
        raise HTTPException(422, "选择中有重复行")
    reason = str(data.get("reason") or "").strip() or None
    with reading() as conn:
        saved = one(conn, "SELECT * FROM import_previews WHERE id=?", (preview_id,))
    if not saved or saved["created_by"] != user["id"]:
        raise HTTPException(404, "导入预览已过期或不属于当前用户")
    metadata = json.loads(saved["preview_json"])
    outcome = metadata["outcome"]
    if any(error.get("row_number") is None for error in outcome["errors"]):
        raise HTTPException(422, "预览有错误，不能应用")
    selected_rows = []
    for choice in selections:
        if not isinstance(choice, dict) or not isinstance(choice.get("row_index"), int):
            raise HTTPException(422, "选择格式应为 {row_index,fields?}")
        index = choice["row_index"]
        if not 0 <= index < len(outcome["rows"]):
            raise HTTPException(422, "选择的行不存在")
        row = outcome["rows"][index]
        if not row["valid"]:
            raise HTTPException(422, f"第 {row['row_number']} 行有错误，不能应用")
        fields = choice.get("fields")
        if fields is not None and (not isinstance(fields, list) or not set(fields).issubset(row["values"])):
            raise HTTPException(422, "选择字段无效")
        selected = set(row["values"] if row["action"] == "create" else
                       (fields if fields is not None else [change["field"] for change in row["changes"]]))
        if not selected:
            continue
        if row["action"] != "create" and selected & {"quantity", "due_date", "promised_date", "eta", "quantity_per", "loss_rate", "unit_price"} and not reason:
            raise HTTPException(422, "更新数量、价格或日期必须填写原因")
        selected_rows.append((row, selected))
    if not selected_rows:
        raise HTTPException(422, "所选内容没有需要应用的差异")
    with APP_LOCK:
        safety = backup_database(DB_PATH)
        with writing() as conn:
            saved = one(conn, "SELECT * FROM import_previews WHERE id=?", (preview_id,))
            if not saved:
                raise HTTPException(409, "预览已经应用，请重新预览")
            # Re-run the same parser under the write lock to catch edits made after preview.
            parse_name, parse_content = saved["filename"], saved["source_blob"]
            if metadata.get("ai_extraction_id"):
                extracted = ai_candidate_for(conn, metadata["ai_extraction_id"], saved["kind"],
                                             saved["source_blob"], user["id"])
                parse_name, parse_content = "AI.csv", extracted["csv_blob"]
            current = preview_import(saved["kind"], parse_name, parse_content,
                                     metadata["column_map"], existing_rows=existing_import_rows(conn, saved["kind"]),
                                     key_fields=metadata["key_fields"], sheet_name=metadata["sheet_name"],
                                     header_row=metadata["header_row"],
                                     fixed_fields=metadata.get("fixed_fields", {}),
                                     bom_parent_column=metadata.get("bom_parent_column"))
            for original, selected in selected_rows:
                fresh = current["rows"][original["row_index"]]
                if fresh["key"] != original["key"] or not fresh["valid"]:
                    raise HTTPException(409, f"第 {original['row_number']} 行匹配状态已变化，请重新预览")
                old_diff = {c["field"]: c for c in original["changes"]}
                new_diff = {c["field"]: c for c in fresh["changes"]}
                if original["action"] == "create" and fresh["action"] != "create":
                    raise HTTPException(409, f"第 {original['row_number']} 行已由其他人创建")
                for field in selected:
                    if field in old_diff and (field not in new_diff or old_diff[field]["old"] != new_diff[field]["old"]):
                        raise HTTPException(409, f"第 {original['row_number']} 行 {field} 已被其他人修改")
            batch_cur = conn.execute("INSERT INTO import_batches(kind,filename,source_blob,selection_json,rows_applied,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                                     (saved["kind"], saved["filename"], saved["source_blob"],
                                      json.dumps({"selections": selections, "reason": reason, "metadata": {k: v for k, v in metadata.items() if k != "outcome"}}, ensure_ascii=False),
                                      len(selected_rows), user["id"], utcnow()))
            batch_id = batch_cur.lastrowid
            if metadata.get("ai_extraction_id"):
                conn.execute("UPDATE ai_extractions SET applied_import_batch_id=? WHERE id=?",
                             (batch_id, metadata["ai_extraction_id"]))
            inventory_items = []
            inventory_as_of = metadata.get("snapshot_as_of")
            inventory_basis = metadata.get("snapshot_basis")
            for row, selected in selected_rows:
                values = row["values"]
                kind = saved["kind"]
                if kind == "orders":
                    record_id = import_order_row(conn, values, selected, row["key"], user["id"], reason)
                elif kind == "bom":
                    record_id = import_bom_row(conn, values, selected, row["key"], user["id"], reason)
                elif kind == "purchases":
                    record_id = import_purchase_row(conn, values, selected, row["key"], user["id"], reason)
                elif kind == "receipts":
                    record_id = import_receipt_row(conn, values, selected, row["key"], user["id"], reason)
                elif kind == "inventory":
                    inventory_as_of = inventory_as_of or values.get("as_of")
                    inventory_basis = inventory_basis or values.get("basis")
                    inventory_items.append({"material_code": values["material_code"], "quantity": values["quantity"], "key": row["key"]})
                    record_id = None
                else:
                    raise HTTPException(422, "导入类型无效")
                merged = json.loads(one(conn, "SELECT values_json FROM source_rows WHERE kind=? AND source_key=?", (kind, row["key"]))["values_json"]) if source_record(conn, kind, row["key"]) is not None else {}
                merged.update({field: values[field] for field in selected})
                for field in SCHEMAS[kind]["key"]:
                    if field in values:
                        merged[field] = values[field]
                if values.get("source_line_no"):
                    merged["source_line_no"] = values["source_line_no"]
                conn.execute("INSERT INTO source_rows(kind,source_key,record_id,values_json,last_import_batch_id) VALUES(?,?,?,?,?) ON CONFLICT(kind,source_key) DO UPDATE SET record_id=excluded.record_id,values_json=excluded.values_json,last_import_batch_id=excluded.last_import_batch_id",
                             (kind, row["key"], record_id, json.dumps(merged, ensure_ascii=False), batch_id))
            if inventory_items:
                if not inventory_as_of or not inventory_basis:
                    raise HTTPException(422, "库存应用需明确快照日期和口径")
                raw_as_of = str(inventory_as_of)
                day_end = len(raw_as_of) == 10
                as_of = valid_datetime(raw_as_of + "T23:59:59" if day_end else raw_as_of)
                cur = conn.execute("INSERT INTO inventory_snapshots(as_of,as_of_precision,basis,source_name,created_by,created_at) VALUES(?,?,?,?,?,?)",
                                   (as_of, "day_end" if day_end else "timestamp", inventory_basis, saved["filename"], user["id"], utcnow()))
                snapshot_id = cur.lastrowid
                for item in inventory_items:
                    row_cur = conn.execute("INSERT INTO inventory_rows(snapshot_id,material_code,quantity) VALUES(?,?,?)",
                                           (snapshot_id, item["material_code"], item["quantity"]))
                    conn.execute("UPDATE source_rows SET record_id=? WHERE kind='inventory' AND source_key=?", (row_cur.lastrowid, item["key"]))
                    sync_ready_for_material(conn, item["material_code"])
                audit(conn, "inventory_snapshot", snapshot_id, "import_create", user["id"], None,
                      {"as_of": as_of, "basis": inventory_basis, "items": inventory_items}, reason)
            conn.execute("DELETE FROM import_previews WHERE id=?", (preview_id,))
            audit(conn, "import_batches", batch_id, "apply", user["id"], None,
                  {"kind": saved["kind"], "filename": saved["filename"], "rows_applied": len(selected_rows)}, reason)
            return {"id": batch_id, "kind": saved["kind"], "rows_applied": len(selected_rows),
                    "pre_import_backup": safety.name}


@app.get("/api/imports")
def get_imports(user: dict = Depends(require_user)):
    with reading() as conn:
        return rows(conn, "SELECT id,kind,filename,selection_json,rows_applied,created_by,created_at FROM import_batches ORDER BY id DESC LIMIT 100")


@app.get("/api/bom-catalog")
def get_bom_catalog(product_code: str | None = None, bom_version: str | None = None,
                    user: dict = Depends(require_user)):
    with reading() as conn:
        return rows(conn, "SELECT * FROM bom_catalog WHERE (? IS NULL OR product_code=?) AND (? IS NULL OR bom_version=?) ORDER BY product_code,bom_version,source_line_no,id",
                    (product_code, product_code, bom_version, bom_version))


@app.get("/api/bom-catalog/summary")
def get_bom_catalog_summary(product_code: str, bom_version: str, quantity: str = "1",
                            user: dict = Depends(require_user)):
    if not product_code.strip() or not bom_version.strip():
        raise HTTPException(422, "产品编码和 BOM 版本不能为空")
    with reading() as conn:
        items = rows(conn, "SELECT * FROM bom_catalog WHERE product_code=? AND bom_version=? ORDER BY source_line_no,id",
                     (product_code, bom_version))
        return bom_pricing(items, quantity)


@app.patch("/api/bom-catalog/prices")
def patch_bom_catalog_prices(data: dict, user: dict = Depends(require_user)):
    updates = data.get("items")
    if not isinstance(updates, list) or not 0 < len(updates) <= 2000:
        raise HTTPException(422, "请选择 1 至 2000 条 BOM 价格行")
    reason = str(data.get("reason") or "").strip()
    if not reason:
        raise HTTPException(422, "修改价格必须填写原因")
    quantity = dec(data.get("quantity", "1"), allow_zero=False)
    with writing() as conn:
        scope = None
        seen = set()
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("id"), int) or not isinstance(update.get("version"), int):
                raise HTTPException(422, "价格行须包含整数 id 和 version")
            row_id = update["id"]
            if row_id in seen or "unit_price" not in update:
                raise HTTPException(422, "价格行重复或缺少单价")
            seen.add(row_id)
            current = must_get(conn, "bom_catalog", row_id)
            row_scope = (current["product_code"], current["bom_version"])
            if scope is not None and scope != row_scope:
                raise HTTPException(422, "一次只能修改同一产品和 BOM 版本的价格")
            scope = row_scope
            patch_record(conn, "bom_catalog", row_id, update["version"],
                         {"unit_price": unit_price(update["unit_price"])}, user["id"], reason)
        priced = rows(conn, "SELECT * FROM bom_catalog WHERE product_code=? AND bom_version=? ORDER BY source_line_no,id", scope)
        return {"items": priced, "bom_pricing": bom_pricing(priced, quantity)}


@app.patch("/api/bom-catalog/{item_id}")
def patch_bom_catalog_item(item_id: int, data: dict, user: dict = Depends(require_user)):
    changes = normalize_changes(data.get("changes", {}), {"unit_price"},
                                protected={"unit_price"}, reason=data.get("reason"))
    changes["unit_price"] = unit_price(changes["unit_price"])
    with writing() as conn:
        return patch_record(conn, "bom_catalog", item_id, int(need(data, "version")),
                            changes, user["id"], data.get("reason"))


def legacy_source_identity(product: dict) -> tuple[str, str]:
    """Use the old product ID where present; fall back to exact-content dedup."""
    serialized = json.dumps(product, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    old_id = product.get("id")
    source_key = f"id:{old_id}" if old_id is not None and old_id != "" else f"sha256:{digest}"
    return source_key, digest


def legacy_missing_fields(conn: sqlite3.Connection, record: dict) -> list[str]:
    """Recompute pending fields from current records, keeping the old JSON intact."""
    order = must_get(conn, "orders", record["order_id"])
    missing: list[str] = []

    def add(label: str) -> None:
        if label not in missing:
            missing.append(label)

    lines = rows(conn, "SELECT * FROM order_lines WHERE order_id=? ORDER BY id", (order["id"],))
    if record["order_line_id"]:
        lines = [line for line in lines if line["id"] == record["order_line_id"]]
    all_batches = [batch for line in lines for batch in rows(
        conn, "SELECT * FROM production_batches WHERE order_line_id=?", (line["id"],))]
    rd_only = bool(all_batches) and all(batch["phase"] == "rd" for batch in all_batches)
    customer_target = any(batch["target_kind"] == "customer" for batch in all_batches)
    if order["order_no"].startswith("PCBA-TEMP-") and not rd_only and (
            not all_batches or any(batch["phase"] != "rd" and not batch["phase_order_no"] for batch in all_batches)):
        add("正式销售订单号")
    if (not order["customer"] or order["customer"] == "待补录") and (not rd_only or customer_target):
        add("客户")
    if order["owner_id"] is None:
        add("负责人账号待绑定" if legacy_owner_name(record) else "业务负责人")
    if not lines:
        add("订单数量")
        add("产品编码")
        add("生产批次")
    for line in lines:
        if not line["product_code"] or line["product_code"].startswith("PCBA-TEMP-") or line["product_code"] == "待补录":
            add("产品编码")
        if dec(line["quantity"]) <= 0:
            add("订单数量")
        batches = rows(conn, "SELECT * FROM production_batches WHERE order_line_id=?", (line["id"],))
        if not batches:
            add("生产批次")
        if not line["bom_version"] or not batches or any(not batch["bom_locked"] for batch in batches):
            add("BOM 版本与逐项物料")
        if any(batch["phase"] in {"pp", "mp", "unclassified"} and not batch["vendor"] for batch in batches):
            add("外协板厂")
        if any(batch["phase"] in {"pp", "mp", "unclassified"} and not batch["commit_start"] for batch in batches):
            add("板厂承诺开工日")
        if any(batch["phase"] in {"pp", "mp", "unclassified"} and not batch["commit_finish"] for batch in batches):
            add("板厂承诺完工日")
        for batch in batches:
            if batch["phase"] == "rd":
                if not batch["target_kind"]:
                    add("研发打样目标口径")
                if not batch["target_date"]:
                    add("研发打样目标日")
                if not batch["board_eta"]:
                    add("预计到板日")

        deliveries = rows(conn, "SELECT * FROM delivery_batches WHERE order_line_id=?", (line["id"],))
        if not rd_only and (not deliveries or sum((dec(item["quantity"]) for item in deliveries), Decimal(0)) != dec(line["quantity"])):
            add("交付批次与数量关联")
        if not rd_only and (not deliveries or any(not item["due_date"] for item in deliveries)):
            add("客户要求交期")
        if not rd_only and (not deliveries or any(not item["promised_date"] for item in deliveries)):
            add("业务正式承诺交期")
        if deliveries and not rd_only:
            for delivery in deliveries:
                total = sum((dec(item["quantity"]) for item in rows(conn, "SELECT quantity FROM allocations WHERE delivery_batch_id=?", (delivery["id"],))), Decimal(0))
                if total != dec(delivery["quantity"]):
                    add("交付批次与数量关联")
                    break

    if not lines:
        for label in ("BOM 版本与逐项物料", "外协板厂", "板厂承诺开工日", "板厂承诺完工日",
                      "交付批次与数量关联", "客户要求交期", "业务正式承诺交期"):
            add(label)
    return missing


@app.post("/api/legacy/preview")
def preview_legacy(file: UploadFile = File(...), user: dict = Depends(require_user)):
    content = file.file.read(LEGACY_MAX_BYTES + 1)
    if len(content) > LEGACY_MAX_BYTES:
        raise HTTPException(413, "导入的 JSON 超过 10 MB，请分批整理")
    outcome = preview_legacy_json(file.filename or "", content)
    by_key: dict[str, list[dict]] = {}
    for product in outcome["products"]:
        product["action"] = "invalid"
        if product["valid"]:
            source_key, _ = legacy_source_identity(product["source"])
            product["source_key"] = source_key
            by_key.setdefault(source_key, []).append(product)
    for source_key, matching in by_key.items():
        if len(matching) > 1:
            for product in matching:
                product["valid"] = False
                outcome["errors"].append({"path": f"products[{product['index']}].id", "code": "duplicate_source_id",
                                          "message": "此文件内存在重复的导入产品 ID 或完全相同的无 ID 产品，请先核对"})

    with writing() as conn:
        for product in outcome["products"]:
            if not product["valid"]:
                continue
            old = one(conn, "SELECT order_id,source_hash FROM legacy_records WHERE source_key=?", (product["source_key"],))
            if old:
                product["action"] = "duplicate"
                product["existing_order_id"] = old["order_id"]
                _, current_hash = legacy_source_identity(product["source"])
                if old["source_hash"] != current_hash:
                    outcome["warnings"].append({"path": f"products[{product['index']}]", "code": "changed_duplicate",
                                                "message": "该导入产品 ID 已导入但文件内容不同；不会覆盖现有订单，请人工核对"})
            else:
                product["action"] = "create"
        preview_id = secrets.token_urlsafe(20)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM import_previews WHERE created_at<?", (cutoff,))
        conn.execute("INSERT INTO import_previews(id,kind,filename,preview_json,source_blob,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                     (preview_id, "legacy", file.filename or "", json.dumps(outcome, ensure_ascii=False), content, user["id"], utcnow()))
    return {"preview_id": preview_id, **outcome}


def legacy_record_public(conn: sqlite3.Connection, record: dict) -> dict:
    missing = legacy_missing_fields(conn, record)
    source = json.loads(record["source_json"])
    return {key: value for key, value in record.items() if key not in {"source_json", "missing_fields_json"}} | {
        "source": source,
        "legacy_schedule": project_legacy_schedule(source),
        "legacy_owner_name": legacy_owner_name(record),
        "missing_fields": missing,
        "status": "pending" if missing else "complete",
    }


@app.get("/api/legacy/records")
def get_legacy_records(order_id: int | None = None, user: dict = Depends(require_user)):
    with reading() as conn:
        return [legacy_record_public(conn, record) for record in rows(
            conn, "SELECT * FROM legacy_records WHERE (? IS NULL OR order_id=?) ORDER BY id DESC LIMIT 500",
            (order_id, order_id))]


@app.post("/api/legacy/apply")
def apply_legacy(data: dict, user: dict = Depends(require_user)):
    preview_id = str(need(data, "preview_id"))
    selections = data.get("selections")
    if not isinstance(selections, list) or not selections:
        raise HTTPException(422, "请逐项选择要导入的导入产品")
    indices = [choice.get("index") if isinstance(choice, dict) else None for choice in selections]
    if any(isinstance(index, bool) or not isinstance(index, int) for index in indices) or len(set(indices)) != len(indices):
        raise HTTPException(422, "选择格式应为不重复的 {index} 数组")

    with APP_LOCK:
        with reading() as conn:
            saved = one(conn, "SELECT * FROM import_previews WHERE id=? AND kind='legacy'", (preview_id,))
        if not saved or saved["created_by"] != user["id"]:
            raise HTTPException(404, "导入预览已过期或不属于当前用户")
        outcome = json.loads(saved["preview_json"])
        if any(issue["path"] in {"file", "products"} for issue in outcome["errors"]):
            raise HTTPException(422, "预览文件整体有错误，不能导入")
        by_index = {product["index"]: product for product in outcome["products"]}
        selected = []
        skipped = []
        for index in indices:
            product = by_index.get(index)
            if product is None:
                raise HTTPException(422, f"第 {index + 1} 条导入产品不存在")
            if not product["valid"] or product["action"] == "invalid":
                raise HTTPException(422, f"第 {index + 1} 条导入产品有错误，请先修正 JSON")
            if product["action"] == "duplicate":
                skipped.append({"index": index, "reason": "该导入产品 ID 已导入", "order_id": product["existing_order_id"]})
            else:
                selected.append(product)
        if not selected:
            return {"created": [], "skipped": skipped, "pre_import_backup": None}

        # Source is parsed again, so only the reviewed bytes can be applied.
        fresh = preview_legacy_json(saved["filename"], saved["source_blob"])
        fresh_by_index = {product["index"]: product for product in fresh["products"]}
        for product in selected:
            counterpart = fresh_by_index.get(product["index"])
            if not counterpart or not counterpart["valid"] or counterpart["source"] != product["source"]:
                raise HTTPException(409, "导入文件与预览不一致，请重新预览")
        with reading() as conn:
            for product in selected:
                if one(conn, "SELECT id FROM legacy_records WHERE source_key=?", (product["source_key"],)):
                    raise HTTPException(409, "该导入产品已由其他人导入，请重新预览")
        safety = backup_database(DB_PATH)

        created = []
        with writing() as conn:
            if not one(conn, "SELECT id FROM import_previews WHERE id=?", (preview_id,)):
                raise HTTPException(409, "预览已被应用，请重新预览")
            batch_cur = conn.execute("INSERT INTO import_batches(kind,filename,source_blob,selection_json,rows_applied,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                                     ("legacy", saved["filename"], saved["source_blob"],
                                      json.dumps({"selections": selections}, ensure_ascii=False),
                                      len(selected), user["id"], utcnow()))
            batch_id = batch_cur.lastrowid
            for product in selected:
                source = product["source"]
                source_key, source_hash = legacy_source_identity(source)
                if source_key != product["source_key"] or one(conn, "SELECT id FROM legacy_records WHERE source_key=?", (source_key,)):
                    raise HTTPException(409, "导入产品标识已变化，请重新预览")
                identifier_hash = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:12].upper()
                order_no_base = f"PCBA-TEMP-{identifier_hash}"
                order_no = order_no_base
                suffix = 2
                while one(conn, "SELECT id FROM orders WHERE order_no=?", (order_no,)):
                    order_no = f"{order_no_base}-{suffix}"
                    suffix += 1
                order_cur = conn.execute("INSERT INTO orders(order_no,customer,created_at,updated_at) VALUES(?,?,?,?)",
                                         (order_no, "待补录", utcnow(), utcnow()))
                order_id = order_cur.lastrowid
                line_id = None
                batch_id_created = None
                if product["quantity"] is not None:
                    line = insert_line(conn, order_id, {"line_no": "1", "product_code": f"PCBA-TEMP-{identifier_hash}",
                                                        "product_name": product["name"], "quantity": product["quantity"]}, user["id"])
                    line_id = line["id"]
                    schedule = project_legacy_schedule(source)
                    phase = suggest_phase(product["quantity"]) or "unclassified"
                    node_names = [node["name"] for node in schedule["nodes"]]
                    if len(set(node_names)) != len(node_names):
                        raise HTTPException(422, f"导入产品 {product['name']} 的节点重名，请先在源文件中核对")
                    batch_cur = conn.execute("""
                        INSERT INTO production_batches(order_line_id,batch_no,quantity,phase,phase_source,
                            schedule_start,schedule_mode,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        """, (line_id, "旧排期-1", product["quantity"], phase,
                              "quantity_rule" if phase != "unclassified" else "manual",
                              schedule["start_date"], schedule["calendar_mode"] or "workday", utcnow(), utcnow()))
                    batch_id_created = batch_cur.lastrowid
                    insert_legacy_stages(conn, batch_id_created, schedule)
                    audit(conn, "production_batches", batch_id_created, "legacy_create", user["id"], None,
                          must_get(conn, "production_batches", batch_id_created))
                record_cur = conn.execute("INSERT INTO legacy_records(source_key,source_hash,filename,source_index,source_json,order_id,order_line_id,production_batch_id,import_batch_id,missing_fields_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                          (source_key, source_hash, saved["filename"], product["index"],
                                           json.dumps(source, ensure_ascii=False), order_id, line_id,
                                           batch_id_created, batch_id, "[]",
                                           user["id"], utcnow()))
                legacy_id = record_cur.lastrowid
                missing_fields = legacy_missing_fields(conn, must_get(conn, "legacy_records", legacy_id))
                conn.execute("UPDATE legacy_records SET missing_fields_json=? WHERE id=?",
                             (json.dumps(missing_fields, ensure_ascii=False), legacy_id))
                audit(conn, "orders", order_id, "legacy_create", user["id"], None, order_detail(conn, order_id),
                      "原始 JSON 排期导入；全部缺失字段待补录")
                audit(conn, "legacy_records", legacy_id, "import", user["id"], None,
                      {"source_key": source_key, "order_id": order_id, "missing_fields": missing_fields})
                created.append({"index": product["index"], "order_id": order_id, "order_no": order_no,
                                "order_line_id": line_id, "production_batch_id": batch_id_created,
                                "legacy_record_id": legacy_id, "pending_fields": missing_fields})
            conn.execute("DELETE FROM import_previews WHERE id=?", (preview_id,))
            audit(conn, "import_batches", batch_id, "apply", user["id"], None,
                  {"kind": "legacy", "filename": saved["filename"], "rows_applied": len(created)})
        return {"created": created, "skipped": skipped, "pre_import_backup": safety.name}

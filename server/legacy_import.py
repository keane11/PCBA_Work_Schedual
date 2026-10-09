"""Read-only preview of JSON exported by the original PCBA排期工作台.html.

The old page exports its ``products`` array directly. Its own importer also
accepts ``{"products": [...]}`` and a single legacy product object. This
parser accepts those three shapes, reports problems, and returns source values
unchanged. It deliberately does not create orders, customers, BOMs, or dates
that were absent from the file.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

MAX_BYTES = 10 * 1024 * 1024
MAX_PRODUCTS = 500
MAX_NODES_PER_PRODUCT = 200
MAX_NODES = 20_000

# The old file has no stable sales-order identity or material provenance.
# These fields must be supplied or linked before a temporary order is treated
# as a formal customer, BOM, or vendor commitment.
MISSING_NEW_SYSTEM_FIELDS = (
    "销售订单号和客户",
    "产品编码与订单行",
    "客户要求交期与业务正式承诺交期",
    "生产批次与交付批次及数量关联",
    "BOM 版本与逐项物料明细",
    "外协厂和其承诺开工、完工日",
)


def _issue(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"JSON 含无效数字：{value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON 含超出范围的数字")
    return number


def _decimal(value: Any, *, integer: bool, positive: bool, limit: int) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("应为数字")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("应为有效数字") from exc
    if not number.is_finite() or abs(number) > limit:
        raise ValueError(f"应为不超过 {limit} 的有限数字")
    if positive and number <= 0:
        raise ValueError("必须大于 0")
    if not positive and number < 0:
        raise ValueError("不能小于 0")
    if integer and number != number.to_integral_value():
        raise ValueError("必须是整数件数")
    return format(number, "f")


def _date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("日期应为 YYYY-MM-DD 文本")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期必须是有效的 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("日期必须是有效的 YYYY-MM-DD")
    return value


def _string(value: Any, *, maximum: int, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError("不能为空")
        return None
    if not isinstance(value, str):
        raise ValueError("应为文本")
    if not value.strip():
        if required:
            raise ValueError("不能为空")
        return None
    if len(value) > maximum:
        raise ValueError(f"最多 {maximum} 字")
    return value


def _identifier(value: Any) -> bool:
    return value is None or (
        not isinstance(value, bool)
        and isinstance(value, (str, int))
        and len(str(value)) <= 128
    )


def _source_products(parsed: Any) -> tuple[str, list]:
    if isinstance(parsed, list):
        return "array", parsed
    if isinstance(parsed, dict):
        if "products" in parsed:
            if not isinstance(parsed["products"], list):
                raise ValueError("products 必须是数组")
            return "wrapper", parsed["products"]
        if "nodes" in parsed or "product" in parsed:
            return "single_product", [parsed]
    raise ValueError("导入文件应是产品数组、含 products 数组的对象，或单个导入产品对象")


def suggest_phase(quantity: Any) -> str | None:
    """Return a quantity-based hint only; the user still assigns the phase."""
    amount = _decimal(quantity, integer=True, positive=True, limit=10**12)
    if amount is None:
        return None
    if Decimal(amount) <= 20:
        return "rd"
    if Decimal(amount) > 1000:
        return "mp"
    return "pp"


def project_legacy_schedule(source: dict[str, Any]) -> dict[str, Any]:
    """Reproduce the old HTML's displayed dates without inventing formal dates.

    The browser export only stores inputs. ``compute()`` in the original page
    advances twelve hours per rounded half-day and, in workday mode, moves a
    weekend landing to the following Monday at midnight. A milestone's
    ``actual`` date moves the cursor forward; ``actualDone`` is only a record.
    Missing start dates remain unknown rather than becoming today's date.
    """
    if not isinstance(source, dict) or not isinstance(source.get("nodes"), list):
        raise ValueError("导入产品及 nodes 必须是对象和数组")

    start_date = _date(source.get("startDate"))
    mode = source.get("mode")
    if mode not in ("workday", "natural", None, ""):
        raise ValueError("旧日历规则应为 workday 或 natural")
    calendar_mode = mode or None
    cursor = datetime.combine(date.fromisoformat(start_date), datetime.min.time()) if start_date and calendar_mode else None
    start_at = cursor
    nodes: list[dict[str, Any]] = []

    for sequence, node in enumerate(source["nodes"], start=1):
        if not isinstance(node, dict):
            raise ValueError(f"第 {sequence} 个旧节点不是对象")
        duration = _decimal(node.get("duration"), integer=False, positive=False, limit=3660)
        if duration is None:
            raise ValueError(f"第 {sequence} 个旧节点缺少耗时")
        node_type = node.get("type")
        if node_type not in ("normal", "milestone", None, ""):
            raise ValueError(f"第 {sequence} 个旧节点类型无效")
        actual = _date(node.get("actual"))
        actual_done = _date(node.get("actualDone"))

        if cursor and node_type == "milestone" and actual:
            cursor = max(cursor, datetime.combine(date.fromisoformat(actual), datetime.min.time()))
        planned_start = cursor
        if cursor:
            # JS Math.round() rounds a nonnegative x.5 up, unlike Python round().
            half_days = int((Decimal(duration) * 2).to_integral_value(rounding=ROUND_HALF_UP))
            for _ in range(half_days):
                cursor += timedelta(hours=12)
                if calendar_mode == "workday":
                    while cursor.weekday() >= 5:
                        cursor = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time())

        nodes.append({
            "sequence": sequence,
            "source_id": node.get("id"),
            "name": node.get("name"),
            "owner": node.get("owner") or None,
            "type": node_type or None,
            "duration_days": duration,
            "actual_arrival_date": actual,
            "actual_finish_date": actual_done,
            "remark": node.get("remark") or None,
            "planned_start": planned_start.date().isoformat() if planned_start else None,
            "estimated_finish": cursor.date().isoformat() if cursor else None,
            "planned_start_at": planned_start.isoformat(timespec="seconds") if planned_start else None,
            "estimated_finish_at": cursor.isoformat(timespec="seconds") if cursor else None,
        })

    elapsed = ((cursor - start_at).total_seconds() / 86400) if cursor and start_at else None
    return {
        "estimate_kind": "legacy_calculated",
        "name": source.get("name") or source.get("product"),
        "owner": source.get("owner") or None,
        "quantity": _decimal(source.get("qty"), integer=True, positive=True, limit=10**12),
        "suggested_phase": suggest_phase(source.get("qty")),
        "start_date": start_date,
        "calendar_mode": calendar_mode,
        "estimated_delivery_date": cursor.date().isoformat() if cursor else None,
        "elapsed_calendar_days": format(Decimal(str(elapsed)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f") if elapsed is not None else None,
        "node_count": len(nodes),
        "nodes": nodes,
    }


def preview_legacy_json(filename: str, content: bytes) -> dict[str, Any]:
    """Validate and preview an old export without changing data or writing a DB.

    ``source`` in each product and node is the original JSON object. Parsed
    fields are only for display; they do not imply a safe automatic migration.
    Issues use paths such as ``products[0].nodes[2].duration``. A malformed
    whole file returns an empty preview with a file-level error.
    """
    result: dict[str, Any] = {
        "source_format": None,
        "product_count": 0,
        "node_count": 0,
        "products": [],
        "missing_new_system_fields": list(MISSING_NEW_SYSTEM_FIELDS),
        "warnings": [],
        "errors": [],
    }

    def error(path: str, code: str, message: str) -> None:
        result["errors"].append(_issue(path, code, message))

    def warning(path: str, code: str, message: str) -> None:
        result["warnings"].append(_issue(path, code, message))

    if not isinstance(content, bytes):
        error("file", "type", "上传内容必须是字节数据")
        return result
    if Path(filename or "").suffix.lower() != ".json":
        error("file", "extension", "请选择旧页面导出的 .json 文件")
        return result
    if not content:
        error("file", "empty", "文件为空")
        return result
    if len(content) > MAX_BYTES:
        error("file", "size", "JSON 超过 10 MB；请分批整理后再预览")
        return result
    try:
        parsed = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_keys,
                            parse_constant=_invalid_constant, parse_float=_finite_float)
        source_format, products = _source_products(parsed)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        error("file", "format", f"无法读取旧页面 JSON：{exc}")
        return result

    result["source_format"] = source_format
    result["product_count"] = len(products)
    if not products:
        error("products", "empty", "产品数组为空")
        return result
    if len(products) > MAX_PRODUCTS:
        error("products", "limit", f"产品超过 {MAX_PRODUCTS} 条，请分批整理")
        return result

    product_ids: set[str] = set()
    node_total = 0
    for product_index, product in enumerate(products):
        path = f"products[{product_index}]"
        if not isinstance(product, dict):
            error(path, "type", "产品必须是对象")
            continue
        raw_nodes = product.get("nodes")
        if isinstance(raw_nodes, list):
            node_total += len(raw_nodes)
            if len(raw_nodes) > MAX_NODES_PER_PRODUCT:
                error(f"{path}.nodes", "limit", f"单个产品节点超过 {MAX_NODES_PER_PRODUCT} 个")
                continue
        if node_total > MAX_NODES:
            error("products", "limit", f"节点总数超过 {MAX_NODES} 个")
            break

        viewed: dict[str, Any] = {
            "index": product_index,
            "source": product,
            "name": None,
            "quantity": None,
            "suggested_phase": None,
            "start_date": None,
            "mode": product.get("mode"),
            "nodes": [],
        }
        result["products"].append(viewed)
        before_errors = len(result["errors"])

        if not _identifier(product.get("id")):
            error(f"{path}.id", "identifier", "导入产品 ID 应为短文本或整数")
        elif product.get("id") is not None:
            identifier = str(product["id"])
            if identifier in product_ids:
                warning(f"{path}.id", "duplicate_id", "导入产品 ID 重复；后续关联应按预览序号核对")
            product_ids.add(identifier)
        else:
            warning(f"{path}.id", "missing_id", "导入产品没有稳定 ID；仅能按内容识别完全相同的重复导出")

        name_source = "name" if product.get("name") else "product"
        try:
            viewed["name"] = _string(product.get(name_source), maximum=200, required=True)
            if name_source == "product":
                warning(f"{path}.name", "legacy_name", "名称来自导入文件的 product 字段，原字段已保留")
        except ValueError as exc:
            error(f"{path}.{name_source}", "name", str(exc))

        try:
            _string(product.get("owner"), maximum=100)
        except ValueError as exc:
            error(f"{path}.owner", "owner", str(exc))

        try:
            viewed["quantity"] = _decimal(product.get("qty"), integer=True, positive=True, limit=10**12)
            viewed["suggested_phase"] = suggest_phase(product.get("qty"))
            if viewed["quantity"] is None:
                warning(f"{path}.qty", "missing_quantity", "导入产品未填写数量；后续须关联订单行数量")
        except ValueError as exc:
            error(f"{path}.qty", "quantity", str(exc))

        try:
            viewed["start_date"] = _date(product.get("startDate"))
            if viewed["start_date"] is None:
                warning(f"{path}.startDate", "missing_date", "导入文件没有起始日期，须人工核对")
        except ValueError as exc:
            error(f"{path}.startDate", "date", str(exc))

        mode = product.get("mode")
        if mode is None or mode == "":
            warning(f"{path}.mode", "missing_mode", "旧日历规则缺失，须人工核对")
        elif mode not in ("workday", "natural"):
            error(f"{path}.mode", "mode", "日历规则应为 workday 或 natural")
        elif mode == "natural":
            warning(f"{path}.mode", "calendar", "导入文件按自然日计算；工作台按公司工作日排产，日期须核对")

        if not isinstance(raw_nodes, list):
            error(f"{path}.nodes", "type", "nodes 必须是数组")
        elif not raw_nodes:
            error(f"{path}.nodes", "empty", "导入产品没有排期节点")
        else:
            node_ids: set[str] = set()
            for node_index, node in enumerate(raw_nodes):
                node_path = f"{path}.nodes[{node_index}]"
                if not isinstance(node, dict):
                    error(node_path, "type", "节点必须是对象")
                    continue
                viewed_node = {"index": node_index, "source": node, "name": None,
                               "type": node.get("type"), "duration_days": None,
                               "actual": node.get("actual"), "actual_done": node.get("actualDone")}
                viewed["nodes"].append(viewed_node)

                if not _identifier(node.get("id")):
                    error(f"{node_path}.id", "identifier", "旧节点 ID 应为短文本或整数")
                elif node.get("id") is not None:
                    identifier = str(node["id"])
                    if identifier in node_ids:
                        warning(f"{node_path}.id", "duplicate_id", "此产品内旧节点 ID 重复")
                    node_ids.add(identifier)

                try:
                    viewed_node["name"] = _string(node.get("name"), maximum=200, required=True)
                except ValueError as exc:
                    error(f"{node_path}.name", "name", str(exc))
                for field, maximum in (("owner", 100), ("remark", 2000)):
                    try:
                        _string(node.get(field), maximum=maximum)
                    except ValueError as exc:
                        error(f"{node_path}.{field}", field, str(exc))

                node_type = node.get("type")
                if node_type is None or node_type == "":
                    warning(f"{node_path}.type", "missing_type", "节点类型缺失，须人工核对")
                elif node_type not in ("normal", "milestone"):
                    error(f"{node_path}.type", "type", "节点类型应为 normal 或 milestone")

                try:
                    duration = _decimal(node.get("duration"), integer=False, positive=False, limit=3660)
                    if duration is None:
                        error(f"{node_path}.duration", "missing_duration", "节点耗时缺失")
                    else:
                        viewed_node["duration_days"] = duration
                        number = Decimal(duration)
                        if number != number.to_integral_value():
                            warning(f"{node_path}.duration", "fractional_day",
                                    "旧节点使用非整天耗时；新工作台按整工作日计划，须人工确定映射")
                        if number * 2 != (number * 2).to_integral_value():
                            warning(f"{node_path}.duration", "legacy_rounding",
                                    "旧页面实际按半天取整计算；此处保留原填写值")
                except ValueError as exc:
                    error(f"{node_path}.duration", "duration", str(exc))

                for field in ("actual", "actualDone"):
                    try:
                        _date(node.get(field))
                    except ValueError as exc:
                        error(f"{node_path}.{field}", "date", str(exc))

                if node.get("actual") and node_type != "milestone":
                    warning(f"{node_path}.actual", "non_milestone_actual",
                            "旧页面仅到料里程碑使用 actual；该日期须人工核对")

        viewed["valid"] = len(result["errors"]) == before_errors
        if viewed["valid"]:
            viewed["legacy_schedule"] = project_legacy_schedule(product)

    result["node_count"] = node_total
    warning("products", "mapping_required",
            "旧 JSON 仅保存产品和节点排期；销售订单、BOM、外协与交付字段须人工关联，不能自动补造")
    return result

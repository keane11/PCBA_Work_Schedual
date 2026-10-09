"""Configurable, read-only parser for small KIS CSV/XLSX/PDF export batches.

This module deliberately does not guess KIS column names or write to the DB.
The caller maps source headers to the canonical fields, reviews every diff,
and decides which fields to apply. A real KIS sample is needed to validate a
site-specific mapping.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50_000
MAX_PDF_PAGES = 100

# Key fields identify one source row across repeated imports. Duplicate keys in
# one file are errors; they must be resolved instead of being silently merged.
SCHEMAS = {
    "orders": {
        "fields": ("order_no", "line_no", "customer", "product_code", "product_name", "quantity", "due_date", "promised_date"),
        "required": ("order_no", "line_no", "product_code", "quantity"),
        "key": ("order_no", "line_no"),
    },
    "bom": {
        "fields": ("product_code", "bom_version", "source_line_no", "material_code", "material_name", "quantity_per", "loss_rate", "unit_price"),
        "required": ("product_code", "bom_version", "material_code", "quantity_per"),
        "key": ("product_code", "bom_version", "material_code"),
    },
    "inventory": {
        "fields": ("material_code", "quantity", "as_of", "basis"),
        "required": ("material_code", "quantity"),
        "key": ("material_code",),
    },
    "purchases": {
        "fields": ("reference", "source_line_no", "material_code", "quantity", "eta", "order_no", "line_no"),
        "required": ("reference", "material_code", "quantity"),
        "key": ("reference", "material_code"),
    },
    "receipts": {
        "fields": ("reference", "source_line_no", "material_code", "quantity", "received_at", "iqc_status", "accepted_quantity", "order_no", "line_no"),
        "required": ("reference", "material_code", "quantity", "received_at"),
        "key": ("reference", "material_code"),
    },
}

DATE_FIELDS = {"due_date", "promised_date", "as_of", "eta", "received_at"}
POSITIVE_DECIMAL_FIELDS = {"quantity_per"}
NONNEGATIVE_DECIMAL_FIELDS = {"quantity", "accepted_quantity", "loss_rate", "unit_price"}
DECIMAL_FIELDS = POSITIVE_DECIMAL_FIELDS | NONNEGATIVE_DECIMAL_FIELDS
IDENTIFIER_FIELDS = {"order_no", "line_no", "product_code", "bom_version", "source_line_no", "material_code", "reference"}


def _issue(message: str, row_number: int | None = None, field: str | None = None, code: str = "invalid") -> dict:
    return {"row_number": row_number, "field": field, "code": code, "message": message}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        result = value.strip()
    elif isinstance(value, (date, datetime)):
        result = value.isoformat()
    elif isinstance(value, float) and value.is_integer():
        result = str(int(value))
    else:
        result = str(value).strip()
    return result or None


def _header_row(values: Iterable[Any]) -> list[str]:
    return [(_text(value) or "") for value in values]


def _pdf_table_rows(table: list[list[str | None]]) -> list[list[str | None]]:
    """Discard separator rows while preserving column positions for mapping."""
    rows = []
    for source_row in table:
        cells = [re.sub(r"\s+", " ", cell).strip() if cell else None for cell in source_row]
        if any(cells):
            rows.append(cells)
    return rows


def _load_pdf_sheets(content: bytes, header_row: int) -> list[tuple[str, list[list[str | None]], bool]]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError("缺少 pdfplumber；请安装 requirements.txt 中的依赖") from exc

    sheets: list[tuple[str, list[list[str | None]], bool]] = []
    headers: dict[tuple[str, ...], int] = {}
    text_found = False
    total_rows = 0
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise ValueError(f"PDF 超过 {MAX_PDF_PAGES} 页；请按单号或日期拆分")
            for page_number, page in enumerate(pdf.pages, start=1):
                text_found |= bool(page.chars)
                if len(page.chars) > 100_000:
                    raise ValueError("PDF 单页文字过多；请拆分后再导入")
                parsed = [_pdf_table_rows(table) for table in page.extract_tables()]
                parsed = [table for table in parsed if len(table) > header_row and max(map(len, table), default=0) >= 2]
                if not parsed:
                    # A KIS export may align columns with text instead of rules.
                    parsed = [_pdf_table_rows(table) for table in page.extract_tables(table_settings={
                        "vertical_strategy": "text", "horizontal_strategy": "text",
                        "min_words_vertical": 2,
                    })]
                    parsed = [table for table in parsed if len(table) > header_row and max(map(len, table), default=0) >= 2]
                if not parsed and not (page.chars or page.images or page.lines or page.rects):
                    continue
                if not parsed:
                    raise ValueError(f"PDF 第 {page_number} 页没有可映射文字表格；扫描件请使用 DeepSeek 智能解析")
                for matrix in parsed:
                    key = tuple(_header_row(matrix[header_row - 1]))
                    if not any(key):
                        continue
                    if key in headers:
                        index = headers[key]
                        sheets[index][1].extend(matrix[header_row:])
                    else:
                        name = f"PDF 表格 {len(sheets) + 1}"
                        headers[key] = len(sheets)
                        sheets.append((name, matrix, False))
                    total_rows += len(matrix) - header_row
                    if total_rows > MAX_ROWS:
                        raise ValueError("PDF 表格超过 50000 行；请按单号或日期拆分")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取 PDF；请检查文件是否损坏或受密码保护") from exc
    if not sheets:
        if not text_found:
            raise ValueError("扫描 PDF 没有文字表格；请使用 DeepSeek 智能解析")
        raise ValueError("PDF 中未找到可映射的表格；请检查表头和清晰度，或导出 Excel/CSV")
    return sheets


def _load_sheets(filename: str, content: bytes, header_row: int = 1) -> list[dict]:
    if not isinstance(content, bytes):
        raise ValueError("上传内容必须是字节数据")
    if not content:
        raise ValueError("文件为空")
    if len(content) > MAX_BYTES:
        raise ValueError("文件超过 20 MB；请从 KIS 按单号或日期分批导出")
    if header_row < 1 or header_row > 100:
        raise ValueError("表头行必须在第 1 至 100 行之间")

    suffix = Path(filename or "").suffix.lower()
    if suffix == ".csv":
        decoded = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                decoded = content.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise ValueError("CSV 编码无法识别；请导出 UTF-8 或 GB18030")
        try:
            dialect = csv.Sniffer().sniff(decoded[:8192], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        lines = list(csv.reader(io.StringIO(decoded, newline=""), dialect))
        sheets = [("CSV", lines)]
    elif suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError("缺少 openpyxl；请安装 requirements.txt") from exc
        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True, keep_links=False)
            try:
                sheets = []
                for sheet in workbook.worksheets:
                    rows = []
                    for row in sheet.iter_rows(values_only=True):
                        rows.append(row)
                        if len(rows) > MAX_ROWS + header_row:
                            raise ValueError("工作表超过 50000 行；请从 KIS 按单号或日期分批导出")
                    sheets.append((sheet.title, rows))
            finally:
                workbook.close()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("无法读取 XLSX 文件；请检查文件是否损坏或受密码保护") from exc
    elif suffix == ".pdf":
        sheets = _load_pdf_sheets(content, header_row)
    else:
        raise ValueError("仅支持 .csv、.xlsx 或含文字表格的 .pdf；旧版 .xls 请先另存为 .xlsx")

    result = []
    for source_sheet in sheets:
        name, matrix = source_sheet[:2]
        is_ocr = source_sheet[2] if len(source_sheet) > 2 else False
        if len(matrix) > MAX_ROWS + header_row:
            raise ValueError("工作表超过 50000 行；请从 KIS 按单号或日期分批导出")
        if len(matrix) < header_row:
            result.append({"name": name, "columns": [], "rows": [], "row_count": 0, "ocr": is_ocr})
            continue
        columns = _header_row(matrix[header_row - 1])
        width = len(columns)
        rows = []
        for row_number, source_row in enumerate(matrix[header_row:], start=header_row + 1):
            if any(_text(value) is not None for value in source_row[width:]):
                raise ValueError(f"第 {row_number} 行有未命名的额外数据列；请补全表头")
            values = list(source_row[:width])
            values.extend([None] * (width - len(values)))
            if any(_text(value) is not None for value in values):
                rows.append((row_number, values))
        result.append({"name": name, "columns": columns, "rows": rows, "row_count": len(rows), "ocr": is_ocr})
    return result


def _suggest_column_map(kind: str | None, sheet: dict) -> dict[str, str]:
    if kind != "orders":
        return {}
    names = {re.sub(r"\s+", "", name).casefold(): name for name in sheet["columns"] if name}
    suggestions = {}
    for field, source in (
        ("product_code", "物料编号"),
        ("product_name", "物料描述"),
        ("quantity", "数量"),
        ("due_date", "交货日期/备注"),
    ):
        if source.casefold() in names:
            suggestions[field] = names[source.casefold()]
    project = names.get("项目")
    if project:
        index = sheet["columns"].index(project)
        nonempty = [_text(row[index]) for _, row in sheet["rows"] if _text(row[index])]
        if nonempty and all(re.fullmatch(r"\d+", value) for value in nonempty[:20]):
            suggestions["line_no"] = project
    return suggestions


def _bom_level(value: Any) -> str | None:
    """Recognize hierarchy markers only when the operator opts into this mode."""
    marker = re.sub(r"\s+", "", _text(value) or "")
    if marker == "母件":
        return "parent"
    if re.fullmatch(r"[1-9]\d*级子件", marker):
        return "child"
    return None


def _suggest_bom_parent_column(kind: str | None, sheet: dict) -> str | None:
    if kind != "bom" or "级次" not in sheet["columns"] or "物料代码" not in sheet["columns"]:
        return None
    level_index = sheet["columns"].index("级次")
    code_index = sheet["columns"].index("物料代码")
    parent_seen = False
    for _, cells in sheet["rows"]:
        level = _bom_level(cells[level_index])
        if level == "parent" and _text(cells[code_index]):
            parent_seen = True
        elif level == "child" and parent_seen and _text(cells[code_index]):
            return "级次"
    return None


def inspect_upload(filename: str, content: bytes, *, header_row: int = 1, kind: str | None = None) -> dict:
    """Return available sheet names and headers without importing data."""
    try:
        sheets = _load_sheets(filename, content, header_row)
    except ValueError as exc:
        return {"sheets": [], "columns": [], "row_count": 0, "errors": [_issue(str(exc))]}
    summaries = [{"name": sheet["name"], "columns": sheet["columns"], "row_count": sheet["row_count"],
                  "ocr": sheet["ocr"], "suggested_column_map": _suggest_column_map(kind, sheet),
                  "suggested_bom_parent_column": _suggest_bom_parent_column(kind, sheet)} for sheet in sheets]
    first = summaries[0] if summaries else {"columns": [], "row_count": 0, "suggested_column_map": {},
                                           "suggested_bom_parent_column": None}
    return {"sheets": summaries, "columns": first["columns"], "row_count": first["row_count"],
            "ocr": first.get("ocr", False), "suggested_column_map": first["suggested_column_map"],
            "suggested_bom_parent_column": first["suggested_bom_parent_column"], "errors": []}


def _normalize_decimal(field: str, raw: Any) -> str | None:
    value = _text(raw)
    if value is None:
        return None
    percent = field == "loss_rate" and value.endswith("%")
    value = value.removesuffix("%")
    if "," in value:
        if not re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?", value):
            raise ValueError("数字中的逗号分组不明确；请使用 1,234 或 1234.5 格式")
        value = value.replace(",", "")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("必须是有效数字") from exc
    if not number.is_finite():
        raise ValueError("必须是有限数字")
    if len(number.as_tuple().digits) > 32 or abs(number.adjusted()) > 18:
        raise ValueError("数值精度或量级超过工作台导入范围")
    if percent:
        number /= 100
    if field in POSITIVE_DECIMAL_FIELDS and number <= 0:
        raise ValueError("必须大于 0")
    if field in NONNEGATIVE_DECIMAL_FIELDS and number < 0:
        raise ValueError("不能小于 0")
    if field == "loss_rate" and number > 1:
        raise ValueError("损耗率应为 0 至 1，或明确写成百分比（例如 5%）")
    if field == "unit_price" and (number >= Decimal("1000000000000") or number.normalize().as_tuple().exponent < -8):
        raise ValueError("单价须小于 1 万亿元，且最多保留 8 位小数")
    return format(number.normalize(), "f")


def _normalize_date(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (datetime, date)):
        return raw.date().isoformat() if isinstance(raw, datetime) else raw.isoformat()
    if isinstance(raw, (int, float)):
        try:
            from openpyxl.utils.datetime import from_excel

            converted = from_excel(raw)
            if isinstance(converted, datetime):
                return converted.date().isoformat()
        except (ImportError, ValueError, OverflowError, TypeError):
            pass
        raise ValueError("无法识别 Excel 日期序号")
    value = _text(raw)
    if value is None:
        return None
    # Some KIS PDFs print a date/remarks column as "YYYY-MM-DD /" when the
    # remarks side is empty. Keep nonempty remarks invalid for manual review.
    date_with_empty_remark = re.fullmatch(r"\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)\s*/\s*", value)
    if date_with_empty_remark:
        value = date_with_empty_remark.group(1)
    value = value.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")
    simple = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if simple:
        try:
            return date(*(int(group) for group in simple.groups())).isoformat()
        except ValueError as exc:
            raise ValueError("日期必须能转换为 YYYY-MM-DD") from exc
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError as exc:
            raise ValueError("日期必须能转换为 YYYY-MM-DD") from exc


def _normalize(field: str, raw: Any) -> str | None:
    if field in DECIMAL_FIELDS:
        return _normalize_decimal(field, raw)
    if field in DATE_FIELDS:
        return _normalize_date(raw)
    if field == "basis":
        value = _text(raw)
        if value is None:
            return None
        aliases = {"现存量": "on_hand", "可用量": "available"}
        value = aliases.get(value, value)
        if value not in ("on_hand", "available"):
            raise ValueError("库存口径必须明确为 on_hand（现存量）或 available（可用量）")
        return value
    if field == "iqc_status":
        value = _text(raw)
        aliases = {"合格": "pass", "不合格": "fail", "待检": "pending", "待检验": "pending"}
        value = aliases.get(value, value)
        if value is not None and value not in ("pass", "fail", "pending"):
            raise ValueError("IQC 状态必须为 pass/合格、fail/不合格或 pending/待检；部分合格请核对合格数量")
        return value
    return _text(raw)


def _stable_key(values: Mapping[str, Any], fields: tuple[str, ...]) -> str:
    return json.dumps([values.get(field) for field in fields], ensure_ascii=False, separators=(",", ":"))


def _existing_index(existing_rows: Mapping | Iterable[Mapping], key_fields: tuple[str, ...]) -> tuple[dict[str, Mapping], set[str]]:
    if isinstance(existing_rows, Mapping):
        # Dict values may be keyed by our stable key or by application-specific IDs.
        existing_rows = existing_rows.values()
    index = {}
    collisions = set()
    for row in existing_rows:
        values = row.get("values", row)
        if all(_text(values.get(field)) is not None for field in key_fields):
            key = _stable_key({field: _text(values.get(field)) for field in key_fields}, key_fields)
        else:
            key = row.get("key")
        if key is not None:
            if key in index:
                collisions.add(key)
            index[key] = values
    return index, collisions


def preview_import(
    kind: str,
    filename: str,
    content: bytes,
    column_map: Mapping[str, str | int] | None,
    existing_rows: Mapping | Iterable[Mapping] | None = None,
    key_fields: Iterable[str] | None = None,
    sheet_name: str | None = None,
    header_row: int = 1,
    fixed_fields: Mapping[str, Any] | None = None,
    bom_parent_column: str | None = None,
) -> dict:
    """Parse and diff source rows. Applying chosen changes is the caller's job.

    ``column_map`` maps canonical field names to source header names or 1-based
    column numbers. ``fixed_fields`` fills fields absent from the mapped table,
    such as a sales order number printed once above PDF detail rows. A mapped
    column always takes precedence, including when a particular cell is empty.
    Omitted fields are never included in ``values`` and thus cannot overwrite
    prior manual entries.
    """
    outcome = {"kind": kind, "rows": [], "changes": [], "errors": [], "warnings": [], "summary": {}}
    schema = SCHEMAS.get(kind)
    if schema is None:
        outcome["errors"].append(_issue(f"未知导入类型：{kind}"))
        return outcome
    hierarchy_mode = bool(bom_parent_column)
    if hierarchy_mode and (kind != "bom" or Path(filename or "").suffix.lower() != ".xlsx"):
        outcome["errors"].append(_issue("按级次拆分母件/子件只适用于 BOM XLSX 文件"))
        return outcome
    try:
        sheets = _load_sheets(filename, content, header_row)
    except ValueError as exc:
        outcome["errors"].append(_issue(str(exc)))
        return outcome
    sheet = next((item for item in sheets if item["name"] == sheet_name), None) if sheet_name else (sheets[0] if sheets else None)
    if sheet is None:
        outcome["errors"].append(_issue("找不到指定工作表"))
        return outcome
    columns = sheet["columns"]
    if not columns:
        outcome["errors"].append(_issue("工作表没有表头"))
        return outcome
    if len({name.casefold() for name in columns if name}) != len([name for name in columns if name]):
        outcome["errors"].append(_issue("表头有重复列名；请先给重复列改名"))
        return outcome
    parent_position = None
    if hierarchy_mode:
        parent_position = next((index for index, name in enumerate(columns)
                                if name.casefold() == bom_parent_column.strip().casefold()), None)
        if parent_position is None:
            outcome["errors"].append(_issue("级次列不存在；请重新选择表头和级次列", header_row, "bom_parent_column", "missing_column"))
            return outcome

    requested_map = dict(column_map or {})
    invalid_fields = set(requested_map) - set(schema["fields"])
    if invalid_fields:
        outcome["errors"].append(_issue("未知目标字段：" + ", ".join(sorted(invalid_fields))))
        return outcome
    if fixed_fields is not None and not isinstance(fixed_fields, Mapping):
        outcome["errors"].append(_issue("固定字段必须是对象"))
        return outcome
    supplied_fixed = dict(fixed_fields or {})
    invalid_fixed = set(supplied_fixed) - set(schema["fields"])
    if invalid_fixed:
        outcome["errors"].append(_issue("未知固定字段：" + ", ".join(sorted(invalid_fixed))))
        return outcome
    if hierarchy_mode and ("product_code" in requested_map or "product_code" in supplied_fixed
                           or "source_line_no" in requested_map or "source_line_no" in supplied_fixed):
        outcome["errors"].append(_issue(
            "按级次导入时，产品编码由母件行提取、行号取原 Excel 行号；请取消 product_code/source_line_no 的列映射或固定值"))
        return outcome
    normalized_fixed = {}
    for field, raw in supplied_fixed.items():
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            outcome["errors"].append(_issue("固定值必须是非空文字或数字", field=field))
            continue
        try:
            normalized = _normalize(field, raw)
            if normalized is None:
                raise ValueError("固定值不能为空")
            normalized_fixed[field] = normalized
        except ValueError as exc:
            outcome["errors"].append(_issue(str(exc), field=field))
    if outcome["errors"]:
        return outcome
    lookup = {name.casefold(): index for index, name in enumerate(columns) if name}
    positions = {}
    for field in schema["fields"]:
        source = requested_map.get(field, field)
        if isinstance(source, int) and not isinstance(source, bool):
            index = source - 1
        elif isinstance(source, str):
            index = lookup.get(source.strip().casefold(), -1)
        else:
            index = -1
        if 0 <= index < len(columns):
            positions[field] = index
            if field in normalized_fixed:
                outcome["warnings"].append(_issue(
                    f"{field} 已映射源列，固定值已忽略，逐行以文件内容为准",
                    field=field, code="fixed_ignored",
                ))
        elif field in requested_map:
            outcome["errors"].append(_issue(f"字段 {field} 映射的源列不存在：{source}", header_row, field, "missing_column"))
    if hierarchy_mode:
        positions.pop("product_code", None)
        positions.pop("source_line_no", None)
        if "material_code" not in positions or positions["material_code"] == parent_position:
            outcome["errors"].append(_issue("按级次导入必须将 material_code 映射到物料代码列，且不能映射到级次列"))
            return outcome
        outcome["warnings"].append(_issue(
            "本次以 Excel 原始行号匹配 BOM 行；重新排序或插入行会改变匹配键，应用差异前须逐项核对。改版请使用新的 BOM 版本。",
            code="source_row_key_review"))
    required = set(schema["required"])
    default_key = ("product_code", "bom_version", "source_line_no") if hierarchy_mode else schema["key"]
    if "source_line_no" in positions:
        default_key = (("product_code", "bom_version", "source_line_no") if kind == "bom" else ("reference", "source_line_no"))
    keys = tuple(key_fields or default_key)
    if not keys or any(field not in schema["fields"] for field in keys):
        outcome["errors"].append(_issue("唯一键字段不合法"))
        return outcome
    if hierarchy_mode and keys != default_key:
        outcome["errors"].append(_issue("按级次导入须以产品编码、BOM 版本和原 Excel 行号作为匹配键"))
        return outcome
    for field in sorted(required | set(keys)):
        if field not in positions and field not in normalized_fixed and not (hierarchy_mode and field in {"product_code", "source_line_no"}):
            outcome["errors"].append(_issue(f"缺少必需列 {field}，请配置列映射", header_row, field, "missing_column"))
    if outcome["errors"]:
        return outcome

    existing_source = list(existing_rows.values()) if isinstance(existing_rows, Mapping) else list(existing_rows or [])
    prior, existing_collisions = _existing_index(existing_source, keys)
    if existing_collisions:
        outcome["errors"].append(_issue("工作台已有记录使用相同匹配键；请先明确源单据行号，不能自动选择覆盖哪一行", code="ambiguous_existing_key"))
        return outcome
    existing_material_lines: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    if hierarchy_mode:
        for old_row in existing_source:
            old_values = old_row.get("values", old_row)
            product = _text(old_values.get("product_code"))
            version = _text(old_values.get("bom_version"))
            material = _text(old_values.get("material_code"))
            line = _text(old_values.get("source_line_no"))
            if product and version and material:
                existing_material_lines[(product, version, material)].add(line or "")
    seen: dict[str, list[dict]] = defaultdict(list)
    active_parent: str | None = None
    parent_count = 0
    child_count = 0
    for row_number, source_row in sheet["rows"]:
        hierarchy_errors = []
        if hierarchy_mode:
            level = _bom_level(source_row[parent_position])
            source_code = _text(source_row[positions["material_code"]])
            source_quantity = (_text(source_row[positions["quantity_per"]])
                               if "quantity_per" in positions else None)
            if level == "parent":
                active_parent = source_code
                parent_count += 1
                if active_parent:
                    outcome["warnings"].append(_issue("母件行已用于确定产品编码，不作为 BOM 子件导入",
                                                      row_number, "product_code", "bom_parent_skipped"))
                    continue
                hierarchy_errors.append(_issue("母件行缺少产品编码，后续子件无法归属", row_number,
                                               "product_code", "bom_parent_missing"))
            elif not source_code and not source_quantity:
                outcome["warnings"].append(_issue("空物料编码和用量的说明/汇总行已跳过，请核对", row_number,
                                                  None, "bom_summary_skipped"))
                continue
            elif level == "child":
                child_count += 1
                if not active_parent:
                    hierarchy_errors.append(_issue("子件前缺少可识别的母件行，不能确定产品编码", row_number,
                                                   "product_code", "bom_parent_missing"))
            else:
                active_parent = None
                hierarchy_errors.append(_issue("级次不是明确的“母件”或“数字级子件”，请人工核对",
                                               row_number, "bom_parent_column", "bom_level_ambiguous"))
        row_index = len(outcome["rows"])
        values = {field: value for field, value in normalized_fixed.items() if field not in positions}
        row_errors = hierarchy_errors
        if hierarchy_mode:
            values["product_code"] = active_parent if level == "child" else None
            values["source_line_no"] = str(row_number)
        for field, position in positions.items():
            raw = source_row[position]
            if field in IDENTIFIER_FIELDS and isinstance(raw, (date, datetime)):
                row_errors.append(_issue("标识符列被 Excel 识别为日期；请按文本重新导出", row_number, field))
                continue
            try:
                values[field] = _normalize(field, raw)
            except ValueError as exc:
                row_errors.append(_issue(str(exc), row_number, field))
            if field in IDENTIFIER_FIELDS and isinstance(raw, (float, int)):
                outcome["warnings"].append(_issue("源文件把标识符存为数字；如有前导零，导出时需改为文本", row_number, field, "numeric_identifier"))
        for field in required | set(keys):
            if values.get(field) is None:
                row_errors.append(_issue("必需值为空", row_number, field, "missing_value"))
        if kind in ("orders", "purchases", "receipts") and values.get("quantity") is not None:
            if Decimal(values["quantity"]) <= 0:
                row_errors.append(_issue("数量必须大于 0", row_number, "quantity"))
        if kind == "receipts" and values.get("accepted_quantity") is not None and values.get("quantity") is not None:
            if Decimal(values["accepted_quantity"]) > Decimal(values["quantity"]):
                row_errors.append(_issue("合格数量不能大于实收数量", row_number, "accepted_quantity"))
        if kind == "receipts" and values.get("iqc_status") in ("fail", "pending"):
            if values.get("accepted_quantity") is not None and Decimal(values["accepted_quantity"]) > 0:
                row_errors.append(_issue("未合格的入库行不能填写正数合格数量", row_number, "accepted_quantity"))
        if kind == "inventory":
            for field in ("as_of", "basis"):
                if values.get(field) is None:
                    outcome["warnings"].append(_issue("库存快照缺少日期或口径；应用前必须由操作者明确填写", row_number, field, "snapshot_metadata_missing"))
        if kind == "purchases" and values.get("eta") is None:
            outcome["warnings"].append(_issue("采购行缺少预计到货日；交期预测须标为无法确定并生成补日期待办", row_number, "eta", "eta_missing"))
        key = _stable_key(values, keys) if not row_errors else None
        old = prior.get(key) if key else None
        if hierarchy_mode and not row_errors:
            if old is not None and _text(old.get("material_code")) != values.get("material_code"):
                row_errors.append(_issue("该 Excel 行号在工作台已对应另一子件；请使用新 BOM 版本或人工核对后修改",
                                         row_number, "source_line_no", "bom_row_reassigned"))
            elif old is None and existing_material_lines.get((values["product_code"], values["bom_version"],
                                                               values["material_code"])):
                row_errors.append(_issue("该子件在同一 BOM 版本已有不同或缺失行号的记录；请检查文件是否重新排序或插行",
                                         row_number, "source_line_no", "bom_row_key_drift"))
        field_changes = []
        if old is not None and not row_errors:
            for field, new_value in values.items():
                try:
                    old_value = _normalize(field, old.get(field))
                except ValueError:
                    old_value = _text(old.get(field))
                if new_value != old_value:
                    field_changes.append({"row_index": row_index, "row_number": row_number, "key": key, "field": field, "old": old_value, "new": new_value})
        action = "invalid" if row_errors else ("create" if old is None else ("update" if field_changes else "unchanged"))
        entry = {"row_index": row_index, "row_number": row_number, "key": key, "values": values, "action": action, "changes": field_changes, "valid": not row_errors}
        outcome["rows"].append(entry)
        outcome["errors"].extend(row_errors)
        outcome["changes"].extend(field_changes)
        if key is not None:
            seen[key].append(entry)

    if hierarchy_mode and (not parent_count or not child_count):
        outcome["errors"].append(_issue("未识别到完整的母件与子件分组；请检查表头、级次列和物料编码列",
                                        code="bom_hierarchy_missing"))

    for duplicates in seen.values():
        if len(duplicates) > 1:
            for entry in duplicates:
                entry["action"] = "invalid"
                entry["valid"] = False
                entry["changes"] = []
                outcome["errors"].append(_issue("文件内唯一键重复；请映射源单据行号 source_line_no 或调整唯一键", entry["row_number"], None, "duplicate_key"))
    if kind == "inventory":
        for field in ("as_of", "basis"):
            unique_values = {entry["values"].get(field) for entry in outcome["rows"] if entry["values"].get(field) is not None}
            if len(unique_values) > 1:
                outcome["errors"].append(_issue(f"同一库存导入批次包含不同的 {field}；请拆分快照", field=field, code="mixed_snapshot"))
                for entry in outcome["rows"]:
                    entry["action"] = "invalid"
                    entry["valid"] = False
                    entry["changes"] = []
    outcome["changes"] = [change for row in outcome["rows"] if row["valid"] for change in row["changes"]]
    counts = {action: sum(row["action"] == action for row in outcome["rows"]) for action in ("create", "update", "unchanged", "invalid")}
    outcome["summary"] = {"sheet": sheet["name"], "ocr": sheet["ocr"],
                          "row_count": len(outcome["rows"]), **counts}
    return outcome

"""Phase vocabulary and draft schedule math for PCBA work.

The three phases represent separate runs under the same product line.  A
legacy run is left unclassified until a person confirms its phase.  Durations
are starting values for a draft, never a vendor or customer commitment.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

PHASE_LABELS = {
    "unclassified": "待归类",
    "rd": "研发试产打样",
    "pp": "PP 试产",
    "mp": "MP 量产",
}

PHASE_TEMPLATES = {
    "rd": [
        ("需求与打样目标", "0.5"),
        ("原理图与 PCB 设计", "2"),
        ("工程 BOM 确认", "0.5"),
        ("嘉立创打样下单", "0.5"),
        ("样板到料", "0"),
        ("研发焊接与调试", "2"),
        ("功能验证", "1"),
        ("问题关闭", "1"),
        ("样机交付", "0"),
    ],
    "pp": [
        ("PP 试产评审", "0.5"),
        ("BOM 版本锁定", "0.5"),
        ("物料齐套与 IQC", "1"),
        ("我方备料发料", "0.5"),
        ("板厂签收", "0"),
        ("SMT 贴片", "1.5"),
        ("DIP 插件", "0.5"),
        ("烧录与功能测试", "1"),
        ("FQC 检验", "0.5"),
        ("PP 问题关闭", "1"),
        ("试产交付", "0"),
    ],
    "mp": [
        ("订单确认", "0"),
        ("BOM 锁定", "0"),
        ("采购与到料", "1"),
        ("IQC 检验", "0.5"),
        ("我方备料发料", "0.5"),
        ("板厂签收", "0"),
        ("SMT 贴片", "1.5"),
        ("DIP 插件", "0.5"),
        ("烧录与功能测试", "1"),
        ("FQC 检验", "0.5"),
        ("板厂完工", "0"),
        ("回厂或直发", "0.5"),
        ("客户签收", "0"),
    ],
}


def phase_template(phase: str) -> list[dict[str, str]]:
    """Return fresh objects so callers cannot mutate the default template."""
    return [{"name": name, "duration_days": duration}
            for name, duration in PHASE_TEMPLATES.get(phase, ())]


def draft_dates(start_day: str, durations: Iterable[str | float | Decimal],
                mode: str = "workday",
                is_workday: Callable[[date], bool] | None = None) -> list[tuple[str, str]]:
    """Plan serial nodes in half days and return start/end calendar dates.

    This is a planning aid.  Its result has no formal promise status.  A
    workday callback can include the company holiday table.
    """
    steps = [{"duration_days": value} for value in durations]
    return [(row["planned_start"], row["planned_finish"])
            for row in plan_nodes(start_day, steps, mode, is_workday)]


def plan_nodes(start_day: str | None, nodes: Iterable[dict],
               mode: str = "workday",
               is_workday: Callable[[date], bool] | None = None) -> list[dict[str, str | None]]:
    """Compute a draft across nodes, including confirmed material milestones.

    A manually adjusted node remains at its chosen date.  Later nodes start
    from that node's finish, so a changed material arrival moves the tail of
    the plan without overwriting a human adjustment.
    """
    if mode not in {"workday", "natural"}:
        raise ValueError("calendar mode must be workday or natural")
    cursor = datetime.combine(date.fromisoformat(start_day), time.min) if start_day else None
    working = is_workday or (lambda day: day.weekday() < 5)
    planned = []
    for node in nodes:
        try:
            duration = Decimal(str(node.get("duration_days") or "0"))
        except InvalidOperation as exc:
            raise ValueError("duration must be numeric") from exc
        half_days = duration * 2
        if duration < 0 or duration > 365 or half_days != half_days.to_integral_value():
            raise ValueError("duration must be 0–365 days in half-day steps")
        arrival = node.get("actual_material_at")
        if cursor is not None and node.get("stage_type") == "milestone" and arrival:
            actual = datetime.combine(date.fromisoformat(str(arrival)), time.min)
            cursor = max(cursor, actual)
        if cursor is not None and mode == "workday" and not node.get("plan_overridden"):
            while not working(cursor.date()):
                cursor = datetime.combine(cursor.date() + timedelta(days=1), time.min)
        if cursor is not None and node.get("plan_overridden") and node.get("planned_start"):
            cursor = datetime.combine(date.fromisoformat(str(node["planned_start"])), time.min)
        start = cursor
        if cursor is not None and node.get("plan_overridden") and node.get("planned_finish"):
            cursor = datetime.combine(date.fromisoformat(str(node["planned_finish"])), time.min)
        elif cursor is not None:
            for _ in range(int(half_days)):
                cursor += timedelta(hours=12)
                if mode == "workday":
                    while not working(cursor.date()):
                        cursor = datetime.combine(cursor.date() + timedelta(days=1), time.min)
        planned.append({
            "planned_start": start.date().isoformat() if start else None,
            "planned_finish": cursor.date().isoformat() if cursor else None,
            "planned_start_at": start.isoformat() if start else None,
            "planned_finish_at": cursor.isoformat() if cursor else None,
        })
    return planned

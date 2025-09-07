# examples/fridge_agent/src/fridge_agent/inventory_tool.py
from __future__ import annotations

import typing
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Optional, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from nat.cli.register_workflow import register_function
from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.data_models.function import FunctionBaseConfig

from .db import (
    connect, migrate, get_default_days, upsert_default_days, add_item,
    list_inventory, consume, expiring_within, update_default_and_recalc_items,
    get_items_by_name, update_item_exp_by_id,
    discard,  # ← 新增
)


DATE_FMT = "%Y-%m-%d"  # 仅日期


class InventoryToolConfig(FunctionBaseConfig, name="inventory_tool"):
    db_path: str = Field(description="SQLite 数据库文件路径，例如 ./data/inventory.db")
    timezone: str = Field(default="Asia/Shanghai", description="IANA 时区名称")


class InventoryInput(BaseModel):
    """
    action:
      - add             : 入库（优先 exp_days / exp_at）。
      - consume         : 消耗（先消耗更早过期批次）。
      - discard         : 清除/丢弃（与 consume 扣减口径一致，但会记录 waste_logs）。
      - query           : 查询库存（按到期日升序，日期粒度）。
      - expiring        : 未来 N 天内会过期。
      - set_shelf_life  : 设置默认保质期并重算 exp_source='default'。
      - update_expiry   : 修改已有记录过期日（按 id 或按 name）。
    所有日期字段均为 YYYY-MM-DD；若给 ISO 日期时间，将自动取本地时区下的日期部分。
    """
    action: Literal["add", "consume", "discard", "query", "expiring", "set_shelf_life", "update_expiry"]


    # 通用
    name: Optional[str] = Field(default=None, description="食材名称")
    quantity: Optional[float] = Field(default=None, description="数量（浮点）")
    unit: Optional[str] = Field(default=None, description="单位，如 盒/个/g/kg/升")

    # 入库
    in_at: Optional[str] = Field(default=None, description="入库时间（YYYY-MM-DD 或 ISO），不传默认今天")
    exp_days: Optional[int] = Field(default=None, description="保质期天数")
    exp_at: Optional[str] = Field(default=None, description="过期日期（YYYY-MM-DD 或 ISO）")

    # expiring
    n_days: Optional[int] = Field(default=None, description="未来 N 天")

    # update_expiry
    id: Optional[int] = Field(default=None, description="要更新的记录 id（优先于 name）")
    mode: Optional[Literal["all", "earliest", "latest"]] = Field(default="all", description="按 name 更新时的范围")


def _parse_to_date(s: Optional[str], tz: str) -> date:
    if not s:
        return datetime.now(ZoneInfo(tz)).date()
    s = s.strip()
    # YYYY-MM-DD
    try:
        return datetime.strptime(s, DATE_FMT).date()
    except Exception:
        pass
    # ISO 兼容
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        else:
            dt = dt.astimezone(ZoneInfo(tz))
        return dt.date()
    except Exception:
        return datetime.strptime(s[:10], DATE_FMT).date()


@register_function(config_type=InventoryToolConfig)
async def register_inventory_tool(config: InventoryToolConfig, builder: Builder):
    """库存管理工具（按“日期”粒度处理 in_at/exp_at；到期早者先扣）"""
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(config.db_path)
    migrate(conn)

    async def _inventory_fn(payload: dict[str, typing.Any]) -> dict[str, typing.Any]:
        tz = config.timezone
        action = payload.get("action")

        try:
            # ---------------------- ADD ----------------------
            if action == "add":
                name = payload.get("name")
                quantity = payload.get("quantity")
                unit = payload.get("unit")
                in_at_raw = payload.get("in_at")
                exp_days = payload.get("exp_days")
                exp_at_raw = payload.get("exp_at")
                if not name or quantity is None:
                    return {"status": "error", "message": "add 需要提供 name 与 quantity"}

                in_d = _parse_to_date(in_at_raw, tz)
                if exp_days is not None:
                    exp_d = in_d + timedelta(days=int(exp_days))
                    exp_source = "user"
                elif exp_at_raw is not None:
                    exp_d = _parse_to_date(exp_at_raw, tz)
                    exp_source = "user"
                else:
                    days = get_default_days(conn, name)
                    if days is None:
                        upsert_default_days(conn, name, 7, tz)
                        days = 7
                    exp_d = in_d + timedelta(days=int(days))
                    exp_source = "default"

                item_id = add_item(conn, name, float(quantity), unit, in_d, exp_d, exp_source, tz)
                return {
                    "status": "ok",
                    "id": item_id,
                    "name": name,
                    "quantity": quantity,
                    "unit": unit,
                    "in_at": in_d.strftime(DATE_FMT),
                    "exp_at": exp_d.strftime(DATE_FMT),
                    "exp_source": exp_source,
                }

            # ---------------------- CONSUME ----------------------
            elif action == "consume":
                name = payload.get("name")
                quantity = payload.get("quantity")
                if not name or quantity is None:
                    return {"status": "error", "message": "consume 需要提供 name 与 quantity"}
                result = consume(conn, name, float(quantity), tz)
                return {"status": "ok", "result": result}

            # ---------------------- QUERY ----------------------
            elif action == "query":
                today = datetime.now(ZoneInfo(tz)).date()
                rows = list_inventory(conn)
                enriched: list[dict[str, typing.Any]] = []
                for r in rows:
                    exp_d = _parse_to_date(r["exp_at"], tz)
                    days_left = (exp_d - today).days
                    r["expired"] = days_left < 0
                    r["days_remaining"] = days_left
                    enriched.append(r)
                return {"status": "ok", "items": enriched}

            # ---------------------- EXPIRING ----------------------
            elif action == "expiring":
                n_days = payload.get("n_days")
                if n_days is None:
                    return {"status": "error", "message": "expiring 需要提供 n_days"}
                today = datetime.now(ZoneInfo(tz)).date()
                rows = expiring_within(conn, today, int(n_days))
                return {"status": "ok", "items": rows}

            # ---------------------- SET SHELF LIFE ----------------------
            elif action == "set_shelf_life":
                name = payload.get("name")
                exp_days = payload.get("exp_days")
                if not name or exp_days is None:
                    return {"status": "error", "message": "set_shelf_life 需要提供 name 与 exp_days"}
                changed = update_default_and_recalc_items(conn, name, int(exp_days), tz)
                return {"status": "ok", "updated_item_count": changed}
            
            # ---------------------- DISCARD ----------------------
            elif action == "discard":
                name = payload.get("name")
                quantity = payload.get("quantity")
                if not name or quantity is None:
                    return {"status": "error", "message": "discard 需要提供 name 与 quantity"}
                result = discard(conn, name, float(quantity), tz)
                return {"status": "ok", "result": result}


            # ---------------------- UPDATE EXPIRY ----------------------
            elif action == "update_expiry":
                item_id = payload.get("id")
                name = payload.get("name")
                mode = payload.get("mode") or "all"
                exp_days = payload.get("exp_days")
                exp_at_raw = payload.get("exp_at")
                if item_id is None and not name:
                    return {"status": "error", "message": "update_expiry 需要提供 id 或 name 其一"}

                updated = 0
                if item_id is not None:
                    row = next((r for r in list_inventory(conn) if r["id"] == int(item_id)), None)
                    if not row:
                        return {"status": "error", "message": f"未找到 id={item_id} 的记录"}
                    in_d = _parse_to_date(row["in_at"], tz)
                    if exp_days is not None:
                        new_exp_d = in_d + timedelta(days=int(exp_days))
                    elif exp_at_raw is not None:
                        new_exp_d = _parse_to_date(exp_at_raw, tz)
                    else:
                        return {"status": "error", "message": "update_expiry 需要提供 exp_days 或 exp_at"}
                    updated += update_item_exp_by_id(conn, int(item_id), new_exp_d, tz, exp_source="user")
                else:
                    rows = list(get_items_by_name(conn, name))
                    if not rows:
                        return {"status": "ok", "updated_item_count": 0}
                    if exp_days is None and exp_at_raw is None:
                        return {"status": "error", "message": "update_expiry 需要提供 exp_days 或 exp_at"}
                    if mode == "earliest":
                        targets = [rows[0]]
                    elif mode == "latest":
                        targets = [rows[-1]]
                    else:
                        targets = rows
                    for r in targets:
                        in_d = _parse_to_date(r["in_at"], tz)
                        if exp_days is not None:
                            new_exp_d = in_d + timedelta(days=int(exp_days))
                        else:
                            new_exp_d = _parse_to_date(exp_at_raw, tz)
                        updated += update_item_exp_by_id(conn, int(r["id"]), new_exp_d, tz, exp_source="user")
                return {"status": "ok", "updated_item_count": updated}

            return {"status": "error", "message": f"未知 action: {action}"}

        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    yield FunctionInfo.from_fn(
        _inventory_fn,
        description=register_inventory_tool.__doc__,
        input_schema=InventoryInput,
    )

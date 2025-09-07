# examples/fridge_agent/src/fridge_agent/db.py
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Iterable, Dict, Any
from zoneinfo import ZoneInfo

DATE_FMT = "%Y-%m-%d"  # 仅日期（年-月-日）


# -------------------- 库存模型 --------------------
@dataclass
class Item:
    id: int
    name: str
    quantity: float
    unit: Optional[str]
    in_at: str   # YYYY-MM-DD
    exp_at: str  # YYYY-MM-DD
    exp_source: str  # 'user' | 'default'
    created_at: str
    updated_at: str


# -------------------- 连接与迁移 --------------------
def connect(db_path: str) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        -- 默认保质期（按品名）
        CREATE TABLE IF NOT EXISTS shelf_life_defaults(
            name TEXT PRIMARY KEY,
            days INTEGER NOT NULL CHECK(days > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- 库存表（in_at/exp_at 仅存 YYYY-MM-DD）
        CREATE TABLE IF NOT EXISTS inventory(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            quantity REAL NOT NULL CHECK(quantity >= 0),
            unit TEXT,
            in_at TEXT NOT NULL,
            exp_at TEXT NOT NULL,
            exp_source TEXT NOT NULL CHECK(exp_source IN ('user','default')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_name ON inventory(name);
        CREATE INDEX IF NOT EXISTS idx_inventory_exp ON inventory(exp_at);

        -- 用户画像 profiles（最小可用字段）
        CREATE TABLE IF NOT EXISTS profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,              -- 小写存储
            allergens TEXT NOT NULL DEFAULT '[]',   -- JSON array
            avoid TEXT NOT NULL DEFAULT '[]',       -- JSON array
            diet_pattern TEXT,                      -- omnivore/vegetarian_ovo_lacto/vegan/halal/low_carb/...
            near_expiry_days INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS profile_aliases(
            alias TEXT PRIMARY KEY,                 -- 小写
            profile_id INTEGER NOT NULL,
            FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_profiles_name ON profiles(name);
        """
    )
    conn.commit()
    _normalize_existing_rows_to_date_only(conn, tz="Asia/Shanghai")


# -------------------- 时间与解析 --------------------
def _now_text(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")


def _to_date_str(d: date) -> str:
    return d.strftime(DATE_FMT)


def _parse_text_to_date(s: str, tz: str) -> date:
    s = s.strip()
    # YYYY-MM-DD
    try:
        return datetime.strptime(s, DATE_FMT).date()
    except Exception:
        pass
    # ISO 变体
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        else:
            dt = dt.astimezone(ZoneInfo(tz))
        return dt.date()
    except Exception:
        return datetime.strptime(s[:10], DATE_FMT).date()


def _normalize_existing_rows_to_date_only(conn: sqlite3.Connection, tz: str) -> None:
    rows = conn.execute(
        "SELECT id, in_at, exp_at FROM inventory "
        "WHERE length(in_at) != 10 OR length(exp_at) != 10 OR instr(in_at,'T')>0 OR instr(exp_at,'T')>0"
    ).fetchall()
    if not rows:
        return
    for r in rows:
        new_in = _to_date_str(_parse_text_to_date(r["in_at"], tz))
        new_exp = _to_date_str(_parse_text_to_date(r["exp_at"], tz))
        conn.execute("UPDATE inventory SET in_at=?, exp_at=? WHERE id=?", (new_in, new_exp, r["id"]))
    conn.commit()


# -------------------- 默认保质期逻辑 --------------------
def get_default_days(conn: sqlite3.Connection, name: str) -> Optional[int]:
    row = conn.execute("SELECT days FROM shelf_life_defaults WHERE name=?", (name,)).fetchone()
    return int(row["days"]) if row else None


def upsert_default_days(conn: sqlite3.Connection, name: str, days: int, tz: str) -> None:
    now = _now_text(tz)
    conn.execute(
        """
        INSERT INTO shelf_life_defaults(name, days, created_at, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(name) DO UPDATE SET days=excluded.days, updated_at=excluded.updated_at
        """,
        (name, days, now, now),
    )
    conn.commit()


# -------------------- 库存 CRUD --------------------
def add_item(
    conn: sqlite3.Connection,
    name: str,
    qty: float,
    unit: Optional[str],
    in_at_date: date,
    exp_at_date: date,
    exp_source: str,
    tz: str,
) -> int:
    now = _now_text(tz)
    cur = conn.execute(
        """
        INSERT INTO inventory(name, quantity, unit, in_at, exp_at, exp_source, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (name, qty, unit, _to_date_str(in_at_date), _to_date_str(exp_at_date), exp_source, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_default_and_recalc_items(conn: sqlite3.Connection, name: str, new_days: int, tz: str) -> int:
    upsert_default_days(conn, name, new_days, tz)
    rows = conn.execute("SELECT id, in_at FROM inventory WHERE name=? AND exp_source='default'", (name,)).fetchall()
    changed = 0
    now = _now_text(tz)
    for r in rows:
        in_d = _parse_text_to_date(r["in_at"], tz)
        new_exp = _to_date_str(in_d + timedelta(days=new_days))
        conn.execute("UPDATE inventory SET exp_at=?, updated_at=? WHERE id=?", (new_exp, now, r["id"]))
        changed += 1
    conn.commit()
    return changed


def list_inventory(conn: sqlite3.Connection) -> Iterable[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, name, quantity, unit, in_at, exp_at, exp_source, created_at, updated_at
        FROM inventory
        ORDER BY date(exp_at) ASC, date(in_at) ASC, id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_items_by_name(conn: sqlite3.Connection, name: str) -> Iterable[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, name, quantity, unit, in_at, exp_at, exp_source
        FROM inventory
        WHERE name=?
        ORDER BY date(exp_at) ASC, date(in_at) ASC, id ASC
        """,
        (name,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_item_exp_by_id(conn: sqlite3.Connection, item_id: int, new_exp_date: date, tz: str, exp_source: str = "user") -> int:
    now = _now_text(tz)
    cur = conn.execute(
        "UPDATE inventory SET exp_at=?, exp_source=?, updated_at=? WHERE id=?",
        (_to_date_str(new_exp_date), exp_source, now, item_id),
    )
    conn.commit()
    return cur.rowcount


def consume(conn: sqlite3.Connection, name: str, qty: float, tz: str) -> Dict[str, Any]:
    remaining = qty
    details = []
    rows = conn.execute(
        """
        SELECT id, quantity, exp_at
        FROM inventory
        WHERE name=?
        ORDER BY date(exp_at) ASC, id ASC
        """,
        (name,),
    ).fetchall()
    for r in rows:
        if remaining <= 0:
            break
        row_qty = float(r["quantity"])
        if row_qty <= remaining + 1e-9:
            conn.execute("DELETE FROM inventory WHERE id=?", (r["id"],))
            details.append({"row_id": int(r["id"]), "consumed": row_qty, "deleted": True})
            remaining -= row_qty
        else:
            new_qty = row_qty - remaining
            conn.execute(
                "UPDATE inventory SET quantity=?, updated_at=? WHERE id=?",
                (new_qty, _now_text(tz), r["id"]),
            )
            details.append({"row_id": int(r["id"]), "consumed": remaining, "deleted": False})
            remaining = 0.0
    conn.commit()
    return {"requested": qty, "unfulfilled": max(0.0, remaining), "details": details}


def expiring_within(conn: sqlite3.Connection, today: date, days: int) -> Iterable[Dict[str, Any]]:
    start = _to_date_str(today)
    end = _to_date_str(today + timedelta(days=days))
    rows = conn.execute(
        """
        SELECT id, name, quantity, unit, in_at, exp_at, exp_source
        FROM inventory
        WHERE date(exp_at) >= date(?) AND date(exp_at) <= date(?)
        ORDER BY date(exp_at) ASC, date(in_at) ASC, id ASC
        """,
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


# -------------------- 用户画像 CRUD --------------------
def _norm_name(name: str) -> str:
    return name.strip().lower()


def _loads(s: str) -> list[str]:
    try:
        v = json.loads(s or "[]")
        if isinstance(v, list):
            return [str(x) for x in v]
        return []
    except Exception:
        return []


def _dumps_unique(arr: Iterable[str]) -> str:
    # 去重 + 去空 + 保留原顺序（简单稳定）
    seen = set()
    out: list[str] = []
    for x in arr or []:
        t = str(x).strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return json.dumps(out, ensure_ascii=False)


def get_profile(conn: sqlite3.Connection, name: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM profiles WHERE name=?", (_norm_name(name),)).fetchone()
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "allergens": _loads(row["allergens"]),
        "avoid": _loads(row["avoid"]),
        "diet_pattern": row["diet_pattern"],
        "near_expiry_days": int(row["near_expiry_days"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_profiles(conn: sqlite3.Connection) -> list[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, allergens, avoid, diet_pattern, near_expiry_days, created_at, updated_at FROM profiles ORDER BY name ASC"
    ).fetchall()
    return [get_profile(conn, r["name"]) for r in rows]  # 复用解析


def set_profile(
    conn: sqlite3.Connection,
    name: str,
    *,
    allergens: Optional[Iterable[str]] = None,
    avoid: Optional[Iterable[str]] = None,
    diet_pattern: Optional[str] = None,
    near_expiry_days: Optional[int] = None,
    tz: str,
) -> int:
    """幂等 upsert：未提供的字段保留原值；提供空列表则清空。"""
    name_n = _norm_name(name)
    now = _now_text(tz)
    existing = conn.execute("SELECT * FROM profiles WHERE name=?", (name_n,)).fetchone()
    if existing:
        # 使用原值作为默认
        allergens_s = _dumps_unique(allergens if allergens is not None else _loads(existing["allergens"]))
        avoid_s = _dumps_unique(avoid if avoid is not None else _loads(existing["avoid"]))
        diet = diet_pattern if diet_pattern is not None else existing["diet_pattern"]
        ned = int(near_expiry_days) if near_expiry_days is not None else int(existing["near_expiry_days"])
        conn.execute(
            "UPDATE profiles SET allergens=?, avoid=?, diet_pattern=?, near_expiry_days=?, updated_at=? WHERE name=?",
            (allergens_s, avoid_s, diet, ned, now, name_n),
        )
        conn.commit()
        return int(existing["id"])
    else:
        allergens_s = _dumps_unique(allergens or [])
        avoid_s = _dumps_unique(avoid or [])
        ned = int(near_expiry_days) if near_expiry_days is not None else 3
        cur = conn.execute(
            "INSERT INTO profiles(name, allergens, avoid, diet_pattern, near_expiry_days, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (name_n, allergens_s, avoid_s, diet_pattern, ned, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)

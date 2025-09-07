# examples/fridge_agent/src/fridge_agent/prefs_tool.py
from __future__ import annotations

import typing
from typing import Optional, Literal
from pathlib import Path
import re

from pydantic import BaseModel, Field, conint, constr
from nat.cli.register_workflow import register_function
from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.data_models.function import FunctionBaseConfig

from .db import connect, migrate, get_profile, list_profiles, set_profile

DietPattern = Literal["omnivore", "vegetarian_ovo_lacto", "vegan", "halal", "low_carb"]

class PrefsToolConfig(FunctionBaseConfig, name="prefs_tool"):
    db_path: str = Field(description="SQLite 数据库文件路径，例如 ./data/inventory.db")
    timezone: str = Field(default="Asia/Shanghai", description="IANA 时区名称")


class ProfileModel(BaseModel):
    name: constr(strip_whitespace=True, min_length=1, max_length=64)
    allergens: list[constr(min_length=1, max_length=32)] = []
    avoid: list[constr(min_length=1, max_length=32)] = []
    diet_pattern: Optional[DietPattern] = None
    near_expiry_days: conint(ge=0) = 3


Action = Literal["get", "set", "list", "ensure"]


class PrefsInput(BaseModel):
    action: Action
    name: Optional[str] = None
    names: Optional[list[str]] = None
    profile: Optional[ProfileModel] = None
    language: Optional[Literal["zh", "ja", "en"]] = "zh"
    agent_input: Optional[str] = None  # 自由文本画像，如“不过敏，不吃香菜，荤素都可”


@register_function(config_type=PrefsToolConfig)
async def register_prefs_tool(config: PrefsToolConfig, builder: Builder):
    """用户画像管理（MVP：get / set / list / ensure）。缺画像时返回 3 个关键问题。"""
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(config.db_path)
    migrate(conn)

    def _q_prompt(lang: str, name: str, field: str) -> dict[str, typing.Any]:
        if lang == "ja":
            if field == "allergens":
                return {"name": name, "field": "allergens", "type": "multi",
                        "prompt": f"{name}は食物アレルギーがありますか？（例：ピーナッツ/甲殻類/牛乳/卵）"}
            if field == "avoid":
                return {"name": name, "field": "avoid", "type": "multi",
                        "prompt": f"{name}が苦手・避けたい食材はありますか？（例：パクチー/アルコール）"}
            if field == "diet_pattern":
                return {"name": name, "field": "diet_pattern", "type": "single",
                        "options": ["omnivore","vegetarian_ovo_lacto","vegan","halal","low_carb"],
                        "prompt": f"{name}の食事パターンは？"}
        # 默认中文
        if field == "allergens":
            return {"name": name, "field": "allergens", "type": "multi",
                    "prompt": f"{name}有食物过敏吗？(如花生/贝类/牛奶/鸡蛋)"}
        if field == "avoid":
            return {"name": name, "field": "avoid", "type": "multi",
                    "prompt": f"{name}有什么明确不吃/不爱的吗？(如香菜/酒精)"}
        if field == "diet_pattern":
            return {"name": name, "field": "diet_pattern", "type": "single",
                    "options": ["omnivore","vegetarian_ovo_lacto","vegan","halal","low_carb"],
                    "prompt": f"{name}的饮食模式是？"}
        return {"name": name, "field": field, "type": "text", "prompt": f"{name} - {field}？"}

    def _parse_free_text(text: str) -> dict[str, typing.Any]:
        """
        极简中文画像解析器：
        - “不过敏/没有过敏/无过敏” => allergens=[]
        - “对X过敏/过敏原X” => allergens=[...]
        - “不吃/不要/忌/避开/讨厌 + 词” => avoid=[...]
        - 饮食模式：荤素都可=omnivore；素食/蛋奶素=vegetarian_ovo_lacto；纯素=vegan；清真=halal；低碳=low_carb
        """
        text = (text or "").strip()
        allergens: list[str] = []
        avoid: list[str] = []
        diet: typing.Optional[str] = None

        # allergens
        if re.search(r"(不过敏|没有过敏|无过敏)", text):
            allergens = []
        for seg in re.findall(r"(?:对)([^。；;，,]+?)(?:过敏|过敏原?)", text):
            for it in re.split(r"[和、/，, ]+", seg):
                it = it.strip()
                if it:
                    allergens.append(it)

        # avoid
        for it in re.findall(r"(?:不吃|不要|不喜欢|忌|避开|讨厌)([^。；;，,\s]+)", text):
            it = it.strip()
            if it:
                if it in {"香菜", "芫荽", "香荽", "cilantro", "coriander", "coriander leaf"}:
                    it = "香菜"
                avoid.append(it)
        # 去重保序
        seen=set(); avoid=[x for x in avoid if not (x in seen or seen.add(x))]
        seen=set(); allergens=[x for x in allergens if not (x in seen or seen.add(x))]

        # diet
        if re.search(r"(荤素都可|不挑食)", text):
            diet = "omnivore"
        if re.search(r"(素食|吃素|蛋奶素)", text):
            diet = "vegetarian_ovo_lacto"
        if re.search(r"(纯素|全素|vegan)", text, flags=re.I):
            diet = "vegan"
        if re.search(r"(清真|halal)", text, flags=re.I):
            diet = "halal"
        if re.search(r"(低碳|low\s*carb)", text, flags=re.I):
            diet = "low_carb"

        return {"allergens": allergens, "avoid": avoid, "diet_pattern": diet}

    async def _prefs_fn(payload: dict[str, typing.Any]) -> dict[str, typing.Any]:
        action = payload.get("action")

        try:
            if action == "get":
                name = payload.get("name")
                if not name:
                    return {"status": "error", "message": "get 需要 name"}
                prof = get_profile(conn, name)
                if not prof:
                    return {"status": "not_found"}
                return {"status": "ok", "profile": prof}

            elif action == "list":
                return {"status": "ok", "items": list_profiles(conn)}

            elif action == "set":
                p = payload.get("profile") or {}
                name = (p.get("name") or payload.get("name"))
                # 允许仅给自由文本画像
                if not p and payload.get("agent_input"):
                    parsed = _parse_free_text(payload["agent_input"])
                    p = {"name": name, **parsed}
                if not name:
                    return {"status": "error", "message": "set 需要 profile.name 或 name"}
                pid = set_profile(
                    conn,
                    name=name,
                    allergens=p.get("allergens"),
                    avoid=p.get("avoid"),
                    diet_pattern=p.get("diet_pattern"),
                    near_expiry_days=p.get("near_expiry_days"),
                    tz=config.timezone,
                )
                return {"status": "ok", "id": pid, "profile": get_profile(conn, name)}

            elif action == "ensure":
                names = payload.get("names") or ([] if not payload.get("name") else [payload.get("name")])
                if not names:
                    return {"status": "error", "message": "ensure 需要 names"}
                lang = payload.get("language") or "zh"
                known, missing = [], []
                for n in names:
                    if get_profile(conn, n):
                        known.append(n)
                    else:
                        missing.append(n)

                # 若仅缺一个，且给了自由文本或结构化画像，则解析/落库后直接满足
                if len(missing) == 1 and (payload.get("agent_input") or payload.get("profile")):
                    n = missing[0]
                    parsed = {}
                    if payload.get("agent_input"):
                        parsed = _parse_free_text(payload["agent_input"])
                    elif payload.get("profile"):
                        pr = payload["profile"] or {}
                        parsed = {
                            "allergens": pr.get("allergens") or [],
                            "avoid": pr.get("avoid") or [],
                            "diet_pattern": pr.get("diet_pattern"),
                        }
                    set_profile(
                        conn, name=n,
                        allergens=parsed.get("allergens"),
                        avoid=parsed.get("avoid"),
                        diet_pattern=parsed.get("diet_pattern"),
                        tz=config.timezone,
                    )
                    known.append(n)
                    missing.clear()

                questions: list[dict[str, typing.Any]] = []
                for n in missing:
                    questions.append(_q_prompt(lang, n, "allergens"))
                    questions.append(_q_prompt(lang, n, "avoid"))
                    questions.append(_q_prompt(lang, n, "diet_pattern"))
                return {"status": "ok", "known": known, "missing": missing, "questions": questions}

            return {"status": "error", "message": f"未知 action: {action}"}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    yield FunctionInfo.from_fn(
        _prefs_fn,
        description=register_prefs_tool.__doc__,
        input_schema=PrefsInput,
    )

# examples/fridge_agent/src/fridge_agent/recipe_inspect_tool.py
from __future__ import annotations

import typing
import re
import json
import html
import requests
from typing import Optional, Literal
from pydantic import BaseModel, Field, HttpUrl
from nat.cli.register_workflow import register_function
from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.data_models.function import FunctionBaseConfig


class RecipeInspectConfig(FunctionBaseConfig, name="recipe_inspect_tool"):
    timeout_sec: int = Field(default=20, description="HTTP 超时时间")


class RecipeInspectInput(BaseModel):
    action: Literal["inspect"] = "inspect"
    url: HttpUrl
    servings: Optional[int] = None  # 可选，作为等比缩放参考


def _extract_ld_json(html_text: str) -> list[dict]:
    """从 HTML 中抓取 <script type=application/ld+json> 的 JSON-LD"""
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, flags=re.IGNORECASE | re.DOTALL
    )
    out: list[dict] = []
    for s in scripts:
        try:
            js = html.unescape(s)
            data = json.loads(js)
            if isinstance(data, dict):
                out.append(data)
            elif isinstance(data, list):
                for d in data:
                    if isinstance(d, dict):
                        out.append(d)
        except Exception:
            continue
    return out


def _pick_recipe(objs: list[dict]) -> Optional[dict]:
    for o in objs:
        t = o.get("@type")
        if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
            return o
    return None


def _parse_ingredients(arr: typing.Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(arr, list):
        return out
    for line in arr:
        raw = str(line).strip()
        # 粗糙解析：数量/单位/名称（失败就只保留原文）
        m = re.match(r'^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z一-龥/]+)?\s*(.*)$', raw)
        if m:
            amount = float(m.group(1))
            unit = (m.group(2) or "").strip() or None
            name = (m.group(3) or "").strip() or raw
            out.append({"name": name, "amount": amount, "unit": unit, "raw": raw})
        else:
            out.append({"name": raw, "amount": None, "unit": None, "raw": raw})
    return out


@register_function(config_type=RecipeInspectConfig)
async def register_recipe_inspect_tool(config: RecipeInspectConfig, builder: Builder):
    """解析菜谱页（JSON-LD）：返回 ingredients / yield / totalTime。
    拿不到时尽力兜底并保留 raw 文本，供用户确认后再扣库。
    """

    async def _inspect_fn(payload: dict[str, typing.Any]) -> dict[str, typing.Any]:
        try:
            url = payload.get("url")
            servings = payload.get("servings")
            r = requests.get(url, timeout=config.timeout_sec, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            html_text = r.text

            objs = _extract_ld_json(html_text)
            recipe = _pick_recipe(objs) or {}
            title = recipe.get("name") or re.findall(r"<title>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
            title = title[0].strip() if isinstance(title, list) and title else (title or "")

            ingredients = _parse_ingredients(recipe.get("recipeIngredient") or [])
            recipe_yield = recipe.get("recipeYield")
            total_time = recipe.get("totalTime")

            return {
                "status": "ok",
                "title": title,
                "url": url,
                "ingredients": ingredients,   # [{name, amount, unit, raw}]
                "yield": recipe_yield,
                "totalTime": total_time,
                "servings": servings,
                "note": "若 amount/unit 缺失，请用户确认后再扣库；单位换算可后续补强。",
            }
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    yield FunctionInfo.from_fn(
        _inspect_fn,
        description=register_recipe_inspect_tool.__doc__,
        input_schema=RecipeInspectInput,
    )

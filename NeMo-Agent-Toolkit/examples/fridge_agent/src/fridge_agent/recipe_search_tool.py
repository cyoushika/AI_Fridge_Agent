# examples/fridge_agent/src/fridge_agent/recipe_search_tool.py
from __future__ import annotations

import typing
import re
import requests
from typing import Optional, Literal
from pydantic import BaseModel, Field, conint
from nat.cli.register_workflow import register_function
from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.data_models.function import FunctionBaseConfig

CN_SITES = ["xiachufang.com", "douguo.com", "meishij.net"]
EN_SITES = ["allrecipes.com", "seriouseats.com", "thekitchn.com", "bbcgoodfood.com", "epicurious.com"]

class RecipeSearchConfig(FunctionBaseConfig, name="recipe_search_tool"):
    tavily_api_key: str = Field(description="Tavily API Key")
    max_results: conint(ge=1, le=25) = 12


class RecipeSearchInput(BaseModel):
    action: Literal["search"] = "search"
    ingredients: list[str] = Field(default_factory=list, description="正向关键词，来自库存")
    exclude: list[str] = Field(default_factory=list, description="负向关键词，来自画像（中英别名）")
    max_results: Optional[int] = None


def _mk_query_cn(ingredients: list[str]) -> str:
    core = " ".join(ingredients[:3]) if ingredients else "家常菜"
    return f"{core} 家常做法 快手 30分钟"


def _mk_query_en(ingredients: list[str]) -> str:
    core = " ".join(ingredients[:3]) if ingredients else "easy dinner"
    return f"{core} easy recipe quick 30 minutes"


def _search_tavily(api_key: str, query: str, include_domains: list[str], max_results: int) -> list[dict]:
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": False,
        "include_images": False,
        "include_domains": include_domains,
    }
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    data = r.json() or {}
    results = data.get("results") or []
    out = []
    for it in results:
        out.append({
            "title": it.get("title") or "",
            "url": it.get("url") or "",
            "snippet": (it.get("content") or "")[:500],
        })
    return out


def _contains_any(text: str, words: list[str]) -> list[str]:
    hits = []
    if not text or not words:
        return hits
    tl = text.lower()
    for w in words:
        if not w:
            continue
        if w.lower() in tl:
            hits.append(w)
    return hits


def _dedup(results: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in results:
        key = r.get("url") or r.get("title")
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


@register_function(config_type=RecipeSearchConfig)
async def register_recipe_search_tool(config: RecipeSearchConfig, builder: Builder):
    """基于 Tavily 的菜谱搜索：双语两拨 + 负关键词过滤 + 简单去重。"""
    api_key = config.tavily_api_key

    async def _recipe_search_fn(payload: dict[str, typing.Any]) -> dict[str, typing.Any]:
        try:
            ingredients = (payload.get("ingredients") or [])[:4]
            exclude = payload.get("exclude") or []
            max_results = int(payload.get("max_results") or config.max_results)

            # 生成查询
            q_cn = _mk_query_cn(ingredients)
            q_en = _mk_query_en(ingredients)

            # Tavily 检索
            rs_cn = _search_tavily(api_key, q_cn, CN_SITES, max_results)
            rs_en = _search_tavily(api_key, q_en, EN_SITES, max_results)

            # 合并 + 过滤负词 + 最少命中一个食材
            all_rs = _dedup(rs_cn + rs_en)
            filtered: list[dict] = []
            for r in all_rs:
                text = f"{r.get('title','')} {r.get('snippet','')}"
                if _contains_any(text, exclude):
                    continue
                hits = _contains_any(text, ingredients)
                if not hits and ingredients:
                    continue
                r["hits"] = hits
                filtered.append(r)

            # 截断 Top 5~12
            take = min(max_results, len(filtered)) if filtered else 0
            return {"status": "ok", "items": filtered[:take], "query_cn": q_cn, "query_en": q_en}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    yield FunctionInfo.from_fn(
        _recipe_search_fn,
        description=register_recipe_search_tool.__doc__,
        input_schema=RecipeSearchInput,
    )

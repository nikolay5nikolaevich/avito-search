"""
Чистая логика отчёта «Поиск под перепродажу» (docs/specs/resale-finder.md).

Без I/O: принимает уже собранные объявления и уже разобранные ИИ
классификации, считает рынок по модели (медиана цены рабочих объявлений)
и находит лоты заметно дешевле рынка.

Публичный API:
    model_key(classification) -> str | None
    build_report(items, classifications, *, threshold_pct=20, min_sample=5) -> dict
"""

from __future__ import annotations

import logging
import statistics
from typing import Any, Optional

logger = logging.getLogger(__name__)


def model_key(c: dict[str, Any]) -> Optional[str]:
    """
    "acer nitro 5 | rtx 3060": brand+series+gpu в нижнем регистре, пробелы
    схлопнуты. None, если нет brand, series или gpu, или is_laptop=False.
    """
    if not c.get("is_laptop"):
        return None
    brand = c.get("brand")
    series = c.get("series")
    gpu = c.get("gpu")
    if not brand or not series or not gpu:
        return None

    def _norm(text: str) -> str:
        return " ".join(str(text).split()).lower()

    return f"{_norm(brand)} {_norm(series)} | {_norm(gpu)}"


def _display_model(c: dict[str, Any]) -> str:
    """"{brand} {series} · {gpu}" в написании первого объявления группы."""
    return f"{c.get('brand')} {c.get('series')} · {c.get('gpu')}"


def build_report(
    items: list[dict[str, Any]],
    classifications: dict[str, dict[str, Any]],
    *,
    threshold_pct: float = 20,
    min_sample: int = 5,
) -> dict[str, Any]:
    """
    Группирует объявления по model_key, считает рынок (медиана цены рабочих
    объявлений группы, по ВСЕМ объявлениям группы независимо от is_local) и
    находит выгодные лоты — заметно дешевле рынка среди МЕСТНЫХ объявлений
    (is_local не False; ключа нет — считается местным). См. спеку, раздел
    «Решения / География».

    excluded считает объявления, не попавшие ни в один рынок: parts,
    is_laptop=False, без ключа модели (нет brand/series/gpu), с error.

    local_items в результате — сколько из total_items местных.
    """
    groups: dict[str, dict[str, Any]] = {}
    excluded = {"parts": 0, "not_laptop": 0, "unrecognized": 0, "errors": 0}

    for item in items:
        item_id = item.get("item_id")
        classification = classifications.get(item_id) if item_id else None
        if classification is None:
            # Объявление не разобрано вовсе (нет item_id или его не было во
            # входе classify_items) — считаем как нераспознанное.
            excluded["unrecognized"] += 1
            continue

        if classification.get("error"):
            excluded["errors"] += 1
            continue
        if not classification.get("is_laptop"):
            excluded["not_laptop"] += 1
            continue
        if classification.get("condition") == "parts":
            excluded["parts"] += 1
            continue

        key = model_key(classification)
        if key is None:
            excluded["unrecognized"] += 1
            continue

        group = groups.setdefault(key, {"model_key": key, "display": _display_model(classification), "candidates": []})
        group["candidates"].append((item, classification))

    deals: list[dict[str, Any]] = []
    group_summaries: list[dict[str, Any]] = []

    for key, group in groups.items():
        candidates = group["candidates"]

        working_prices = [
            item.get("price")
            for item, classification in candidates
            if classification.get("condition") == "working" and (item.get("price") or 0) > 0
        ]
        sample = len(working_prices)
        low_data = sample < min_sample

        if sample == 0:
            # Рынка нет — лотов из этой группы не показываем (см. спеку).
            continue

        market_price = statistics.median(working_prices)

        for item, classification in candidates:
            # Выгодный лот — только местное объявление (см. спеку, раздел
            # «Решения / География»); рынок выше уже посчитан по ВСЕЙ группе
            # независимо от is_local. Ключа нет — считается местным (на этом
            # держатся старые тесты, где is_local не заполнялся).
            if item.get("is_local") is False:
                continue
            price = item.get("price")
            condition = classification.get("condition")
            if condition not in ("working", "unknown"):
                continue
            if not price or price <= 0:
                continue
            if price <= market_price * (1 - threshold_pct / 100):
                discount_pct = round((market_price - price) / market_price * 100, 1)
                deals.append({
                    "item_id": item.get("item_id"),
                    "url": item.get("url"),
                    "title": item.get("title"),
                    "price": price,
                    "market_price": market_price,
                    "discount_pct": discount_pct,
                    "sample": sample,
                    "model": group["display"],
                    "model_key": key,
                    "condition": condition,
                    "reason": classification.get("reason") or "",
                    "low_data": low_data,
                })

        group_summaries.append({
            "model_key": key,
            "model": group["display"],
            "sample": sample,
            "market_price": market_price,
            "min_price": min(working_prices),
            "max_price": max(working_prices),
            "low_data": low_data,
        })

    deals.sort(key=lambda d: d["discount_pct"], reverse=True)
    group_summaries.sort(key=lambda g: g["sample"], reverse=True)

    local_items = sum(1 for item in items if item.get("is_local") is not False)

    return {
        "deals": deals,
        "groups": group_summaries,
        "excluded": excluded,
        "total_items": len(items),
        "local_items": local_items,
    }

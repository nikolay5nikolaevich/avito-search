"""
Оркестрация «Поиска под перепродажу» (docs/specs/resale-finder.md).

Сбор объявлений по запросу в городе → разбор ИИ → расчёт рынка по модели →
добор уточняющими запросами для групп с кандидатом в выгодные лоты, но мало
рабочих объявлений → пересчёт отчёта.

Только чтение Авито: сбор идёт через parser._collect_listing_items — ту же
функцию, что использует аналитика спроса (парсер и фильтрация местных
объявлений уже отработаны там), ни одного клика по кнопкам Авито.

Публичный API:
    run_resale_scan(query, city, *, max_items=150, threshold_pct=20,
                     min_sample=5, filters=None, progress_cb=None) -> dict
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Optional

from playwright.async_api import async_playwright

from browser import connect_over_cdp
from cities import City
from filters import SearchFilters
from parser import AvitoBlockedError, _collect_listing_items
import resale_classifier
import resale_market

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int], None]  # (stage, done, total)

# Не более стольких уточняющих поисков за задачу — нагрузка на Авито (см. спеку).
MAX_TOPUP_QUERIES = 8

# Уточняющий поиск уже, чем основной (спека: "≤ 50 объявлений").
TOPUP_MAX_ITEMS = 50


@asynccontextmanager
async def _owned_page():
    """Подключается по CDP; закрывает исключительно созданную здесь вкладку
    (тот же приём, что seller_scan._owned_page / outreach._owned_page)."""
    async with async_playwright() as pw:
        context = await connect_over_cdp(pw, os.getenv("AVITO_CDP_URL", "http://127.0.0.1:9222"))
        page = await context.new_page()
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:
                logger.warning("Не удалось закрыть вкладку поиска под перепродажу", exc_info=True)


def _progress(cb: Optional[ProgressCallback], stage: str, done: int, total: int) -> None:
    if cb:
        try:
            cb(stage, done, total)
        except Exception:
            logger.warning("Не удалось обновить прогресс поиска под перепродажу", exc_info=True)


def _dedup_by_item_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Оставляет первое вхождение каждого item_id; объявления без item_id
    оставляет как есть (classify_items их и так пропустит — нечем ключевать)."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        item_id = item.get("item_id")
        if item_id:
            if item_id in seen:
                continue
            seen.add(item_id)
        result.append(item)
    return result


def _topup_query(display_model: str) -> str:
    """"Acer Nitro 5 · RTX 3060" -> "Acer Nitro 5 RTX 3060" ("{brand} {series}
    {gpu}" из спеки — display_model уже несёт эти три части в нужном порядке,
    достаточно убрать разделитель "·")."""
    return display_model.replace(" · ", " ").replace("·", " ").strip()


def _find_topup_candidate(report: dict[str, Any], exclude: set[str]) -> Optional[dict[str, Any]]:
    """
    Группа-кандидат на добор: low_data (рабочих объявлений меньше min_sample)
    и при этом уже дала хотя бы один выгодный лот (см. спеку — «если в группе
    есть кандидат в выгодные лоты»). exclude — модели, для которых добор уже
    делали в этой задаче (не повторяем один и тот же добор бесконечно — см.
    docstring run_resale_scan про MAX_TOPUP_QUERIES).
    """
    deal_keys = {deal["model_key"] for deal in report["deals"]}
    for group in report["groups"]:
        if group["model_key"] in exclude:
            continue
        if group["low_data"] and group["model_key"] in deal_keys:
            return group
    return None


def _all_failed(classifications: dict[str, dict[str, Any]]) -> Optional[str]:
    """
    Если ХОТЬ ОДНО объявление было передано на разбор и ВСЕ они получили
    error — вероятно, claude CLI систематически не работает (не найден,
    авторизация, сеть — resale_classifier.classify_items сама такие сбои не
    поднимает исключением, см. её docstring: ошибка одного шага — лог и
    пропуск, а не падение). Возвращает текст первой ошибки или None, если
    словарь пуст либо хотя бы одна классификация прошла успешно.
    """
    if not classifications:
        return None
    errors = [c.get("error") for c in classifications.values() if c.get("error")]
    if len(errors) == len(classifications):
        return errors[0]
    return None


async def run_resale_scan(
    query: str,
    city: City,
    *,
    max_items: int = 150,
    threshold_pct: float = 20,
    min_sample: int = 5,
    filters: Optional[SearchFilters] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> dict[str, Any]:
    """
    Полный сценарий поиска под перепродажу для одного запроса в одном городе.

    Решение (не расписано в спеке дословно, уточнено после независимых
    тестов resale_classifier — см. tests/test_resale_classifier.py):
    resale_classifier.classify_items никогда не бросает исключение сама
    (ошибка одного шага — лог и пропуск, не падение) — даже если сам вызов
    claude CLI систематически не работает (не найден в PATH, авторизация,
    сеть), она просто проставляет одну и ту же ошибку всем объявлениям пачки
    и не тратит время на заведомо провальные следующие пачки. Обнаружить
    «CLI сломан целиком» после этого — дело run_resale_scan: если после
    ОСНОВНОГО разбора у ВСЕХ переданных на классификацию объявлений error —
    вся задача останавливается исключением ClaudeCliError с этим текстом
    (см. _all_failed; app.py ловит его и ставит job статус error). Тот же
    сбой на пачке ДОБОРА не валит задачу — добор просто прекращается,
    уже посчитанный отчёт по основному сбору не теряется.

    Стадии progress_cb: collecting, classifying, topup, done.
    """
    async with _owned_page() as page:
        _progress(progress_cb, "collecting", 0, max_items)
        # local_only=False — рынок считается по всей выдаче города, включая
        # чужие города с доставкой (см. спеку, раздел «Решения / География»).
        raw_items = await _collect_listing_items(
            page, city, query, max_items, filters, local_only=False,
        )
        items = _dedup_by_item_id(raw_items)
        _progress(progress_cb, "collecting", len(items), max_items)

        _progress(progress_cb, "classifying", 0, len(items))
        # Синхронный subprocess — в отдельном потоке, чтобы не держать event loop
        # (см. спеку).
        classifications = await asyncio.to_thread(resale_classifier.classify_items, items)
        _progress(progress_cb, "classifying", len(items), len(items))

        systemic_error = _all_failed(classifications)
        if systemic_error is not None:
            # См. docstring выше: сама classify_items не поднимает исключение,
            # это делаем мы — увидев error у 100% разобранных объявлений.
            raise resale_classifier.ClaudeCliError(systemic_error)

        report = resale_market.build_report(
            items, classifications, threshold_pct=threshold_pct, min_sample=min_sample,
        )

        seen_ids: set[str] = {item["item_id"] for item in items if item.get("item_id")}
        topped_up_keys: set[str] = set()
        topup_queries: list[str] = []

        while len(topup_queries) < MAX_TOPUP_QUERIES:
            candidate = _find_topup_candidate(report, topped_up_keys)
            if candidate is None:
                break
            topped_up_keys.add(candidate["model_key"])

            topup_q = _topup_query(candidate["model"])
            _progress(progress_cb, "topup", len(topup_queries), MAX_TOPUP_QUERIES)
            logger.info("Добор: '%s' (модель %s, сейчас sample=%d)", topup_q, candidate["model"], candidate["sample"])

            try:
                new_raw = await _collect_listing_items(
                    page, city, topup_q, TOPUP_MAX_ITEMS, filters, local_only=False,
                )
            except AvitoBlockedError:
                # Блокировка останавливает всю задачу (статус "blocked"), а
                # не только этот добор — см. спеку и seller_scan.scan_seller.
                raise
            except Exception as exc:
                # Ошибка одного шага (уточняющего поиска) — лог и пропуск,
                # не падение всей задачи (см. спеку, раздел «Сценарий»).
                logger.warning("Добор '%s' не удался: %s", topup_q, exc)
                topup_queries.append(topup_q)
                continue

            topup_queries.append(topup_q)
            new_items = [it for it in new_raw if it.get("item_id") and it["item_id"] not in seen_ids]
            if not new_items:
                logger.info("Добор '%s' не дал новых объявлений", topup_q)
                continue
            for it in new_items:
                seen_ids.add(it["item_id"])
            items.extend(new_items)

            new_classifications = await asyncio.to_thread(resale_classifier.classify_items, new_items)
            topup_systemic_error = _all_failed(new_classifications)
            if topup_systemic_error is not None:
                # См. docstring выше: сбой CLI на доборе не валит всю задачу,
                # просто дальше не доборам — отчёт по уже собранному остаётся.
                logger.warning("Добор остановлен — разбор ИИ недоступен: %s", topup_systemic_error)
                break
            classifications.update(new_classifications)

            report = resale_market.build_report(
                items, classifications, threshold_pct=threshold_pct, min_sample=min_sample,
            )

        _progress(progress_cb, "done", len(items), len(items))

    return {**report, "topup_queries": topup_queries, "query": query, "city": city.slug}

"""
Приёмочные тесты правила «География» «Поиска под перепродажу»
(docs/specs/resale-finder.md, раздел «Решения / География», пересмотрено
28.09.2026 после первого живого прогона: в Петербурге по «ноутбук Gigabyte
G5 MF» Авито показал 41 объявление, местных из них 3 — по местным рынок
не посчитать).

Правило: рынок считается по ВСЕМ объявлениям выдачи города (включая чужие
города с доставкой), выгодные лоты — ТОЛЬКО местные.

Тесты написаны от спеки, а не от кода parser.py/resale_market.py — они
проверяют то же поведение, что описано в docs/specs/resale-finder.md,
независимо от того, как это реализовал другой агент параллельно. Падения
против текущего кода ожидаемы, пока правка не закончена (см. бриф).

Никакой сети и живого Авито — фейковая Page (как в test_parser_collect.py)
и словари-фикстуры (как в test_resale_market.py).
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from bs4 import BeautifulSoup  # noqa: E402

import parser as avito_parser  # noqa: E402
import resale_market  # noqa: E402
from cities import City  # noqa: E402

CITY = City("Санкт-Петербург", "sankt-peterburg", True, 5602)
FOREIGN_SLUG = "moskva"  # чужой город с доставкой — не равен CITY.slug


# ── Фикстуры: build_report (items/classifications) ─────────────────────────

def _item(
    item_id: str,
    price,
    *,
    is_local: bool = True,
    omit_is_local_key: bool = False,
    title: str | None = None,
) -> dict:
    """Объявление в формате parser._item_from_json / _extract_items_from_html
    при local_only=False: несёт ключ is_local (см. спеку, раздел «Сценарий»,
    шаг 1). omit_is_local_key=True имитирует объявление старого формата без
    этого ключа вовсе — спека требует трактовать такое как местное."""
    d = {
        "item_id": item_id,
        "title": title or f"Ноутбук {item_id}",
        "price": price,
        "url": f"https://www.avito.ru/{CITY.slug if is_local else FOREIGN_SLUG}/noutbuki/item_{item_id}",
        "category_slug": "noutbuki",
        "description": "",
    }
    if not omit_is_local_key:
        d["is_local"] = is_local
    return d


def _cls(
    *,
    brand="Acer",
    series="Nitro 5",
    gpu="RTX 3060",
    condition="working",
    is_laptop=True,
    reason="исправен",
    error=None,
) -> dict:
    """Классификация в формате resale_classifier.classify_items."""
    return {
        "is_laptop": is_laptop,
        "brand": brand,
        "series": series,
        "gpu": gpu,
        "condition": condition,
        "reason": reason,
        "error": error,
    }


# ── build_report: рынок по всем городам, лоты — только местные ─────────────

class MarketAcrossCitiesTests(unittest.TestCase):
    def test_cheap_non_local_item_lowers_median_but_is_not_a_deal(self) -> None:
        """Дешёвое неместное рабочее объявление участвует в подсчёте медианы
        (рынок — по всем объявлениям выдачи города), но само в лоты не
        попадает (лоты — только местные)."""
        local_items = [
            _item(f"w{i}", price, is_local=True)
            for i, price in enumerate((50000, 52000, 54000, 56000, 58000), start=1)
        ]
        classifications = {item["item_id"]: _cls(condition="working") for item in local_items}
        median_without = statistics.median([50000, 52000, 54000, 56000, 58000])
        self.assertEqual(median_without, 54000)

        cheap_non_local = _item("cheap_nonlocal", 10000, is_local=False)
        items = local_items + [cheap_non_local]
        classifications["cheap_nonlocal"] = _cls(condition="working")

        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=1)
        group = report["groups"][0]

        # Медиана сдвинулась — значит неместное объявление учтено в рынке.
        expected_median = statistics.median([10000, 50000, 52000, 54000, 56000, 58000])
        self.assertEqual(expected_median, 53000)
        self.assertEqual(group["market_price"], expected_median)
        self.assertEqual(group["sample"], 6)

        # Но несмотря на явную скидку (10000 против рынка ~53000) — это
        # неместное объявление, в лоты оно попасть не должно.
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertNotIn("cheap_nonlocal", deal_ids)

    def test_local_item_below_threshold_is_a_deal(self) -> None:
        """Контрольная проверка: местное объявление ниже порога — лот
        (симметрично предыдущему тесту, чтобы не потерять базовый случай)."""
        items = [
            _item(f"w{i}", price, is_local=True)
            for i, price in enumerate((50000, 52000, 54000, 56000, 58000), start=1)
        ]
        classifications = {item["item_id"]: _cls(condition="working") for item in items}
        deal = _item("deal", 30000, is_local=True)
        items.append(deal)
        classifications["deal"] = _cls(condition="unknown")

        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=1)
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertIn("deal", deal_ids)

    def test_item_without_is_local_key_is_treated_as_local(self) -> None:
        """Спека: «объявление без ключа is_local считается местным». Проверяем
        именно отсутствие ключа (не is_local=False)."""
        items = [
            _item(f"w{i}", price, is_local=True)
            for i, price in enumerate((50000, 52000, 54000, 56000, 58000), start=1)
        ]
        classifications = {item["item_id"]: _cls(condition="working") for item in items}
        deal = _item("no_key_deal", 30000, omit_is_local_key=True)
        self.assertNotIn("is_local", deal)
        items.append(deal)
        classifications["no_key_deal"] = _cls(condition="unknown")

        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=1)
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertIn("no_key_deal", deal_ids)

    def test_non_local_item_below_threshold_is_never_a_deal_even_as_only_candidate(self) -> None:
        """Даже если единственный кандидат ниже порога — неместный, лотов
        в отчёте быть не должно (не путать с «рынок есть, лот не найден»)."""
        items = [
            _item(f"w{i}", price, is_local=True)
            for i, price in enumerate((50000, 52000, 54000, 56000, 58000), start=1)
        ]
        classifications = {item["item_id"]: _cls(condition="working") for item in items}
        cheap = _item("nonlocal_only_candidate", 20000, is_local=False)
        items.append(cheap)
        classifications["nonlocal_only_candidate"] = _cls(condition="unknown")

        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=1)
        self.assertEqual(report["deals"], [])

    def test_sample_and_low_data_counted_over_all_cities_not_only_local(self) -> None:
        """Ключевой случай из живого прогона: местных объявлений в группе
        меньше min_sample, но с учётом неместных выборка достаточна — рынок
        считается, low_data=False. Без этого правила (рынок только по
        местным) выборка была бы 1 < min_sample=5 и группа была бы low_data."""
        # 4 неместных рабочих + 1 местное рабочее = sample 5, ровно min_sample.
        non_local_working = [
            _item(f"nl{i}", price, is_local=False)
            for i, price in enumerate((50000, 52000, 54000, 56000), start=1)
        ]
        local_working = [_item("local1", 58000, is_local=True)]
        items = non_local_working + local_working
        classifications = {item["item_id"]: _cls(condition="working") for item in items}

        # Местный лот заметно дешевле рынка.
        deal = _item("local_deal", 30000, is_local=True)
        items.append(deal)
        classifications["local_deal"] = _cls(condition="unknown")

        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=5)
        group = report["groups"][0]
        self.assertEqual(group["sample"], 5)
        self.assertFalse(group["low_data"])
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertIn("local_deal", deal_ids)
        self.assertFalse(next(d for d in report["deals"] if d["item_id"] == "local_deal")["low_data"])


# ── build_report: local_items ────────────────────────────────────────────────

class LocalItemsCountTests(unittest.TestCase):
    def test_local_items_counts_only_local(self) -> None:
        items = [
            _item("1", 50000, is_local=True),
            _item("2", 52000, is_local=False),
            _item("3", 54000, is_local=False),
            _item("4", 56000, omit_is_local_key=True),  # без ключа — местное
        ]
        classifications = {item["item_id"]: _cls(condition="working") for item in items}
        report = resale_market.build_report(items, classifications, min_sample=1)
        # Местные: "1" (is_local=True явно) и "4" (без ключа — считается местным).
        self.assertEqual(report["local_items"], 2)
        self.assertEqual(report["total_items"], 4)

    def test_local_items_present_even_when_everything_excluded(self) -> None:
        """local_items считает по всем входным items, а не только по тем,
        что попали в рынок/лоты — исключённые (parts/not_laptop/…) тоже
        учитываются в local_items, если они местные."""
        items = [_item("1", 30000, is_local=True)]
        classifications = {"1": _cls(condition="parts")}
        report = resale_market.build_report(items, classifications, min_sample=1)
        self.assertEqual(report["local_items"], 1)
        self.assertEqual(report["excluded"]["parts"], 1)


# ── _collect_listing_items(local_only=False): местные и неместные, дедуп ────

def _card(item_id: str, slug: str, title: str, description: str = "") -> str:
    """Минимальная карточка выдачи с нужными data-marker (см. avito_selectors.py).
    slug задаёт «город» объявления в href — для проверки is_local."""
    return f"""
    <div data-marker="item" data-item-id="{item_id}">
      <a data-marker="item-title" href="/{slug}/{item_id}">{title}</a>
      <span data-marker="item-price-value">10 000 &#8381;</span>
      <meta itemprop="description" content="{description}">
    </div>
    """


def _page_html(cards: list[str]) -> str:
    return f"<html><body>{''.join(cards)}</body></html>"


class _FakePage:
    """Фейковая Playwright Page: отдаёт заранее заготовленный HTML по счётчику
    вызовов goto(), без сети и таймингов (см. test_parser_collect.py)."""

    def __init__(self, html_pages: list[str]) -> None:
        self.html_pages = html_pages
        self.goto_calls = 0
        self._current_html = ""

    async def goto(self, url: str, **_kwargs: object) -> None:
        self._current_html = self.html_pages[self.goto_calls]
        self.goto_calls += 1

    async def content(self) -> str:
        return self._current_html

    async def title(self) -> str:
        return ""

    async def wait_for_selector(self, *_args: object, **_kwargs: object) -> None:
        return None

    def locator(self, selector: str) -> "_FakeLocator":
        return _FakeLocator(self._current_html, selector)

    async def evaluate(self, *_args: object, **_kwargs: object) -> None:
        return None


class _FakeLocator:
    def __init__(self, html: str, selector: str) -> None:
        self.html = html
        self.selector = selector

    async def count(self) -> int:
        soup = BeautifulSoup(self.html, "html.parser")
        return len(soup.select("[data-marker='item']"))


class CollectListingItemsGeoTests(unittest.TestCase):
    def test_local_only_false_collects_local_and_foreign_with_is_local_flag(self) -> None:
        page1 = _page_html([
            _card("1", CITY.slug, "Местный товар"),
            _card("A", FOREIGN_SLUG, "Чужой товар с доставкой"),
        ])
        fake_page = _FakePage([page1])

        with mock.patch.object(avito_parser.asyncio, "sleep", new=mock.AsyncMock()):
            collected = asyncio.run(
                avito_parser._collect_listing_items(
                    fake_page, CITY, "тест", max_items=40, filters=None, local_only=False,
                )
            )

        by_id = {item["item_id"]: item for item in collected}
        self.assertEqual(set(by_id), {"1", "A"})
        self.assertTrue(by_id["1"]["is_local"])
        self.assertFalse(by_id["A"]["is_local"])

    def test_local_only_false_dedups_across_pages_and_stops_when_no_new(self) -> None:
        """Тот же предохранитель пагинации, что и для местных (см.
        test_parser_collect.py), должен работать и когда собираются все
        города: дедуп по item_id, стоп, если на странице нет новых —
        неважно, местное объявление или с доставкой."""
        page1 = _page_html([
            _card("1", CITY.slug, "Местный 1"),
            _card("A", FOREIGN_SLUG, "Чужой A"),
        ])
        page2 = _page_html([
            _card("1", CITY.slug, "Местный 1"),   # повтор
            _card("A", FOREIGN_SLUG, "Чужой A"),   # повтор
            _card("B", FOREIGN_SLUG, "Чужой B"),   # новое
        ])
        page3 = _page_html([
            _card("1", CITY.slug, "Местный 1"),   # только повторы — стоп
            _card("A", FOREIGN_SLUG, "Чужой A"),
        ])
        fake_page = _FakePage([page1, page2, page3])

        with mock.patch.object(avito_parser.asyncio, "sleep", new=mock.AsyncMock()):
            collected = asyncio.run(
                avito_parser._collect_listing_items(
                    fake_page, CITY, "тест", max_items=40, filters=None, local_only=False,
                )
            )

        self.assertEqual({item["item_id"] for item in collected}, {"1", "A", "B"})
        # Третья страница (одни повторы) запрошена, но пагинация на ней и
        # остановилась — четвёртой уже не будет (см. IndexError-предохранитель
        # _FakePage, если сборщик запросит лишнюю страницу).
        self.assertEqual(fake_page.goto_calls, 3)

    def test_local_only_true_default_still_returns_only_local(self) -> None:
        """Регрессия: старое поведение (аналитика) не должно измениться —
        local_only по умолчанию True, чужие объявления не возвращаются."""
        page1 = _page_html([
            _card("1", CITY.slug, "Местный товар"),
            _card("A", FOREIGN_SLUG, "Чужой товар с доставкой"),
        ])
        fake_page = _FakePage([page1])

        with mock.patch.object(avito_parser.asyncio, "sleep", new=mock.AsyncMock()):
            collected = asyncio.run(
                avito_parser._collect_listing_items(
                    fake_page, CITY, "тест", max_items=40, filters=None,
                )
            )

        self.assertEqual({item["item_id"] for item in collected}, {"1"})


# ── _extract_items_from_html: описание вне блока продавца ──────────────────

def _card_with_seller_block(
    item_id: str,
    slug: str,
    title: str,
    *,
    description_text: str | None,
    meta_description: str | None = None,
) -> str:
    """Карточка, воспроизводящая реальную вёрстку живой выдачи
    (debug/search_live_noutbuki.html, снято 28.09.2026):
    несколько <p style="--module-max-lines-size:..."> в одной карточке —
    первый (если он есть) это описание объявления, ellipsis-однострочные
    дальше — имя продавца (внутри <a href=".../?...iid=...">) и стаж
    продавца (data-marker="seller-info/summary"). Meta itemprop=description
    — старый резерв, добавляется отдельно, если задан.
    """
    description_p = (
        f'<p style="--module-max-lines-size:4">{description_text}</p>'
        if description_text is not None
        else ""
    )
    meta_tag = (
        f'<meta itemprop="description" content="{meta_description}">'
        if meta_description is not None
        else ""
    )
    return f"""
    <div data-marker="item" data-item-id="{item_id}">
      <a data-marker="item-title" href="/{slug}/{item_id}">{title}</a>
      <span data-marker="item-price-value">10 000 &#8381;</span>
      {meta_tag}
      {description_p}
      <a href="/site/seller?iid=8268157710">
        <p style="--module-max-lines-size:1">Продавец Иванов</p>
      </a>
      <div>
        <div data-marker="seller-rating">5.0</div>
      </div>
      <p data-marker="seller-info/summary" style="--module-max-lines-size:1">1 месяц на Авито</p>
    </div>
    """


class ExtractItemsFromHtmlSellerBlockTests(unittest.TestCase):
    def test_description_is_first_p_outside_seller_block(self) -> None:
        html = _page_html([
            _card_with_seller_block(
                "1", CITY.slug, "Ноутбук с описанием",
                description_text="Продаю ноутбук Gigabyte G5 MF, состояние отличное",
            )
        ])
        items = avito_parser._extract_items_from_html(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["description"],
            "Продаю ноутбук Gigabyte G5 MF, состояние отличное",
        )

    def test_seller_name_is_not_used_as_description_when_description_missing(self) -> None:
        """Карточка без реального описания (только блок продавца с тем же
        style) — description не должен стать именем продавца «Продавец
        Иванов» или стажем «1 месяц на Авито»."""
        html = _page_html([
            _card_with_seller_block(
                "1", CITY.slug, "Ноутбук без описания",
                description_text=None,
            )
        ])
        items = avito_parser._extract_items_from_html(html)
        self.assertEqual(len(items), 1)
        self.assertNotEqual(items[0]["description"], "Продавец Иванов")
        self.assertNotEqual(items[0]["description"], "1 месяц на Авито")

    def test_old_meta_itemprop_description_used_as_reserve(self) -> None:
        """Карточка старой вёрстки: нет p[style*='--module-max-lines-size']
        вовсе, только meta itemprop=description — резерв должен сработать."""
        html = """<html><body>
          <div data-marker="item" data-item-id="1">
            <a data-marker="item-title" href="/sankt-peterburg/1">Старая вёрстка</a>
            <span data-marker="item-price-value">1 000 &#8381;</span>
            <meta itemprop="description" content="Описание из старого резерва">
          </div>
        </body></html>"""

        items = avito_parser._extract_items_from_html(html)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["description"], "Описание из старого резерва")


if __name__ == "__main__":
    unittest.main()

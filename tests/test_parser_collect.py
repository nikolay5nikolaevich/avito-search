"""
Регрессии сбора выдачи в _collect_listing_items / _extract_items_from_html.

Контекст (docs/specs/resale-finder.md, живой прогон 28.09.2026): Авито
(выдача с localPriority) повторяет одни и те же местные объявления на
каждой странице пагинации — без дедупа сборщик листал 12 страниц ради
3 уникальных объявлений. Заодно HTML-путь не отдавал описание карточки
(нужно классификатору «Поиска под перепродажу»), хотя оно есть в meta
itemprop="description" внутри каждой карточки.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from bs4 import BeautifulSoup  # noqa: E402

import parser as avito_parser  # noqa: E402
from cities import City  # noqa: E402

CITY = City("Санкт-Петербург", "sankt-peterburg", True, 5602)


def _card(item_id: str, slug: str, title: str, description: str = "") -> str:
    """Минимальная карточка выдачи с нужными data-marker (см. avito_selectors.py)."""
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
    вызовов goto(), без сети и таймингов."""

    def __init__(self, html_pages: list[str]) -> None:
        self.html_pages = html_pages
        self.goto_calls = 0
        self._current_html = ""

    async def goto(self, url: str, **_kwargs: object) -> None:
        # Ходить дальше заготовленных страниц сборщик не должен —
        # если это случится, пусть упадёт IndexError, а не тихо повторит HTML.
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
        # Достаточно постоянного числа: цикл прокрутки в
        # _wait_for_cards_and_scroll останавливается, как только count
        # перестаёт расти между итерациями.
        soup = BeautifulSoup(self.html, "html.parser")
        return len(soup.select("[data-marker='item']"))


class CollectListingItemsDedupTests(unittest.TestCase):
    def test_repeated_local_items_deduped_and_pagination_stopped(self) -> None:
        """Три страницы с одними и теми же 3 местными объявлениями:
        собраны 3 уникальных, сборщик останавливается на второй странице
        (третья страница вообще не запрашивается)."""
        page1 = _page_html([
            _card("1", CITY.slug, "Товар 1"),
            _card("2", CITY.slug, "Товар 2"),
            _card("3", CITY.slug, "Товар 3"),
        ])
        page2 = _page_html([
            _card("1", CITY.slug, "Товар 1"),
            _card("2", CITY.slug, "Товар 2"),
            _card("3", CITY.slug, "Товар 3"),
        ])
        page3 = _page_html([
            _card("1", CITY.slug, "Товар 1"),
            _card("2", CITY.slug, "Товар 2"),
            _card("3", CITY.slug, "Товар 3"),
        ])
        fake_page = _FakePage([page1, page2, page3])

        with mock.patch.object(avito_parser.asyncio, "sleep", new=mock.AsyncMock()):
            collected = asyncio.run(
                avito_parser._collect_listing_items(
                    fake_page, CITY, "тест", max_items=40, filters=None,
                )
            )

        self.assertEqual(len(collected), 3)
        self.assertEqual({item["item_id"] for item in collected}, {"1", "2", "3"})
        # Третья страница (с точными повторами) не должна была запрашиваться:
        # предохранитель сработал уже на второй.
        self.assertEqual(fake_page.goto_calls, 2)

    def test_new_local_items_on_second_page_are_collected(self) -> None:
        """Если на второй странице среди повторов есть НОВОЕ местное
        объявление — оно собирается, пагинация продолжается дальше."""
        page1 = _page_html([
            _card("1", CITY.slug, "Товар 1"),
            _card("2", CITY.slug, "Товар 2"),
        ])
        page2 = _page_html([
            _card("1", CITY.slug, "Товар 1"),  # повтор
            _card("3", CITY.slug, "Товар 3"),  # новое
        ])
        page3 = _page_html([
            _card("1", CITY.slug, "Товар 1"),  # только повторы — стоп
        ])
        fake_page = _FakePage([page1, page2, page3])

        with mock.patch.object(avito_parser.asyncio, "sleep", new=mock.AsyncMock()):
            collected = asyncio.run(
                avito_parser._collect_listing_items(
                    fake_page, CITY, "тест", max_items=40, filters=None,
                )
            )

        self.assertEqual({item["item_id"] for item in collected}, {"1", "2", "3"})
        self.assertEqual(fake_page.goto_calls, 3)


class ExtractItemsFromHtmlDescriptionTests(unittest.TestCase):
    def test_description_read_from_meta_itemprop(self) -> None:
        """Описание карточки берётся из meta itemprop="description" (content),
        а не остаётся пустой строкой, как раньше в CSS-резерве."""
        html = _page_html([
            _card("1", CITY.slug, "Товар с описанием", description="Продаю на запчасти"),
        ])

        items = avito_parser._extract_items_from_html(html)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["description"], "Продаю на запчасти")

    def test_missing_description_meta_falls_back_to_empty_string(self) -> None:
        """Если meta нет вовсе — description остаётся "" (как раньше), а не падает."""
        html = """<html><body>
          <div data-marker="item" data-item-id="1">
            <a data-marker="item-title" href="/sankt-peterburg/1">Без описания</a>
            <span data-marker="item-price-value">1 000 &#8381;</span>
          </div>
        </body></html>"""

        items = avito_parser._extract_items_from_html(html)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["description"], "")

    def test_city_of_delivery_card_is_not_taken_as_description(self) -> None:
        """У карточки из чужого города («Только доставка») город в item-location
        размечен тем же стилем, что и описание, и стоит раньше него — описанием
        должен стать текст объявления, а не «Москва» (живой прогон 28.09.2026)."""
        style = 'style="--module-max-lines-size:1"'
        html = f"""<html><body>
          <div data-marker="item" data-item-id="7">
            <a data-marker="item-title" href="/moskva/noutbuki/7">Ноутбук</a>
            <span data-marker="item-price-value">50 000 &#8381;</span>
            <div data-marker="item-location"><p {style}>Москва</p></div>
            <p {style}>Продаю ноутбук, видеокарта RTX 4050</p>
            <a href="/brands/1"><p {style}>Иван</p></a>
          </div>
        </body></html>"""

        items = avito_parser._extract_items_from_html(html)

        self.assertEqual(items[0]["description"], "Продаю ноутбук, видеокарта RTX 4050")


if __name__ == "__main__":
    unittest.main()

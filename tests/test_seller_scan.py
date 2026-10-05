"""
Тесты чистых функций разбора продавца (backend/seller_scan.py) без живого Авито.

Разметка страницы профиля подтверждена живой разведкой 23.09.2026 (см.
docstring seller_scan.py и debug/seller_profile_map_20260923T085405Z.txt).
Фикстуры карточек здесь синтетические, но повторяют реальную разметку:
data-marker='item_list_with_filters/item(N)', N с нуля.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))

import seller_scan  # noqa: E402


_next_index = iter(range(10_000))


def card(item_href: str, index: int | None = None) -> str:
    """
    Карточка профиля продавца со ссылкой на объявление внутри.

    Разметка — data-marker='item_list_with_filters/item(N)' (N с нуля),
    ПОДТВЕРЖДЕНО живой разведкой 23.09.2026 (SELLER_PROFILE_CARD в
    avito_selectors.py). index по умолчанию берётся из общего счётчика —
    каждый вызов card() в тесте получает свою уникальную карточку ленты, как
    на живой странице.
    """
    if index is None:
        index = next(_next_index)
    return f"""<div data-marker="item_list_with_filters/item({index})">
        <a data-marker="item-title" href="{item_href}">Заголовок объявления</a>
    </div>"""


class ExtractItemUrlsTests(unittest.TestCase):
    def test_order_and_dedup_by_id_within_cards(self) -> None:
        html = (
            card("/moskva/telefony/iphone_15_pro_1234567")
            + card("/moskva/telefony/iphone_15_pro_max_9999999?context=abc")
            # Повтор того же ID другим текстом слага в ДРУГОЙ карточке ленты —
            # должен схлопнуться в один (Авито сам повторяет объявления в
            # ленте профиля, см. debug/seller_profile_map_20260923T085405Z.txt).
            + card("/moskva/telefony/iphone_15_pro_povtor_1234567")
        )
        urls = seller_scan._extract_item_urls_from_html(html)
        self.assertEqual(urls, [
            "https://www.avito.ru/moskva/telefony/iphone_15_pro_1234567",
            "https://www.avito.ru/moskva/telefony/iphone_15_pro_max_9999999",
        ])

    def test_links_outside_cards_ignored_when_cards_present(self) -> None:
        html = (
            "<a href='/moskva/telefony/chuzhoe_7777777'>Похожее объявление</a>"
            + card("/moskva/telefony/svoe_1234567")
        )
        urls = seller_scan._extract_item_urls_from_html(html)
        self.assertEqual(urls, ["https://www.avito.ru/moskva/telefony/svoe_1234567"])

    def test_falls_back_to_whole_page_without_cards(self) -> None:
        html = "<a href='/moskva/telefony/bez_kartochek_1234567'>Объявление</a>"
        urls = seller_scan._extract_item_urls_from_html(html)
        self.assertEqual(urls, ["https://www.avito.ru/moskva/telefony/bez_kartochek_1234567"])

    def test_short_ids_and_non_avito_hosts_rejected(self) -> None:
        html = (
            card("/moskva/telefony/slishkom_korotkij_id_123")  # < 6 цифр
            + card("https://evil.example/moskva/telefony/chужое_1234567")
            + card("/brands/some-seller")  # профиль, не объявление
        )
        self.assertEqual(seller_scan._extract_item_urls_from_html(html), [])


class FoundTotalTests(unittest.TestCase):
    def test_parses_found_total_with_thin_space(self) -> None:
        html = "<div>Найдено 170 объявлений</div>"
        self.assertEqual(seller_scan._extract_found_total(html), 170)

    def test_parses_thousands_with_nbsp(self) -> None:
        html = "<div>Найдено 1 234 объявления</div>"
        self.assertEqual(seller_scan._extract_found_total(html), 1234)

    def test_missing_phrase_returns_none(self) -> None:
        self.assertIsNone(seller_scan._extract_found_total("<div>Ничего не найдено</div>"))

    def test_parses_thousands_with_narrow_nbsp(self) -> None:
        # Узкий неразрывный пробел (U+202F) — второй разделитель тысяч,
        # встреченный на живой странице 23.09.2026, помимо обычного \xa0.
        html = "<div>Найдено 1 234 объявления</div>"
        self.assertEqual(seller_scan._extract_found_total(html), 1234)


class CountProfileCardsTests(unittest.TestCase):
    def test_counts_all_cards_including_id_repeats(self) -> None:
        # Авито сам повторяет часть объявлений в разных карточках ленты
        # (ПОДТВЕРЖДЕНО живой разведкой 23.09.2026: 170 карточек, 156
        # уникальных ID) — счётчик карточек считает DOM-узлы, не уникальные ID.
        html = (
            card("/moskva/telefony/povtor_1234567", index=1)
            + card("/moskva/telefony/drugoe_2234567", index=2)
            + card("/moskva/telefony/povtor_snova_1234567", index=135)  # тот же ID, другая карточка
        )
        self.assertEqual(seller_scan._count_profile_cards(html), 3)
        # А уникальных ID (как их видит _extract_item_urls_from_html) — два:
        # разница 3 - 2 = 1 повтор, именно так collect_item_urls считает
        # duplicates_skipped.
        self.assertEqual(len(seller_scan._extract_item_urls_from_html(html)), 2)

    def test_no_cards_returns_zero(self) -> None:
        self.assertEqual(seller_scan._count_profile_cards("<div>пусто</div>"), 0)


class SortItemsByViewsTests(unittest.TestCase):
    def test_descending_with_none_pushed_to_end_preserving_relative_order(self) -> None:
        items = [
            {"url": "a", "views_total": 10},
            {"url": "b", "views_total": None},
            {"url": "c", "views_total": 50},
            {"url": "d", "views_total": None},
            {"url": "e", "views_total": 20},
        ]
        result = seller_scan.sort_items_by_views(items, "views_total")
        self.assertEqual([item["url"] for item in result], ["c", "e", "a", "b", "d"])

    def test_sorts_by_requested_key_independently(self) -> None:
        items = [
            {"url": "a", "views_total": 5, "views_today": 1},
            {"url": "b", "views_total": 1, "views_today": 9},
        ]
        self.assertEqual(
            [item["url"] for item in seller_scan.sort_items_by_views(items, "views_today")],
            ["b", "a"],
        )


class ValidateProfileUrlTests(unittest.TestCase):
    def test_expands_bare_brands_id(self) -> None:
        self.assertEqual(
            seller_scan.validate_profile_url("https://www.avito.ru/brands/abc123"),
            "https://www.avito.ru/brands/abc123/items/all",
        )

    def test_keeps_full_brands_path_and_query(self) -> None:
        url = "https://www.avito.ru/brands/abc123/items/all/bytovaya_elektronika?sellerId=x"
        self.assertEqual(seller_scan.validate_profile_url(url), url)

    def test_accepts_user_profile_path(self) -> None:
        url = "https://www.avito.ru/user/c6aaa5dfd61ea32ecba46f28bc6a9e7e/profile/all"
        self.assertEqual(seller_scan.validate_profile_url(url), url)

    def test_accepts_host_without_www(self) -> None:
        self.assertEqual(
            seller_scan.validate_profile_url("https://avito.ru/brands/abc123"),
            "https://www.avito.ru/brands/abc123/items/all",
        )

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            seller_scan.validate_profile_url("")

    def test_rejects_foreign_host(self) -> None:
        with self.assertRaises(ValueError):
            seller_scan.validate_profile_url("https://evil.example/brands/abc123")

    def test_rejects_unrelated_avito_path(self) -> None:
        with self.assertRaises(ValueError):
            seller_scan.validate_profile_url("https://www.avito.ru/moskva/telefony/iphone_1234567")


class ShowAllHrefTests(unittest.TestCase):
    """«Показать все» на витрине продавца — разметка из дампов
    debug/seller_scan_short_20260927T181806Z (<a>) и ..._20260923T111159Z (<button>)."""

    def test_link_gives_absolute_url(self) -> None:
        html = (
            "<a data-marker='item_list_with_filters/show_all_button' "
            "href='/brands/i223647454/items?s=profile_search_show_all'>"
            "<span><span>Показать все</span></span></a>"
        )
        self.assertEqual(
            seller_scan._show_all_href(html),
            "https://www.avito.ru/brands/i223647454/items?s=profile_search_show_all",
        )

    def test_button_without_href_is_none(self) -> None:
        html = (
            "<button type='button' data-marker='item_list_with_filters/show_all_button'>"
            "Показать все</button>"
        )
        self.assertIsNone(seller_scan._show_all_href(html))

    def test_no_button_is_none(self) -> None:
        self.assertIsNone(seller_scan._show_all_href(card("/moskva/telefony/x_1234567")))

    def test_foreign_label_or_host_is_none(self) -> None:
        marker = "data-marker='item_list_with_filters/show_all_button'"
        self.assertIsNone(seller_scan._show_all_href(f"<a {marker} href='/brands/x/items'>Подписаться</a>"))
        self.assertIsNone(seller_scan._show_all_href(f"<a {marker} href='https://evil.example/x'>Показать все</a>"))

    def test_reviews_button_not_confused(self) -> None:
        html = "<button data-marker='rating-list/moreReviewsButton'>Показать все отзывы</button>"
        self.assertIsNone(seller_scan._show_all_href(html))


if __name__ == "__main__":
    unittest.main()

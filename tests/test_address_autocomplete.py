"""Регрессии выбора адреса из географических подсказок Авито."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import publisher  # noqa: E402


class _Option:
    def __init__(self, text: str) -> None:
        self.text = text
        self.clicked = False

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self) -> str:
        return self.text

    async def click(self, *, timeout: int) -> None:
        if timeout <= 0:
            raise AssertionError("таймаут клика должен быть положительным")
        self.clicked = True


class _Options:
    def __init__(self, options: list[_Option]) -> None:
        self.options = options

    async def count(self) -> int:
        return len(self.options)

    def nth(self, index: int) -> _Option:
        return self.options[index]


class _ChangingSuggestPage:
    """Сначала отдаёт старую подсказку, затем результаты нового ввода."""

    def __init__(self) -> None:
        self.stale = _Option("Республика Дагестан, Махачкала, пл. Ленина, 3")
        self.current = _Option("Невский проспект, 52\nСанкт-Петербург")
        self.locator_calls = 0

    async def wait_for_selector(self, *_args: object, **_kwargs: object) -> None:
        return None

    def locator(self, _selector: str) -> _Options:
        self.locator_calls += 1
        if self.locator_calls == 1:
            return _Options([self.stale])
        return _Options([self.current])


class _IndexedSuggestPage:
    """Живой DOM: город и первый дом делят option(0), следующий дом — option(1)."""

    def __init__(self) -> None:
        self.city = _Option("Мурманск")
        self.house_4 = _Option("улица Капитана Маклакова, 4\nМурманск")
        self.house_46 = _Option("улица Капитана Маклакова, 46\nМурманск")

    async def wait_for_selector(self, *_args: object, **_kwargs: object) -> None:
        return None

    def locator(self, selector: str) -> _Options:
        if "custom-option(0)" in selector:
            return _Options([self.city, self.house_4])
        return _Options([self.city, self.house_4, self.house_46])


class _GeoInput:
    def __init__(self, *, fail_first_click: bool = False) -> None:
        self.fail_first_click = fail_first_click
        self.clicks = 0
        self.no_wait_after_values: list[bool] = []

    async def click(self, *, timeout: int, no_wait_after: bool = False) -> None:
        if timeout <= 0:
            raise AssertionError("таймаут клика должен быть положительным")
        self.clicks += 1
        self.no_wait_after_values.append(no_wait_after)
        if self.fail_first_click and self.clicks == 1:
            raise TimeoutError("waiting for scheduled navigations to finish")


class _GeoLocator:
    def __init__(self, geo_input: _GeoInput) -> None:
        self.first = geo_input


class _Keyboard:
    def __init__(self) -> None:
        self.typed = ""

    async def type(self, text: str, *, delay: int) -> None:
        if delay <= 0:
            raise AssertionError("ввод адреса должен оставаться посимвольным")
        self.typed = text


class _AddressPage:
    def __init__(self, *, fail_first_click: bool = False) -> None:
        self.keyboard = _Keyboard()
        self.geo_input = _GeoInput(fail_first_click=fail_first_click)

    def locator(self, _selector: str) -> _GeoLocator:
        return _GeoLocator(self.geo_input)


class AddressAutocompleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_requested_house_is_selected_after_same_street_other_house(self) -> None:
        page = _IndexedSuggestPage()

        selected = await publisher._click_suggest_option(
            page,
            publisher.psel.GEO_SUGGEST_OPTION,
            prefer_text="Мурманск улица Маклакова 46",
            timeout_ms=1,
        )

        self.assertEqual(selected, "улица Капитана Маклакова, 46\nМурманск")
        self.assertFalse(page.city.clicked)
        self.assertFalse(page.house_4.clicked)
        self.assertTrue(page.house_46.clicked)

    async def test_stale_other_city_is_not_clicked_before_current_suggestion(self) -> None:
        page = _ChangingSuggestPage()

        with mock.patch.object(
            publisher.asyncio,
            "sleep",
            new=mock.AsyncMock(return_value=None),
        ):
            selected = await publisher._click_suggest_option(
                page,
                (
                    "[data-marker='geo/field/suggest'] "
                    "button[data-marker$='/custom-option(0)']"
                ),
                prefer_text="Санкт-Петербург",
                timeout_ms=1_000,
            )

        self.assertEqual(selected, "Невский проспект, 52\nСанкт-Петербург")
        self.assertFalse(page.stale.clicked)
        self.assertTrue(page.current.clicked)

    async def test_official_street_qualifier_matches_without_clicking_other_city(self) -> None:
        page = _ChangingSuggestPage()
        page.stale = _Option("улица Капитана Маклакова, 4\nМагадан")
        page.current = _Option("улица Капитана Маклакова, 4\nМурманск")

        with mock.patch.object(
            publisher.asyncio,
            "sleep",
            new=mock.AsyncMock(return_value=None),
        ):
            selected = await publisher._click_suggest_option(
                page,
                publisher.psel.GEO_SUGGEST_OPTION,
                prefer_text="Мурманск улица Маклакова",
                timeout_ms=1_000,
            )

        self.assertEqual(selected, "улица Капитана Маклакова, 4\nМурманск")
        self.assertFalse(page.stale.clicked)
        self.assertTrue(page.current.clicked)

    async def test_street_only_suggestion_is_allowed_before_strict_post_click_check(self) -> None:
        page = _ChangingSuggestPage()
        page.stale = _Option("улица Артёма Избышева, 23\nОмск")
        page.current = _Option("улица Избышева\nОмск")

        with mock.patch.object(
            publisher.asyncio,
            "sleep",
            new=mock.AsyncMock(return_value=None),
        ):
            selected = await publisher._click_suggest_option(
                page,
                publisher.psel.GEO_SUGGEST_OPTION,
                prefer_text="Омск, улица Избышева, 2",
                timeout_ms=1_000,
            )

        self.assertEqual(selected, "улица Избышева\nОмск")
        self.assertFalse(page.stale.clicked)
        self.assertTrue(page.current.clicked)

    async def test_address_step_scopes_options_and_requires_entered_city(self) -> None:
        page = _AddressPage()
        click_suggest = mock.AsyncMock(
            return_value="Невский проспект, 52\nСанкт-Петербург"
        )

        with (
            mock.patch.object(
                publisher,
                "_keyboard_clear",
                new=mock.AsyncMock(return_value=None),
            ),
            mock.patch.object(
                publisher,
                "_selector_visible",
                new=mock.AsyncMock(return_value=True),
            ),
            mock.patch.object(
                publisher,
                "_click_suggest_option",
                new=click_suggest,
            ),
            mock.patch.object(
                publisher,
                "_wait_until",
                new=mock.AsyncMock(return_value=True),
            ),
            mock.patch.object(
                publisher.asyncio,
                "sleep",
                new=mock.AsyncMock(return_value=None),
            ),
        ):
            await publisher._step_fill_address(
                page,
                "Санкт-Петербург, Невский проспект, 52",
            )

        self.assertEqual(
            page.keyboard.typed,
            "Санкт-Петербург, Невский проспект, 52",
        )
        click_suggest.assert_awaited_once_with(
            page,
            publisher.psel.GEO_SUGGEST_OPTION,
            prefer_text="Санкт-Петербург, Невский проспект, 52",
            timeout_ms=5_000,
        )

    def test_address_match_accepts_yo_and_house_suffix_but_rejects_other_street(self) -> None:
        requested = "Санкт-Петербург, Проспект Королева 26"

        self.assertTrue(
            publisher._address_matches_requested(
                requested,
                "Санкт-Петербург, проспект Королёва, 26к1",
            )
        )
        self.assertFalse(
            publisher._address_matches_requested(
                requested,
                "Санкт-Петербург, проспект Стачек",
            )
        )
        self.assertTrue(
            publisher._address_matches_requested(
                "Мурманск, улица Маклакова,4",
                "Мурманск, улица Капитана Маклакова, 4",
            )
        )
        self.assertFalse(
            publisher._address_matches_requested(
                "Мурманск, улица Маклакова,46",
                "Мурманск, улица Капитана Маклакова, 4",
            )
        )
        self.assertFalse(
            publisher._address_matches_requested(
                "Омск, улица Избышева, 2",
                (
                    "микрорайон Привокзальный, Омск, улица Артёма Избышева, "
                    "23, подъезд 2"
                ),
            )
        )

    async def test_navigation_race_on_geo_click_is_retried_without_waiting(self) -> None:
        page = _AddressPage(fail_first_click=True)

        with (
            mock.patch.object(
                publisher,
                "_keyboard_clear",
                new=mock.AsyncMock(return_value=None),
            ),
            mock.patch.object(
                publisher,
                "_selector_visible",
                new=mock.AsyncMock(return_value=True),
            ),
            mock.patch.object(
                publisher,
                "_click_suggest_option",
                new=mock.AsyncMock(return_value="Невский проспект, 52\nСанкт-Петербург"),
            ),
            mock.patch.object(
                publisher,
                "_wait_until",
                new=mock.AsyncMock(return_value=True),
            ),
            mock.patch.object(
                publisher.asyncio,
                "sleep",
                new=mock.AsyncMock(return_value=None),
            ),
        ):
            await publisher._step_fill_address(
                page,
                "Санкт-Петербург, Невский проспект, 52",
            )

        self.assertEqual(page.geo_input.clicks, 2)
        self.assertEqual(page.geo_input.no_wait_after_values, [True, True])


if __name__ == "__main__":
    unittest.main()

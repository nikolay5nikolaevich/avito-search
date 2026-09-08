"""Регрессии автокомплита бренда на форме Авито."""

import os
import sys
import unittest
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import category_profiles  # noqa: E402
import publisher  # noqa: E402


class _VisibleBrandOption:
    def __init__(self, text: str = "Stussy") -> None:
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


class _VisibleBrandOptions:
    def __init__(self, option: _VisibleBrandOption | None) -> None:
        self.option = option

    async def count(self) -> int:
        return int(self.option is not None)

    def nth(self, index: int) -> _VisibleBrandOption:
        if index != 0 or self.option is None:
            raise AssertionError(f"неожиданный индекс пункта: {index}")
        return self.option


class _BrandPage:
    def __init__(self, option_text: str = "Stussy") -> None:
        self.option = _VisibleBrandOption(option_text)
        self.requested_text: tuple[str, bool] | None = None

    def get_by_text(self, text: str, *, exact: bool) -> _VisibleBrandOptions:
        self.requested_text = (text, exact)
        matches = self.option.text == text if exact else text.casefold() in self.option.text.casefold()
        return _VisibleBrandOptions(self.option if matches else None)


class BrandCapitalizationTest(unittest.TestCase):
    def test_lowercase_brand_words_start_with_capitals(self) -> None:
        self.assertEqual(publisher._brand_text_for_avito("stussy"), "Stussy")
        self.assertEqual(publisher._brand_text_for_avito("hugo boss"), "Hugo Boss")
        self.assertEqual(publisher._brand_text_for_avito("the north face"), "The North Face")

    def test_existing_brand_spelling_is_not_lowercased(self) -> None:
        self.assertEqual(publisher._brand_text_for_avito("H&M"), "H&M")
        self.assertEqual(publisher._brand_text_for_avito("Levi's"), "Levi's")


class BrandAutocompleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_visible_exact_brand_match_ignores_case(self) -> None:
        page = _BrandPage("Tommy Hilfiger")

        clicked = await publisher._click_visible_text(
            page,
            "TOMMY HILFIGER",
            timeout_s=0.01,
        )

        self.assertTrue(clicked)
        self.assertTrue(page.option.clicked)

    async def test_brand_uses_visible_exact_suggestion_instead_of_old_marker(self) -> None:
        page = _BrandPage()
        type_mock = mock.AsyncMock(return_value=None)
        old_marker_mock = mock.AsyncMock(
            side_effect=AssertionError("старый data-marker бренда использовать нельзя")
        )

        with (
            mock.patch.object(publisher, "_clear_and_type", new=type_mock),
            mock.patch.object(
                publisher,
                "_click_suggest_option",
                new=old_marker_mock,
            ),
        ):
            selected = await publisher._select_brand(
                page,
                category_profiles.TSHIRTS,
                "stussy",
            )

        self.assertEqual(selected, "Stussy")
        type_mock.assert_awaited_once_with(
            page,
            category_profiles.TSHIRTS.brand_input,
            "Stussy",
        )
        self.assertTrue(page.option.clicked)
        old_marker_mock.assert_not_awaited()

    async def test_missing_nonempty_brand_stops_without_fallback(self) -> None:
        type_text = mock.AsyncMock(return_value=None)
        click_text = mock.AsyncMock(return_value=False)

        with (
            mock.patch.object(
                publisher,
                "_clear_and_type",
                new=type_text,
            ),
            mock.patch.object(
                publisher,
                "_click_visible_text",
                new=click_text,
            ),
            self.assertRaisesRegex(publisher.StepError, "Stussy"),
        ):
            await publisher._select_brand(
                object(),
                category_profiles.TSHIRTS,
                "stussy",
            )

        type_text.assert_awaited_once_with(
            mock.ANY,
            category_profiles.TSHIRTS.brand_input,
            "Stussy",
        )
        click_text.assert_awaited_once_with(mock.ANY, "Stussy", timeout_s=6.0)

    async def test_missing_no_brand_option_stops_safely(self) -> None:
        with (
            mock.patch.object(
                publisher,
                "_clear_and_type",
                new=mock.AsyncMock(return_value=None),
            ),
            mock.patch.object(
                publisher,
                "_click_visible_text",
                new=mock.AsyncMock(return_value=False),
            ),
            self.assertRaisesRegex(
                publisher.StepError,
                "Без бренда",
            ),
        ):
            await publisher._select_brand(
                object(),
                category_profiles.TSHIRTS,
                "",
            )


if __name__ == "__main__":
    unittest.main()

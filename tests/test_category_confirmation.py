"""Регрессия промежуточного экрана подтверждения категории Авито."""

import asyncio
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


CONTINUE_SELECTOR = "button[data-marker='item-edit/button-next']"
TITLE_SELECTOR = "input[name='title']"


class _Locator:
    def __init__(self, page: "_CategoryConfirmationPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_Locator":
        return self

    async def count(self) -> int:
        return 1

    async def input_value(self, timeout: int | None = None) -> str:
        del timeout
        values = {
            "category_id": "27",
            "params[175]": "748",
            "params[176]": "756",
        }
        for name, value in values.items():
            if f"name='{name}'" in self.selector:
                return value
        return ""

    async def is_visible(self) -> bool:
        if self.selector == TITLE_SELECTOR:
            return self.page.title_visible
        if self.selector == CONTINUE_SELECTOR:
            return True
        return False

    async def inner_text(self, timeout: int | None = None) -> str:
        del timeout
        if self.selector == CONTINUE_SELECTOR:
            return "Продолжить\ntiming"
        return ""

    async def click(self, timeout: int | None = None) -> None:
        del timeout
        if self.selector == CONTINUE_SELECTOR:
            self.page.continue_clicks += 1
            self.page.title_visible = True


class _CategoryConfirmationPage:
    def __init__(self, *, title_visible: bool = False) -> None:
        self.title_visible = title_visible
        self.continue_clicks = 0

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    async def wait_for_selector(
        self, selector: str, *, timeout: int, state: str
    ) -> _Locator:
        del timeout, state
        locator = self.locator(selector)
        if not await locator.is_visible():
            raise TimeoutError(selector)
        return locator


class _DelayedHiddenLocator(_Locator):
    async def input_value(self, timeout: int | None = None) -> str:
        del timeout
        values = {
            "category_id": "27",
            "params[175]": "748",
            "params[176]": "756",
        }
        for name, value in values.items():
            if f"name='{name}'" in self.selector:
                reads = self.page.hidden_reads.get(name, 0) + 1
                self.page.hidden_reads[name] = reads
                return "" if reads == 1 else value
        return ""


class _DelayedHiddenPage(_CategoryConfirmationPage):
    def __init__(self) -> None:
        super().__init__()
        self.hidden_reads: dict[str, int] = {}

    def locator(self, selector: str) -> _Locator:
        return _DelayedHiddenLocator(self, selector)


class CategoryConfirmationTest(unittest.TestCase):
    def test_verified_category_confirmation_opens_edit_form(self) -> None:
        """После проверенной категории промежуточный экран открывает форму."""
        page = _CategoryConfirmationPage()

        asyncio.run(
            publisher._step_check_category(page, category_profiles.TSHIRTS)
        )

        self.assertTrue(page.title_visible)
        self.assertEqual(page.continue_clicks, 1)

    def test_visible_edit_form_never_clicks_continue(self) -> None:
        """На уже открытой форме опасная кнопка «Продолжить» не нажимается."""
        page = _CategoryConfirmationPage(title_visible=True)

        asyncio.run(
            publisher._step_check_category(page, category_profiles.TSHIRTS)
        )

        self.assertTrue(page.title_visible)
        self.assertEqual(page.continue_clicks, 0)

    def test_known_category_waits_for_delayed_hidden_ids(self) -> None:
        page = _DelayedHiddenPage()

        with mock.patch.object(
            publisher.asyncio,
            "sleep",
            new=mock.AsyncMock(return_value=None),
        ):
            asyncio.run(
                publisher._step_check_category(page, category_profiles.TSHIRTS)
            )

        self.assertTrue(page.title_visible)
        self.assertEqual(page.continue_clicks, 1)
        self.assertEqual(page.hidden_reads, {
            "category_id": 2,
            "params[175]": 2,
            "params[176]": 2,
        })


if __name__ == "__main__":
    unittest.main()

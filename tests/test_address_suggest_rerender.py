"""Регрессии перерисовки гео-подсказок на изолированном локальном DOM."""

from __future__ import annotations

import asyncio
import html
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import publish_test_isolation  # noqa: E402, F401
import publisher  # noqa: E402
from playwright.async_api import (  # noqa: E402
    Locator,
    Page,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


REQUESTED = "Москва, Свободный проспект"
TARGET = "Свободный проспект Москва"
FOREIGN = "Свободный проспект Калужская область"
MOSCOW_REGION = "Свободный проспект Московская область"


def _options_dom(texts: list[str]) -> str:
    buttons = "".join(
        f'<button data-marker="geo/field/suggest/custom-option({index})">'
        f'{html.escape(text)}</button>'
        for index, text in enumerate(texts)
    )
    return f'<div data-marker="geo/field/suggest">{buttons}</div>'


class AddressSuggestRerenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        chromium = self.playwright.chromium
        options = {"headless": True}
        if not Path(chromium.executable_path).is_file():
            options["channel"] = "chrome"
        self.browser = await chromium.launch(**options)
        self.addAsyncCleanup(self.browser.close)
        context = await self.browser.new_context(service_workers="block")

        async def abort_network(route: Route) -> None:
            await route.abort()

        await context.route("**/*", abort_network)
        self.page: Page = await context.new_page()
        await self.page.set_content(
            _options_dom(["Тверь", "Омск", "Пермь", "Калуга", TARGET])
        )
        await self.page.evaluate(
            """() => {
                window.clicked = [];
                document.addEventListener('click', event => {
                    const button = event.target.closest('button');
                    if (button) window.clicked.push(button.textContent);
                });
            }"""
        )

    async def _replace(self, texts: list[str]) -> None:
        # Сохраняем document и обработчик кликов, заменяя только список.
        await self.page.locator("[data-marker='geo/field/suggest']").evaluate(
            "(container, markup) => {container.outerHTML = markup;}",
            _options_dom(texts),
        )

    async def _select(self, timeout_ms: int = 350) -> str | None:
        return await publisher._click_suggest_option(
            self.page,
            publisher.psel.GEO_SUGGEST_OPTION,
            prefer_text=REQUESTED,
            timeout_ms=timeout_ms,
        )

    async def test_target_moves_from_fifth_to_first_before_click(self) -> None:
        original_click = Locator.click
        rerendered = False

        async def rerender_then_click(locator: Locator, **kwargs: object) -> None:
            nonlocal rerendered
            if not rerendered:
                rerendered = True
                await self._replace([TARGET])
            await original_click(locator, **kwargs)

        with mock.patch.object(Locator, "click", new=rerender_then_click):
            selected = await self._select()

        self.assertTrue(rerendered)
        self.assertEqual(selected, TARGET)
        self.assertEqual(await self.page.evaluate("window.clicked"), [TARGET])

    async def test_same_index_replaced_by_other_region_is_never_clicked(self) -> None:
        original_click = Locator.click
        rerendered = False

        async def rerender_then_click(locator: Locator, **kwargs: object) -> None:
            nonlocal rerendered
            if not rerendered:
                rerendered = True
                await self._replace(["Тверь", "Омск", "Пермь", "Калуга", FOREIGN])
            await original_click(locator, **kwargs)

        with mock.patch.object(Locator, "click", new=rerender_then_click):
            selected = await self._select()

        self.assertIsNone(selected)
        self.assertEqual(await self.page.evaluate("window.clicked"), [])

    async def test_click_timeout_reloads_candidates_and_returns_current_text(self) -> None:
        original_click = Locator.click
        attempts = 0
        current = "Москва, Свободный проспект"

        async def timeout_then_click(locator: Locator, **kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await self._replace([FOREIGN, current])
                raise PlaywrightTimeoutError("Список заменился перед кликом")
            await original_click(locator, **kwargs)

        with mock.patch.object(Locator, "click", new=timeout_then_click):
            selected = await self._select(timeout_ms=900)

        self.assertGreaterEqual(attempts, 2)
        self.assertEqual(selected, current)
        self.assertEqual(await self.page.evaluate("window.clicked"), [current])

    async def test_repeated_click_timeouts_stop_at_overall_deadline(self) -> None:
        attempts = 0

        async def timeout_click(_locator: Locator, **kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 180)
            await asyncio.sleep(0.03)
            raise PlaywrightTimeoutError("Подсказка исчезла")

        started = time.monotonic()
        with mock.patch.object(Locator, "click", new=timeout_click):
            selected = await self._select(timeout_ms=180)

        self.assertIsNone(selected)
        self.assertGreaterEqual(attempts, 1)
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(await self.page.evaluate("window.clicked"), [])

    async def test_moscow_accepts_same_street_in_moscow_region(self) -> None:
        # Финальное разрешение пользователя: Москва либо Московская область.
        await self._replace([MOSCOW_REGION])

        selected = await self._select()

        self.assertEqual(selected, MOSCOW_REGION)
        self.assertEqual(await self.page.evaluate("window.clicked"), [MOSCOW_REGION])
        self.assertTrue(
            publisher._address_matches_requested(REQUESTED, MOSCOW_REGION)
        )

    async def test_exact_moscow_option_has_priority_over_region(self) -> None:
        await self._replace([MOSCOW_REGION, TARGET])

        selected = await self._select()

        self.assertEqual(selected, TARGET)
        self.assertEqual(await self.page.evaluate("window.clicked"), [TARGET])

    async def test_region_does_not_accept_another_street_or_house(self) -> None:
        requested = "Москва, Свободный проспект, 4"
        wrong_street = "Московская область, Ленинский проспект, 4"
        wrong_house = "Московская область, Свободный проспект, 46"
        await self._replace([wrong_street, wrong_house])

        selected = await publisher._click_suggest_option(
            self.page,
            publisher.psel.GEO_SUGGEST_OPTION,
            prefer_text=requested,
            timeout_ms=80,
        )

        self.assertIsNone(selected)
        self.assertEqual(await self.page.evaluate("window.clicked"), [])
        self.assertFalse(publisher._address_matches_requested(requested, wrong_street))
        self.assertFalse(publisher._address_matches_requested(requested, wrong_house))
        self.assertFalse(publisher._address_matches_requested(REQUESTED, FOREIGN))
        self.assertTrue(
            publisher._address_matches_requested(
                requested, "Московская область, Свободный проспект, 4"
            )
        )
        self.assertFalse(
            publisher._address_matches_requested(
                "Калуга, Свободный проспект, 4",
                "Московская область, Свободный проспект, 4",
            )
        )


if __name__ == "__main__":
    unittest.main()

"""Денежная кнопка сохраняет смысл после перестановки узлов DOM."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import publish_test_isolation  # noqa: E402, F401
import publisher  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


class ViewPriceActionRerenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        chromium = self.playwright.chromium
        options = {"headless": True}
        if not Path(chromium.executable_path).is_file():
            options["channel"] = "chrome"
        self.browser = await chromium.launch(**options)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        await self.page.route("**/*", lambda route: route.fulfill(
            body="<html><body></body></html>", content_type="text/html",
        ))
        await self.page.goto("https://www.avito.ru/cpxpromo/1234567890")
        await self.page.set_content("""
            <div id="buttons">
              <button data-marker="action-buttons-secondary">←</button>
              <button data-marker="action-buttons-primary">Продолжить с минимальной ценой</button>
            </div>
            <script>
              window.clicked = [];
              document.addEventListener('click', event => {
                const button = event.target.closest('button');
                if (button) window.clicked.push(button.textContent);
              });
            </script>
        """)

    async def test_reordered_indices_keep_exact_financial_target(self) -> None:
        button = await publisher._find_view_price_action(self.page, timeout_s=0.5)
        self.assertIsNotNone(button)
        # Изначальный nth(1) после React-rerender указывает уже на стрелку.
        await self.page.evaluate("""() => {
            document.querySelector('#buttons').innerHTML = `
              <button data-marker="action-buttons-primary">Продолжить с минимальной ценой</button>
              <button data-marker="action-buttons-secondary">←</button>`;
        }""")
        await button.click(timeout=1000)
        self.assertEqual(
            await self.page.evaluate("window.clicked"),
            ["Продолжить с минимальной ценой"],
        )

    async def test_unknown_financial_suffix_is_not_selected_or_clicked(self) -> None:
        await self.page.locator('[data-marker="action-buttons-primary"]').evaluate(
            "button => button.textContent = 'Продолжить с минимальной ценой и услугами'",
        )
        button = await publisher._find_view_price_action(self.page, timeout_s=0.05)
        self.assertIsNone(button)
        self.assertEqual(await self.page.evaluate("window.clicked"), [])

    async def test_continue_rechecks_arrow_and_rejects_before_click(self) -> None:
        arrow = self.page.locator('[data-marker="action-buttons-secondary"]')
        with mock.patch.object(
            publisher, "_find_view_price_action", new=mock.AsyncMock(return_value=arrow),
        ):
            with self.assertRaises(publisher.StepError):
                await publisher._step_continue_view_price(self.page, "1234567890")
        self.assertEqual(await self.page.evaluate("window.clicked"), [])


if __name__ == "__main__":
    unittest.main()

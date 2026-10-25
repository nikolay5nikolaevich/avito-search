"""Проверка состояния телефона на локальной DOM-форме без сети и Авито."""

import os
import sys
import unittest
from unittest import mock

from playwright.async_api import async_playwright


BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import publish_test_isolation  # noqa: E402, F401
import category_profiles  # noqa: E402
import publisher  # noqa: E402


SHORT = "[data-marker='params[110395]/option(431241)']"
DETAILED = "[data-marker='params[121588]/2850684']"
TRADE = "[data-marker='params[2822]/option(20018)']"
REQUIRED_MARKERS = [
    "params[184666]/option(3338066)",
    "params[184667]/option(3375453)",
    "params[167055]/option(3270714)",
]


class PhoneRadioAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        await self.page.route("**/*", lambda route: route.abort())
        self.enterContext(mock.patch.object(
            publisher, "WAIT_SELECTOR_TIMEOUT_MS", 300,
        ))
        self.enterContext(mock.patch.object(
            publisher, "_select_fixed_manufacturer", mock.AsyncMock(return_value="Apple"),
        ))
        self.enterContext(mock.patch.object(
            publisher, "_select_cascade_combobox", mock.AsyncMock(),
        ))
        self.enterContext(mock.patch.object(publisher, "_pause", mock.AsyncMock()))
        self.draft = publisher.build_draft_data({
            "title": "iPhone 17 Pro 256 ГБ новый",
            "trade_type": "Продаю своё",
            "condition": "Новое",
            "model": "iPhone 17 Pro",
            "memory": "256 ГБ",
            "color": "Синий",
            "description": "1 физическая SIM + eSIM. Не активирован. Запечатанный.",
            "price": "120000",
            "locations_json": "[]",
            "view_price_max": "5",
        }, ["phone.jpg"], category="iphone")

    async def _form(
        self, *, detailed_visible: bool, broken_marker: str = "",
    ) -> None:
        display = "block" if detailed_visible else "none"
        required_radios = "".join(
            f'<label data-marker="{marker}" role="radio">'
            f'<input type="radio" name="required-{index}">Свойство {index}</label>'
            for index, marker in enumerate(REQUIRED_MARKERS)
        )
        await self.page.set_content(f"""
            <label data-marker="params[110395]/option(431241)" role="radio">
                <input type="radio" name="params[110395]" value="431241">Новое
            </label>
            <div style="display:{display}">
                <label data-marker="params[121588]/2850684" role="radio">
                    <input type="radio" name="params[121588]" value="2850684">
                    Новое. Телефоном не пользовались.
                </label>
            </div>
            {required_radios}
            <label data-marker="params[2822]/option(20018)" role="radio">
                <input type="radio" name="params[2822]" value="20018">Продаю своё
            </label>
        """)
        await self.page.evaluate("""brokenMarker => {
            window.radioClicks = [];
            document.addEventListener('click', event => {
                const label = event.target.closest('label[data-marker]');
                if (!label) return;
                if (window.radioClicks.at(-1) !== label.dataset.marker)
                    window.radioClicks.push(label.dataset.marker);
                if (label.dataset.marker === brokenMarker) event.preventDefault();
            });
        }""", broken_marker)

    async def _fill(self) -> str:
        return await publisher._step_fill_fields(
            self.page, self.draft, category_profiles.IPHONE,
        )

    async def test_hidden_detailed_condition_is_not_required_or_clicked(self) -> None:
        await self._form(detailed_visible=False)

        self.assertEqual(await self._fill(), "Apple")

        self.assertTrue(await self.page.locator(SHORT + " input").is_checked())
        self.assertFalse(await self.page.locator(DETAILED + " input").is_checked())
        self.assertTrue(await self.page.locator(TRADE + " input").is_checked())
        self.assertEqual(await self.page.evaluate("window.radioClicks"), [
            "params[110395]/option(431241)", *REQUIRED_MARKERS,
            "params[2822]/option(20018)",
        ])

    async def test_visible_detailed_condition_is_selected(self) -> None:
        await self._form(detailed_visible=True)

        await self._fill()

        for selector in (SHORT, DETAILED, TRADE):
            self.assertTrue(await self.page.locator(selector + " input").is_checked())
        self.assertCountEqual(await self.page.evaluate("window.radioClicks"), [
            "params[110395]/option(431241)", "params[121588]/2850684",
            *REQUIRED_MARKERS, "params[2822]/option(20018)",
        ])

    async def test_short_condition_reveals_and_selects_detailed_condition(self) -> None:
        await self._form(detailed_visible=False)
        await self.page.evaluate("""([shortSelector, detailedSelector]) => {
            document.querySelector(shortSelector).addEventListener('change', () => {
                document.querySelector(detailedSelector).parentElement.style.display = 'block';
            });
        }""", [SHORT, DETAILED])

        await self._fill()

        self.assertTrue(await self.page.locator(DETAILED).is_visible())
        for selector in (SHORT, DETAILED, TRADE):
            self.assertTrue(await self.page.locator(selector + " input").is_checked())
        self.assertEqual(await self.page.evaluate("window.radioClicks"), [
            "params[110395]/option(431241)", "params[121588]/2850684",
            *REQUIRED_MARKERS, "params[2822]/option(20018)",
        ])

    async def test_required_options_are_checked_before_trade(self) -> None:
        await self._form(detailed_visible=False)

        await self._fill()

        for marker in REQUIRED_MARKERS:
            self.assertTrue(await self.page.locator(
                f'[data-marker="{marker}"] input',
            ).is_checked())
        self.assertTrue(await self.page.locator(TRADE + " input").is_checked())
        self.assertEqual(await self.page.evaluate("window.radioClicks"), [
            "params[110395]/option(431241)", *REQUIRED_MARKERS,
            "params[2822]/option(20018)",
        ])

    async def test_unconfirmed_required_option_stops_before_trade(self) -> None:
        await self._form(
            detailed_visible=False, broken_marker=REQUIRED_MARKERS[1],
        )

        with self.assertRaises(publisher.StepError):
            await self._fill()

        self.assertTrue(await self.page.locator(
            f'[data-marker="{REQUIRED_MARKERS[0]}"] input',
        ).is_checked())
        for marker in REQUIRED_MARKERS[1:]:
            self.assertFalse(await self.page.locator(
                f'[data-marker="{marker}"] input',
            ).is_checked())
        self.assertFalse(await self.page.locator(TRADE + " input").is_checked())
        self.assertEqual(await self.page.evaluate("window.radioClicks"), [
            "params[110395]/option(431241)", *REQUIRED_MARKERS[:2],
        ])

    async def test_unconfirmed_short_condition_stops_before_other_radios(self) -> None:
        await self._form(
            detailed_visible=False, broken_marker="params[110395]/option(431241)",
        )

        with self.assertRaises(publisher.StepError):
            await self._fill()

        self.assertFalse(await self.page.locator(SHORT + " input").is_checked())
        self.assertFalse(await self.page.locator(DETAILED + " input").is_checked())
        self.assertFalse(await self.page.locator(TRADE + " input").is_checked())
        self.assertEqual(await self.page.evaluate("window.radioClicks"), [
            "params[110395]/option(431241)",
        ])

    async def test_unconfirmed_visible_detailed_condition_stops_before_trade(self) -> None:
        await self._form(
            detailed_visible=True, broken_marker="params[121588]/2850684",
        )

        with self.assertRaises(publisher.StepError):
            await self._fill()

        self.assertFalse(await self.page.locator(DETAILED + " input").is_checked())
        self.assertFalse(await self.page.locator(TRADE + " input").is_checked())


if __name__ == "__main__":
    unittest.main()

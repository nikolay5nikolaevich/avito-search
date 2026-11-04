"""Три SIM на локальной DOM-форме: без CDP, сети и публикации."""
import sys
import unittest
from pathlib import Path
from unittest import mock

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import publish_test_isolation  # noqa: F401, E402
import category_profiles
import publisher


SIM_OPTIONS = {"eSIM": (3338069, "Только eSIM"), "SIM + eSIM": (3338066, "SIM + eSIM"), "SIM + SIM": (3338067, "2 SIM")}


class ExplicitSimDomAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        bundled = Path(playwright.chromium.executable_path)
        options = {} if bundled.is_file() else {"channel": "chrome"}
        browser = await playwright.chromium.launch(headless=True, **options)
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        await self.page.route("**/*", lambda route: route.abort())
        self.enterContext(mock.patch.object(publisher, "WAIT_SELECTOR_TIMEOUT_MS", 3000))
        self.enterContext(mock.patch.object(publisher, "_select_fixed_manufacturer", mock.AsyncMock(return_value="Apple")))
        self.enterContext(mock.patch.object(publisher, "_select_cascade_combobox", mock.AsyncMock()))
        self.enterContext(mock.patch.object(publisher, "_pause", mock.AsyncMock()))

    async def form(self, wrong_label=False):
        options = [("params[110395]/option(431241)", "condition", 431241, "Новое"), ("params[184667]/option(3375453)", "activation", 3375453, "Неактивированный"), ("params[167055]/option(3270714)", "packaging", 3270714, "Запечатана"), ("params[2822]/option(20018)", "trade", 20018, "Продаю своё")]
        options += [(f"params[184666]/option({value})", "sim", value, "Незнакомая подпись" if wrong_label else label) for value, label in SIM_OPTIONS.values()]
        await self.page.set_content("".join(f'<label data-marker="{marker}" role="radio"><input type="radio" name="{name}" value="{value}">{label}</label>' for marker, name, value, label in options))

    def draft(self, sim):
        return publisher.build_draft_data(dict(title="iPhone 17 Pro 256 GB", trade_type="Продаю своё", condition="Новое", model="iPhone 17 Pro", memory="256 ГБ", color="Синий", sim_type=sim, description="Доставка по договорённости.", price="120000", locations_json="[]", view_price_max="5"), ["phone.jpg"], category="iphone")

    async def test_each_explicit_sim_selects_exactly_its_input(self):
        for sim, (value, _) in SIM_OPTIONS.items():
            with self.subTest(sim=sim):
                await self.form()
                self.assertEqual(await publisher._step_fill_phone_fields(self.page, self.draft(sim), category_profiles.IPHONE), "Apple")
                self.assertEqual(await self.page.locator('input[name="sim"]:checked').count(), 1)
                self.assertEqual(await self.page.locator('input[name="sim"]:checked').input_value(), str(value))
                for name in ["activation", "packaging", "trade"]:
                    self.assertTrue(await self.page.locator(f'input[name="{name}"]').is_checked())

    async def test_unrecognized_sim_label_stops_before_sim_and_trade(self):
        for sim in SIM_OPTIONS:
            with self.subTest(sim=sim):
                await self.form(wrong_label=True)
                with self.assertRaisesRegex(publisher.StepError, "Подпись варианта SIM"):
                    await publisher._step_fill_phone_fields(self.page, self.draft(sim), category_profiles.IPHONE)
                self.assertEqual(await self.page.locator('input[name="sim"]:checked').count(), 0)
                self.assertFalse(await self.page.locator('input[name="trade"]').is_checked())


if __name__ == "__main__":
    unittest.main()

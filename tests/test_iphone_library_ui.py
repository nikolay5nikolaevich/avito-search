"""Локальная браузерная приёмка iPhone; запуск публикации полностью заглушён."""

import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import uvicorn
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import publish_test_isolation  # noqa: E402, F401
import app as api  # noqa: E402
import iphone_library  # noqa: E402


class IphoneLibraryBrowserTest(unittest.TestCase):
    def test_form_catalog_preview_edit_and_explicit_launch(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            library = root / "library"
            model = library / "iPhone 17 Pro Max"
            descriptions = model / "descriptions"
            descriptions.mkdir(parents=True)
            (descriptions / "01.txt").write_text(
                "{модель}, {память}, {SIM}\n\nОписание тестового телефона.",
                encoding="utf-8", newline="",
            )
            for name in ("Стол", "Витрина"):
                pack = model / "512 GB" / name
                pack.mkdir(parents=True)
                picture = Image.new("RGB", (160, 180), "navy")
                ImageDraw.Draw(picture).rectangle((30, 30, 130, 150), fill="orange")
                picture.save(pack / "01.png")
            stack.enter_context(patch.object(iphone_library, "LIBRARY_ROOT", library))
            stack.enter_context(patch.object(api, "TMP_PUBLISH_DIR", root / "publish"))
            stack.enter_context(patch.dict(api.PREP_JOBS, {}, clear=True))
            stack.enter_context(patch.dict(api.PUBLISH_JOBS, {}, clear=True))
            stack.enter_context(patch.object(api.journal, "log_event"))
            stack.enter_context(patch.object(api, "ALLOWED_ORIGINS", {
                *api.ALLOWED_ORIGINS, "http://127.0.0.1:18779",
            }))
            schedule = stack.enter_context(patch.object(api, "_schedule_publish"))
            run_publish = stack.enter_context(patch.object(api.publisher, "run_publish_job"))
            server = uvicorn.Server(uvicorn.Config(
                api.app, host="127.0.0.1", port=18779, lifespan="off", log_level="error",
            ))
            worker = threading.Thread(target=server.run, daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started and worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(server.started)
                with sync_playwright() as browser_api:
                    bundled = Path(browser_api.chromium.executable_path)
                    options = {} if bundled.is_file() else {"channel": "chrome"}
                    browser = browser_api.chromium.launch(headless=True, **options)
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        page.route("**/*", lambda route: route.continue_()
                                   if route.request.url.startswith("http://127.0.0.1:18779/")
                                   else route.abort())
                        page_errors = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto("http://127.0.0.1:18779/#/draft")
                        page.wait_for_load_state("networkidle")
                        page.locator("select[name='category']").select_option("iphone")
                        self.assertEqual(page.locator("input[type='file']").count(), 0)
                        self.assertEqual(page.locator("textarea[name='description']").count(), 0)
                        expect(page.locator("select[name='sim_type']")).to_have_value("")

                        page.locator("input[name='title']").fill("iPhone 17")
                        expect(page.locator("select[name='memory']")).to_be_enabled()
                        page.locator("select[name='memory']").select_option(label="128 ГБ")
                        page.locator("input[name='title']").fill("17 Pro Max iPhone")
                        expect(page.locator("select[name='memory']")).to_have_value("")
                        page.locator("select[name='memory']").select_option(label="512 ГБ")
                        page.locator("select[name='color']").select_option(label="Синий")
                        page.locator("input[name='price']").fill("120000")
                        page.locator("select[name='drafts_count']").select_option("2")
                        for index in (1, 2):
                            page.locator(f"input[name='locations.{index}.city']").fill("Москва")
                            page.locator(f"input[name='locations.{index}.address']").fill("ул. Арбат, 1")
                        page.locator("input[name='view_price_max']").fill("5")
                        prepare = page.get_by_role("button", name="Подготовить варианты")
                        expect(prepare).to_be_disabled()
                        page.locator("select[name='sim_type']").select_option("SIM + SIM")
                        expect(page.locator("select[name='pack_id']")).to_be_enabled()
                        page.locator("select[name='pack_id']").select_option("Стол")

                        # Исчезновение выбранного пака требует нового явного выбора.
                        (model / "512 GB" / "Стол").rename(model / "512 GB" / "Архив")
                        with page.expect_response(lambda response: "/iphone-library?" in response.url):
                            page.get_by_role("button", name="Обновить библиотеку").click()
                        expect(page.locator("select[name='pack_id']")).to_have_value("Стол")
                        expect(prepare).to_be_disabled()
                        page.locator("select[name='pack_id']").select_option("Витрина")
                        expect(prepare).to_be_enabled()
                        prepare.click()
                        page.get_by_role("heading", name="Варианты объявлений").wait_for()
                        cards = page.locator(".draft-preview-card")
                        expect(cards).to_have_count(2)
                        for index in range(2):
                            expect(cards.nth(index)).to_contain_text("SIM + SIM")
                            expect(cards.nth(index)).to_contain_text("Витрина")
                            expect(cards.nth(index).locator("img")).to_have_count(1)
                        schedule.assert_not_called()
                        card = cards.nth(0)
                        card.get_by_role("button", name="Редактировать текст").click()
                        card.get_by_label("Описание варианта 1").fill("Описание после проверки. SIM + SIM.")
                        card.get_by_role("button", name="Сохранить текст").click()
                        expect(card).to_contain_text("Описание после проверки")
                        output = ROOT / "docs" / "_internal" / "iphone-library" / "preview.png"
                        output.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(output), full_page=True)
                        with page.expect_response(lambda response: response.url.endswith("/api/publish/start")) as response:
                            page.get_by_role("button", name="Отправить 2 объявления на Авито").click()
                        self.assertEqual(response.value.status, 200, response.value.text())
                        schedule.assert_called_once()
                        draft = schedule.call_args.args[1]
                        self.assertEqual(draft.sim_type, "SIM + SIM")
                        self.assertIn("Описание после проверки", draft.description)
                        run_publish.assert_not_called()
                        self.assertEqual(page_errors, [])
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                worker.join(timeout=10)


if __name__ == "__main__":
    unittest.main()

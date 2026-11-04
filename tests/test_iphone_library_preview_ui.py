"""Независимая приёмка основной формы: локальный Chrome, без доступа к Авито."""

import io
import json
import socket
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import publish_test_isolation  # noqa: F401 — изоляция до app/publisher
import app as api
import iphone_library
import preparation
import uvicorn
from PIL import Image
from playwright.sync_api import expect, sync_playwright


class IphoneLibraryMainFormPreviewTest(unittest.TestCase):
    def test_preview_reorder_reload_and_prepare_keep_same_random_pack(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            library = root / "library" / "iphone"
            description = library / "description" / "iphone.txt"
            description.parent.mkdir(parents=True)
            description.write_text(
                "{модель} | {память} | {цвет} | {SIM}\n\nОбщий текст проверки.",
                encoding="utf-8", newline="",
            )
            for pack_name in ("Стол", "Витрина"):
                pack = library / "iPhone 17 Pro Max" / "512 GB" / pack_name
                pack.mkdir(parents=True)
                for index, color in enumerate(("red", "blue", "green"), 1):
                    Image.new("RGB", (140, 120), color).save(pack / f"{index:02d}.png")
            stack.enter_context(patch.object(iphone_library, "LIBRARY_ROOT", library))
            stack.enter_context(patch.object(api, "TMP_PUBLISH_DIR", root / "publish"))
            stack.enter_context(patch.dict(api.PREP_JOBS, {}, clear=True))
            stack.enter_context(patch.dict(api.PUBLISH_JOBS, {}, clear=True))
            stack.enter_context(patch.object(api.journal, "log_event"))
            schedule = stack.enter_context(patch.object(api, "_schedule_publish"))
            run_publish = stack.enter_context(patch.object(api.publisher, "run_publish_job"))
            listener = stack.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"
            stack.enter_context(patch.object(api, "ALLOWED_ORIGINS", {*api.ALLOWED_ORIGINS, base_url}))
            server = uvicorn.Server(uvicorn.Config(
                api.app, host="127.0.0.1", port=port, lifespan="off", log_level="error",
            ))
            worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started and worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(server.started, "Локальный тестовый сервер не запустился")
                with sync_playwright() as playwright:
                    bundled = Path(playwright.chromium.executable_path)
                    options = {} if bundled.is_file() else {"channel": "chrome"}
                    browser = playwright.chromium.launch(headless=True, **options)
                    try:
                        context = browser.new_context(viewport={"width": 1440, "height": 1100})
                        context.route("**/*", lambda route: route.continue_()
                                      if route.request.url.startswith(base_url + "/")
                                      else route.abort())
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(base_url + "/#/draft")
                        page.wait_for_load_state("networkidle")
                        page.locator("select[name='category']").select_option("iphone")
                        page.locator("input[name='title']").fill("17 Pro Max iPhone")
                        expect(page.locator("select[name='memory']")).to_be_enabled()
                        page.locator("select[name='memory']").select_option(label="512 ГБ")
                        page.locator("select[name='color']").select_option(label="Синий")
                        page.locator("select[name='sim_type']").select_option("SIM + SIM")
                        page.locator("input[name='price']").fill("120000")
                        page.locator("select[name='drafts_count']").select_option("2")
                        for index in (1, 2):
                            page.locator(f"input[name='locations.{index}.city']").fill("Москва")
                            page.locator(f"input[name='locations.{index}.address']").fill("ул. Арбат, 1")
                        page.locator("input[name='view_price_max']").fill("5")
                        expect(page.locator("select[name='pack_id']")).to_have_value("")
                        images = page.locator(".iphone-library-preview img")
                        expect(images).to_have_count(3)
                        page.wait_for_function("""() => [...document.querySelectorAll('.iphone-library-preview img')]
                            .every(image => image.complete && image.naturalWidth > 0)""")
                        self.assertEqual(api.PREP_JOBS, {})
                        self.assertEqual(page.locator(".draft-preview-card").count(), 0)
                        schedule.assert_not_called()

                        saved = page.evaluate("JSON.parse(localStorage.getItem('avito.draft.form')).form")
                        chosen_pack = saved["library_preview_pack_id"]
                        self.assertIn(chosen_pack, ("Стол", "Витрина"))
                        self.assertEqual(saved["photo_order"], ["01.png", "02.png", "03.png"])
                        response = context.request.get(images.first.get_attribute("src")
                                                       if images.first.get_attribute("src").startswith("http")
                                                       else base_url + images.first.get_attribute("src"))
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers["content-type"].split(";")[0], "image/jpeg")
                        with Image.open(io.BytesIO(response.body())) as image:
                            self.assertEqual(image.format, "JPEG")
                            image.load()

                        page.get_by_role("button", name="Переместить фото 3 левее", exact=True).click()
                        page.get_by_role("button", name="Переместить фото 2 левее", exact=True).click()
                        expected_order = ["03.png", "01.png", "02.png"]
                        expect(images.first).to_have_attribute("alt", "Фото 1: 03.png")
                        page.wait_for_function("""expected => JSON.stringify(JSON.parse(
                            localStorage.getItem('avito.draft.form')).form.photo_order) === JSON.stringify(expected)""",
                                               arg=expected_order)
                        with page.expect_response(lambda response: "/iphone-library?" in response.url):
                            page.get_by_role("button", name="Обновить библиотеку", exact=True).click()
                        expect(images.first).to_have_attribute("alt", "Фото 1: 03.png")
                        page.reload()
                        expect(page.locator("select[name='pack_id']")).to_have_value("")
                        expect(images).to_have_count(3)
                        expect(images.first).to_have_attribute("alt", "Фото 1: 03.png")
                        restored = page.evaluate("JSON.parse(localStorage.getItem('avito.draft.form')).form")
                        self.assertEqual(restored["library_preview_pack_id"], chosen_pack)
                        self.assertEqual(restored["photo_order"], expected_order)
                        schedule.assert_not_called()

                        prepare = page.get_by_role("button", name="Подготовить варианты", exact=True)
                        expect(prepare).to_be_enabled()
                        with page.expect_response(lambda response: response.url.endswith("/api/publish/prepare")) as prepared:
                            prepare.click()
                        self.assertEqual(prepared.value.status, 200, prepared.value.text())
                        request = prepared.value.request
                        message = BytesParser().parsebytes(
                            b"Content-Type: " + request.headers["content-type"].encode()
                            + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.post_data_buffer)
                        fields = {part.get_param("name", header="content-disposition"):
                                  part.get_payload(decode=True).decode("utf-8")
                                  for part in message.walk() if part.get_content_disposition() == "form-data"}
                        self.assertEqual(fields["pack_id"], chosen_pack)
                        self.assertEqual(json.loads(fields["photo_order"]), expected_order)
                        page.get_by_role("heading", name="Варианты объявлений", exact=True).wait_for()
                        expect(page.locator(".draft-preview-card")).to_have_count(2)
                        prep_id = prepared.value.json()["prep_id"]
                        self.assertEqual(api.PREP_JOBS[prep_id]["status"], "done")
                        prep_dir = root / "publish" / f"prep_{prep_id}"
                        original_pack = library / "iPhone 17 Pro Max" / "512 GB" / chosen_pack
                        self.assertEqual(
                            [path.read_bytes() for path in sorted((prep_dir / "source" / "photos").iterdir())],
                            [(original_pack / name).read_bytes() for name in expected_order],
                        )
                        snapshot = preparation.read_library_snapshot(prep_dir)
                        self.assertEqual(snapshot["pack_id"], chosen_pack)
                        expected_text = "iPhone 17 Pro Max | 512 ГБ | Синий | SIM + SIM\n\nОбщий текст проверки."
                        self.assertEqual(snapshot["descriptions"], [expected_text] * 2)
                        schedule.assert_not_called()
                        run_publish.assert_not_called()
                        self.assertEqual(api.PUBLISH_JOBS, {})
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                worker.join(timeout=10)


if __name__ == "__main__":
    unittest.main()

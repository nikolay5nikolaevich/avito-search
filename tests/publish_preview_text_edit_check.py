"""Browser-проверка ручного редактирования текста подготовленного варианта."""

import json
import os
from pathlib import Path

from playwright.sync_api import Route, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("PUBLISH_UI_BASE_URL", "http://127.0.0.1:18768")
PHOTO_PATH = PROJECT_ROOT / "debug" / "item.png"
SCREENSHOT = PROJECT_ROOT / "debug" / "publish-preview-text-edit.png"


def main() -> None:
    drafts = [
        {
            "index": 1,
            "title": "Исходный заголовок",
            "description": "Исходное описание",
            "preset_name": "",
            "photo_urls": [],
            "notes": "",
            "warnings": [],
        },
        {
            "index": 2,
            "title": "Вариант заголовка",
            "description": "Вариант описания",
            "preset_name": "test",
            "photo_urls": [],
            "notes": "",
            "warnings": [],
        },
    ]
    saved_payloads: list[dict] = []
    console_errors: list[str] = []

    def handle_publish_api(route: Route) -> None:
        url = route.request.url
        if url.endswith("/api/publish/categories"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "categories": [{
                    "key": "jackets",
                    "label": "Пиджаки и костюмы",
                    "trade_types": ["Продаю своё"],
                    "conditions": ["Отличное"],
                    "sizes": ["48 (M)"],
                    "colors": ["Чёрный"],
                    "item_types": [],
                }],
            }, ensure_ascii=False))
            return
        if url.endswith("/api/publish/prepare"):
            route.fulfill(status=200, content_type="application/json", body='{"prep_id":"prep-ui-test"}')
            return
        if url.endswith("/api/publish/prepare/status/prep-ui-test"):
            route.fulfill(status=200, content_type="application/json", body='{"status":"done","done":4,"total":4}')
            return
        if url.endswith("/api/publish/prepare/result/prep-ui-test"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"prep_id": "prep-ui-test", "drafts": drafts}, ensure_ascii=False),
            )
            return
        if url.endswith("/api/publish/prepare/update-text"):
            payload = route.request.post_data_json
            saved_payloads.append(payload)
            updated = {**drafts[payload["draft_index"] - 1]}
            updated["title"] = payload["title"].strip()
            updated["description"] = payload["description"].strip()
            drafts[payload["draft_index"] - 1] = updated
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(updated, ensure_ascii=False),
            )
            return
        route.abort()

    SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
    assert PHOTO_PATH.is_file(), f"Тестовое фото не найдено: {PHOTO_PATH}"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.route("**/api/publish/**", handle_publish_api)
        page.goto(f"{BASE_URL}/#/draft")
        page.wait_for_load_state("networkidle")

        page.locator("input[name='title']").fill("Тестовый пиджак")
        page.locator("select[name='size']").select_option(label="48 (M)")
        page.locator("input[name='brand']").fill("Hugo Boss")
        page.locator("select[name='color']").select_option(label="Чёрный")
        page.locator("textarea[name='description']").fill("Подробное исходное описание")
        page.locator("input[name='price']").fill("5000")
        page.locator("select[name='drafts_count']").select_option("2")
        page.locator("input[name='locations.1.city']").fill("Москва")
        page.locator("input[name='locations.1.address']").fill("Тверская, 10")
        page.locator("input[name='locations.2.city']").fill("Одинцово")
        page.locator("input[name='locations.2.address']").fill("Центральная, 7")
        page.locator("input[name='view_price_max']").fill("2")
        page.locator("input[type='file']").set_input_files(str(PHOTO_PATH))
        page.get_by_role("button", name="Подготовить варианты").click()

        page.get_by_role("heading", name="Варианты объявлений").wait_for()
        assert page.get_by_role("button", name="Редактировать текст").count() == 2

        card = page.locator(".draft-preview-card").nth(1)
        publish_button = page.get_by_role("button", name="Опубликовать 2 объявления")
        card.get_by_role("button", name="Редактировать текст").click()
        assert publish_button.is_disabled(), (
            "Публикацию нужно блокировать, пока в карточке есть несохранённый текст"
        )
        card.get_by_label("Название варианта 2").fill("  Исправленный заголовок  ")
        card.get_by_label("Описание варианта 2").fill("  Исправленное описание  ")
        card.get_by_role("button", name="Сохранить текст").click()

        card.get_by_text("Исправленный заголовок", exact=True).wait_for()
        assert card.get_by_text("Исправленное описание", exact=True).is_visible()
        assert publish_button.is_enabled()
        assert saved_payloads == [{
            "prep_id": "prep-ui-test",
            "draft_index": 2,
            "title": "Исправленный заголовок",
            "description": "Исправленное описание",
        }]
        page.screenshot(path=str(SCREENSHOT), full_page=True)
        browser.close()

    assert not console_errors, console_errors
    print(f"publish preview text edit check passed; screenshot={SCREENSHOT}")


if __name__ == "__main__":
    main()
